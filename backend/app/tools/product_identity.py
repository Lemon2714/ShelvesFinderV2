"""Shared normalization and conservative fallback recovery for product identity."""

from __future__ import annotations

import re
import urllib.parse
from typing import Mapping


_ANTI_BOT_TITLES = ("robot or human", "verify you are human", "captcha")
_PRODUCT_CUES = {
    "conditioner", "cream", "dandruff", "dress", "gel", "lotion", "mask", "oil",
    "pack", "powder", "serum", "shampoo", "soap", "spray", "tablet",
    "tablets", "toothpaste", "vitamin", "vitamins", "wash", "wipes",
}
_WORD_RE = re.compile(r"[A-Za-z0-9]+|&")
_INFERRED_BRAND_SOURCES = {"inferred_url_title", "inferred_llm"}


def is_anti_bot_title(title: str) -> bool:
    """Return True when a scraped title is an anti-bot challenge, not a PDP."""
    lowered = str(title or "").strip().lower()
    return any(marker in lowered for marker in _ANTI_BOT_TITLES)


def product_identity_from_url(url: str) -> tuple[str, str]:
    """Return ``(slug title, product id)`` from a Walmart ``/ip/`` URL."""
    try:
        segments = [
            urllib.parse.unquote(part)
            for part in urllib.parse.urlparse(url).path.split("/")
            if part
        ]
    except Exception:
        return "", ""

    try:
        ip_index = next(i for i, part in enumerate(segments) if part.lower() == "ip")
    except StopIteration:
        return "", ""

    remainder = segments[ip_index + 1:]
    if not remainder:
        return "", ""

    product_id = remainder[-1] if remainder[-1].isdigit() else ""
    slug_parts = remainder[:-1] if product_id else remainder
    slug = slug_parts[-1] if slug_parts else ""
    title = re.sub(r"[-_]+", " ", slug).strip()
    return title, product_id


def _format_conjunction_brand(tokens: list[str]) -> str:
    display = []
    for token in tokens:
        if token.lower() in ("and", "&"):
            display.append("&")
        else:
            display.append(token.capitalize())
    return " ".join(display)


def infer_brand_from_title(title: str) -> tuple[str, float]:
    """
    Infer only a strong multi-word conjunction brand at the start of a title.

    A product cue must immediately follow the candidate. This intentionally
    does not treat the first title word as a brand and leaves ordinary or
    ambiguous titles unknown.
    """
    tokens = _WORD_RE.findall(title or "")
    if len(tokens) < 4:
        return "", 0.0

    cue_index = next(
        (i for i, token in enumerate(tokens) if token.lower() in _PRODUCT_CUES),
        -1,
    )
    if cue_index < 3 or cue_index > 5:
        return "", 0.0

    candidate = tokens[:cue_index]
    if not any(token.lower() in ("and", "&") for token in candidate):
        return "", 0.0
    if any(token.isdigit() for token in candidate):
        return "", 0.0

    return _format_conjunction_brand(candidate), 0.9


def normalize_product_identity(url: str, product_data: Mapping | None) -> dict:
    """
    Produce a complete, provenance-aware identity without network access.

    Explicit structured/metadata brands are authoritative. URL/title inference
    is deliberately conservative and is always marked non-authoritative.
    """
    result = dict(product_data or {})
    slug_title, url_product_id = product_identity_from_url(url)

    title = str(result.get("title") or "").strip()
    if is_anti_bot_title(title):
        title = ""
    if not title and slug_title:
        title = slug_title.title()
        result["title_source"] = "url_slug"
    else:
        result.setdefault("title_source", "structured_or_html" if title else "unknown")
    result["title"] = title

    product_id = str(result.get("id") or result.get("product_id") or "").strip()
    if not product_id and url_product_id:
        product_id = url_product_id
        result["product_id_source"] = "url"
    else:
        result.setdefault(
            "product_id_source", "structured" if product_id else "unknown"
        )
    result["id"] = product_id

    brand = str(result.get("brand") or "").strip()
    if brand:
        result["brand"] = brand
        existing_source = str(result.get("brand_source") or "").strip()
        if existing_source:
            # An already-normalized inferred brand must remain inferred on
            # every subsequent pass. Only fill missing provenance fields;
            # never promote an existing source to authoritative.
            result["brand_source"] = existing_source
            inferred = existing_source in _INFERRED_BRAND_SOURCES
            unknown = existing_source == "unknown"
            result.setdefault(
                "brand_confidence",
                0.9 if existing_source == "inferred_url_title"
                else (0.0 if inferred or unknown else 1.0),
            )
            result.setdefault(
                "brand_authoritative", not inferred and not unknown
            )
        else:
            result["brand_source"] = "structured_or_metadata"
            result["brand_confidence"] = 1.0
            result["brand_authoritative"] = True
        return result

    inferred_brand, confidence = infer_brand_from_title(title or slug_title)
    result["brand"] = inferred_brand
    if inferred_brand:
        result["brand_source"] = "inferred_url_title"
        result["brand_confidence"] = confidence
        result["brand_authoritative"] = False
    else:
        result["brand_source"] = "unknown"
        result["brand_confidence"] = 0.0
        result["brand_authoritative"] = False
    return result


def slug_from_url(url: str) -> str:
    """Backward-compatible public slug helper."""
    return product_identity_from_url(url)[0]
