import logging
import asyncio
import json
import re
import urllib.parse
from typing import Optional
from app.config import settings
import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Walmart "page not found" detection
# ---------------------------------------------------------------------------

# Phrases Walmart embeds in its 404 / deleted-category pages.
# All checks are case-insensitive against the raw HTML.
_NOT_FOUND_MARKERS = [
    "this page couldn't be found",
    "we can't find the page",
    "page not found",
    "no results for",                       # empty browse page with no categories
    '"statusCode":404',                     # __NEXT_DATA__ JSON error field
    '"errorCode":"PAGE_NOT_FOUND"',         # alternate __NEXT_DATA__ pattern
]

# Strict 404 markers — a genuinely non-existent category page.
# Excludes "no results for" so an *empty brand-filtered* page is treated as a
# valid category that simply doesn't carry the brand (brand_present = False),
# rather than being discarded as invalid.
_STRICT_404_MARKERS = [
    "this page couldn't be found",
    "we can't find the page",
    "page not found",
    '"statuscode":404',
    '"errorcode":"page_not_found"',
]

# Markers/JSON fields indicating a brand facet returned zero items.
_EMPTY_RESULT_MARKERS = [
    "no results for",
    "0 results",
    "we couldn't find",
    '"totalitemcount":0',
    '"itemcount":0',
    '"totalcount":0',
]


def _is_page_not_found(html: str) -> bool:
    """
    Returns True when the fetched HTML is a Walmart "not found" page.
    Checks multiple known patterns so both rendered and non-rendered
    responses are caught.
    """
    if not html:
        return False
    lower = html.lower()
    return any(marker.lower() in lower for marker in _NOT_FOUND_MARKERS)


def _is_strict_404(html: str) -> bool:
    """True only for genuinely non-existent pages (ignores empty-result text)."""
    if not html:
        return False
    lower = html.lower()
    return any(marker in lower for marker in _STRICT_404_MARKERS)


def _is_empty_results(html: str) -> bool:
    """True when a (brand-filtered) browse page returned zero items."""
    if not html:
        return True
    lower = html.lower()
    return any(marker in lower for marker in _EMPTY_RESULT_MARKERS)


# How many paginated pages of a shelf to scan when looking for the product.
# Higher = more accurate depth/placement data, but more requests per shelf.
MAX_SHELF_PAGES = 3


# ---------------------------------------------------------------------------
# Single-page fetch
# ---------------------------------------------------------------------------

def _fetch_html(shelf_url: str) -> str:
    """Fetch raw HTML for a shelf URL (WebScrapingAPI with direct fallback)."""
    html = ""
    try:
        if settings.webscraping_api_key:
            api_url = "https://api.webscrapingapi.com/v2"
            params  = {
                "api_key":   settings.webscraping_api_key,
                "url":       shelf_url,
                "render_js": "1",
                "country":   "us",
            }
            api_headers = {
                "Accept-Language":     "en-US,en;q=0.9",
                "Wsa-Accept-Language": "en-US,en;q=0.9",
            }
            try:
                response = requests.get(api_url, params=params, headers=api_headers, timeout=60)
                response.raise_for_status()
                html = response.text
            except Exception as api_err:
                logger.warning(f"[ShelfChecker] WebScrapingAPI failed ({api_err}). Falling back.")

        if not html:
            headers = {
                "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/122.0.0.0 Safari/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            }
            response = requests.get(shelf_url, headers=headers, timeout=30)
            response.raise_for_status()
            html = response.text
    except Exception as fetch_err:
        logger.warning(f"[ShelfChecker] Fetch failed for {shelf_url}: {fetch_err}")
    return html


def _with_page(shelf_url: str, page: int) -> str:
    """Append a `page=N` query param to a shelf URL (page 1 = the bare URL)."""
    if page <= 1:
        return shelf_url
    sep = "&" if "?" in shelf_url else "?"
    return f"{shelf_url}{sep}page={page}"


# ---------------------------------------------------------------------------
# Shelf fetch — brand presence, product presence, and placement depth
# ---------------------------------------------------------------------------

def fetch_shelf_sync(
    url: str, product_id: str, brand: str, max_pages: int = MAX_SHELF_PAGES
) -> Optional[dict]:
    """
    Fetch a Walmart browse shelf and derive the three visibility signals for a
    product, fetching BOTH views of the shelf:

      * Base browse URL          — the general "Walmart Digital Shelf".
      * Brand-filtered URL        — `<url>?facet=brand:<brand>`, the
                                    "Digital Shelf (Brand Filter)".

    Signals (all page-1 presence — the single fetched page is treated as page 1;
    no real pagination is used for them):

      * visibility      — product present on the base/general shelf (raw
                          presence; may be driven by sponsored placements).
      * discoverability — product present on the brand-filtered shelf.
                          When no brand is supplied the brand filter can't apply,
                          so discoverability falls back to `visibility`.
      * organic         — True when any discrete placement is both visible
                          and discoverable.
      * sponsored       — True when any discrete placement carries Walmart's
                          sponsored marker. This is independent of `organic`.

    Returns:
        {
            "visibility": bool, "discoverability": bool,
            "organic": bool, "sponsored": bool,
            "placement_rank": int|None,  # first absolute Page 1 product rank
            "placements": [{"placement_rank": int|None,
                              "visibility": bool, "discoverability": bool,
                              "organic": bool, "sponsored": bool, ...}],
            # Legacy fields kept for the v2 orchestrator / email report:
            "brand":   bool|None,  # brand facet returns results on this shelf
            "product": bool,       # product ID appears on the brand-filtered
                                   # shelf (across paginated pages)
            "page":    int,        # 1-based page the product was found on (0=none)
        }
        None  — the category page itself does not exist ("page couldn't be found").
                Callers must exclude None results from all counts.
    """
    brand_applied = bool(brand)
    try:
        base_url = url.split("?")[0]
        if brand_applied:
            encoded_brand = urllib.parse.quote(brand)
            shelf_url = f"{base_url}?facet=brand%3A{encoded_brand}"
        else:
            shelf_url = url

        # --- Page 1 of the shelf we filter on (brand-filtered, or base when no brand) ---
        first_html = _fetch_html(shelf_url)

        # --- Guard: skip pages that don't exist ---
        # With a brand facet, an empty result means "brand absent" (a valid
        # page), so only a strict 404 counts as invalid. Without a brand we
        # keep the broader marker set to skip genuinely empty categories.
        is_invalid = _is_strict_404(first_html) if brand_applied else _is_page_not_found(first_html)
        if is_invalid:
            logger.warning(f"[ShelfChecker] Page not found (skipping): {shelf_url}")
            return None   # signals "invalid" to the caller

        # Discoverability — product on page 1 of the brand-filtered shelf. The
        # single fetched page is treated as page 1; we do not paginate for this.
        page1_occurrences = _extract_ranked_product_occurrences(first_html, product_id)
        page1_present = (
            bool(page1_occurrences)
            if page1_occurrences is not None
            else bool(product_id) and str(product_id) in first_html
        )
        discoverability = page1_present

        # --- Legacy: page through the shelf to record *where* the product
        # appears (feeds the v2 orchestrator's page-depth data). ---
        found_page = 1 if page1_present else 0
        if not found_page and product_id and max_pages > 1 and not _is_empty_results(first_html):
            for page_num in range(2, max_pages + 1):
                page_html = _fetch_html(_with_page(shelf_url, page_num))
                if not page_html or _is_strict_404(page_html) or _is_empty_results(page_html):
                    break
                if str(product_id) in page_html:
                    found_page = page_num
                    break

        product_present = found_page > 0

        # Visibility — raw presence on the base/general shelf (page 1). When no
        # brand is applied the shelf we already fetched *is* the base shelf.
        if brand_applied:
            base_html = _fetch_html(base_url)
            base_occurrences = _extract_ranked_product_occurrences(
                base_html, product_id
            )
            visibility = (
                bool(base_occurrences)
                if base_occurrences is not None
                else bool(product_id) and str(product_id) in base_html
            )
        else:
            base_html = first_html
            visibility = page1_present
            discoverability = visibility   # no brand filter → falls back to visibility

        placements = classify_placements(
            base_html, product_id, visibility, discoverability
        )
        organic = any(p["organic"] for p in placements)
        sponsored = any(p["sponsored"] for p in placements)
        placement_ranks = [
            p["placement_rank"]
            for p in placements
            if isinstance(p.get("placement_rank"), int)
            and p["placement_rank"] > 0
        ]
        placement_rank = min(placement_ranks) if placement_ranks else None

        if not brand_applied:
            brand_present = None
        elif product_present:
            # Product is on the brand-filtered shelf → brand is necessarily present
            brand_present = True
        else:
            brand_present = not _is_empty_results(first_html)

        return {
            "visibility":      visibility,
            "discoverability": discoverability,
            "organic":         organic,
            "sponsored":       sponsored,
            "placement_rank":  placement_rank,
            "placements":      placements,
            "brand":           brand_present,
            "product":         product_present,
            "page":            found_page,
        }

    except Exception as e:
        logger.warning(f"[ShelfChecker] Unexpected error for {url}: {e}")
        return {
            "visibility": False, "discoverability": False,
            "organic": False, "sponsored": False,
            "placement_rank": None, "placements": [],
            "brand": None, "product": False, "page": 0,
        }


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------

async def check_shelf_visibility(pages: list, product_id: str, brand: str) -> dict:
    """
    Check whether the product appears on each candidate shelf page.

    Invalid pages ("This page couldn't be found.") are excluded from all
    counts — they are neither found nor missing.

    Each shelf is checked on TWO views (base + brand-filtered), yielding page
    visibility/discoverability plus discrete organic/sponsored placements.

    Returns:
        {
            # Discoverability-driven (feeds the shared Discoverability Dashboard):
            "found":   int,   # shelves where the product is DISCOVERABLE
            "missing": int,   # shelves where the product is NOT discoverable
            "invalid": int,   # pages that no longer exist (excluded from score)
            "total":   int,   # total keyword returns checked (invalid not counted)
            "score":   float, # discoverable / total * 100
            # Per-signal aggregate counts:
            "visible":      int,  # shelves where the product is visible (base shelf)
            "discoverable": int,  # == found
            "placements":   int,  # product placement/impression records
            "organic":      int,  # organic placement/impression count
            "sponsored":    int,  # sponsored placement/impression count
            "organic_pages": int, # page-level compatibility count
            "details": {url: {"visibility": bool, "discoverability": bool,
                              "organic": bool, "sponsored": bool,
                              "placements": list, ...} | None, ...}
        }
    """
    if not pages or not product_id:
        return {
            "found": 0, "missing": 0, "invalid": 0, "total": 0, "score": 0.0,
            "visible": 0, "discoverable": 0, "placements": 0,
            "organic": 0, "sponsored": 0, "organic_pages": 0,
            "sponsored_pages": 0, "details": {},
        }

    sem = asyncio.Semaphore(3)

    async def process_page(url: str):
        async with sem:
            result = await asyncio.to_thread(fetch_shelf_sync, url, product_id, brand)
            return url, result

    tasks    = [process_page(item.get("url", "")) for item in pages if item.get("url")]
    outcomes = await asyncio.gather(*tasks)

    details:           dict = {}
    visible_count:     int  = 0
    discoverable_count: int = 0
    placement_count:   int  = 0
    organic_count:     int  = 0
    sponsored_count:   int  = 0
    organic_page_count: int = 0
    sponsored_page_count: int = 0
    invalid_count:     int  = 0

    for url, result in outcomes:
        details[url] = result
        if result is None:
            invalid_count += 1
            continue
        if result.get("visibility"):
            visible_count += 1
        if result.get("discoverability"):
            discoverable_count += 1
        placements = result.get("placements", [])
        placement_count += len(placements)
        organic_count += sum(1 for p in placements if p.get("organic"))
        sponsored_count += sum(1 for p in placements if p.get("sponsored"))
        if result.get("organic"):
            organic_page_count += 1
        if result.get("sponsored"):
            sponsored_page_count += 1

    valid_total   = len(outcomes) - invalid_count   # pages that actually exist
    found_count   = discoverable_count              # Discoverability-driven (Discoverability Dashboard)
    missing_count = valid_total - discoverable_count
    score         = (discoverable_count / valid_total * 100) if valid_total > 0 else 0.0

    logger.info(
        f"[ShelfChecker] Results — visible={visible_count} discoverable={discoverable_count} "
        f"placements={placement_count} organic={organic_count} sponsored={sponsored_count} "
        f"missing={missing_count} invalid={invalid_count} "
        f"total={valid_total} score={score:.1f}%"
    )

    return {
        "found":        found_count,
        "missing":      missing_count,
        "invalid":      invalid_count,
        "total":        valid_total,
        "score":        round(score, 1),
        "visible":      visible_count,
        "discoverable": discoverable_count,
        "placements":   placement_count,
        "organic":      organic_count,
        "sponsored":    sponsored_count,
        "organic_pages": organic_page_count,
        "sponsored_pages": sponsored_page_count,
        "details":      details,
    }


# ---------------------------------------------------------------------------
# Sponsored vs organic placement (search results)
# ---------------------------------------------------------------------------

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def _iter_items(node):
    """Yield product-item dicts (those carrying an isSponsoredFlag) from JSON."""
    if isinstance(node, dict):
        if "isSponsoredFlag" in node and ("usItemId" in node or "id" in node):
            yield node
        for v in node.values():
            yield from _iter_items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _iter_items(v)


def _item_product_id(item: dict) -> str:
    """Return the Walmart item identifier from a structured result item."""
    if not isinstance(item, dict):
        return ""
    value = item.get("usItemId") or item.get("itemId")
    if value is None and "isSponsoredFlag" in item:
        value = item.get("id")
    return str(value or "")


def _product_items(items) -> list[dict]:
    """Keep only product cards from one ordered result-item collection."""
    if not isinstance(items, list):
        return []
    return [item for item in items if _item_product_id(item)]


def _items_from_search_result(search_result: dict) -> list[dict]:
    """Flatten Walmart's ordered Page 1 item stacks into display order."""
    if not isinstance(search_result, dict):
        return []

    item_stacks = search_result.get("itemStacks")
    if isinstance(item_stacks, list):
        ordered_items = []
        for stack in item_stacks:
            if not isinstance(stack, dict):
                continue
            stack_items = stack.get("items")
            if not isinstance(stack_items, list):
                stack_items = stack.get("itemsV2")
            ordered_items.extend(_product_items(stack_items))
        if ordered_items:
            return ordered_items

    return _product_items(search_result.get("items"))


def _find_ranked_result_items(node) -> Optional[list[dict]]:
    """
    Find the canonical ordered Page 1 result collection in __NEXT_DATA__.

    ``None`` means structured ordering is unavailable. An empty list means a
    result collection was found but it contains no product cards.
    """
    if isinstance(node, dict):
        search_result = node.get("searchResult")
        if isinstance(search_result, dict):
            items = _items_from_search_result(search_result)
            if items or "itemStacks" in search_result or "items" in search_result:
                return items

        if "itemStacks" in node:
            items = _items_from_search_result(node)
            if items or isinstance(node.get("itemStacks"), list):
                return items

        for value in node.values():
            result = _find_ranked_result_items(value)
            if result is not None:
                return result
    elif isinstance(node, list):
        for value in node:
            result = _find_ranked_result_items(value)
            if result is not None:
                return result
    return None


def _extract_ranked_product_occurrences(
    html: str, product_id: str
) -> Optional[list[dict]]:
    """
    Return the target's occurrences with absolute Page 1 product-card ranks.

    The rank counts all product cards, sponsored and organic, in Walmart's
    structured display order. ``None`` indicates that no trustworthy ordered
    result collection was present, allowing callers to retain legacy fallback
    behavior without presenting an inferred value as an exact rank.
    """
    if not html or not product_id:
        return None

    match = _NEXT_DATA_RE.search(html)
    if not match:
        return None

    try:
        data = json.loads(match.group(1))
    except Exception as exc:
        logger.warning(f"[ShelfChecker] Placement rank parse failed: {exc}")
        return None

    ranked_items = _find_ranked_result_items(data)
    if ranked_items is None:
        return None

    target_id = str(product_id)
    occurrences = []
    for rank, item in enumerate(ranked_items, start=1):
        if _item_product_id(item) != target_id:
            continue
        raw_flag = item.get("isSponsoredFlag")
        occurrences.append({
            "placement_rank": rank,
            "sponsored": raw_flag is True or str(raw_flag).lower() == "true",
        })
    return occurrences


def _extract_sponsored_flags(html: str, product_id: str) -> list[bool]:
    """Return one sponsorship flag for every structured product occurrence."""
    if not html or not product_id:
        return []

    match = _NEXT_DATA_RE.search(html)
    if not match:
        return []

    try:
        data = json.loads(match.group(1))
    except Exception as exc:
        logger.warning(f"[ShelfChecker] Placement parse failed: {exc}")
        return []

    product_id = str(product_id)
    flags = []
    for item in _iter_items(data):
        item_id = str(item.get("usItemId") or item.get("id") or "")
        if item_id != product_id:
            continue
        raw_flag = item.get("isSponsoredFlag")
        flags.append(raw_flag is True or str(raw_flag).lower() == "true")
    return flags


def classify_placements(
    html: str,
    product_id: str,
    visibility: bool,
    discoverability: bool,
) -> list[dict]:
    """
    Classify every occurrence of a product on one page independently.

    Structured Walmart data supplies sponsorship and the absolute Page 1 rank
    per occurrence. The existing organic rule is evaluated for each placement:
    `organic = visibility and discoverability`. A sponsored occurrence is not
    assigned the separate natural-discoverability signal merely because the
    same product also has an organic occurrence elsewhere on the page.

    If exact structured result ordering is unavailable, the placement rank is
    left as ``None``. Legacy occurrence/fallback classification remains
    reportable without presenting an inferred occurrence index as an exact
    product-card rank.
    """
    if not visibility or not product_id:
        return []

    ranked_occurrences = _extract_ranked_product_occurrences(html, product_id)
    if ranked_occurrences is not None:
        placements = []
        for index, occurrence in enumerate(ranked_occurrences, start=1):
            sponsored = occurrence["sponsored"]
            placement_discoverability = bool(discoverability and not sponsored)
            placement_visibility = True
            placements.append({
                "placement_index": index,
                "placement_rank": occurrence["placement_rank"],
                "visibility": placement_visibility,
                "discoverability": placement_discoverability,
                "organic": placement_visibility and placement_discoverability,
                "sponsored": sponsored,
                "classification_source": "structured",
            })
        return placements

    sponsored_flags = _extract_sponsored_flags(html, product_id)
    if sponsored_flags:
        placements = []
        for index, sponsored in enumerate(sponsored_flags, start=1):
            placement_discoverability = bool(discoverability and not sponsored)
            placement_visibility = True
            placements.append({
                "placement_index": index,
                "placement_rank": None,
                "visibility": placement_visibility,
                "discoverability": placement_discoverability,
                "organic": placement_visibility and placement_discoverability,
                "sponsored": sponsored,
                "classification_source": "structured_unranked",
            })
        return placements

    inferred_discoverability = bool(discoverability)
    return [{
        "placement_index": 1,
        "placement_rank": None,
        "visibility": True,
        "discoverability": inferred_discoverability,
        "organic": inferred_discoverability,
        "sponsored": not inferred_discoverability,
        "classification_source": "inferred",
    }]


def analyze_placement(html: str, product_id: str) -> dict:
    """
    Determine how a product is placed on a Walmart results page.

    Walmart embeds each result item in the page's __NEXT_DATA__ JSON with a
    per-item `isSponsoredFlag`. A product can appear twice (sponsored ad +
    organic listing), so we collect every match for the product id.

    Returns {"organic": bool, "sponsored": bool}. Falls back to a raw
    substring presence check (counted as organic) when structured data is
    unavailable.
    """
    product_visible = bool(html and product_id and str(product_id) in html)
    placements = classify_placements(
        html, product_id, product_visible, product_visible
    )
    return {
        "organic": any(p["organic"] for p in placements),
        "sponsored": any(p["sponsored"] for p in placements),
    }


def fetch_search_placement_sync(keyword: str, product_id: str) -> dict:
    """
    Fetch the Walmart search results page for `keyword` and report whether the
    product appears as a sponsored listing, an organic listing, both, or not
    at all.

    Returns {"organic": bool, "sponsored": bool}.
    """
    if not keyword or not product_id:
        return {"organic": False, "sponsored": False}
    q = urllib.parse.quote_plus(keyword.strip())
    search_url = f"https://www.walmart.com/search?q={q}"
    html = _fetch_html(search_url)
    placement = analyze_placement(html, product_id)
    logger.info(
        f"[Placement] keyword='{keyword}' "
        f"sponsored={placement['sponsored']} organic={placement['organic']}"
    )
    return placement
