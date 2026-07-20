"""Branded vs generic classification of discovered Walmart browse shelves."""

import json

from app.tools.shelf_classifier import (
    classify_fetched_shelf,
    classify_shelf,
    extract_facet_brand,
    extract_page_brand_metadata,
    harvest_known_brands,
)

BRAND = "Head & Shoulders"

GENERIC_URL = "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_1071969"
PRODUCT_BRAND_URL = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/1085666_123"
COMPETITOR_BARE_URL = "https://www.walmart.com/browse/beauty/shampoo/ogx/1085666_790"


class TestGenericShelves:
    def test_generic_category_is_not_branded(self) -> None:
        cls = classify_shelf(GENERIC_URL, product_brand=BRAND,
                             title="Dandruff Shampoo - Walmart.com")
        assert cls.is_branded is False

    def test_empty_product_brand_does_not_crash(self) -> None:
        cls = classify_shelf(GENERIC_URL, product_brand="", title="")
        assert cls.is_branded is False

    def test_generic_title_containing_in_is_not_branded(self) -> None:
        # "All in One Organizers" must not trip the "<Brand> in <Category>" rule.
        cls = classify_shelf(
            "https://www.walmart.com/browse/home/storage/all-in-one-organizers/123",
            product_brand=BRAND,
            title="All in One Organizers - Walmart.com",
        )
        assert cls.is_branded is False

    def test_brand_substring_in_generic_slug_is_not_branded(self) -> None:
        # Exact-node match only: "dove" ≠ "dove-chocolate-gifts" category.
        cls = classify_shelf(
            "https://www.walmart.com/browse/food/chocolate-covered-fruit/976759_123",
            product_brand="Dove",
        )
        assert cls.is_branded is False


class TestAnalyzedBrandShelves:
    def test_final_category_node_is_product_brand(self) -> None:
        cls = classify_shelf(PRODUCT_BRAND_URL, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_category_node"

    def test_case_insensitive_brand_match(self) -> None:
        cls = classify_shelf(PRODUCT_BRAND_URL, product_brand="HEAD & SHOULDERS")
        assert cls.is_branded is True

    def test_url_encoded_brand_segment(self) -> None:
        url = "https://www.walmart.com/browse/beauty/shampoo/Head%20%26%20Shoulders/123"
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True

    def test_brand_in_breadcrumb_title_with_opaque_url(self) -> None:
        cls = classify_shelf(
            "https://www.walmart.com/browse/1085666_9999",
            product_brand=BRAND,
            title="Beauty > Shampoo > Head & Shoulders | Walmart.com",
        )
        assert cls.is_branded is True
        assert cls.reason == "brand_breadcrumb_node"


class TestCompetitorBrandShelves:
    def test_known_competitor_final_node(self) -> None:
        cls = classify_shelf(COMPETITOR_BARE_URL, product_brand=BRAND,
                             known_brands={"OGX"})
        assert cls.is_branded is True
        assert cls.brand == "OGX"

    def test_brand_in_category_title_pattern(self) -> None:
        # Unknown competitor detected because the title leads with the same
        # name as the URL's final category node ("OGX in Shampoo").
        cls = classify_shelf(COMPETITOR_BARE_URL, product_brand=BRAND,
                             title="OGX in Shampoo - Walmart.com")
        assert cls.is_branded is True
        assert cls.reason == "brand_in_category_title"


class TestBrandFacetUrls:
    def test_raw_facet_param(self) -> None:
        url = GENERIC_URL + "?facet=brand:OGX"
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_facet"
        assert cls.brand == "OGX"

    def test_encoded_facet_param(self) -> None:
        url = GENERIC_URL + "?facet=brand%3AHead%20%26%20Shoulders"
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.brand == "Head & Shoulders"

    def test_multi_value_facet(self) -> None:
        url = GENERIC_URL + "?facet=rating%3A4%7C%7Cbrand%3ADove"
        assert extract_facet_brand(url) == "Dove"

    def test_direct_brand_query_param(self) -> None:
        url = GENERIC_URL + "?brand=Dove"
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.brand == "Dove"

    def test_no_facet_returns_none(self) -> None:
        assert extract_facet_brand(GENERIC_URL) is None


class TestBrandPathUrls:
    def test_brands_directory_path(self) -> None:
        url = "https://www.walmart.com/browse/brands/dove/1085666"
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_path"


def next_data_html(payload: dict) -> str:
    return (
        '<script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload)
        + "</script>"
    )


def page_payload(
    breadcrumbs,
    brand_values=(),
    selected=(),
    page_type="BrowsePage",
) -> dict:
    """Deterministic __NEXT_DATA__ fixture shaped like a Walmart browse page."""
    return {"props": {"pageProps": {"initialData": {"searchResult": {
        "pageType": page_type,
        "breadCrumb": [{"name": name, "url": "#"} for name in breadcrumbs],
        "facets": [{
            "name": "Brand",
            "values": [
                {"name": value, "checked": value in selected}
                for value in brand_values
            ],
        }],
    }}}}}


GENERIC_PAGE_HTML = next_data_html(page_payload(
    ["Beauty", "Dandruff Shampoo"],
    brand_values=["Head & Shoulders", "OGX", "Selsun Blue"],
))


class TestFetchedShelfClassification:
    """Post-fetch verification from the page's own __NEXT_DATA__."""

    def test_generic_page_not_branded_and_brands_harvested(self) -> None:
        cls, harvested = classify_fetched_shelf(
            GENERIC_PAGE_HTML, product_brand=BRAND
        )
        assert cls.is_branded is False
        assert harvested == {"Head & Shoulders", "OGX", "Selsun Blue"}

    def test_unknown_competitor_detected_via_own_brand_facet(self) -> None:
        # The lexical gap: "Selsun Blue" is unknown beforehand, but the page's
        # breadcrumb ends in a name its own brand facet lists.
        html = next_data_html(page_payload(
            ["Beauty", "Shampoo", "Selsun Blue"],
            brand_values=["Head & Shoulders", "OGX", "Selsun Blue"],
        ))
        cls, harvested = classify_fetched_shelf(html, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "breadcrumb_is_page_brand"
        assert cls.brand == "Selsun Blue"
        assert "OGX" in harvested

    def test_product_brand_breadcrumb_without_facet_list(self) -> None:
        html = next_data_html(page_payload(
            ["Beauty", "Shampoo", "Head & Shoulders"], brand_values=[]
        ))
        cls, _ = classify_fetched_shelf(html, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_breadcrumb_node"

    def test_known_competitor_breadcrumb_without_facet_list(self) -> None:
        html = next_data_html(page_payload(
            ["Beauty", "Shampoo", "OGX"], brand_values=[]
        ))
        cls, _ = classify_fetched_shelf(
            html, product_brand=BRAND, known_brands={"OGX"}
        )
        assert cls.is_branded is True
        assert cls.brand == "OGX"

    def test_brand_page_type_marker(self) -> None:
        html = next_data_html(page_payload(
            ["Beauty", "Shampoo"], page_type="BrandPage"
        ))
        cls, _ = classify_fetched_shelf(html, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_page_type"

    def test_preselected_brand_facet_on_base_page(self) -> None:
        html = next_data_html(page_payload(
            ["Beauty", "Shampoo"],
            brand_values=["OGX", "Dove"],
            selected=["OGX"],
        ))
        cls, _ = classify_fetched_shelf(html, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "preselected_brand_facet"
        assert cls.brand == "OGX"

    def test_html_without_next_data_is_safe(self) -> None:
        cls, harvested = classify_fetched_shelf(
            "<html><body>plain page</body></html>", product_brand=BRAND
        )
        assert cls.is_branded is False
        assert cls.reason == "no_page_metadata"
        assert harvested == set()

    def test_malformed_next_data_is_safe(self) -> None:
        html = ('<script id="__NEXT_DATA__" type="application/json">'
                "{not valid json</script>")
        cls, harvested = classify_fetched_shelf(html, product_brand=BRAND)
        assert cls.is_branded is False
        assert harvested == set()

    def test_empty_html_is_safe(self) -> None:
        cls, harvested = classify_fetched_shelf("", product_brand="")
        assert cls.is_branded is False
        assert harvested == set()

    def test_metadata_extraction_shape(self) -> None:
        meta = extract_page_brand_metadata(GENERIC_PAGE_HTML)
        assert meta is not None
        assert meta.breadcrumb_nodes == ["Beauty", "Dandruff Shampoo"]
        assert meta.brand_facet_values == ["Head & Shoulders", "OGX", "Selsun Blue"]
        assert meta.selected_brands == []
        assert meta.page_types == ["BrowsePage"]


class TestHarvestKnownBrands:
    def test_harvests_facet_brands_from_batch(self) -> None:
        raw = [
            {"url": GENERIC_URL, "position": 1},
            {"url": GENERIC_URL + "?facet=brand%3AOGX", "position": 2},
            {"url": GENERIC_URL + "?brand=Dove", "position": 3},
        ]
        assert harvest_known_brands(raw) == {"OGX", "Dove"}

    def test_empty_batch(self) -> None:
        assert harvest_known_brands([]) == set()
