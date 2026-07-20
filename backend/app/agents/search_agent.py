import logging
import urllib.parse
from typing import List
from app.tools.search_api import search_walmart_browse, _ALLOWED_HOSTS

logger = logging.getLogger(__name__)


def _is_allowed_host(url: str) -> bool:
    """Secondary domain guard — rejects any non-walmart.com hostname."""
    try:
        return urllib.parse.urlparse(url).netloc.lower() in _ALLOWED_HOSTS
    except Exception:
        return False


def find_browse_pages(keywords: List[str]) -> List[dict]:
    """
    Runs a Serper search for each keyword and returns a deduplicated list
    of Walmart browse page URLs.

    Deduplication rule: if the same URL appears for multiple keywords,
    keep the entry with the lowest (best) search rank position.

    Domain filter: only walmart.com and www.walmart.com are accepted.
    Any subdomain (e.g. business.walmart.com) is silently dropped here
    as a second line of defence after search_api.py's primary filter.
    """
    url_map: dict = {}

    for kw in keywords:
        logger.info(f"[SearchAgent] Searching: '{kw}'")
        results = search_walmart_browse(kw)

        for res in results:
            url   = res.get("url", "")
            pos   = res.get("position")
            title = res.get("title", "") or ""

            # Secondary domain guard
            if not _is_allowed_host(url):
                logger.warning(f"[SearchAgent] Dropped disallowed host: {url}")
                continue

            if url not in url_map:
                url_map[url] = {"keyword": kw, "position": pos, "title": title}
            else:
                # Keep the best (lowest numbered) rank position
                current = url_map[url]
                current_pos = current.get("position")
                if pos is not None and (current_pos is None or pos < current_pos):
                    url_map[url] = {"keyword": kw, "position": pos,
                                    "title": title or current.get("title", "")}
                elif title and not current.get("title"):
                    current["title"] = title

    pages = [
        {"url": u, "keyword": v["keyword"], "position": v["position"],
         "title": v.get("title", "")}
        for u, v in url_map.items()
    ]
    logger.info(f"[SearchAgent] {len(pages)} unique browse pages from {len(keywords)} keywords")
    return pages
