"""
shelf_classifier.py — ShelvesFinder v2

Centralized classification of discovered Walmart browse/category pages as
either GENERIC or INHERENTLY BRANDED.

An *inherently branded* shelf is a category page whose identity is a brand,
e.g. ``/browse/beauty/shampoo/head-shoulders/1085666_123`` or any URL carrying
a Walmart brand facet. This is distinct from the *brand-filtered view* of a
generic shelf (``<generic-url>?facet=brand%3A<brand>``) that the application
constructs itself for the "Digital Shelf (Brand Filter)" column — that URL is
built downstream of this classifier and is never passed through it.

When the "Include Branded Results" setting is OFF, the orchestrator uses this
module to reject inherently branded base shelves (for the analyzed brand AND
competitor brands) before they are checked, reported, persisted, or rendered.

Detection signals, in order of strength:
  1. Explicit Walmart brand facets in candidate query parameters or encoded
     browse-path tokens (``facet=brand:X``, ``?brand=X``, or Base64-encoded
     ``brand:X``), which also reveal the brand name for competitor harvesting.
  2. A ``brands`` segment in the URL path (``/browse/brands/...``).
  3. The final category node of the URL path or of the search-result
     title/breadcrumb matching the analyzed product's brand.
  4. The final category node matching any known competitor brand harvested
     from search-result metadata during the session.

Pre-fetch classification (``classify_shelf``) works from search-result
metadata alone, so a competitor-brand shelf whose URL slug and title carry no
known brand markers can slip past it. In particular, Walmart uses the same
``"<X> in <Y>"`` title pattern for generic category shelves and brand shelves,
so that pattern is deliberately not treated as grounds for irreversible
pre-fetch rejection. The gap is closed post-fetch by
``classify_fetched_shelf``, which reads the shelf page's own ``__NEXT_DATA__``
payload — breadcrumbs, the page's brand facet list, pre-selected brand
facets, and brand page-type markers — and by harvesting every brand name the
page's brand facet reveals so *later* candidates in the same session are
rejected before they are ever fetched.

Residual limitation: a branded page whose ``__NEXT_DATA__`` exposes no
breadcrumb, no brand facet, and no page-type marker remains undetectable.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
import urllib.parse
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Path segments that are Walmart result/rank identifiers, not category nodes.
_ID_SEGMENT_RE = re.compile(r"^\d[\d_]*$")

# Trailing site-name suffixes on search-result titles.
_TITLE_SUFFIX_RE = re.compile(r"\s*[-–|]\s*walmart(\.com)?\s*$", re.IGNORECASE)


# Facet values become session metadata and may be logged/reported, so keep the
# accepted surface deliberately small. 100 characters is comfortably above
# real-world brand names while bounding malformed search-provider input.
_MAX_BRAND_LENGTH = 100
_MAX_ENCODED_FACET_LENGTH = 512
_UNSAFE_BRAND_CHARS = frozenset('"`<>[]{}\\|')
_BASE64_SEGMENT_RE = re.compile(r"^[A-Za-z0-9+/_-]+={0,2}$")
_ENCODED_BRAND_PREFIX_LENGTH = 8  # Base64('brand:') has no variant/padding.


def _brand_key(text: str) -> str:
    """
    Normalize a brand/category name for comparison.

    Handles URL encoding, case, punctuation, and separator differences so that
    "Head & Shoulders", "head-shoulders", and "Head%20%26%20Shoulders" all
    produce the same key ("head shoulders"). The filler token "and" is dropped
    because URL slugs usually omit the ampersand entirely.
    """
    if not text:
        return ""
    t = urllib.parse.unquote_plus(str(text)).lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[^a-z0-9]+", " ", t)
    tokens = [tok for tok in t.split() if tok != "and"]
    return " ".join(tokens)


def _category_segments(url: str) -> list[str]:
    """Return the category path segments of a browse URL, IDs stripped."""
    try:
        path = urllib.parse.urlparse(url).path
    except Exception:
        return []
    segments = [seg for seg in path.split("/") if seg]
    if segments and segments[0].lower() == "browse":
        segments = segments[1:]
    return [seg for seg in segments if not _ID_SEGMENT_RE.match(seg)]


def _validated_brand_value(value: str) -> Optional[str]:
    """Return a safe, plausible brand value or None."""
    if not isinstance(value, str):
        return None

    value = value.strip()
    if not value or len(value) > _MAX_BRAND_LENGTH:
        return None
    if not any(char.isalnum() for char in value):
        return None
    if any(
        not char.isprintable()
        or unicodedata.category(char).startswith("C")
        or char in _UNSAFE_BRAND_CHARS
        for char in value
    ):
        return None
    return value


def _strict_base64_decode(token: str) -> Optional[bytes]:
    """Decode one complete standard/URL-safe Base64 token, adding padding."""
    if len(token) % 4 == 1:
        return None
    try:
        padded = token + ("=" * (-len(token) % 4))
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        return None


def _brand_from_decoded_facet(decoded: bytes) -> Optional[str]:
    """Validate a decoded ``brand:<value>`` facet, with narrow noise recovery."""
    if decoded[:6].lower() != b"brand:":
        return None

    raw_value = decoded[6:]

    # Walmart can pack multiple facets into one token, for example
    # ``brand:Head & Shoulders||category:Shampoos``. A real facet delimiter is
    # an unambiguous end to the brand value, so the remaining facets need not
    # participate in brand-value validation.
    delimiter_positions = [
        position
        for delimiter in (b"||", b",")
        if (position := raw_value.find(delimiter)) >= 0
    ]
    if delimiter_positions:
        try:
            value = raw_value[:min(delimiter_positions)].decode("utf-8")
        except UnicodeDecodeError:
            return None
        return _validated_brand_value(value)

    try:
        value = raw_value.decode("utf-8")
    except UnicodeDecodeError:
        value = ""
    else:
        # Printable-but-unsafe suffixes are not silently discarded: doing so
        # would turn arbitrary partial decodes into fabricated brand names.
        if all(
            char.isprintable() and not unicodedata.category(char).startswith("C")
            for char in value
        ):
            return _validated_brand_value(value)

    # A few observed search-result URLs append bytes that decode as control or
    # invalid UTF-8 data. Recover only the longest complete UTF-8 prefix that
    # is itself a safe brand value.
    for end in range(len(raw_value) - 1, 0, -1):
        try:
            candidate = raw_value[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        validated = _validated_brand_value(candidate)
        if validated:
            return validated
    return None


def _decode_path_facet_brand(segment: str) -> Optional[str]:
    """Decode an explicit Base64 ``brand:`` facet from one browse segment."""
    try:
        token = urllib.parse.unquote(segment)
    except Exception:
        return None

    if not (
        _ENCODED_BRAND_PREFIX_LENGTH < len(token) <= _MAX_ENCODED_FACET_LENGTH
        and _BASE64_SEGMENT_RE.fullmatch(token)
    ):
        return None

    # Avoid probing arbitrary Base64-looking category slugs. Exactly six bytes
    # are represented by the first eight characters, so this is a complete,
    # lossless check for the explicit facet name before recovery is attempted.
    prefix = _strict_base64_decode(token[:_ENCODED_BRAND_PREFIX_LENGTH])
    if prefix is None or prefix.lower() != b"brand:":
        return None

    decoded = _strict_base64_decode(token)
    if decoded is not None:
        return _brand_from_decoded_facet(decoded)

    # Valid unpadded Base64 can never have length == 1 mod 4. Walmart/Serper
    # occasionally appends a short Base64-alphabet suffix, producing exactly
    # that shape. Remove at most four trailing characters and accept only a
    # fully decoded, explicitly prefixed, independently validated facet.
    if "=" not in token and len(token) % 4 == 1:
        for suffix_length in range(1, 5):
            candidate = token[:-suffix_length]
            decoded = _strict_base64_decode(candidate)
            if decoded is None:
                continue
            brand = _brand_from_decoded_facet(decoded)
            if brand:
                return brand
    return None


def _path_facet_brand(parsed: urllib.parse.ParseResult) -> Optional[str]:
    """Inspect Base64 facet tokens on Walmart browse paths only."""
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if hostname != "walmart.com" and not hostname.endswith(".walmart.com"):
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    if not segments or segments[0].lower() != "browse":
        return None

    for segment in segments[1:]:
        brand = _decode_path_facet_brand(segment)
        if brand:
            return brand
    return None


def extract_facet_brand(url: str) -> Optional[str]:
    """
    Return the brand named by an explicit brand facet/param in ``url``,
    or None when the URL carries no brand facet.

    Handles ``facet=brand:X`` (raw or URL-encoded, any casing), a direct
    ``brand=X`` query parameter, and Walmart browse-path segments containing
    standard or URL-safe Base64-encoded ``brand:X`` facets.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        params = urllib.parse.parse_qsl(parsed.query)
    except Exception:
        return None

    for name, value in params:
        name_l = name.lower()
        if name_l == "brand" and value:
            brand = _validated_brand_value(value)
            if brand:
                return brand
        if "facet" in name_l and value:
            # A facet value may pack several filters: "brand:X||category:Y"
            for part in re.split(r"\|\||,", value):
                match = re.match(r"\s*brand\s*:\s*(.+)", part, re.IGNORECASE)
                if match:
                    brand = _validated_brand_value(match.group(1))
                    if brand:
                        return brand
    return _path_facet_brand(parsed)


def _clean_title(title: str) -> str:
    """Strip the trailing ' - Walmart.com' style suffix from a result title."""
    return _TITLE_SUFFIX_RE.sub("", str(title or "")).strip()


def _final_breadcrumb_node(title: str) -> str:
    """Final node of a breadcrumb-like title ('Beauty > Shampoo > Brand')."""
    cleaned = _clean_title(title)
    for sep in (">", "»", "/", "|"):
        if sep in cleaned:
            cleaned = cleaned.split(sep)[-1]
    return cleaned.strip()


@dataclass
class ShelfClassification:
    """Outcome of classifying one discovered base shelf URL."""
    is_branded: bool
    reason: str = ""      # which signal fired ("brand_facet", "brand_path", ...)
    brand: str = ""       # brand name detected, when one could be extracted


def classify_shelf(
    url: str,
    product_brand: str = "",
    title: str = "",
    known_brands: Iterable[str] = (),
) -> ShelfClassification:
    """
    Classify a discovered candidate base shelf URL.

    Args:
        url:            candidate browse URL as returned by search
        product_brand:  the analyzed product's brand (may be empty)
        title:          search-result title/breadcrumb metadata for the URL
        known_brands:   brand names harvested from session metadata
                        (competitor brands revealed by facets, etc.)

    Returns a ShelfClassification; ``is_branded`` is True when the page is
    inherently brand-specific rather than a generic category.
    """
    # 1. Explicit brand facet on the candidate URL itself.
    facet_brand = extract_facet_brand(url)
    if facet_brand:
        return ShelfClassification(True, "brand_facet", facet_brand)

    segments = _category_segments(url)
    segment_keys = [_brand_key(seg) for seg in segments]

    # 2. Walmart brand-directory path structure.
    if any(key in ("brand", "brands") for key in segment_keys):
        brand_name = segments[-1] if segments else ""
        return ShelfClassification(True, "brand_path", brand_name)

    final_key = segment_keys[-1] if segment_keys else ""

    brand_keys: dict[str, str] = {}
    if product_brand and _brand_key(product_brand):
        brand_keys[_brand_key(product_brand)] = product_brand
    for kb in known_brands or ():
        key = _brand_key(kb)
        if key:
            brand_keys.setdefault(key, kb)

    # 3 & 4. Final category node IS a brand (analyzed or known competitor).
    if final_key and final_key in brand_keys:
        return ShelfClassification(True, "brand_category_node", brand_keys[final_key])

    # Same check against the final node of the title/breadcrumb metadata.
    breadcrumb_node_key = _brand_key(_final_breadcrumb_node(title))
    if breadcrumb_node_key and breadcrumb_node_key in brand_keys:
        return ShelfClassification(
            True, "brand_breadcrumb_node", brand_keys[breadcrumb_node_key]
        )

    return ShelfClassification(False)


# ---------------------------------------------------------------------------
# Post-fetch classification from the shelf page's own __NEXT_DATA__ payload
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)

# JSON keys (lowercased) that hold the breadcrumb trail / page-type markers.
_BREADCRUMB_KEYS = {"breadcrumb", "breadcrumbs"}
_PAGE_TYPE_KEYS = {"pagetype", "templatetype"}
# Keys that mark a facet value as currently applied on the page.
_SELECTED_VALUE_KEYS = ("checked", "isselected", "selected", "isapplied")
# Keys that may carry a facet group's identity / its value list.
_FACET_NAME_KEYS = ("name", "title", "param", "displayname", "facetname")
_FACET_VALUE_LIST_KEYS = ("values", "facetvalues")
_VALUE_NAME_KEYS = ("name", "title", "value", "displayname")


@dataclass
class PageBrandMetadata:
    """Brand-relevant structure mined from one fetched shelf page."""
    breadcrumb_nodes: list[str] = field(default_factory=list)  # ordered names
    brand_facet_values: list[str] = field(default_factory=list)
    selected_brands: list[str] = field(default_factory=list)
    page_types: list[str] = field(default_factory=list)


def _first_str(item: dict, keys: Iterable[str]) -> str:
    for key, value in item.items():
        if key.lower() in keys and isinstance(value, str):
            return value
    return ""


def _breadcrumb_names(value) -> list[str]:
    """Interpret a candidate breadcrumb value as an ordered list of names."""
    if not isinstance(value, list):
        return []
    names = []
    for entry in value:
        if isinstance(entry, dict):
            name = _first_str(entry, {"name", "displayname", "title"})
            if name:
                names.append(name)
    return names


def _collect_brand_facet(node: dict, meta: PageBrandMetadata) -> None:
    """If ``node`` is a brand facet group, record its values into ``meta``."""
    group_name = ""
    for key in _FACET_NAME_KEYS:
        for k, v in node.items():
            if k.lower() == key and isinstance(v, str):
                group_name = v
                break
        if group_name:
            break
    if _brand_key(group_name) not in ("brand", "brands"):
        return

    for k, v in node.items():
        if k.lower() not in _FACET_VALUE_LIST_KEYS or not isinstance(v, list):
            continue
        for entry in v:
            if not isinstance(entry, dict):
                continue
            name = _first_str(entry, set(_VALUE_NAME_KEYS))
            if not name:
                continue
            if name not in meta.brand_facet_values:
                meta.brand_facet_values.append(name)
            is_selected = any(
                value is True
                for sel_key, value in entry.items()
                if str(sel_key).lower() in _SELECTED_VALUE_KEYS
            )
            if is_selected and name not in meta.selected_brands:
                meta.selected_brands.append(name)


def _walk_page_metadata(node, meta: PageBrandMetadata) -> None:
    if isinstance(node, dict):
        _collect_brand_facet(node, meta)
        for key, value in node.items():
            key_l = str(key).lower()
            if key_l in _BREADCRUMB_KEYS:
                names = _breadcrumb_names(value)
                # Keep the longest breadcrumb trail found in the payload.
                if len(names) > len(meta.breadcrumb_nodes):
                    meta.breadcrumb_nodes = names
            if key_l in _PAGE_TYPE_KEYS and isinstance(value, str) and value:
                if value not in meta.page_types:
                    meta.page_types.append(value)
            _walk_page_metadata(value, meta)
    elif isinstance(node, list):
        for value in node:
            _walk_page_metadata(value, meta)


def extract_page_brand_metadata(html: str) -> Optional[PageBrandMetadata]:
    """
    Mine brand-relevant metadata from a fetched shelf page's ``__NEXT_DATA__``.

    Returns None when the page has no parseable ``__NEXT_DATA__`` payload.
    """
    if not html:
        return None
    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except Exception:
        return None
    meta = PageBrandMetadata()
    _walk_page_metadata(data, meta)
    return meta


def classify_fetched_shelf(
    html: str,
    product_brand: str = "",
    known_brands: Iterable[str] = (),
) -> tuple[ShelfClassification, set[str]]:
    """
    Post-fetch verification of a BASE shelf page using its own structured data.

    Must only ever be given the base/general shelf HTML — never the
    brand-filtered view the application constructs itself, whose facet is
    selected by design and would (correctly) classify as branded.

    Returns ``(classification, harvested_brands)`` where ``harvested_brands``
    is every brand name the page's brand facet revealed, regardless of the
    classification outcome — callers feed these back into the session's known
    brand set so later candidates are rejected before being fetched.
    """
    meta = extract_page_brand_metadata(html)
    if meta is None:
        return ShelfClassification(False, "no_page_metadata"), set()

    harvested = {b for b in meta.brand_facet_values if b}

    # 1. Walmart marks brand shelves with a brand page/template type.
    for page_type in meta.page_types:
        if "brand" in page_type.lower():
            return ShelfClassification(True, "brand_page_type"), harvested

    # 2. A brand facet already applied on the BASE page means the page is
    #    inherently scoped to that brand.
    if meta.selected_brands:
        return (
            ShelfClassification(
                True, "preselected_brand_facet", meta.selected_brands[0]
            ),
            harvested,
        )

    final_node = meta.breadcrumb_nodes[-1] if meta.breadcrumb_nodes else ""
    final_key = _brand_key(final_node)
    if final_key:
        # 3. The page's own brand facet list names the final breadcrumb node —
        #    the strongest signal, needing no prior knowledge of the brand.
        facet_keys = {_brand_key(b): b for b in harvested if _brand_key(b)}
        if final_key in facet_keys:
            return (
                ShelfClassification(
                    True, "breadcrumb_is_page_brand", facet_keys[final_key]
                ),
                harvested,
            )

        # 4. Final breadcrumb node matches the analyzed or a known brand.
        brand_keys: dict[str, str] = {}
        if product_brand and _brand_key(product_brand):
            brand_keys[_brand_key(product_brand)] = product_brand
        for kb in known_brands or ():
            key = _brand_key(kb)
            if key:
                brand_keys.setdefault(key, kb)
        if final_key in brand_keys:
            return (
                ShelfClassification(
                    True, "brand_breadcrumb_node", brand_keys[final_key]
                ),
                harvested,
            )

    return ShelfClassification(False), harvested


def harvest_known_brands(raw_pages: Iterable[dict]) -> set[str]:
    """
    Mine competitor brand names out of a batch of raw search results.

    Brands are revealed by explicit query or encoded-path facets in result
    URLs; the harvested set lets the classifier reject the *unfaceted* sibling
    shelf of the same competitor brand.
    """
    brands: set[str] = set()
    for rp in raw_pages or ():
        if not isinstance(rp, dict):
            continue
        facet_brand = extract_facet_brand(rp.get("url", "") or "")
        if facet_brand:
            brands.add(facet_brand)
    return brands
