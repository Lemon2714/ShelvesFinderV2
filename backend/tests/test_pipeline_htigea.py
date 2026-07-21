"""
End-to-end pipeline test for the Htigea dress product URL (mocked externals).

Validates URL → ID/slug → keywords/search/evaluate/shelf flow without live APIs.
"""

import json
from unittest.mock import patch

import pytest

from app.agents.evaluation_agent import evaluate_and_rank
from app.agents.search_agent import find_browse_pages
from app.tools.scraper import _slug_from_url, fetch_product_content
from app.tools.shelf_checker import fetch_shelf_sync
from tests.conftest import HTIGEA_DRESS_URL, HTIGEA_PRODUCT_ID, HTIGEA_SLUG_TITLE


@pytest.mark.integration
class TestExamplePipelineMocked:
    """Full v1-style path with network calls mocked."""

    @patch("app.tools.scraper.requests.get")
    @patch("app.tools.scraper.settings")
    @pytest.mark.parametrize(
        "url,product_id,slug_title",
        [
            (HTIGEA_DRESS_URL, HTIGEA_PRODUCT_ID, HTIGEA_SLUG_TITLE),
        ],
    )
    def test_scrape_extracts_id_and_slug_on_fetch_failure(
        self, mock_settings, mock_get, url, product_id, slug_title
    ) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.side_effect = ConnectionError("Robot or human?")

        data = fetch_product_content(url)

        assert data["id"] == product_id
        assert _slug_from_url(url) == slug_title

    @patch("app.agents.search_agent.search_walmart_browse")
    @patch("app.agents.evaluation_agent.score_relevance")
    @patch("app.tools.shelf_checker.requests.get")
    @patch("app.tools.shelf_checker.settings")
    def test_search_evaluate_shelf_check_flow(
        self,
        mock_shelf_settings,
        mock_shelf_get,
        mock_score,
        mock_search,
    ) -> None:
        browse_url = (
            "https://www.walmart.com/browse/clothing/"
            "womens-dresses/cocktail-dresses/1234"
        )
        mock_search.return_value = [
            {"url": browse_url, "position": 1},
        ]
        mock_score.return_value = (
            [("clothing womens dresses cocktail dresses 1234", 0.85)],
            0.0,
        )
        mock_shelf_settings.webscraping_api_key = ""
        mock_shelf_get.return_value.raise_for_status.return_value = None
        payload = json.dumps({
            "props": {
                "pageProps": {
                    "initialData": {
                        "searchResult": {
                            # Well-stocked base shelf (>= the sparse-shelf
                            # minimum) that does not carry our product.
                            "items": [
                                {
                                    "usItemId": "different-product",
                                    "isSponsoredFlag": False,
                                },
                                {
                                    "usItemId": "different-product-2",
                                    "isSponsoredFlag": False,
                                },
                                {
                                    "usItemId": "different-product-3",
                                    "isSponsoredFlag": False,
                                },
                            ]
                        }
                    }
                }
            }
        })
        mock_shelf_get.return_value.text = (
            '<html><script id="__NEXT_DATA__" type="application/json">'
            f"{payload}</script></html>"
        )

        pages = find_browse_pages(["cocktail dresses", "womens dresses"])
        assert len(pages) == 1

        product_info = {
            "title": "Htigea Wedding Guest Midi Dress",
            "description": "Formal cocktail dress.",
        }
        ranked, confidence, _ = evaluate_and_rank(product_info, pages)
        assert ranked[0]["url"] == browse_url
        assert confidence > 0

        on_shelf = fetch_shelf_sync(browse_url, HTIGEA_PRODUCT_ID, "Htigea")
        assert on_shelf["product"] is False  # product missing from shelf
