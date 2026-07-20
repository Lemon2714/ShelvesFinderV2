"""Regression tests for JSON escapes captured from raw Walmart markup."""

from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup

from app.tools.scraper import _extract_brand_fallback, fetch_product_content
from app.tools.shelf_checker import fetch_shelf_sync
from app.tools.shelf_classifier import classify_shelf


PRODUCT_URL = "https://www.walmart.com/ip/head-and-shoulders/408353826"
PRODUCT_ID = "408353826"
BRAND = "Head & Shoulders"
OWN_BRAND_SHELF = (
    "https://www.walmart.com/browse/beauty/shampoo/"
    "head-shoulders/1085666_123"
)
GENERIC_SHELF = (
    "https://www.walmart.com/browse/beauty/shampoo/1085666_1071969"
)


def _scrape_html(html: str) -> dict:
    with patch("app.tools.scraper.settings") as mock_settings, patch(
        "app.tools.scraper.requests.get"
    ) as mock_get:
        mock_settings.webscraping_api_key = ""
        response = mock_get.return_value
        response.raise_for_status.return_value = None
        response.text = html
        return fetch_product_content(PRODUCT_URL)


@pytest.mark.parametrize(
    "html",
    [
        '<meta property="og:brand" content="L’Oréal / Paris &amp; Co">',
        '<span itemprop="brand">L’Oréal / Paris &amp; Co</span>',
        r'<script>{"brand":{"name":"L\u2019Oréal \/ Paris \u0026 Co"}}</script>',
        r'<script>{"brand":"L\u2019Oréal \/ Paris \u0026 Co"}</script>',
    ],
    ids=["og-meta", "itemprop", "brand-object-regex", "brand-string-regex"],
)
def test_all_brand_fallback_strategies_emit_decoded_text(html: str) -> None:
    brand = _extract_brand_fallback(html, BeautifulSoup(html, "html.parser"))

    assert brand == "L’Oréal / Paris & Co"
    assert "\\" not in brand


def test_malformed_json_string_capture_falls_back_to_raw_value() -> None:
    html = '<script>{"brand":"Broken \\"}</script>'

    brand = _extract_brand_fallback(html, BeautifulSoup(html, "html.parser"))

    assert brand == "Broken \\"


def test_scraped_brand_classifies_matching_own_brand_shelf() -> None:
    product = _scrape_html(
        r'<html><h1>Head &amp; Shoulders Shampoo</h1>'
        r'<script>{"brand":"Head \u0026 Shoulders"}</script></html>'
    )

    classification = classify_shelf(
        OWN_BRAND_SHELF,
        product_brand=product["brand"],
        title="",
    )

    assert product["brand"] == BRAND
    assert classification.is_branded is True
    assert classification.reason == "brand_category_node"


def test_scraped_brand_builds_decoded_facet_url() -> None:
    product = _scrape_html(
        r'<html><h1>Head &amp; Shoulders Shampoo</h1>'
        r'<script>{"brand":"Head \u0026 Shoulders"}</script></html>'
    )
    fetched = []

    def fake_fetch(url: str) -> str:
        fetched.append(url)
        return f"<html>{PRODUCT_ID}</html>"

    with patch("app.tools.shelf_checker._fetch_html", side_effect=fake_fetch):
        result = fetch_shelf_sync(GENERIC_SHELF, PRODUCT_ID, product["brand"])

    assert result is not None
    assert fetched[0] == GENERIC_SHELF + "?facet=brand%3AHead%20%26%20Shoulders"
    assert "%5C" not in fetched[0]


def test_price_string_regex_decodes_json_escapes() -> None:
    product = _scrape_html(
        r'<html><h1>Sample Product</h1>'
        r'<script>{"priceString":"44.2 \u00a2\/fl oz"}</script></html>'
    )

    assert product["price"] == "44.2 ¢/fl oz"
