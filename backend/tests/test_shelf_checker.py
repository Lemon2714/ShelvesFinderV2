"""Shelf page detection and product ID presence checks."""

import json
from unittest.mock import patch

import pytest

from app.tools.shelf_checker import _is_page_not_found, analyze_placement, fetch_shelf_sync
from tests.conftest import HTIGEA_PRODUCT_ID


def _next_data_html(items: list) -> str:
    """Wrap a list of item dicts in a minimal Walmart-style __NEXT_DATA__ page."""
    payload = json.dumps({"props": {"pageProps": {"items": items}}})
    return (
        '<html><body>'
        f'<script id="__NEXT_DATA__" type="application/json" nonce="">{payload}</script>'
        '</body></html>'
    )


class TestAnalyzePlacement:
    def test_organic_only(self) -> None:
        html = _next_data_html([{"usItemId": "123", "isSponsoredFlag": False, "name": "A"}])
        assert analyze_placement(html, "123") == {"organic": True, "sponsored": False}

    def test_sponsored_only(self) -> None:
        html = _next_data_html([{"usItemId": "123", "isSponsoredFlag": True, "name": "A"}])
        assert analyze_placement(html, "123") == {"organic": False, "sponsored": True}

    def test_sponsored_and_organic(self) -> None:
        html = _next_data_html([
            {"usItemId": "123", "isSponsoredFlag": True, "name": "A ad"},
            {"usItemId": "123", "isSponsoredFlag": False, "name": "A organic"},
            {"usItemId": "999", "isSponsoredFlag": True, "name": "other"},
        ])
        assert analyze_placement(html, "123") == {"organic": True, "sponsored": True}

    def test_not_found(self) -> None:
        html = _next_data_html([{"usItemId": "999", "isSponsoredFlag": True, "name": "other"}])
        assert analyze_placement(html, "123") == {"organic": False, "sponsored": False}

    def test_substring_fallback_without_next_data(self) -> None:
        html = "<html><body>some product 123 here</body></html>"
        assert analyze_placement(html, "123") == {"organic": True, "sponsored": False}


class TestIsPageNotFound:
    @pytest.mark.parametrize(
        "snippet",
        [
            "This page couldn't be found",
            "We can't find the page",
            '"statusCode":404',
            '"errorCode":"PAGE_NOT_FOUND"',
        ],
    )
    def test_detects_walmart_404_markers(self, snippet: str) -> None:
        assert _is_page_not_found(f"<html>{snippet}</html>") is True

    def test_valid_shelf_html_is_not_not_found(self) -> None:
        html = f"<html><div>product-{HTIGEA_PRODUCT_ID}</div></html>"
        assert _is_page_not_found(html) is False

    def test_empty_html_is_not_not_found(self) -> None:
        assert _is_page_not_found("") is False


class TestFetchShelfSync:
    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_product_found_when_id_in_html(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = f"<html>items {HTIGEA_PRODUCT_ID} here</html>"

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        # Product present on page 1 → product True and brand inferred present
        assert result == {"brand": True, "product": True, "page": 1}
        call_url = mock_get.call_args[0][0]
        assert "facet=brand%3A" in call_url or "facet=brand%3AHtigea" in call_url

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_product_missing_when_id_absent_no_brand(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = "<html>other products only</html>"

        # No brand supplied → brand not evaluated (None)
        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "",
        )

        assert result == {"brand": None, "product": False, "page": 0}

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_brand_present_but_product_missing(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        # Brand-filtered page has products (no empty markers) but not our ID
        mock_get.return_value.text = "<html>Htigea brand dresses, other styles</html>"

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result == {"brand": True, "product": False, "page": 0}

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_brand_absent_when_empty_results(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        # Brand facet returns zero items → brand not carried on this shelf
        mock_get.return_value.text = '<html>0 results for this filter</html>'

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result == {"brand": False, "product": False, "page": 0}

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_product_found_on_page_two(self, mock_get, mock_settings) -> None:
        from unittest.mock import MagicMock

        mock_settings.webscraping_api_key = ""

        page1 = MagicMock()
        page1.raise_for_status.return_value = None
        page1.text = "<html>Htigea dresses, other styles on page 1</html>"
        page2 = MagicMock()
        page2.raise_for_status.return_value = None
        page2.text = f"<html>Htigea dresses including {HTIGEA_PRODUCT_ID}</html>"
        mock_get.side_effect = [page1, page2]

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result == {"brand": True, "product": True, "page": 2}
        # Second request should target page 2
        second_url = mock_get.call_args_list[1][0][0]
        assert "page=2" in second_url

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_invalid_page_returns_none(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = "This page couldn't be found"

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/deleted",
            HTIGEA_PRODUCT_ID,
            "",
        )

        assert result is None
