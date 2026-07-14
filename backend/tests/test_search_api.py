"""Browse URL validation and Serper search filtering."""

from unittest.mock import MagicMock, patch

import pytest

from app.tools.search_api import _is_valid_walmart_browse_url, search_walmart_browse


class TestIsValidWalmartBrowseUrl:
    @pytest.mark.parametrize(
        "url,expected",
        [
            (
                "https://www.walmart.com/browse/clothing/womens-dresses/cocktail-dresses/1234",
                True,
            ),
            ("https://walmart.com/browse/food/nut-butters", True),
            (
                "https://business.walmart.com/browse/clothing/dresses",
                False,
            ),
            (
                "https://www.walmart.com/ip/Htigea-Dress/19307773882",
                False,
            ),
            ("https://www.walmart.com/search?q=dresses", False),
            ("", False),
        ],
    )
    def test_browse_url_filter(self, url: str, expected: bool) -> None:
        assert _is_valid_walmart_browse_url(url) is expected


class TestSearchWalmartBrowse:
    @patch("app.tools.search_api.settings")
    @patch("app.tools.search_api.requests.post")
    def test_filters_non_browse_and_subdomain_links(
        self, mock_post, mock_settings
    ) -> None:
        mock_settings.serper_api_key = "test-key"
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "organic": [
                {
                    "link": "https://www.walmart.com/browse/clothing/dresses/1",
                    "position": 1,
                },
                {
                    "link": "https://www.walmart.com/ip/product/19307773882",
                    "position": 2,
                },
                {
                    "link": "https://business.walmart.com/browse/clothing/dresses/2",
                    "position": 3,
                },
            ]
        }
        mock_post.return_value = mock_response

        results = search_walmart_browse("cocktail dresses")

        assert len(results) == 1
        assert results[0]["url"].endswith("/1")
        assert results[0]["position"] == 1

        call_kwargs = mock_post.call_args
        assert 'site:walmart.com/browse "cocktail dresses"' in call_kwargs[1]["json"]["q"]

    @patch("app.tools.search_api.settings")
    def test_returns_empty_when_no_serper_key(self, mock_settings) -> None:
        mock_settings.serper_api_key = ""
        assert search_walmart_browse("dresses") == []
