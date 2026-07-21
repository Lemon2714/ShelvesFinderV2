"""URL parsing and slug fallback (no live Walmart fetch)."""

from unittest.mock import patch

import pytest

from app.tools.scraper import _slug_from_url, fetch_product_content
from tests.conftest import HTIGEA_DRESS_URL, HTIGEA_PRODUCT_ID, HTIGEA_SLUG_TITLE


class TestSlugFromUrl:
    def test_htigea_dress_slug(self, htigea_dress_url: str) -> None:
        slug = _slug_from_url(htigea_dress_url)
        assert slug == HTIGEA_SLUG_TITLE

    def test_empty_url_returns_empty(self) -> None:
        assert _slug_from_url("") == ""

    def test_invalid_path_returns_empty(self) -> None:
        assert _slug_from_url("https://www.walmart.com/") == ""


class TestProductIdFromUrl:
    @patch("app.tools.scraper.requests.get")
    @patch("app.tools.scraper.settings")
    def test_extracts_id_before_fetch_fails(
        self,
        mock_settings,
        mock_get,
        htigea_dress_url: str,
    ) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.side_effect = ConnectionError("blocked")

        result = fetch_product_content(htigea_dress_url)

        assert result["id"] == HTIGEA_PRODUCT_ID
        assert result["title"] == HTIGEA_SLUG_TITLE.title()
        assert result["brand"] == ""
        assert result["brand_source"] == "unknown"

    @patch("app.tools.scraper.requests.get")
    @patch("app.tools.scraper.settings")
    def test_robot_or_human_page_uses_empty_title(
        self,
        mock_settings,
        mock_get,
        htigea_dress_url: str,
    ) -> None:
        mock_settings.webscraping_api_key = ""
        mock_response = mock_get.return_value
        mock_response.raise_for_status.return_value = None
        mock_response.text = "<html><title>Robot or human?</title></html>"

        result = fetch_product_content(htigea_dress_url)

        assert result["id"] == HTIGEA_PRODUCT_ID
