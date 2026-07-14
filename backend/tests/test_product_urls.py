"""
Parametrized URL parsing tests for all example Walmart product URLs.

No live network — validates ID extraction and slug fallback only.
"""

from unittest.mock import patch

import pytest

from app.tools.scraper import _slug_from_url, fetch_product_content
from tests.conftest import (
    BROCCOLI_PRODUCT_ID,
    BROCCOLI_SLUG_TITLE,
    BROCCOLI_URL,
    HTIGEA_PRODUCT_ID,
    HTIGEA_SLUG_TITLE,
    HTIGEA_DRESS_URL,
    SOURDOUGH_PRODUCT_ID,
    SOURDOUGH_SLUG_TITLE,
    SOURDOUGH_URL,
)


class TestExampleProductSlugs:
    @pytest.mark.parametrize(
        "url,expected_slug",
        [
            (HTIGEA_DRESS_URL, HTIGEA_SLUG_TITLE),
            (SOURDOUGH_URL, SOURDOUGH_SLUG_TITLE),
            (BROCCOLI_URL, BROCCOLI_SLUG_TITLE),
        ],
    )
    def test_slug_from_url_matches_expected(self, url: str, expected_slug: str) -> None:
        assert _slug_from_url(url) == expected_slug


class TestExampleProductIds:
    @pytest.mark.parametrize(
        "url,expected_id",
        [
            (HTIGEA_DRESS_URL, HTIGEA_PRODUCT_ID),
            (SOURDOUGH_URL, SOURDOUGH_PRODUCT_ID),
            (BROCCOLI_URL, BROCCOLI_PRODUCT_ID),
        ],
    )
    @patch("app.tools.scraper.requests.get")
    @patch("app.tools.scraper.settings")
    def test_id_extracted_when_fetch_blocked(
        self, mock_settings, mock_get, url: str, expected_id: str
    ) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.side_effect = ConnectionError("Robot or human?")

        result = fetch_product_content(url)

        assert result["id"] == expected_id


class TestExampleProductParametrized:
    def test_slug_and_id(self, example_product) -> None:
        url, product_id, slug_title = example_product
        assert _slug_from_url(url) == slug_title

        with patch("app.tools.scraper.settings") as mock_settings, patch(
            "app.tools.scraper.requests.get"
        ) as mock_get:
            mock_settings.webscraping_api_key = ""
            mock_get.side_effect = ConnectionError("blocked")
            result = fetch_product_content(url)
            assert result["id"] == product_id
