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
    Fetch a Walmart browse shelf and check brand + product presence on it,
    paging through the shelf to record *where* the product appears.

    When a brand is supplied the page is fetched with the brand facet applied
    (`?facet=brand:<Brand>`), letting us distinguish independent signals.

    Returns:
        {"brand": bool|None, "product": bool, "page": int}
            brand   — True  : the brand facet returns results on this shelf
                      False : the brand is not carried on this shelf
                      None  : no brand supplied, so not evaluated
            product — True/False : the product ID appears on the shelf
            page    — 1-based paginated page the product was found on
                      (0 when the product was not found)
        None  — the category page itself does not exist ("page couldn't be found").
                Callers must exclude None results from found/missing counts.
    """
    brand_applied = bool(brand)
    try:
        if brand_applied:
            encoded_brand = urllib.parse.quote(brand)
            base_url  = url.split("?")[0]
            shelf_url = f"{base_url}?facet=brand%3A{encoded_brand}"
        else:
            shelf_url = url

        # --- Page 1 ---
        first_html = _fetch_html(shelf_url)

        # --- Guard: skip pages that don't exist ---
        # With a brand facet, an empty result means "brand absent" (a valid
        # page), so only a strict 404 counts as invalid. Without a brand we
        # keep the broader marker set to skip genuinely empty categories.
        is_invalid = _is_strict_404(first_html) if brand_applied else _is_page_not_found(first_html)
        if is_invalid:
            logger.warning(f"[ShelfChecker] Page not found (skipping): {shelf_url}")
            return None   # signals "invalid" to the caller

        found_page = 0
        if product_id and str(product_id) in first_html:
            found_page = 1

        # --- Page through the shelf until the product is found ---
        # Stop early once a page returns no results (end of shelf).
        if not found_page and product_id and max_pages > 1 and not _is_empty_results(first_html):
            for page_num in range(2, max_pages + 1):
                page_html = _fetch_html(_with_page(shelf_url, page_num))
                if not page_html or _is_strict_404(page_html) or _is_empty_results(page_html):
                    break
                if str(product_id) in page_html:
                    found_page = page_num
                    break

        product_present = found_page > 0

        if not brand_applied:
            brand_present = None
        elif product_present:
            # Product is on the brand-filtered shelf → brand is necessarily present
            brand_present = True
        else:
            brand_present = not _is_empty_results(first_html)

        return {"brand": brand_present, "product": product_present, "page": found_page}

    except Exception as e:
        logger.warning(f"[ShelfChecker] Unexpected error for {url}: {e}")
        return {"brand": None, "product": False, "page": 0}


# ---------------------------------------------------------------------------
# Async orchestration
# ---------------------------------------------------------------------------

async def check_shelf_visibility(pages: list, product_id: str, brand: str) -> dict:
    """
    Check whether the product appears on each candidate shelf page.

    Invalid pages ("This page couldn't be found.") are excluded from all
    counts — they are neither found nor missing.

    Returns:
        {
            "found":   int,   # shelves where product IS present
            "missing": int,   # shelves where product is absent
            "invalid": int,   # pages that no longer exist (excluded from score)
            "total":   int,   # found + missing  (invalid not counted)
            "score":   float, # found / total * 100
            "details": {url: {"brand": bool|None, "product": bool} | None, ...}
        }
    """
    if not pages or not product_id:
        return {"found": 0, "missing": 0, "invalid": 0, "total": 0, "score": 0.0, "details": {}}

    sem = asyncio.Semaphore(3)

    async def process_page(url: str):
        async with sem:
            result = await asyncio.to_thread(fetch_shelf_sync, url, product_id, brand)
            return url, result

    tasks    = [process_page(item.get("url", "")) for item in pages if item.get("url")]
    outcomes = await asyncio.gather(*tasks)

    details:       dict = {}
    found_count:   int  = 0
    invalid_count: int  = 0

    for url, result in outcomes:
        details[url] = result
        if result is None:
            invalid_count += 1
        elif result.get("product"):
            found_count += 1
        # product False → missing, counted below

    valid_total   = len(outcomes) - invalid_count   # pages that actually exist
    missing_count = valid_total - found_count
    score         = (found_count / valid_total * 100) if valid_total > 0 else 0.0

    logger.info(
        f"[ShelfChecker] Results — found={found_count} missing={missing_count} "
        f"invalid={invalid_count} total={valid_total} score={score:.1f}%"
    )

    return {
        "found":   found_count,
        "missing": missing_count,
        "invalid": invalid_count,
        "total":   valid_total,
        "score":   round(score, 1),
        "details": details,
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
    result = {"organic": False, "sponsored": False}
    if not html or not product_id:
        return result
    pid = str(product_id)

    m = _NEXT_DATA_RE.search(html)
    if m:
        try:
            data = json.loads(m.group(1))
            for item in _iter_items(data):
                iid = str(item.get("usItemId") or item.get("id") or "")
                if iid == pid:
                    if item.get("isSponsoredFlag"):
                        result["sponsored"] = True
                    else:
                        result["organic"] = True
            if result["organic"] or result["sponsored"]:
                return result
        except Exception as e:
            logger.warning(f"[ShelfChecker] Placement parse failed: {e}")

    # Fallback: substring presence — can't tell sponsorship, assume organic
    if pid in html:
        result["organic"] = True
    return result


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
