import requests
from bs4 import BeautifulSoup
import logging
import urllib.parse
import json
import re
from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_from_next_data(html: str) -> dict:
    """
    Pull product fields from Walmart's embedded __NEXT_DATA__ JSON blob.
    This is the most reliable source — it always uses the English product name.
    """
    result = {}
    match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not match:
        return result

    try:
        data = json.loads(match.group(1))
        # Navigate to product node — path varies slightly by page version
        product = (
            data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("data", {})
                .get("product", {})
        )
        if not product:
            # Alternative path used on some Walmart pages
            items = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("initialData", {})
                    .get("data", {})
                    .get("idmlMap", {})
            )
            if items:
                product = next(iter(items.values()), {})

        if product:
            result["title"]       = product.get("name", "")
            result["brand"]       = (product.get("brand", "") or "")
            result["description"] = product.get("shortDescription", "") or product.get("longDescription", "")
            result["id"]          = str(product.get("usItemId", "") or product.get("productId", ""))

            image_info = product.get("imageInfo") or {}
            all_images = image_info.get("allImages") or []
            result["image"] = (
                image_info.get("thumbnailUrl")
                or (all_images[0].get("url", "") if all_images else "")
                or product.get("imageUrl", "")
            )

            price_info = product.get("priceInfo") or {}
            current_price = price_info.get("currentPrice") or {}
            result["price"] = current_price.get("priceString") or (
                f"${current_price['price']:.2f}" if current_price.get("price") is not None else ""
            )

        logger.info(f"[Scraper] __NEXT_DATA__ title: '{result.get('title', '')[:60]}'")
    except Exception as e:
        logger.warning(f"[Scraper] __NEXT_DATA__ parse failed: {e}")

    return result


def _extract_from_ld_json(soup: BeautifulSoup) -> dict:
    """Extract product fields from schema.org ld+json blocks."""
    result = {}
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = data[0]
            if data.get("@type") == "Product":
                result["title"] = data.get("name", "")
                brand = data.get("brand", {})
                result["brand"] = brand.get("name", "") if isinstance(brand, dict) else str(brand)
                result["description"] = data.get("description", "")
                image = data.get("image", "")
                if isinstance(image, list):
                    image = image[0] if image else ""
                if isinstance(image, dict):
                    image = image.get("url", "")
                result["image"] = image
                offers = data.get("offers") or {}
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price")
                currency = offers.get("priceCurrency", "USD")
                if price is not None:
                    result["price"] = f"${float(price):.2f}" if currency == "USD" else f"{price} {currency}"
                break
        except Exception:
            pass
    return result


def _is_mostly_non_english(text: str) -> bool:
    """
    Returns True when the title likely contains non-English text.

    Strategy: count accented Latin characters specifically common in Spanish,
    French, Portuguese, Italian (á é í ó ú ñ ã etc.) — distinct from symbols
    like ® ™ © which appear in English product names.

    Threshold: > 2 accented chars in a title of 20+ characters.
    This catches "Probiótico Líquido Orgánico" (7 hits) but ignores
    English titles that may have one incidental accented char.
    """
    if not text or len(text) < 20:
        return False
    accented = re.findall(
        r'[àáâãäåæçèéêëìíîïðñòóôõöùúûüýþÿ'
        r'ÀÁÂÃÄÅÆÇÈÉÊËÌÍÎÏÐÑÒÓÔÕÖÙÚÛÜÝÞ]',
        text,
    )
    return len(accented) >= 2


def _slug_from_url(url: str) -> str:
    """
    Extract a readable English title from the Walmart /ip/<slug>/<id> URL.
    e.g. /ip/2-Pack-Dr-Dan-s-CORTIBALM-4-20-g/306743072 → '2 Pack Dr Dan s CORTIBALM 4 20 g'
    """
    try:
        parts = urllib.parse.urlparse(url).path.strip("/").split("/")
        # parts = ['ip', 'slug-words-here', '1234567']
        if len(parts) >= 2:
            slug = parts[1]  # e.g. '2-Pack-Dr-Dan-s-CORTIBALM-4-20-g'
            return slug.replace("-", " ").strip()
    except Exception:
        pass
    return ""


def _decode_json_string_token(value: str) -> str:
    """Decode a string value captured directly from raw JSON source."""
    try:
        decoded = json.loads(f'"{value}"')
    except (json.JSONDecodeError, TypeError):
        return value
    return decoded if isinstance(decoded, str) else value


def _extract_brand_fallback(html: str, soup: BeautifulSoup) -> str:
    """Try multiple fallback strategies to extract brand name."""
    # 1. og:brand meta
    meta_brand = soup.find("meta", attrs={"property": "og:brand"})
    if meta_brand:
        return meta_brand.get("content", "")

    # 2. itemprop=brand
    span_brand = soup.find(attrs={"itemprop": "brand"})
    if span_brand:
        return span_brand.get_text(strip=True)

    # 3. __NEXT_DATA__ regex
    m = re.search(r'"brand":\{"[^"]*":"([^"]+)"\}', html)
    if m:
        return _decode_json_string_token(m.group(1))

    m = re.search(r'"brand":"([^"]+)"', html)
    if m:
        return _decode_json_string_token(m.group(1))

    return ""


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def fetch_product_content(url: str) -> dict:
    """
    Fetches product content from a Walmart product URL.
    Extracts: title (English), description, features, breadcrumb, brand, id.

    Title priority:
      1. __NEXT_DATA__ JSON  (always English, most reliable)
      2. ld+json schema.org Product
      3. <h1> tag (may be in a foreign language for bilingual listings)
      4. URL slug  (English fallback when h1 is non-English)
    """
    result = {
        "title": "",
        "description": "",
        "features": [],
        "breadcrumb": [],
        "brand": "",
        "id": "",
        "image": "",
        "price": "",
    }

    # Extract product ID from URL path
    try:
        parsed_url = urllib.parse.urlparse(url)
        path_parts = parsed_url.path.strip("/").split("/")
        if path_parts:
            result["id"] = path_parts[-1].split("?")[0]
    except Exception as e:
        logger.warning(f"[Scraper] Failed to parse ID from URL: {e}")

    # ------------------------------------------------------------------
    # Fetch HTML
    # ------------------------------------------------------------------
    html_content = ""
    try:
        if settings.webscraping_api_key:
            logger.info(f"[Scraper] Using WebScrapingAPI for {url}")
            api_url = "https://api.webscrapingapi.com/v2"
            params = {
                "api_key": settings.webscraping_api_key,
                "url": url,
                "render_js": "1",
                "country": "us",
            }
            # Force English-language response
            wsa_headers = {
                "Accept-Language": "en-US,en;q=0.9",
                "Wsa-Accept-Language": "en-US,en;q=0.9",
            }
            try:
                response = requests.get(api_url, params=params, headers=wsa_headers, timeout=30)
                response.raise_for_status()
                html_content = response.text
            except Exception as api_err:
                logger.warning(f"[Scraper] WebScrapingAPI failed ({api_err}). Falling back to direct request.")

        if not html_content:
            logger.info(f"[Scraper] Using direct request for {url}")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            html_content = response.text

    except Exception as e:
        logger.error(f"[Scraper] Error fetching {url}: {e}")
        return result

    soup = BeautifulSoup(html_content, "html.parser")

    # ------------------------------------------------------------------
    # 1. Extract title — priority order
    # ------------------------------------------------------------------

    # Priority 1: __NEXT_DATA__ JSON (English, structured, most reliable)
    next_data = _extract_from_next_data(html_content)
    if next_data.get("title"):
        result["title"] = next_data["title"]
        if next_data.get("brand"):
            result["brand"] = next_data["brand"]
        if next_data.get("description"):
            result["description"] = next_data["description"]
        if next_data.get("id"):
            result["id"] = next_data["id"]
        if next_data.get("image"):
            result["image"] = next_data["image"]
        if next_data.get("price"):
            result["price"] = next_data["price"]

    # Priority 2: ld+json schema.org (fallback)
    if not result["title"]:
        ld = _extract_from_ld_json(soup)
        if ld.get("title"):
            result["title"] = ld["title"]
            logger.info(f"[Scraper] ld+json title: '{result['title'][:60]}'")
        if not result["brand"] and ld.get("brand"):
            result["brand"] = ld["brand"]
        if not result["description"] and ld.get("description"):
            result["description"] = ld["description"]
        if not result["image"] and ld.get("image"):
            result["image"] = ld["image"]
        if not result["price"] and ld.get("price"):
            result["price"] = ld["price"]

    # Priority 3: <h1> tag
    if not result["title"]:
        h1 = soup.find("h1")
        if h1:
            result["title"] = h1.get_text(strip=True)
            logger.info(f"[Scraper] h1 title: '{result['title'][:60]}'")

    # Priority 4: URL slug fallback — used when title is non-English
    if result["title"] and _is_mostly_non_english(result["title"]):
        slug_title = _slug_from_url(url)
        if slug_title:
            logger.warning(
                f"[Scraper] Title appears non-English: '{result['title'][:50]}' "
                f"→ using URL slug: '{slug_title}'"
            )
            result["title"] = slug_title

    # ------------------------------------------------------------------
    # 2. Description from meta if still missing
    # ------------------------------------------------------------------
    if not result["description"]:
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc:
            result["description"] = meta_desc.get("content", "")

    # ------------------------------------------------------------------
    # 3. Breadcrumb
    # ------------------------------------------------------------------
    breadcrumb = []
    nav = soup.find("nav", attrs={"aria-label": "breadcrumb"})
    if nav:
        for item in nav.find_all("a"):
            breadcrumb.append(item.get_text(strip=True))
    elif soup.find("ol"):
        for li in soup.find("ol").find_all("li"):
            breadcrumb.append(li.get_text(strip=True))
    result["breadcrumb"] = breadcrumb

    # ------------------------------------------------------------------
    # 4. Features from <ul><li> after removing noisy elements
    # ------------------------------------------------------------------
    for element in soup(["header", "footer", "nav", "aside", "form"]):
        element.decompose()

    features = []
    for ul in soup.find_all("ul"):
        for li in ul.find_all("li"):
            text = li.get_text(strip=True)
            if text and len(text) > 10:
                features.append(text)
    result["features"] = features[:10]

    # ------------------------------------------------------------------
    # 5. Image / price fallbacks
    # ------------------------------------------------------------------
    if not result["image"]:
        meta_image = soup.find("meta", attrs={"property": "og:image"})
        if meta_image:
            result["image"] = meta_image.get("content", "")

    if not result["price"]:
        price_match = re.search(r'"priceString":"([^"]+)"', html_content)
        if price_match:
            result["price"] = _decode_json_string_token(price_match.group(1))
        else:
            price_match = re.search(r'"currentPrice":\{"price":([\d.]+)', html_content)
            if price_match:
                result["price"] = f"${float(price_match.group(1)):.2f}"

    # ------------------------------------------------------------------
    # 6. Brand fallback
    # ------------------------------------------------------------------
    if not result["brand"]:
        result["brand"] = _extract_brand_fallback(html_content, soup)

    # ------------------------------------------------------------------
    # 7. Captcha guard
    # ------------------------------------------------------------------
    if "robot or human" in result["title"].lower():
        logger.warning(f"[Scraper] Captcha detected for {url}. Clearing title for fallback.")
        result["title"] = ""

    logger.info(
        f"[Scraper] Done — title='{result['title'][:50]}' "
        f"brand='{result['brand']}' id='{result['id']}' "
        f"price='{result['price']}'"
    )
    return result
