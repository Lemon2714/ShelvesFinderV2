"""Branded vs generic classification of discovered Walmart browse shelves."""

import base64
import json
import urllib.parse

import pytest

from app.tools.shelf_classifier import (
    canonicalize_recovery_dedup_url,
    classify_fetched_shelf,
    classify_shelf,
    extract_facet_brand,
    extract_page_brand_metadata,
    harvest_known_brands,
    recover_generic_shelf_url,
)

BRAND = "Head & Shoulders"

GENERIC_URL = "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_1071969"
PRODUCT_BRAND_URL = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/1085666_123"
COMPETITOR_BARE_URL = "https://www.walmart.com/browse/beauty/shampoo/ogx/1085666_790"

ENCODED_BRAND_URLS = {
    "Leader": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/leader/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6TGVhZGVy"
    ),
    "DHS": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/dhs/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6REhT"
    ),
    "Equate": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/equate/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6RXF1YXRl"
    ),
    "High Supreme": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/high-supreme/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6SGlnaCBTdXByZW1l"
    ),
    "Dove": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/dove/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6RG92ZQieie"
    ),
    "Vichy": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/vichy/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6VmljaHkie"
    ),
}


class TestGenericShelves:
    def test_generic_category_is_not_branded(self) -> None:
        cls = classify_shelf(GENERIC_URL, product_brand=BRAND,
                             title="Dandruff Shampoo - Walmart.com")
        assert cls.is_branded is False

    def test_generic_dandruff_shelf_without_facet_remains_generic(self) -> None:
        url = (
            "https://www.walmart.com/browse/beauty/dandruff-shampoo/"
            "1085666_3147628_5752434"
        )
        assert extract_facet_brand(url) is None
        assert classify_shelf(url, product_brand=BRAND).is_branded is False

    def test_empty_product_brand_does_not_crash(self) -> None:
        cls = classify_shelf(GENERIC_URL, product_brand="", title="")
        assert cls.is_branded is False

    def test_generic_title_containing_in_is_not_branded(self) -> None:
        # A title containing "in" is not evidence that a shelf is branded.
        cls = classify_shelf(
            "https://www.walmart.com/browse/home/storage/all-in-one-organizers/123",
            product_brand=BRAND,
            title="All in One Organizers - Walmart.com",
        )
        assert cls.is_branded is False

    @pytest.mark.parametrize(
        ("url", "title"),
        [
            (
                "https://www.walmart.com/browse/beauty/shampoo/"
                "1085666_3147628_5752434",
                "Shampoo in Hair Care - Walmart.com",
            ),
            (
                "https://www.walmart.com/browse/beauty/hair-styling-products/"
                "1085666_3147628_7768896",
                "Hair Styling Products in Hair Care - Walmart.com",
            ),
            (
                "https://www.walmart.com/browse/premium-beauty/premium-hair-care/"
                "7924299_8655252",
                "Premium Hair Care in Premium Beauty",
            ),
            (
                "https://www.walmart.com/browse/beauty/scalp-scrubs-treatments/"
                "1085666_3147628_8428896_5309236",
                "Scalp Scrubs & Treatments in Hair Treatments",
            ),
        ],
    )
    def test_category_title_pattern_is_not_rejected_pre_fetch(
        self, url: str, title: str
    ) -> None:
        cls = classify_shelf(url, product_brand=BRAND, title=title)
        assert cls.is_branded is False
        assert cls.reason == ""

    def test_brand_substring_in_generic_slug_is_not_branded(self) -> None:
        # Exact-node match only: "dove" ≠ "dove-chocolate-gifts" category.
        cls = classify_shelf(
            "https://www.walmart.com/browse/food/dove-chocolate-gifts/976759_123",
            product_brand="Dove",
        )
        assert cls.is_branded is False


class TestAnalyzedBrandShelves:
    def test_product_brand_final_node_is_rejected_without_title(self) -> None:
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
    def test_harvested_competitor_final_node_is_still_rejected(self) -> None:
        cls = classify_shelf(COMPETITOR_BARE_URL, product_brand=BRAND,
                             known_brands={"OGX"})
        assert cls.is_branded is True
        assert cls.reason == "brand_category_node"
        assert cls.brand == "OGX"

    def test_brand_in_category_title_pattern_is_deferred_to_post_fetch(self) -> None:
        # Title + URL alone cannot distinguish this unknown competitor from a
        # generic category. Its page metadata is authoritative after fetch.
        cls = classify_shelf(COMPETITOR_BARE_URL, product_brand=BRAND,
                             title="OGX in Shampoo - Walmart.com")
        assert cls.is_branded is False
        assert cls.reason == ""


class TestBrandFacetUrls:
    @pytest.mark.parametrize(("brand", "url"), ENCODED_BRAND_URLS.items())
    def test_encoded_path_brand_facets(self, brand: str, url: str) -> None:
        assert extract_facet_brand(url) == brand
        cls = classify_shelf(url, product_brand=BRAND)
        assert cls.is_branded is True
        assert cls.reason == "brand_facet"
        assert cls.brand == brand

    @pytest.mark.parametrize(
        "token",
        [
            "Y29sb3I6R3JlZW4ie",          # color:Green plus noisy suffix
            "Y2F0ZWdvcnk6U2hhbXBvb3Mie",  # category:Shampoos plus noise
        ],
    )
    def test_other_encoded_facets_are_not_brands(self, token: str) -> None:
        url = f"https://www.walmart.com/browse/beauty/shampoo/123/{token}"
        assert extract_facet_brand(url) is None
        assert classify_shelf(url, product_brand=BRAND).is_branded is False

    @pytest.mark.parametrize(
        "token",
        [
            "SGVsbG9Xb3JsZA",            # arbitrary Base64-looking value
            "not*base64",
            "YnJhbmQ6",                  # brand: with no value
            "YnJhbmQ6AA",                # brand: followed only by control data
            "YnJhbmQ6ImJyb2tlbg",        # unsafe value beginning with a quote
            "YnJhbmQ6" + ("Q" * 600),   # implausibly long input
        ],
    )
    def test_malformed_or_unsafe_path_tokens_are_ignored(self, token: str) -> None:
        url = f"https://www.walmart.com/browse/beauty/shampoo/123/{token}"
        assert extract_facet_brand(url) is None

    def test_path_facet_decoding_is_limited_to_walmart_browse_urls(self) -> None:
        token = "YnJhbmQ6TGVhZGVy"
        assert extract_facet_brand(f"https://example.com/browse/x/{token}") is None
        assert extract_facet_brand(f"https://www.walmart.com/search/x/{token}") is None

    @pytest.mark.parametrize("urlsafe", [False, True])
    def test_standard_and_urlsafe_base64_are_supported(self, urlsafe: bool) -> None:
        brand = "X\u00be"
        encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        token = encoder(f"brand:{brand}".encode()).decode().rstrip("=")
        url = f"https://www.walmart.com/browse/beauty/shampoo/123/{token}"
        assert extract_facet_brand(url) == brand

    def test_encoded_path_token_can_pack_multiple_facets(self) -> None:
        token = (
            "YnJhbmQ6SGVhZCAmIFNob3VsZGVyc3x8"
            "Y2F0ZWdvcnk6U2hhbXBvb3Mie"
        )
        url = f"https://www.walmart.com/browse/personal-care/shampoo/123/{token}"
        assert extract_facet_brand(url) == "Head & Shoulders"

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


class TestGenericShelfRecovery:
    LONG_GENERIC_URL = (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/"
        "1085666_3147628_5752434_2927912_4339403"
    )

    @pytest.mark.parametrize("brand", ["Leader", "DHS", "Equate"])
    def test_recovers_encoded_path_brand_facets(self, brand: str) -> None:
        assert recover_generic_shelf_url(
            ENCODED_BRAND_URLS[brand], brand=brand
        ) == self.LONG_GENERIC_URL

    def test_preserves_unrelated_path_and_complete_category_id(self) -> None:
        url = (
            "https://www.walmart.com/browse/beauty/hair-care/"
            "dandruff-shampoo/leader/"
            "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6TGVhZGVy"
        )
        assert recover_generic_shelf_url(url) == (
            "https://www.walmart.com/browse/beauty/hair-care/"
            "dandruff-shampoo/1085666_3147628_5752434_2927912_4339403"
        )

    def test_only_removes_exact_matching_seo_brand_slug(self) -> None:
        url = ENCODED_BRAND_URLS["Leader"].replace("/leader/", "/leader-care/")
        assert recover_generic_shelf_url(url) == (
            "https://www.walmart.com/browse/beauty/dandruff-shampoo/leader-care/"
            "1085666_3147628_5752434_2927912_4339403"
        )

    @pytest.mark.parametrize(
        "suffix",
        [
            "?facet=brand:OGX",
            "?facet=brand%3AHead%20%26%20Shoulders",
            "?brand=Dove",
        ],
    )
    def test_removes_query_brand_facets(self, suffix: str) -> None:
        assert recover_generic_shelf_url(GENERIC_URL + suffix) == GENERIC_URL

    def test_preserves_other_query_parameters_and_facet_components(self) -> None:
        url = (
            GENERIC_URL
            + "?facet=rating%3A4%7C%7Cbrand%3ADove%7C%7Ccolor%3ABlue"
            + "&sort=best_seller"
        )
        recovered = recover_generic_shelf_url(url)
        assert recovered is not None
        parsed = urllib.parse.urlparse(recovered)
        assert urllib.parse.parse_qs(parsed.query) == {
            "facet": ["rating:4||color:Blue"],
            "sort": ["best_seller"],
        }
        assert extract_facet_brand(recovered) is None

    @pytest.mark.parametrize(
        "url",
        [
            PRODUCT_BRAND_URL,
            "https://www.walmart.com/browse/brands/dove/1085666",
            GENERIC_URL,
            "https://example.com/browse/beauty/shampoo/123?brand=Dove",
            "https://www.walmart.com/search?q=shampoo&brand=Dove",
            "https://shop.walmart.com/browse/beauty/shampoo/123?brand=Dove",
            "https://www.walmart.com/browse/beauty/shampoo/123/YnJhbmQ6",
        ],
    )
    def test_unsupported_or_unfaceted_urls_fail_closed(self, url: str) -> None:
        assert recover_generic_shelf_url(url) is None

    def test_conflicting_explicit_brands_are_ambiguous(self) -> None:
        url = GENERIC_URL + "?facet=brand%3ADove%7C%7Cbrand%3AOGX"
        assert recover_generic_shelf_url(url) is None

    def test_query_facet_does_not_convert_brand_category_node(self) -> None:
        url = PRODUCT_BRAND_URL + "?brand=Head%20%26%20Shoulders"
        assert recover_generic_shelf_url(url) is None

    def test_caller_brand_must_match_detected_brand(self) -> None:
        assert recover_generic_shelf_url(
            ENCODED_BRAND_URLS["Leader"], brand="DHS"
        ) is None


class TestRecoveryDedupCanonicalization:
    def test_safe_url_variants_have_one_key(self) -> None:
        category_path = (
            "/browse/beauty/dandruff-shampoo/"
            "1085666_3147628_5752434_2927912_4339403"
        )
        variants = [
            f"https://WWW.WALMART.COM{category_path}/?b=2&a=1#results",
            f"https://walmart.com{category_path}?a=1&b=2",
            f"https://www.walmart.com{category_path}?b=2&a=1#other",
        ]

        assert len({canonicalize_recovery_dedup_url(url) for url in variants}) == 1

    def test_distinct_category_id_paths_keep_distinct_keys(self) -> None:
        first = canonicalize_recovery_dedup_url(
            "https://www.walmart.com/browse/beauty/shampoo/1085666_111"
        )
        second = canonicalize_recovery_dedup_url(
            "https://www.walmart.com/browse/beauty/shampoo/1085666_112"
        )

        assert first != second

    def test_query_names_and_values_are_preserved(self) -> None:
        key = canonicalize_recovery_dedup_url(
            "https://walmart.com/browse/beauty/shampoo/123?sort=best&facet=color%3ABlue"
        )

        assert key is not None
        assert urllib.parse.parse_qsl(urllib.parse.urlsplit(key).query) == [
            ("facet", "color:Blue"),
            ("sort", "best"),
        ]


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

    def test_harvests_encoded_path_facet_brands(self) -> None:
        raw = [
            {"url": GENERIC_URL, "position": 1},
            {"url": ENCODED_BRAND_URLS["Leader"], "position": 2},
            {"url": ENCODED_BRAND_URLS["Dove"], "position": 3},
        ]
        assert harvest_known_brands(raw) == {"Leader", "Dove"}

    def test_empty_batch(self) -> None:
        assert harvest_known_brands([]) == set()
