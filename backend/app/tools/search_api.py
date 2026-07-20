import requests
import urllib.parse
from typing import List
import logging
from app.config import settings

logger = logging.getLogger(__name__)

# Accepted hostnames — subdomains like business.walmart.com are rejected
_ALLOWED_HOSTS = {"walmart.com", "www.walmart.com"}


def _is_valid_walmart_browse_url(url: str) -> bool:
    """
    Returns True only when the URL is a genuine walmart.com browse page.

    Accepts : https://www.walmart.com/browse/...
    Rejects : https://business.walmart.com/browse/...   ← subdomain
    Rejects : https://walmart.com/search?...            ← not a browse path
    """
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.netloc.lower().lstrip("www.")   # normalise www prefix
        host   = parsed.netloc.lower()                  # keep original for set lookup
        return (
            host in _ALLOWED_HOSTS
            and "/browse" in parsed.path
        )
    except Exception:
        return False


def search_walmart_browse(keyword: str) -> List[dict]:
    """
    Searches Google for Walmart browse/category pages using Serper API.
    Query: site:walmart.com/browse "<keyword>"

    Only URLs whose hostname is exactly walmart.com or www.walmart.com
    are returned — subdomains (business.walmart.com, etc.) are filtered out.
    """
    if not settings.serper_api_key:
        logger.warning("[SearchAPI] SERPER_API_KEY is not set. Returning empty results.")
        return []

    api_url = "https://google.serper.dev/search"
    query   = f'site:walmart.com/browse "{keyword}"'

    payload = {"q": query, "num": 5}
    headers = {
        "X-API-KEY":    settings.serper_api_key,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        data = response.json()

        results_data = []
        for result in data.get("organic", []):
            link     = result.get("link", "")
            position = result.get("position", None)
            title    = result.get("title", "") or ""

            if not link:
                continue

            if _is_valid_walmart_browse_url(link):
                # Title is retained so downstream branded-shelf classification
                # can inspect breadcrumb/brand metadata for this URL.
                results_data.append({"url": link, "position": position, "title": title})
            else:
                logger.warning(f"[SearchAPI] Rejected non-walmart.com URL: {link}")

        logger.info(
            f"[SearchAPI] '{keyword}' → {len(results_data)} valid browse URLs "
            f"({len(data.get('organic', []))} raw results)"
        )
        return results_data

    except Exception as e:
        logger.error(f"[SearchAPI] Request failed for keyword '{keyword}': {e}")
        return []
