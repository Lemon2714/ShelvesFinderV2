"""Shelf page detection and product ID presence checks."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.tools.shelf_checker import (
    _is_page_not_found,
    analyze_placement,
    check_shelf_visibility,
    classify_placements,
    fetch_shelf_sync,
)
from tests.conftest import HTIGEA_PRODUCT_ID


def _next_data_html(items: list) -> str:
    """Wrap a list of item dicts in a minimal Walmart-style __NEXT_DATA__ page."""
    payload = json.dumps({"props": {"pageProps": {"items": items}}})
    return (
        '<html><body>'
        f'<script id="__NEXT_DATA__" type="application/json" nonce="">{payload}</script>'
        '</body></html>'
    )


def _ranked_next_data_html(items: list) -> str:
    """Wrap items in Walmart's ordered Page 1 search-result structure."""
    payload = json.dumps({
        "props": {
            "pageProps": {
                "initialData": {
                    "searchResult": {
                        "itemStacks": [{"items": items}],
                    }
                }
            }
        }
    })
    return (
        '<html><body>'
        f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
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


class TestClassifyPlacements:
    def test_same_product_has_separate_organic_and_sponsored_impressions(self) -> None:
        html = _next_data_html([
            {"usItemId": "123", "isSponsoredFlag": True, "name": "A ad"},
            {"usItemId": "123", "isSponsoredFlag": False, "name": "A natural"},
        ])

        placements = classify_placements(
            html, "123", visibility=True, discoverability=True
        )

        assert len(placements) == 2
        assert placements[0]["sponsored"] is True
        assert placements[0]["organic"] is False
        assert placements[0]["discoverability"] is False
        assert placements[1]["sponsored"] is False
        assert placements[1]["organic"] is True
        assert placements[1]["visibility"] is True
        assert placements[1]["discoverability"] is True

    def test_absolute_ranks_include_other_products_and_first_occurrence_wins(self) -> None:
        html = _ranked_next_data_html([
            {"usItemId": "other-1", "isSponsoredFlag": False},
            {"usItemId": "123", "isSponsoredFlag": True},
            {"usItemId": "other-2", "isSponsoredFlag": True},
            {"usItemId": "other-3", "isSponsoredFlag": False},
            {"usItemId": "123", "isSponsoredFlag": False},
        ])

        placements = classify_placements(
            html, "123", visibility=True, discoverability=True
        )

        assert [p["placement_index"] for p in placements] == [1, 2]
        assert [p["placement_rank"] for p in placements] == [2, 5]
        assert placements[0]["sponsored"] is True
        assert placements[1]["organic"] is True

    def test_unranked_structured_data_does_not_invent_an_exact_rank(self) -> None:
        html = _next_data_html([
            {"usItemId": "123", "isSponsoredFlag": False},
        ])

        placements = classify_placements(
            html, "123", visibility=True, discoverability=True
        )

        assert placements[0]["placement_rank"] is None
        assert placements[0]["classification_source"] == "structured_unranked"


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
    def test_preserves_both_placement_types_on_one_page(
        self, mock_get, mock_settings
    ) -> None:
        mock_settings.webscraping_api_key = ""
        brand_page = MagicMock()
        brand_page.raise_for_status.return_value = None
        brand_page.text = f"<html>brand shelf {HTIGEA_PRODUCT_ID}</html>"
        base_page = MagicMock()
        base_page.raise_for_status.return_value = None
        base_page.text = _next_data_html([
            {"usItemId": HTIGEA_PRODUCT_ID, "isSponsoredFlag": True},
            {"usItemId": HTIGEA_PRODUCT_ID, "isSponsoredFlag": False},
        ])
        mock_get.side_effect = [brand_page, base_page]

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result["organic"] is True
        assert result["sponsored"] is True
        assert len(result["placements"]) == 2
        assert sum(p["organic"] for p in result["placements"]) == 1
        assert sum(p["sponsored"] for p in result["placements"]) == 1

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_returns_first_absolute_page_one_placement_rank(
        self, mock_get, mock_settings
    ) -> None:
        mock_settings.webscraping_api_key = ""
        brand_page = MagicMock()
        brand_page.raise_for_status.return_value = None
        brand_page.text = f"<html>brand shelf {HTIGEA_PRODUCT_ID}</html>"
        base_page = MagicMock()
        base_page.raise_for_status.return_value = None
        base_page.text = _ranked_next_data_html([
            {"usItemId": "other-1", "isSponsoredFlag": False},
            {"usItemId": "other-2", "isSponsoredFlag": True},
            {"usItemId": HTIGEA_PRODUCT_ID, "isSponsoredFlag": True},
            {"usItemId": "other-3", "isSponsoredFlag": False},
            {"usItemId": HTIGEA_PRODUCT_ID, "isSponsoredFlag": False},
        ])
        mock_get.side_effect = [brand_page, base_page]

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result["visibility"] is True
        assert result["placement_rank"] == 3
        assert [p["placement_rank"] for p in result["placements"]] == [3, 5]

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_visible_and_discoverable_is_organic(self, mock_get, mock_settings) -> None:
        # Product ID present on both the base shelf and the brand-filtered shelf.
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = f"<html>items {HTIGEA_PRODUCT_ID} here</html>"

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        # Visible on the base shelf AND discoverable on the brand shelf → organic.
        assert result["visibility"] is True
        assert result["discoverability"] is True
        assert result["organic"] is True
        # Legacy fields preserved for the v2 orchestrator / email report.
        assert result["product"] is True
        assert result["brand"] is True
        assert result["page"] == 1
        # The brand facet must be applied to the discoverability fetch.
        fetched_urls = [c[0][0] for c in mock_get.call_args_list]
        assert any("facet=brand%3A" in u for u in fetched_urls)

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_missing_everywhere_no_brand(self, mock_get, mock_settings) -> None:
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = "<html>other products only</html>"

        # No brand supplied → brand filter can't apply; discoverability == visibility.
        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "",
        )

        assert result["visibility"] is False
        assert result["discoverability"] is False   # falls back to visibility
        assert result["organic"] is False
        assert result["brand"] is None
        assert result["product"] is False
        assert result["page"] == 0

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_no_brand_discoverability_equals_visibility(self, mock_get, mock_settings) -> None:
        # Product present on the (base == brand-less) shelf: visibility True and
        # discoverability must mirror it exactly when no brand is supplied.
        mock_settings.webscraping_api_key = ""
        mock_get.return_value.raise_for_status.return_value = None
        mock_get.return_value.text = f"<html>found {HTIGEA_PRODUCT_ID}</html>"

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "",
        )

        assert result["visibility"] is True
        assert result["discoverability"] == result["visibility"]
        assert result["organic"] is True

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

        assert result["visibility"] is False
        assert result["discoverability"] is False
        assert result["organic"] is False
        assert result["brand"] is True
        assert result["product"] is False
        assert result["page"] == 0

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

        assert result["discoverability"] is False
        assert result["organic"] is False
        assert result["brand"] is False
        assert result["product"] is False
        assert result["page"] == 0

    @patch("app.tools.shelf_checker.settings")
    @patch("app.tools.shelf_checker.requests.get")
    def test_visible_ad_but_not_discoverable(self, mock_get, mock_settings) -> None:
        from unittest.mock import MagicMock

        # Base shelf carries the product (an ad placement), but the brand-filtered
        # shelf does not → visible but not discoverable → NOT organic.
        mock_settings.webscraping_api_key = ""

        # Fetch order: brand page 1, (pagination pages 2..3), then the base shelf.
        brand_page = MagicMock()
        brand_page.raise_for_status.return_value = None
        brand_page.text = "<html>Htigea brand dresses, other styles</html>"
        base_page = MagicMock()
        base_page.raise_for_status.return_value = None
        base_page.text = f"<html>sponsored {HTIGEA_PRODUCT_ID} appears here</html>"
        # brand p1, brand p2, brand p3, base
        mock_get.side_effect = [brand_page, brand_page, brand_page, base_page]

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        assert result["visibility"] is True
        assert result["discoverability"] is False
        assert result["organic"] is False

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
        base = MagicMock()
        base.raise_for_status.return_value = None
        base.text = f"<html>base shelf with {HTIGEA_PRODUCT_ID}</html>"
        # brand p1 (miss), brand p2 (hit → stop), then the base-shelf fetch
        mock_get.side_effect = [page1, page2, base]

        result = fetch_shelf_sync(
            "https://www.walmart.com/browse/clothing/dresses/1",
            HTIGEA_PRODUCT_ID,
            "Htigea",
        )

        # Legacy pagination still records page 2; discoverability is page-1 only.
        assert result["product"] is True
        assert result["page"] == 2
        assert result["discoverability"] is False   # not on page 1 of brand shelf
        assert result["visibility"] is True          # present on the base shelf
        assert result["organic"] is False
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


class TestCheckShelfVisibility:
    """Aggregation of per-shelf signals, with the per-shelf fetch stubbed."""

    async def test_empty_product_id_returns_all_zeros(self) -> None:
        pages = [{"url": "https://www.walmart.com/browse/a/1"}]
        stats = await check_shelf_visibility(pages, "", "Brand")
        assert stats == {
            "found": 0, "missing": 0, "invalid": 0, "total": 0, "score": 0.0,
            "visible": 0, "discoverable": 0, "placements": 0,
            "organic": 0, "sponsored": 0, "organic_pages": 0,
            "sponsored_pages": 0, "details": {},
        }

    async def test_no_pages_returns_all_zeros(self) -> None:
        stats = await check_shelf_visibility([], "123", "Brand")
        assert stats["total"] == 0
        assert stats["details"] == {}

    async def test_aggregates_visible_discoverable_organic(self) -> None:
        # Three valid shelves + one invalid (None) page.
        canned = {
            "https://www.walmart.com/browse/a/1": {
                "visibility": True, "discoverability": True,
                "organic": True, "sponsored": True,
                "placements": [
                    {"visibility": True, "discoverability": False,
                     "organic": False, "sponsored": True},
                    {"visibility": True, "discoverability": True,
                     "organic": True, "sponsored": False},
                ],
                "brand": True, "product": True, "page": 1,
            },
            "https://www.walmart.com/browse/b/2": {
                # Visible via ad, but not discoverable → not organic.
                "visibility": True, "discoverability": False,
                "organic": False, "sponsored": True,
                "placements": [
                    {"visibility": True, "discoverability": False,
                     "organic": False, "sponsored": True},
                ],
                "brand": True, "product": False, "page": 0,
            },
            "https://www.walmart.com/browse/c/3": {
                # Neither visible nor discoverable.
                "visibility": False, "discoverability": False,
                "organic": False, "sponsored": False, "placements": [],
                "brand": False, "product": False, "page": 0,
            },
            "https://www.walmart.com/browse/d/4": None,   # invalid page
        }

        def fake_fetch(url, product_id, brand, *a, **k):
            return canned[url]

        pages = [{"url": u} for u in canned]
        with patch("app.tools.shelf_checker.fetch_shelf_sync", side_effect=fake_fetch):
            stats = await check_shelf_visibility(pages, "123", "Brand")

        assert stats["invalid"] == 1
        assert stats["total"] == 3            # invalid excluded
        assert stats["visible"] == 2
        assert stats["discoverable"] == 1
        assert stats["placements"] == 3
        assert stats["organic"] == 1
        assert stats["sponsored"] == 2
        assert stats["organic_pages"] == 1
        assert stats["sponsored_pages"] == 2
        # found/missing/score are Discoverability-driven (Discoverability Dashboard).
        assert stats["found"] == 1
        assert stats["missing"] == 2
        assert stats["score"] == round(1 / 3 * 100, 1)
        # details carries the per-shelf signal dicts (and the None for invalid).
        assert stats["details"]["https://www.walmart.com/browse/a/1"]["organic"] is True
        assert stats["details"]["https://www.walmart.com/browse/d/4"] is None
