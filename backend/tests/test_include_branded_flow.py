"""
End-to-end behavior of the "Include Branded Results" setting.

Covers keyword generation/queueing, search-time branded-shelf rejection,
report/persistence enforcement, brand-filtered URL construction, and the
frontend-to-backend flow of the setting — all with deterministic mocks.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents.orchestrator import _call_search, _call_check_shelf
from app.models.session_state import BrowsePage, ProductInfo, SessionState, ShelfResult

BRAND = "Head & Shoulders"
PRODUCT_ID = "12345"

GENERIC_URL = "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_1071969"
PRODUCT_BRAND_URL = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/1085666_123"
COMPETITOR_FACET_URL = (
    "https://www.walmart.com/browse/beauty/shampoo/1085666_456?facet=brand%3AOGX"
)
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
    "Dove": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/dove/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6RG92ZQieie"
    ),
    "Vichy": (
        "https://www.walmart.com/browse/beauty/dandruff-shampoo/vichy/"
        "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6VmljaHkie"
    ),
}


def _make_state(include_branded: bool) -> SessionState:
    state = SessionState(include_branded=include_branded)
    state.product = ProductInfo(
        title="Head & Shoulders Dandruff Shampoo",
        brand=BRAND,
        product_id=PRODUCT_ID,
    )
    return state


def _search_batch() -> list[dict]:
    """One generic candidate plus three branded candidates (all from a
    generic keyword, proving the shelf — not the keyword — is filtered)."""
    return [
        {"url": GENERIC_URL, "keyword": "dandruff shampoo", "position": 1,
         "title": "Dandruff Shampoo - Walmart.com"},
        {"url": PRODUCT_BRAND_URL, "keyword": "dandruff shampoo", "position": 2,
         "title": "Head & Shoulders - Walmart.com"},
        {"url": COMPETITOR_FACET_URL, "keyword": "dandruff shampoo", "position": 3,
         "title": "Shampoo - Walmart.com"},
        {"url": COMPETITOR_BARE_URL, "keyword": "dandruff shampoo", "position": 4,
         "title": "OGX in Shampoo - Walmart.com"},
    ]


def _encoded_path_search_batch() -> list[dict]:
    pages = [
        {"url": url, "keyword": "dandruff shampoo", "position": position,
         "title": f"{brand} Dandruff Shampoo - Walmart.com"}
        for position, (brand, url) in enumerate(ENCODED_BRAND_URLS.items(), 1)
    ]
    pages.append({
        "url": GENERIC_URL,
        "keyword": "dandruff shampoo",
        "position": len(pages) + 1,
        "title": "Dandruff Shampoo - Walmart.com",
    })
    return pages


# ---------------------------------------------------------------------------
# Keyword generation and queueing
# ---------------------------------------------------------------------------

def _fake_llm_result(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=json.dumps(payload),
        prompt_tokens=10,
        completion_tokens=10,
        cost_usd=lambda: 0.0,
    )


class TestKeywordGeneration:
    _LLM_PAYLOAD = {
        "unbranded_keywords": ["dandruff shampoo", "hair care"],
        "branded_keywords": ["head & shoulders shampoo", "ogx shampoo"],
    }

    @patch("app.agents.keyword_agent.call_llm")
    @patch("app.agents.keyword_agent.get_active_client", return_value=object())
    def test_off_discards_branded_even_if_llm_returns_them(self, _client, mock_llm):
        from app.agents.keyword_agent import extract_keywords

        mock_llm.return_value = _fake_llm_result(self._LLM_PAYLOAD)
        all_kw, branded, unbranded, _ = extract_keywords(
            {"title": "t", "brand": BRAND}, include_branded=False
        )
        assert branded == []
        assert unbranded == ["dandruff shampoo", "hair care"]
        assert all_kw == unbranded

    @patch("app.agents.keyword_agent.call_llm")
    @patch("app.agents.keyword_agent.get_active_client", return_value=object())
    def test_on_returns_branded_and_unbranded(self, _client, mock_llm):
        from app.agents.keyword_agent import extract_keywords

        mock_llm.return_value = _fake_llm_result(self._LLM_PAYLOAD)
        all_kw, branded, unbranded, _ = extract_keywords(
            {"title": "t", "brand": BRAND}, include_branded=True
        )
        assert branded == ["head & shoulders shampoo", "ogx shampoo"]
        assert unbranded == ["dandruff shampoo", "hair care"]
        assert all_kw == unbranded + branded

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_initial_keywords_off_queues_only_generic(self, mock_extract):
        from app.agents.keyword_expander import get_initial_keywords

        # Even a (hypothetical) branded list from the agent must not be queued.
        mock_extract.return_value = (
            ["dandruff shampoo", "hair care", "head & shoulders shampoo"],
            ["head & shoulders shampoo"],
            ["dandruff shampoo", "hair care"],
            0.0,
        )
        state = _make_state(include_branded=False)
        new_kws, _ = get_initial_keywords(state)

        assert new_kws == ["dandruff shampoo", "hair care"]
        assert state.keywords_pending == ["dandruff shampoo", "hair care"]
        assert state.keywords_branded == []
        assert state.keywords_unbranded == ["dandruff shampoo", "hair care"]

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_initial_keywords_on_queues_both_and_preserves_classes(self, mock_extract):
        from app.agents.keyword_expander import get_initial_keywords

        mock_extract.return_value = (
            ["dandruff shampoo", "head & shoulders shampoo"],
            ["head & shoulders shampoo"],
            ["dandruff shampoo"],
            0.0,
        )
        state = _make_state(include_branded=True)
        new_kws, _ = get_initial_keywords(state)

        assert set(new_kws) == {"dandruff shampoo", "head & shoulders shampoo"}
        assert "head & shoulders shampoo" in state.keywords_pending
        assert state.keywords_branded == ["head & shoulders shampoo"]
        assert state.keywords_unbranded == ["dandruff shampoo"]
        assert state.keyword_type_for("head & shoulders shampoo") == "branded"
        assert state.keyword_type_for("dandruff shampoo") == "generic"


# ---------------------------------------------------------------------------
# Search-time branded-shelf rejection (central enforcement)
# ---------------------------------------------------------------------------

class TestSearchFiltering:
    @patch("app.agents.search_agent.find_browse_pages")
    async def test_off_rejects_encoded_path_facets_before_admission(
        self, mock_find
    ):
        mock_find.return_value = _encoded_path_search_batch()
        state = _make_state(include_branded=False)
        state.keywords_pending = ["dandruff shampoo"]

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        assert result.success
        assert [page.url for page in state.pages_discovered] == [GENERIC_URL]
        assert result.data["new_pages"] == 1
        assert result.data["rejected_branded"] == len(ENCODED_BRAND_URLS)
        assert set(ENCODED_BRAND_URLS) <= state.known_brand_terms

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_on_admits_and_marks_encoded_path_facets(self, mock_find):
        mock_find.return_value = _encoded_path_search_batch()
        state = _make_state(include_branded=True)
        state.keywords_pending = ["dandruff shampoo"]

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        assert result.success
        assert result.data["new_pages"] == len(ENCODED_BRAND_URLS) + 1
        assert result.data["rejected_branded"] == 0
        flags = {page.url: page.is_branded for page in state.pages_discovered}
        assert flags[GENERIC_URL] is False
        assert all(flags[url] is True for url in ENCODED_BRAND_URLS.values())

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_encoded_facet_brand_classifies_unfaceted_sibling(
        self, mock_find
    ):
        leader_sibling = (
            "https://www.walmart.com/browse/beauty/dandruff-shampoo/leader/"
            "1085666_3147628_5752434_2927912_4339403"
        )
        mock_find.return_value = [
            {"url": ENCODED_BRAND_URLS["Leader"], "keyword": "dandruff shampoo",
             "position": 1, "title": "Leader Dandruff Shampoo - Walmart.com"},
            {"url": leader_sibling, "keyword": "dandruff shampoo", "position": 2,
             "title": "Leader in Dandruff Shampoo - Walmart.com"},
            {"url": GENERIC_URL, "keyword": "dandruff shampoo", "position": 3,
             "title": "Dandruff Shampoo - Walmart.com"},
        ]
        state = _make_state(include_branded=False)

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        assert [page.url for page in state.pages_discovered] == [GENERIC_URL]
        assert result.data["rejected_branded"] == 2
        assert "Leader" in state.known_brand_terms

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_off_rejects_branded_base_shelves(self, mock_find):
        mock_find.return_value = _search_batch()
        state = _make_state(include_branded=False)
        state.keywords_pending = ["dandruff shampoo"]

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        assert result.success
        urls = [p.url for p in state.pages_discovered]
        assert urls == [GENERIC_URL]                      # generic retained
        assert result.data["rejected_branded"] == 3       # brand, facet, competitor
        assert "OGX" in state.known_brand_terms           # harvested from facet
        assert state.keywords_tried == ["dandruff shampoo"]

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_on_retains_branded_and_competitor_shelves(self, mock_find):
        mock_find.return_value = _search_batch()
        state = _make_state(include_branded=True)
        state.keywords_pending = ["dandruff shampoo"]

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        urls = {p.url for p in state.pages_discovered}
        assert urls == {GENERIC_URL, PRODUCT_BRAND_URL,
                        COMPETITOR_FACET_URL, COMPETITOR_BARE_URL}
        assert result.data["rejected_branded"] == 0
        branded_flags = {p.url: p.is_branded for p in state.pages_discovered}
        assert branded_flags[GENERIC_URL] is False
        assert branded_flags[PRODUCT_BRAND_URL] is True
        assert branded_flags[COMPETITOR_FACET_URL] is True
        assert branded_flags[COMPETITOR_BARE_URL] is True

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_off_with_empty_product_brand_does_not_crash(self, mock_find):
        mock_find.return_value = _search_batch()
        state = _make_state(include_branded=False)
        state.product.brand = ""
        state.keywords_pending = ["dandruff shampoo"]

        result = await _call_search(state, {"keywords": ["dandruff shampoo"]})

        assert result.success
        urls = [p.url for p in state.pages_discovered]
        # Generic page always survives; facet + title-detected competitor
        # shelves are still rejected even without a product brand.
        assert GENERIC_URL in urls
        assert COMPETITOR_FACET_URL not in urls
        assert COMPETITOR_BARE_URL not in urls

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_dedup_still_works_with_filtering(self, mock_find):
        mock_find.return_value = _search_batch()
        state = _make_state(include_branded=False)
        state.keywords_pending = ["dandruff shampoo"]
        await _call_search(state, {"keywords": ["dandruff shampoo"]})

        # Second search returns the same URLs — nothing new is added.
        mock_find.return_value = _search_batch()
        state.keywords_pending = ["shampoo"]
        result = await _call_search(state, {"keywords": ["shampoo"]})

        assert result.data["new_pages"] == 0
        assert len(state.pages_discovered) == 1


# ---------------------------------------------------------------------------
# Shelf check, placement reporting, and brand-filter URL retention
# ---------------------------------------------------------------------------

class TestShelfCheckFlow:
    @patch("app.tools.shelf_checker.check_shelf_visibility")
    @patch("app.agents.search_agent.find_browse_pages")
    async def test_retained_generic_shelf_is_checked_with_product_brand(
        self, mock_find, mock_check
    ):
        mock_find.return_value = _search_batch()
        state = _make_state(include_branded=False)
        state.keywords_pending = ["dandruff shampoo"]
        await _call_search(state, {"keywords": ["dandruff shampoo"]})

        captured = {}

        async def fake_check(pages, product_id, brand, known_brands=()):
            captured["pages"] = pages
            captured["brand"] = brand
            return {
                "found": 1, "missing": 0, "invalid": 0, "total": 1,
                "details": {
                    GENERIC_URL: {
                        "visibility": True, "discoverability": True,
                        "organic": True, "sponsored": False,
                        "placement_rank": 3, "placements": [],
                        "brand": True, "product": True, "page": 1,
                    }
                },
            }

        mock_check.side_effect = fake_check
        result = await _call_check_shelf(state, {"max_pages": 5})

        assert result.success
        # The brand-filtered view is built with the ANALYZED product's brand.
        assert captured["brand"] == BRAND
        assert [p["url"] for p in captured["pages"]] == [GENERIC_URL]

        assert len(state.found_pages) == 1
        sr = state.found_pages[0]
        assert sr.page_url == GENERIC_URL
        assert sr.keyword_type == "generic"
        assert sr.is_branded_shelf is False
        assert sr.visibility and sr.discoverability
        assert sr.placement_rank == 3

    def test_fetch_shelf_sync_builds_product_brand_facet_url(self):
        from app.tools.shelf_checker import fetch_shelf_sync

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return f"<html>{PRODUCT_ID}</html>"

        with patch("app.tools.shelf_checker._fetch_html", side_effect=fake_fetch):
            result = fetch_shelf_sync(GENERIC_URL, PRODUCT_ID, BRAND)

        assert result is not None
        assert fetched[0] == GENERIC_URL + "?facet=brand%3AHead%20%26%20Shoulders"
        assert fetched[1] == GENERIC_URL
        assert result["visibility"] is True
        assert result["discoverability"] is True

    def test_fetch_shelf_sync_with_empty_brand_does_not_crash(self):
        from app.tools.shelf_checker import fetch_shelf_sync

        fetched = []

        def fake_fetch(url):
            fetched.append(url)
            return f"<html>{PRODUCT_ID}</html>"

        with patch("app.tools.shelf_checker._fetch_html", side_effect=fake_fetch):
            result = fetch_shelf_sync(GENERIC_URL, PRODUCT_ID, "")

        assert result is not None
        assert fetched == [GENERIC_URL]        # no facet applied, single fetch
        assert result["visibility"] is True


# ---------------------------------------------------------------------------
# Post-fetch verification — unknown competitor brands with clean URLs/titles
# ---------------------------------------------------------------------------

# Lexically indistinguishable from a generic category: no facet, no brand
# token known ahead of time, generic search-result title.
HIDDEN_COMPETITOR_URL = "https://www.walmart.com/browse/beauty/shampoo/selsun-blue/1085666_555"
SIBLING_COMPETITOR_URL = "https://www.walmart.com/browse/beauty/shampoo/ogx/1085666_556"

PAGE_BRANDS = ["Head & Shoulders", "OGX", "Selsun Blue"]


def _shelf_detail(branded_shelf=False, shelf_brand="", page_brands=PAGE_BRANDS,
                  discoverable=True):
    return {
        "visibility": True, "discoverability": discoverable,
        "organic": discoverable, "sponsored": False,
        "placement_rank": None, "placements": [],
        "branded_shelf": branded_shelf,
        "branded_reason": "breadcrumb_is_page_brand" if branded_shelf else "",
        "shelf_brand": shelf_brand,
        "page_brands": page_brands,
        "brand": True, "product": discoverable, "page": 1 if discoverable else 0,
    }


class TestPostFetchVerification:
    @patch("app.tools.shelf_checker.check_shelf_visibility")
    async def test_off_rejects_hidden_competitor_after_fetch(self, mock_check):
        state = _make_state(include_branded=False)
        state.pages_discovered = [
            # Both survive pre-fetch classification (no lexical brand signal).
            BrowsePage(url=GENERIC_URL, keyword="dandruff shampoo",
                       relevance_score=0.9),
            BrowsePage(url=HIDDEN_COMPETITOR_URL, keyword="dandruff shampoo",
                       relevance_score=0.8, title="Shampoo - Walmart.com"),
        ]

        async def fake_check(pages, product_id, brand, known_brands=()):
            return {
                "found": 2, "missing": 0, "invalid": 0,
                "details": {
                    GENERIC_URL: _shelf_detail(),
                    HIDDEN_COMPETITOR_URL: _shelf_detail(
                        branded_shelf=True, shelf_brand="Selsun Blue"
                    ),
                },
            }

        mock_check.side_effect = fake_check
        result = await _call_check_shelf(state, {"max_pages": 5})

        assert result.success
        recorded = [sr.page_url for sr in state.found_pages + state.missing_pages]
        assert recorded == [GENERIC_URL]
        assert result.data["rejected_branded"] == 1
        assert result.data["found"] == 1 and result.data["missing"] == 0
        # Branded shelf never reaches the final report either.
        report_urls = [sr["url"] for sr in state.to_final_report()["shelf_results"]]
        assert report_urls == [GENERIC_URL]

    @patch("app.tools.shelf_checker.check_shelf_visibility")
    async def test_page_brands_harvested_and_unchecked_sibling_pruned(self, mock_check):
        state = _make_state(include_branded=False)
        state.pages_discovered = [
            BrowsePage(url=GENERIC_URL, keyword="dandruff shampoo",
                       relevance_score=0.9),
            # Sibling competitor shelf, not in this check batch (lower score).
            BrowsePage(url=SIBLING_COMPETITOR_URL, keyword="dandruff shampoo",
                       relevance_score=0.1, title="Shampoo - Walmart.com"),
        ]

        async def fake_check(pages, product_id, brand, known_brands=()):
            return {
                "found": 1, "missing": 0, "invalid": 0,
                "details": {GENERIC_URL: _shelf_detail()},
            }

        mock_check.side_effect = fake_check
        result = await _call_check_shelf(state, {"max_pages": 1})

        # Option 2: the checked page's facet list revealed OGX...
        assert set(PAGE_BRANDS) <= state.known_brand_terms
        # ...so the unchecked OGX sibling was pruned before costing a fetch.
        assert result.data["pruned_unchecked"] == 1
        assert SIBLING_COMPETITOR_URL not in [p.url for p in state.pages_discovered]
        assert [sr.page_url for sr in state.found_pages] == [GENERIC_URL]

    @patch("app.tools.shelf_checker.check_shelf_visibility")
    async def test_on_records_branded_shelf_with_flag(self, mock_check):
        state = _make_state(include_branded=True)
        state.pages_discovered = [
            BrowsePage(url=HIDDEN_COMPETITOR_URL, keyword="dandruff shampoo",
                       relevance_score=0.8),
        ]

        async def fake_check(pages, product_id, brand, known_brands=()):
            return {
                "found": 1, "missing": 0, "invalid": 0,
                "details": {
                    HIDDEN_COMPETITOR_URL: _shelf_detail(
                        branded_shelf=True, shelf_brand="Selsun Blue"
                    ),
                },
            }

        mock_check.side_effect = fake_check
        result = await _call_check_shelf(state, {"max_pages": 5})

        assert result.data["rejected_branded"] == 0
        assert len(state.found_pages) == 1
        assert state.found_pages[0].is_branded_shelf is True
        # Provenance still lands in the report when the setting is on.
        report = state.to_final_report()
        assert report["shelf_results"][0]["is_branded_shelf"] is True

    def test_branded_rows_do_not_consume_recommended_result_slots(self):
        """
        Interaction between "Include Branded Results" and the capped
        Recommended Category Pages count: with the setting off, branded rows
        are excluded BEFORE the cap is applied, so they never displace a
        generic row from the requested quota.
        """
        state = _make_state(include_branded=False)
        state.recommended_result_count = 2
        # Branded rows are discovered first and score highest, so a cap
        # applied before filtering would return only branded rows.
        state.found_pages = [
            ShelfResult(page_url=PRODUCT_BRAND_URL, product_found=True,
                        is_branded_shelf=True, visibility=True,
                        discoverability=True, relevance_score=0.99,
                        discovery_index=0),
            ShelfResult(page_url=HIDDEN_COMPETITOR_URL, product_found=True,
                        is_branded_shelf=True, visibility=True,
                        discoverability=True, relevance_score=0.98,
                        discovery_index=1),
            ShelfResult(page_url=GENERIC_URL, product_found=True,
                        is_branded_shelf=False, visibility=True,
                        discoverability=True, relevance_score=0.50,
                        discovery_index=2),
            ShelfResult(page_url=GENERIC_URL + "-two", product_found=True,
                        is_branded_shelf=False, visibility=True,
                        discoverability=True, relevance_score=0.40,
                        discovery_index=3),
        ]

        # Branded rows also must not inflate the loop's completion target.
        assert state.eligible_result_count == 2

        report = state.to_final_report()
        urls = [sr["url"] for sr in report["shelf_results"]]
        assert urls == [GENERIC_URL, GENERIC_URL + "-two"]
        assert report["recommended_result_count_returned"] == 2

    def test_branded_rows_fill_slots_when_setting_is_on(self):
        state = _make_state(include_branded=True)
        state.recommended_result_count = 2
        state.found_pages = [
            ShelfResult(page_url=PRODUCT_BRAND_URL, product_found=True,
                        is_branded_shelf=True, visibility=True,
                        discoverability=True, relevance_score=0.99,
                        discovery_index=0),
            ShelfResult(page_url=GENERIC_URL, product_found=True,
                        is_branded_shelf=False, visibility=True,
                        discoverability=True, relevance_score=0.50,
                        discovery_index=1),
        ]

        assert state.eligible_result_count == 2
        report = state.to_final_report()
        assert [sr["url"] for sr in report["shelf_results"]] == [
            PRODUCT_BRAND_URL, GENERIC_URL
        ]

    def test_fetch_shelf_sync_classifies_base_page_not_own_facet_view(self):
        from app.tools.shelf_checker import fetch_shelf_sync
        from tests.test_shelf_classifier import next_data_html, page_payload

        # The brand-filtered view we build ourselves has the facet selected —
        # it must NOT drive classification. The base page is generic.
        facet_view_html = next_data_html(page_payload(
            ["Beauty", "Dandruff Shampoo"],
            brand_values=PAGE_BRANDS, selected=["Head & Shoulders"],
        )) + PRODUCT_ID
        base_view_html = next_data_html(page_payload(
            ["Beauty", "Dandruff Shampoo"], brand_values=PAGE_BRANDS,
        )) + PRODUCT_ID

        def fake_fetch(url):
            return facet_view_html if "facet=" in url else base_view_html

        with patch("app.tools.shelf_checker._fetch_html", side_effect=fake_fetch):
            result = fetch_shelf_sync(GENERIC_URL, PRODUCT_ID, BRAND)

        assert result is not None
        assert result["branded_shelf"] is False
        assert result["page_brands"] == sorted(PAGE_BRANDS)

    def test_fetch_shelf_sync_detects_branded_base_page(self):
        from app.tools.shelf_checker import fetch_shelf_sync
        from tests.test_shelf_classifier import next_data_html, page_payload

        branded_base_html = next_data_html(page_payload(
            ["Beauty", "Shampoo", "Selsun Blue"], brand_values=PAGE_BRANDS,
        )) + PRODUCT_ID

        with patch("app.tools.shelf_checker._fetch_html",
                   return_value=branded_base_html):
            result = fetch_shelf_sync(HIDDEN_COMPETITOR_URL, PRODUCT_ID, BRAND)

        assert result is not None
        assert result["branded_shelf"] is True
        assert result["shelf_brand"] == "Selsun Blue"
        assert result["branded_reason"] == "breadcrumb_is_page_brand"


# ---------------------------------------------------------------------------
# Final report and persistence enforcement
# ---------------------------------------------------------------------------

def _branded_result() -> ShelfResult:
    return ShelfResult(
        page_url=PRODUCT_BRAND_URL, product_found=True,
        keyword_type="branded", is_branded_shelf=True,
        visibility=True, discoverability=True, keyword="head & shoulders shampoo",
    )


def _generic_result() -> ShelfResult:
    return ShelfResult(
        page_url=GENERIC_URL, product_found=True,
        keyword_type="generic", is_branded_shelf=False,
        visibility=True, discoverability=True, keyword="dandruff shampoo",
    )


class TestFinalReport:
    def test_off_excludes_branded_shelves_from_report(self):
        state = _make_state(include_branded=False)
        state.found_pages = [_generic_result(), _branded_result()]
        state.keywords_tried = ["dandruff shampoo"]

        report = state.to_final_report()

        urls = [sr["url"] for sr in report["shelf_results"]]
        assert urls == [GENERIC_URL]
        assert report["shelf_stats"]["total"] == 1
        assert PRODUCT_BRAND_URL not in report["shelf_stats"]["details"]
        assert report["include_branded"] is False

    def test_on_keeps_branded_shelves_in_report(self):
        state = _make_state(include_branded=True)
        state.found_pages = [_generic_result(), _branded_result()]
        state.keywords_tried = ["dandruff shampoo", "head & shoulders shampoo"]
        state.keywords_branded = ["head & shoulders shampoo"]
        state.keywords_unbranded = ["dandruff shampoo"]

        report = state.to_final_report()

        urls = {sr["url"] for sr in report["shelf_results"]}
        assert urls == {GENERIC_URL, PRODUCT_BRAND_URL}
        assert report["branded_keywords_used"] == ["head & shoulders shampoo"]
        assert report["unbranded_keywords_used"] == ["dandruff shampoo"]
        by_url = {sr["url"]: sr for sr in report["shelf_results"]}
        assert by_url[PRODUCT_BRAND_URL]["is_branded_shelf"] is True
        assert by_url[PRODUCT_BRAND_URL]["keyword_type"] == "branded"
        assert by_url[GENERIC_URL]["is_branded_shelf"] is False

    async def test_off_persists_only_generic_pages_and_keyword_classes(self):
        from app.services.workflow_v2 import _persist_results

        state = _make_state(include_branded=False)
        state.found_pages = [_generic_result(), _branded_result()]
        state.keywords_tried = ["dandruff shampoo"]
        state.keywords_unbranded = ["dandruff shampoo"]

        saved = {}

        def fake_save(**kwargs):
            saved.update(kwargs)
            return True

        with patch("app.services.persistence.save_result_to_csv", side_effect=fake_save), \
             patch("app.services.persistence.upload_csv_to_drive", return_value=True), \
             patch("app.services.persistence.append_result_to_sheet", side_effect=fake_save), \
             patch("app.config.settings.use_google_sheets", False):
            await _persist_results(state)

        assert saved["browse_pages"] == [GENERIC_URL]
        assert saved["branded_keywords"] == []
        assert saved["unbranded_keywords"] == ["dandruff shampoo"]


# ---------------------------------------------------------------------------
# Setting survives the API boundary (frontend → backend flow)
# ---------------------------------------------------------------------------

class TestSettingTransport:
    @pytest.mark.parametrize("flag,expected", [("true", True), ("false", False)])
    def test_include_branded_reaches_workflow(self, flag, expected):
        from starlette.testclient import TestClient
        from app.main import app

        captured = {}

        def fake_workflow(**kwargs):
            captured.update(kwargs)

            async def gen():
                yield {"event": "complete", "data": {}}
            return gen()

        with patch("app.services.workflow_v2.run_v2_workflow", side_effect=fake_workflow):
            with TestClient(app) as client:
                resp = client.get(
                    "/v2/analyze/stream",
                    params={
                        "url": "https://www.walmart.com/ip/x/1",
                        "include_branded": flag,
                    },
                )
        assert resp.status_code == 200
        assert captured["include_branded"] is expected

    def test_session_state_default_is_off(self):
        assert SessionState().include_branded is False
