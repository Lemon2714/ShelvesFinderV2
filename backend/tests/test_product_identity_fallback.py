"""Regression coverage for anti-bot product identity recovery."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.session_state import ProductInfo, SessionState, ShelfResult
from app.tools.product_identity import normalize_product_identity
from app.tools.scraper import fetch_product_content
from app.tools.shelf_checker import check_shelf_visibility, fetch_shelf_sync
from app.tools.shelf_classifier import classify_shelf


PRODUCT_URL = (
    "https://www.walmart.com/ip/Head-and-Shoulders-Dandruff-Shampoo-"
    "Classic-Clean-28-2-fl-Oz/408353826?classType=REGULAR&athbdg=L1600"
)
PRODUCT_ID = "408353826"
TITLE = "Head And Shoulders Dandruff Shampoo Classic Clean 28 2 Fl Oz"
BRAND = "Head & Shoulders"
GENERIC_SHELF = "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_3147628"
OWN_BRAND_SHELF = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/1085666_123"


def _ranked_html(items: list[dict]) -> str:
    payload = {"props": {"pageProps": {"searchResult": {"items": items}}}}
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def test_webscrapingapi_failure_then_captcha_recovers_complete_identity() -> None:
    direct_response = MagicMock()
    direct_response.raise_for_status.return_value = None
    direct_response.text = "<html><h1>Robot or human?</h1></html>"

    with patch("app.tools.scraper.settings") as mock_settings, patch(
        "app.tools.scraper.requests.get",
        side_effect=[ConnectionError("provider failed"), direct_response],
    ) as mock_get:
        mock_settings.webscraping_api_key = "configured"
        result = fetch_product_content(PRODUCT_URL)

    assert mock_get.call_count == 2
    assert result["title"] == TITLE
    assert result["brand"] == BRAND
    assert result["id"] == PRODUCT_ID
    assert result["brand_source"] == "inferred_url_title"
    assert result["brand_authoritative"] is False
    assert result["brand_confidence"] == pytest.approx(0.9)


def test_normalization_preserves_inferred_brand_provenance() -> None:
    once = normalize_product_identity(PRODUCT_URL, {})
    twice = normalize_product_identity(PRODUCT_URL, once)

    assert twice["brand"] == "Head & Shoulders"
    assert twice["brand_source"] == "inferred_url_title"
    assert twice["brand_confidence"] == pytest.approx(0.9)
    assert twice["brand_authoritative"] is False


@pytest.mark.asyncio
async def test_v2_product_state_uses_shared_recovered_identity() -> None:
    from app.services import workflow_v2

    captured = {}

    async def fake_loop(state):
        captured["product"] = state.product
        yield {"event": "complete", "data": state.to_final_report()}

    with patch(
        "app.tools.scraper.fetch_product_content",
        return_value={"title": "", "brand": "", "id": PRODUCT_ID},
    ), patch.object(
        workflow_v2, "get_initial_keywords", return_value=(["dandruff shampoo"], 0.0)
    ), patch.object(
        workflow_v2, "run_react_loop", side_effect=fake_loop
    ), patch.object(
        workflow_v2, "_persist_results", new=AsyncMock()
    ):
        events = [event async for event in workflow_v2.run_v2_workflow(PRODUCT_URL)]

    product = captured["product"]
    assert product.title == TITLE
    assert product.brand == BRAND
    assert product.product_id == PRODUCT_ID
    assert product.brand_source == "inferred_url_title"
    assert product.brand_authoritative is False
    assert events[-1]["event"] == "complete"


def test_recovered_brand_builds_real_filtered_shelf_url() -> None:
    product = normalize_product_identity(PRODUCT_URL, {})
    fetched_urls = []
    # Well-stocked shelf (>= the sparse-shelf minimum) carrying the product.
    filtered_html = _ranked_html([
        {"usItemId": PRODUCT_ID, "isSponsoredFlag": False},
        {"usItemId": "other-1", "isSponsoredFlag": False},
        {"usItemId": "other-2", "isSponsoredFlag": False},
    ])

    def fake_fetch(url: str) -> str:
        fetched_urls.append(url)
        return filtered_html

    with patch("app.tools.shelf_checker._fetch_html", side_effect=fake_fetch):
        result = fetch_shelf_sync(
            GENERIC_SHELF, PRODUCT_ID, product["brand"]
        )

    assert result is not None
    assert fetched_urls[0] == (
        GENERIC_SHELF + "?facet=brand%3AHead%20%26%20Shoulders"
    )
    assert result["brand_url"] == fetched_urls[0]
    assert result["discoverability_available"] is True


def test_recovered_brand_excludes_own_brand_category_when_disabled() -> None:
    product = normalize_product_identity(PRODUCT_URL, {})

    classification = classify_shelf(
        OWN_BRAND_SHELF,
        product_brand=product["brand"],
    )

    assert classification.is_branded is True
    assert classification.brand == BRAND
    assert classification.reason == "brand_category_node"


@pytest.mark.asyncio
async def test_unknown_brand_excludes_discoverability_from_score() -> None:
    ambiguous_url = (
        "https://www.walmart.com/ip/Classic-Clean-Moisturizing-Shampoo-28-oz/999"
    )
    product = normalize_product_identity(ambiguous_url, {})
    assert product["brand"] == ""

    base_html = _ranked_html([
        {"usItemId": "999", "isSponsoredFlag": False},
        {"usItemId": "other-1", "isSponsoredFlag": False},
        {"usItemId": "other-2", "isSponsoredFlag": False},
    ])
    with patch("app.tools.shelf_checker._fetch_html", return_value=base_html):
        stats = await check_shelf_visibility(
            [{"url": GENERIC_SHELF}], "999", product["brand"]
        )

    assert stats["visible"] == 1
    assert stats["found"] == 0
    assert stats["missing"] == 0
    assert stats["total"] == 0
    assert stats["loaded_total"] == 1
    assert stats["discoverability_unavailable"] == 1
    assert stats["score"] is None
    assert stats["details"][GENERIC_SHELF]["discoverability"] is None


def test_structured_brand_remains_authoritative() -> None:
    raw = {
        "title": "Head & Shoulders Dandruff Shampoo",
        "brand": "Head & Shoulders",
        "brand_source": "structured_next_data",
        "id": PRODUCT_ID,
    }

    result = normalize_product_identity(PRODUCT_URL, raw)

    assert result["brand"] == BRAND
    assert result["brand_source"] == "structured_next_data"
    assert result["brand_authoritative"] is True
    assert result["brand_confidence"] == 1.0


def test_scraper_marks_existing_next_data_brand_authoritative() -> None:
    payload = {
        "props": {"pageProps": {"initialData": {"data": {"product": {
            "name": "Head & Shoulders Dandruff Shampoo",
            "brand": BRAND,
            "usItemId": PRODUCT_ID,
        }}}}}
    }
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.text = (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )

    with patch("app.tools.scraper.settings") as mock_settings, patch(
        "app.tools.scraper.requests.get", return_value=response
    ):
        mock_settings.webscraping_api_key = ""
        result = fetch_product_content(PRODUCT_URL)

    assert result["brand"] == BRAND
    assert result["brand_source"] == "structured_next_data"
    assert result["brand_authoritative"] is True
    assert result["brand_confidence"] == 1.0


@pytest.mark.parametrize(
    "url",
    [
        "https://www.walmart.com/ip/Acme-Classic-Shampoo-28-oz/111",
        "https://www.walmart.com/ip/Essential-Hawaiian-Sliced-Sourdough-Loaf/222",
        "https://www.walmart.com/ip/Classic-Clean-Moisturizing-Shampoo/333",
    ],
)
def test_ambiguous_titles_do_not_fabricate_first_word_brand(url: str) -> None:
    result = normalize_product_identity(url, {})

    assert result["brand"] == ""
    assert result["brand_source"] == "unknown"
    assert result["brand_confidence"] == 0.0
    assert result["brand_authoritative"] is False


def test_final_report_surfaces_identity_provenance_and_unknown_measurement() -> None:
    state = SessionState()
    state.product = ProductInfo(
        title="Classic Clean Moisturizing Shampoo",
        brand="",
        brand_source="unknown",
        product_id="999",
    )
    state.unavailable_pages.append(ShelfResult(
        page_url=GENERIC_SHELF,
        product_found=None,
        visibility=True,
        discoverability=None,
    ))

    report = state.to_final_report()

    assert report["product_brand"] == ""
    assert report["product_brand_source"] == "unknown"
    assert report["shelf_results"][0]["discoverability"] is None
    assert report["shelf_results"][0]["discoverability_available"] is False
    assert report["shelf_stats"]["score"] is None
    assert report["shelf_stats"]["unavailable"] == 1
