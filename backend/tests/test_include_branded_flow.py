"""
End-to-end behavior of the "Include Branded Results" setting.

Covers keyword generation/queueing, search-time branded-shelf rejection,
report/persistence enforcement, brand-filtered URL construction, and the
frontend-to-backend flow of the setting — all with deterministic mocks.
"""

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.agents.orchestrator import (
    _call_search,
    _call_check_shelf,
    _check_stopping_conditions,
    _rule_based_decision,
)
from app.models.session_state import BrowsePage, ProductInfo, SessionState, ShelfResult

BRAND = "Head & Shoulders"
PRODUCT_ID = "12345"

GENERIC_URL = "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_1071969"
RECOVERED_GENERIC_URL = (
    "https://www.walmart.com/browse/beauty/dandruff-shampoo/"
    "1085666_3147628_5752434_2927912_4339403"
)
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


def _recovery_info_messages(caplog) -> list[str]:
    return [
        message for message in caplog.messages
        if "[Search] Recovered generic shelf candidate" in message
    ]


def _recovery_debug_messages(caplog) -> list[str]:
    return [
        message for message in caplog.messages
        if "[Search] Skipped recovered generic shelf candidate" in message
    ]


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


class TestInitialKeywordQueue:
    """
    Ordering of the initial keyword queue built by get_initial_keywords.

    The loop consumes keywords_pending front-to-back and can stop on the
    result-count quota, so branded keywords are front-loaded when the setting
    is on to guarantee the analyzed/competitor brand shelves are searched
    before the quota can end the run. Within the generic block, the unbranded
    terms keep the MOST SPECIFIC → MOST GENERAL order the keyword agent returns
    (no sort/shuffle), so product-specific shelves fill the quota before it
    broadens to generic department shelves. Classification bookkeeping is
    untouched.
    """

    _UNBRANDED = ["dandruff shampoo", "hair care", "scalp treatment", "shampoo"]
    _BRANDED = ["head & shoulders shampoo", "ogx shampoo"]

    # An explicitly specific → general unbranded list, mirroring what the
    # updated keyword-agent prompt asks the LLM to return.
    _SPECIFIC_TO_GENERAL = [
        "dandruff shampoo",         # most specific — matches the product name
        "pyrithione zinc shampoo",
        "anti-dandruff shampoo",
        "shampoo",
        "hair care",
        "personal care",            # most general — department
    ]

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_on_front_loads_branded_before_unbranded(self, mock_extract):
        from app.agents.keyword_expander import get_initial_keywords

        # extract_keywords returns (all, branded, unbranded, cost); all is
        # unbranded + branded per the agent contract.
        mock_extract.return_value = (
            self._UNBRANDED + self._BRANDED, self._BRANDED, self._UNBRANDED, 0.0
        )
        state = _make_state(include_branded=True)
        get_initial_keywords(state)

        pending = state.keywords_pending
        # Every branded term precedes every unbranded term in the search queue.
        max_branded_idx = max(pending.index(kw) for kw in self._BRANDED)
        min_unbranded_idx = min(pending.index(kw) for kw in self._UNBRANDED)
        assert max_branded_idx < min_unbranded_idx
        assert pending == self._BRANDED + self._UNBRANDED

        # Only the queue order changed — the class lists keep content and order.
        assert state.keywords_branded == self._BRANDED
        assert state.keywords_unbranded == self._UNBRANDED

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_off_queues_unbranded_only_in_original_order(self, mock_extract):
        from app.agents.keyword_expander import get_initial_keywords

        # The agent may still hand back a branded list; with the setting off it
        # must be dropped and the queue stays unbranded-only in original order.
        mock_extract.return_value = (
            self._UNBRANDED + ["head & shoulders shampoo"],
            ["head & shoulders shampoo"],
            self._UNBRANDED,
            0.0,
        )
        state = _make_state(include_branded=False)
        new_kws, _ = get_initial_keywords(state)

        assert new_kws == self._UNBRANDED
        assert state.keywords_pending == self._UNBRANDED
        assert state.keywords_branded == []
        assert state.keywords_unbranded == self._UNBRANDED

    @patch("app.agents.search_agent.find_browse_pages")
    @patch("app.agents.keyword_agent.extract_keywords")
    async def test_on_searches_branded_in_first_batch(self, mock_extract, mock_find):
        """End-to-end: branded terms are searched in the first rule-based batch,
        ahead of any unbranded term, mirroring the real loop's queue consumption."""
        from app.agents.keyword_expander import get_initial_keywords

        mock_extract.return_value = (
            self._UNBRANDED + self._BRANDED, self._BRANDED, self._UNBRANDED, 0.0
        )
        mock_find.return_value = []   # only the keyword ordering matters here

        state = _make_state(include_branded=True)
        get_initial_keywords(state)

        # The rule-based decision searches the front of the queue (batch of 5).
        action, args = _rule_based_decision(state)
        assert action == "search"
        await _call_search(state, args)

        tried = state.keywords_tried
        assert set(self._BRANDED) <= set(tried)          # both branded searched
        max_branded_idx = max(tried.index(kw) for kw in self._BRANDED)
        tried_unbranded = [kw for kw in self._UNBRANDED if kw in tried]
        assert tried_unbranded                            # some unbranded also tried
        min_unbranded_idx = min(tried.index(kw) for kw in tried_unbranded)
        assert max_branded_idx < min_unbranded_idx        # branded before unbranded

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_unbranded_specificity_order_preserved_on(self, mock_extract):
        """With the setting ON, the unbranded block keeps the exact specific →
        general order the agent returned (branded stays front-loaded); the
        unbranded classification list keeps that order too."""
        from app.agents.keyword_expander import get_initial_keywords

        mock_extract.return_value = (
            self._SPECIFIC_TO_GENERAL + self._BRANDED,
            self._BRANDED,
            self._SPECIFIC_TO_GENERAL,
            0.0,
        )
        state = _make_state(include_branded=True)
        get_initial_keywords(state)

        # Whole queue: branded front-loaded, then the untouched specific→general
        # unbranded list.
        assert state.keywords_pending == self._BRANDED + self._SPECIFIC_TO_GENERAL
        # The generic slice of the queue is byte-for-byte the returned order.
        unbranded_slice = [
            kw for kw in state.keywords_pending if kw in self._SPECIFIC_TO_GENERAL
        ]
        assert unbranded_slice == self._SPECIFIC_TO_GENERAL
        # Classification preserves the specificity order too (no sort/shuffle).
        assert state.keywords_unbranded == self._SPECIFIC_TO_GENERAL

    @patch("app.agents.keyword_agent.extract_keywords")
    def test_unbranded_specificity_order_is_queue_order_off(self, mock_extract):
        """With the setting OFF, the queue is exactly the unbranded list in
        specific → general order — no branded terms, no reordering."""
        from app.agents.keyword_expander import get_initial_keywords

        mock_extract.return_value = (
            self._SPECIFIC_TO_GENERAL, [], self._SPECIFIC_TO_GENERAL, 0.0
        )
        state = _make_state(include_branded=False)
        new_kws, _ = get_initial_keywords(state)

        assert new_kws == self._SPECIFIC_TO_GENERAL
        assert state.keywords_pending == self._SPECIFIC_TO_GENERAL
        assert state.keywords_unbranded == self._SPECIFIC_TO_GENERAL
        assert state.keywords_branded == []


class TestKeywordPromptContract:
    """
    The keyword-agent prompt must instruct specific → general ordering for the
    UNBRANDED list and keep the shelf-realism guardrails, while leaving the
    branded instructions unchanged (no ordering language attached to branded).
    """

    def test_unbranded_prompt_orders_specific_to_general_with_guardrails(self):
        from app.agents.keyword_agent import _build_prompt

        prompt = _build_prompt(
            brand=BRAND,
            text_to_analyze="Title: Head & Shoulders Dandruff Shampoo",
            include_branded=False,
            user_instructions="",
        )
        # Specific → general ordering instruction is present and correctly ordered.
        assert "MOST SPECIFIC" in prompt
        assert "MOST GENERAL" in prompt
        assert prompt.index("MOST SPECIFIC") < prompt.index("MOST GENERAL")
        # Shelf-realism guardrails retained.
        assert "1-4 words" in prompt
        assert "realistic Walmart shelf/category names" in prompt
        assert "Do NOT generate long product descriptions" in prompt

    def test_branded_instructions_unchanged_and_not_reordered(self):
        from app.agents.keyword_agent import _build_prompt

        prompt = _build_prompt(
            brand=BRAND,
            text_to_analyze="Title: Head & Shoulders Dandruff Shampoo",
            include_branded=True,
            user_instructions="",
        )
        # Unbranded still gets the ordering instruction...
        assert "MOST SPECIFIC" in prompt
        # ...and the branded block is present and left intact.
        branded_header = (
            "branded_keywords — 3-5 terms that INCLUDE the brand name "
            "of this brand and competition brand."
        )
        assert branded_header in prompt
        assert f"'{BRAND} <product type>'" in prompt   # branded format unchanged
        # All ordering language sits in the unbranded section, before branded —
        # proving no specific→general instruction was attached to branded terms.
        assert prompt.rfind("MOST SPECIFIC") < prompt.index(branded_header)
        assert prompt.rfind("MOST GENERAL") < prompt.index(branded_header)


# ---------------------------------------------------------------------------
# Search-time branded-shelf rejection (central enforcement)
# ---------------------------------------------------------------------------

class TestSearchFiltering:
    @patch("app.agents.search_agent.find_browse_pages")
    async def test_off_logs_recovered_candidate_without_admitting_it(
        self, mock_find, caplog
    ):
        branded_url = ENCODED_BRAND_URLS["Leader"]
        mock_find.return_value = [{
            "url": branded_url,
            "keyword": "dandruff shampoo",
            "position": 1,
            "title": "Leader Dandruff Shampoo - Walmart.com",
        }]
        state = _make_state(include_branded=False)

        with caplog.at_level(logging.INFO, logger="app.agents.orchestrator"):
            result = await _call_search(
                state, {"keywords": ["dandruff shampoo"]}
            )

        assert result.success
        assert result.data["new_pages"] == 0
        assert result.data["rejected_branded"] == 1
        assert state.pages_discovered == []
        assert RECOVERED_GENERIC_URL not in [
            page.url for page in state.pages_discovered
        ]
        rejection_index = next(
            i for i, message in enumerate(caplog.messages)
            if "[Search] Rejected branded shelf (brand_facet, brand='Leader')" in message
        )
        recovery_index = next(
            i for i, message in enumerate(caplog.messages)
            if (
                "[Search] Recovered generic shelf candidate "
                "(log_only, source=brand_facet, brand='Leader', "
                "keyword='dandruff shampoo')" in message
            )
        )
        assert recovery_index == rejection_index + 1
        assert caplog.messages[recovery_index].endswith(RECOVERED_GENERIC_URL)

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_duplicate_brands_and_keywords_log_one_recovery(
        self, mock_find, caplog
    ):
        mock_find.return_value = [
            {
                "url": ENCODED_BRAND_URLS[brand],
                "keyword": keyword,
                "position": position,
                "title": f"{brand} Dandruff Shampoo - Walmart.com",
            }
            for position, (brand, keyword) in enumerate(
                [
                    ("Leader", "dandruff shampoo"),
                    ("Vichy", "dry scalp shampoo"),
                    ("DHS", "medicated shampoo"),
                ],
                1,
            )
        ]
        state = _make_state(include_branded=False)
        summary_before = state.to_summary_dict()
        stopping_before = _check_stopping_conditions(state)

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            result = await _call_search(
                state,
                {"keywords": [
                    "dandruff shampoo", "dry scalp shampoo", "medicated shampoo"
                ]},
            )

        assert result.data["new_pages"] == 0
        assert result.data["rejected_branded"] == 3
        assert state.pages_discovered == []
        assert len(_recovery_info_messages(caplog)) == 1
        assert len(_recovery_debug_messages(caplog)) == 2
        assert all(
            "reason=duplicate_recovery" in message
            for message in _recovery_debug_messages(caplog)
        )
        assert "_recovered_generic_url_keys" not in state.to_summary_dict()
        assert state.to_summary_dict()["pages_discovered"] == summary_before[
            "pages_discovered"
        ]
        assert state.to_summary_dict()["eligible_rows"] == summary_before[
            "eligible_rows"
        ]
        assert _check_stopping_conditions(state) == stopping_before

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_direct_generic_later_wins_and_keeps_metadata(
        self, mock_find, caplog
    ):
        mock_find.return_value = [
            {
                "url": ENCODED_BRAND_URLS["Leader"],
                "keyword": "source keyword",
                "position": 1,
                "title": "Leader Dandruff Shampoo - Walmart.com",
            },
            {
                "url": ENCODED_BRAND_URLS["DHS"],
                "keyword": "other keyword",
                "position": 2,
                "title": "DHS Dandruff Shampoo - Walmart.com",
            },
            {
                "url": RECOVERED_GENERIC_URL,
                "keyword": "direct keyword",
                "position": 9,
                "title": "Direct Generic Shelf - Walmart.com",
            },
        ]
        state = _make_state(include_branded=False)

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            result = await _call_search(state, {"keywords": ["source keyword"]})

        assert result.data["new_pages"] == 1
        assert result.data["rejected_branded"] == 2
        assert _recovery_info_messages(caplog) == []
        skipped = _recovery_debug_messages(caplog)
        assert len(skipped) == 2
        assert all("reason=direct_in_current_batch" in message for message in skipped)
        assert len(state.pages_discovered) == 1
        direct = state.pages_discovered[0]
        assert direct.url == RECOVERED_GENERIC_URL
        assert direct.keyword == "direct keyword"
        assert direct.position == 9
        assert direct.title == "Direct Generic Shelf - Walmart.com"

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_existing_discovered_url_suppresses_recovery(
        self, mock_find, caplog
    ):
        mock_find.return_value = [{"url": ENCODED_BRAND_URLS["Leader"]}]
        state = _make_state(include_branded=False)
        state.pages_discovered = [BrowsePage(url=RECOVERED_GENERIC_URL)]

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            result = await _call_search(state, {"keywords": ["shampoo"]})

        assert result.data["rejected_branded"] == 1
        assert _recovery_info_messages(caplog) == []
        assert any(
            "reason=already_discovered" in message
            for message in _recovery_debug_messages(caplog)
        )
        assert [page.url for page in state.pages_discovered] == [
            RECOVERED_GENERIC_URL
        ]

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_previously_checked_url_suppresses_recovery(
        self, mock_find, caplog
    ):
        mock_find.return_value = [{"url": ENCODED_BRAND_URLS["Leader"]}]
        state = _make_state(include_branded=False)
        state.pages_checked = [
            BrowsePage(url=RECOVERED_GENERIC_URL, checked=True)
        ]

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            await _call_search(state, {"keywords": ["shampoo"]})

        assert _recovery_info_messages(caplog) == []
        assert any(
            "reason=already_discovered" in message
            for message in _recovery_debug_messages(caplog)
        )
        assert state.pages_discovered == []

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_recovery_logged_in_prior_round_is_debug_only_later(
        self, mock_find, caplog
    ):
        state = _make_state(include_branded=False)
        mock_find.return_value = [{"url": ENCODED_BRAND_URLS["Leader"]}]

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            first = await _call_search(state, {"keywords": ["first keyword"]})
            mock_find.return_value = [{"url": ENCODED_BRAND_URLS["Vichy"]}]
            second = await _call_search(state, {"keywords": ["second keyword"]})

        assert first.data["rejected_branded"] == 1
        assert second.data["rejected_branded"] == 1
        assert len(_recovery_info_messages(caplog)) == 1
        assert any(
            "reason=duplicate_recovery" in message
            for message in _recovery_debug_messages(caplog)
        )
        assert state.pages_discovered == []

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_distinct_recovered_category_ids_each_log_once(
        self, mock_find, caplog
    ):
        second_url = ENCODED_BRAND_URLS["DHS"].replace(
            "1085666_3147628_5752434_2927912_4339403",
            "1085666_3147628_5752434_2927912_4339404",
        )
        mock_find.return_value = [
            {"url": ENCODED_BRAND_URLS["Leader"], "keyword": "shampoo"},
            {"url": second_url, "keyword": "shampoo"},
        ]
        state = _make_state(include_branded=False)

        with caplog.at_level(logging.INFO, logger="app.agents.orchestrator"):
            await _call_search(state, {"keywords": ["shampoo"]})

        messages = _recovery_info_messages(caplog)
        assert len(messages) == 2
        assert any(message.endswith("4339403") for message in messages)
        assert any(message.endswith("4339404") for message in messages)
        assert state.pages_discovered == []

    @patch("app.agents.search_agent.find_browse_pages")
    async def test_canonical_existing_variant_suppresses_recovery(
        self, mock_find, caplog
    ):
        branded_with_query = ENCODED_BRAND_URLS["Leader"] + "?b=2&a=1"
        mock_find.return_value = [{"url": branded_with_query}]
        state = _make_state(include_branded=False)
        state.pages_discovered = [BrowsePage(
            url=RECOVERED_GENERIC_URL.replace(
                "https://www.walmart.com", "https://WALMART.COM"
            ) + "/?a=1&b=2#section"
        )]

        with caplog.at_level(logging.DEBUG, logger="app.agents.orchestrator"):
            await _call_search(state, {"keywords": ["shampoo"]})

        assert _recovery_info_messages(caplog) == []
        assert any(
            "reason=already_discovered" in message
            for message in _recovery_debug_messages(caplog)
        )

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
    async def test_on_admits_and_marks_encoded_path_facets(self, mock_find, caplog):
        mock_find.return_value = _encoded_path_search_batch()
        state = _make_state(include_branded=True)
        state.keywords_pending = ["dandruff shampoo"]

        with caplog.at_level(logging.INFO, logger="app.agents.orchestrator"):
            result = await _call_search(
                state, {"keywords": ["dandruff shampoo"]}
            )

        assert result.success
        assert result.data["new_pages"] == len(ENCODED_BRAND_URLS) + 1
        assert result.data["rejected_branded"] == 0
        flags = {page.url: page.is_branded for page in state.pages_discovered}
        assert flags[GENERIC_URL] is False
        assert all(flags[url] is True for url in ENCODED_BRAND_URLS.values())
        assert not any(
            "Recovered generic shelf candidate" in message
            for message in caplog.messages
        )

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


# ---------------------------------------------------------------------------
# Diversified final selection when "Include Branded Results" is ON
# ---------------------------------------------------------------------------

OWN_A = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/own_a"
OWN_B = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/own_b"
OWN_C = "https://www.walmart.com/browse/beauty/shampoo/head-shoulders/own_c"
COMP_A = "https://www.walmart.com/browse/beauty/shampoo/ogx/comp_a"
COMP_B = "https://www.walmart.com/browse/beauty/shampoo/selsun-blue/comp_b"
GEN_A = "https://www.walmart.com/browse/beauty/shampoo/gen_a"
GEN_B = "https://www.walmart.com/browse/beauty/shampoo/gen_b"
GEN_C = "https://www.walmart.com/browse/beauty/shampoo/gen_c"
GEN_D = "https://www.walmart.com/browse/beauty/shampoo/gen_d"


def _sr(url, *, branded=False, shelf_brand="", relevance=None,
        discovery=0, position=0, keyword_type="generic") -> ShelfResult:
    return ShelfResult(
        page_url=url,
        product_found=True,
        is_branded_shelf=branded,
        shelf_brand=shelf_brand,
        keyword_type=keyword_type,
        visibility=True,
        discoverability=True,
        relevance_score=relevance,
        discovery_index=discovery,
        position=position,
    )


def _own(url, relevance, discovery, keyword_type="branded") -> ShelfResult:
    return _sr(url, branded=True, shelf_brand=BRAND, relevance=relevance,
               discovery=discovery, keyword_type=keyword_type)


def _comp(url, relevance, discovery, brand="OGX", keyword_type="generic") -> ShelfResult:
    return _sr(url, branded=True, shelf_brand=brand, relevance=relevance,
               discovery=discovery, keyword_type=keyword_type)


def _gen(url, relevance, discovery) -> ShelfResult:
    return _sr(url, branded=False, shelf_brand="", relevance=relevance,
               discovery=discovery)


class TestDiversifiedSelection:
    """selected_shelf_results / eligible_result_count with the setting ON."""

    def test_off_preserves_plain_ranking_and_excludes_branded(self):
        """Toggle off: branded shelves excluded, plain relevance ranking."""
        state = _make_state(include_branded=False)
        state.recommended_result_count = 3
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _comp(COMP_A, 0.95, 1),
            _gen(GEN_A, 0.90, 2),
            _gen(GEN_B, 0.80, 3),
            _gen(GEN_C, 0.70, 4),
        ]

        # Branded rows never counted, capped selection is pure relevance order.
        assert state.eligible_result_count == 3
        urls = [sr.page_url for sr in state.selected_shelf_results()]
        assert urls == [GEN_A, GEN_B, GEN_C]

    def test_target_five_one_own_one_competitor_rest_generic(self):
        """
        Three high-scoring own-brand shelves, one competitor shelf from a
        generic keyword, enough generics → exactly five rows, competitor
        included, only one own-brand shelf.
        """
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _own(OWN_B, 0.98, 1),
            _own(OWN_C, 0.97, 2),
            _comp(COMP_A, 0.60, 3, keyword_type="generic"),  # competitor via generic kw
            _gen(GEN_A, 0.55, 4),
            _gen(GEN_B, 0.50, 5),
            _gen(GEN_C, 0.45, 6),
            _gen(GEN_D, 0.40, 7),
        ]

        selected = state.selected_shelf_results()
        urls = [sr.page_url for sr in selected]

        assert len(selected) == 5
        assert COMP_A in urls                                   # competitor included
        own_selected = [sr for sr in selected if sr.page_url in (OWN_A, OWN_B, OWN_C)]
        assert len(own_selected) == 1                           # only one own-brand
        assert own_selected[0].page_url == OWN_A                # the best own-brand
        # Returned in the stable relevance order.
        assert urls == [OWN_A, COMP_A, GEN_A, GEN_B, GEN_C]

    def test_target_five_second_own_fills_when_only_three_nonown(self):
        """Only three non-own shelves → a second own-brand fills the fifth
        slot, but no third own-brand is selected because five is reached."""
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _own(OWN_B, 0.98, 1),
            _own(OWN_C, 0.97, 2),
            _comp(COMP_A, 0.60, 3),
            _gen(GEN_A, 0.55, 4),
            _gen(GEN_B, 0.50, 5),
        ]

        selected = state.selected_shelf_results()
        urls = [sr.page_url for sr in selected]

        assert len(selected) == 5
        own_selected = [u for u in urls if u in (OWN_A, OWN_B, OWN_C)]
        assert set(own_selected) == {OWN_A, OWN_B}   # second own admitted
        assert OWN_C not in urls                      # third own never selected
        assert {COMP_A, GEN_A, GEN_B} <= set(urls)

    def test_target_five_too_few_nonown_returns_fewer_not_third_own(self):
        """
        Search exhausted with too few non-own shelves: the report returns
        fewer than requested rather than admitting a third own-brand shelf.
        """
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _own(OWN_B, 0.98, 1),
            _own(OWN_C, 0.97, 2),
            _comp(COMP_A, 0.60, 3),
            _gen(GEN_A, 0.55, 4),
        ]

        selected = state.selected_shelf_results()
        urls = [sr.page_url for sr in selected]

        assert len(selected) == 4                     # fewer than the requested 5
        own_selected = [u for u in urls if u in (OWN_A, OWN_B, OWN_C)]
        assert set(own_selected) == {OWN_A, OWN_B}    # two own, never a third
        assert OWN_C not in urls
        report = state.to_final_report()
        assert report["recommended_result_count_requested"] == 5
        assert report["recommended_result_count_returned"] == 4

    def test_all_own_brand_fills_up_to_k_when_only_option(self):
        """
        Search-exhaustion clause: when own-brand shelves are the ONLY rows
        available, they are the sole way to reach K, so up to K own-brand
        shelves are returned (the two-shelf cap is relaxed only here).
        """
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _own(OWN_B, 0.98, 1),
            _own(OWN_C, 0.97, 2),
            _own(OWN_A + "_d", 0.96, 3),
            _own(OWN_A + "_e", 0.95, 4),
            _own(OWN_A + "_f", 0.94, 5),
        ]

        selected = state.selected_shelf_results()
        assert len(selected) == 5     # fills K from own-brand, the only option
        # But the loop-progress count stays capped so it never stops early
        # while competitor/generic candidates could still be discovered.
        assert state.eligible_result_count == 2

    def test_competitor_from_generic_keyword_is_competitor_not_own(self):
        """
        Ownership is derived from the shelf brand, never from keyword_type: a
        competitor shelf from a generic keyword is a competitor, and an
        own-brand shelf from a branded keyword is own.
        """
        state = _make_state(include_branded=True)
        state.recommended_result_count = 3
        own = _own(OWN_A, 0.90, 0, keyword_type="branded")
        comp = _comp(COMP_A, 0.50, 1, keyword_type="generic")
        gen1 = _gen(GEN_A, 0.40, 2)
        gen2 = _gen(GEN_B, 0.30, 3)
        state.found_pages = [own, comp, gen1, gen2]

        pools_own, pools_comp, pools_gen = state._partition_pools(state.found_pages)
        assert pools_own == [own]
        assert pools_comp == [comp]            # generic-keyword competitor here
        assert pools_gen == [gen1, gen2]

        urls = [sr.page_url for sr in state.selected_shelf_results()]
        assert COMP_A in urls                  # competitor takes the competitor slot
        assert urls == [OWN_A, COMP_A, GEN_A]

    @pytest.mark.parametrize("variant", [
        "Head & Shoulders",
        "head & shoulders",
        "HEAD & SHOULDERS",
        "head-shoulders",
        "Head%20%26%20Shoulders",   # URL-encoded "Head & Shoulders"
        "  Head   and   Shoulders  ",
    ])
    def test_shelf_brand_match_is_case_insensitive_and_normalized(self, variant):
        state = _make_state(include_branded=True)
        own = _sr(OWN_A, branded=True, shelf_brand=variant, relevance=0.9)
        comp = _comp(COMP_A, 0.8, 1, brand="ogx")   # lower-case competitor stays competitor
        pools_own, pools_comp, pools_gen = state._partition_pools([own, comp])
        assert pools_own == [own], f"{variant!r} should match the product brand"
        assert pools_comp == [comp]
        assert pools_gen == []

    def test_url_dedup_and_stable_ordering_with_branded_on(self):
        """Existing URL dedup and stable relevance ordering still hold."""
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [_gen(GEN_A, 0.90, 0)]
        state.missing_pages = [
            _gen(GEN_A, 0.50, 1),        # exact duplicate URL
            _gen(GEN_A + "/", 0.40, 2),  # trailing-slash duplicate
            _gen(GEN_B, 0.80, 3),
            _gen(GEN_C, 0.70, 4),
        ]

        assert state.eligible_result_count == 3           # duplicates collapse
        urls = [sr.page_url for sr in state.selected_shelf_results()]
        assert urls == [GEN_A, GEN_B, GEN_C]              # first-seen kept, relevance order

    def test_tie_broken_by_position_then_discovery_with_branded_on(self):
        state = _make_state(include_branded=True)
        state.recommended_result_count = 3
        state.found_pages = [
            _sr(GEN_A, relevance=0.5, discovery=0, position=9),
            _sr(GEN_B, relevance=0.5, discovery=1, position=2),
            _sr(GEN_C, relevance=0.5, discovery=2, position=5),
        ]
        urls = [sr.page_url for sr in state.selected_shelf_results()]
        assert urls == [GEN_B, GEN_C, GEN_A]   # equal relevance → position asc

    def test_loop_does_not_stop_early_on_many_own_brand(self):
        """The loop must not treat a pile of own-brand rows as completion."""
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [
            _own(OWN_A, 0.99, 0),
            _own(OWN_B, 0.98, 1),
            _own(OWN_C, 0.97, 2),
            _own(OWN_A + "_d", 0.96, 3),
            _own(OWN_A + "_e", 0.95, 4),
        ]

        # Own-brand rows count only up to two toward the target.
        assert state.eligible_result_count == 2
        should_stop, _ = _check_stopping_conditions(state)
        assert should_stop is False

        # Once enough competitor/generic rows arrive, the composition completes
        # (one competitor + four generics leaves a single own-brand slot).
        state.found_pages += [
            _comp(COMP_A, 0.65, 5),
            _gen(GEN_A, 0.55, 6),
            _gen(GEN_B, 0.50, 7),
            _gen(GEN_C, 0.45, 8),
            _gen(GEN_D, 0.40, 9),
        ]
        assert state.eligible_result_count == 7           # min(5,2) + 1 comp + 4 gen
        should_stop, reason = _check_stopping_conditions(state)
        assert should_stop is True
        assert reason.startswith("recommended_result_count_reached")

        selected = state.selected_shelf_results()
        assert len(selected) == 5
        own_selected = [
            sr for sr in selected
            if sr.page_url in (OWN_A, OWN_B, OWN_C, OWN_A + "_d", OWN_A + "_e")
        ]
        assert len(own_selected) == 1                     # only the best own-brand


class TestDiversifiedIdentityFlow:
    """Shelf-brand plumbing and check prioritization through the orchestrator."""

    @patch("app.tools.shelf_checker.check_shelf_visibility")
    @patch("app.agents.search_agent.find_browse_pages")
    async def test_competitor_from_generic_keyword_flows_to_selection(
        self, mock_find, mock_check
    ):
        # One generic keyword surfaces an own-brand, a competitor (via facet),
        # and a generic shelf. Ownership must survive to final selection.
        mock_find.return_value = [
            {"url": GENERIC_URL, "keyword": "dandruff shampoo", "position": 1,
             "title": "Dandruff Shampoo - Walmart.com"},
            {"url": PRODUCT_BRAND_URL, "keyword": "dandruff shampoo", "position": 2,
             "title": "Head & Shoulders - Walmart.com"},
            {"url": COMPETITOR_FACET_URL, "keyword": "dandruff shampoo", "position": 3,
             "title": "Shampoo - Walmart.com"},
        ]
        state = _make_state(include_branded=True)
        state.recommended_result_count = 3
        state.keywords_pending = ["dandruff shampoo"]
        await _call_search(state, {"keywords": ["dandruff shampoo"]})

        by_url = {p.url: p for p in state.pages_discovered}
        # Brand carried from the classifier at discovery.
        assert by_url[PRODUCT_BRAND_URL].shelf_brand == BRAND
        assert by_url[COMPETITOR_FACET_URL].shelf_brand == "OGX"
        assert by_url[GENERIC_URL].shelf_brand == ""

        relevance = {GENERIC_URL: 0.5, PRODUCT_BRAND_URL: 0.99, COMPETITOR_FACET_URL: 0.6}
        for p in state.pages_discovered:
            p.relevance_score = relevance[p.url]

        async def fake_check(pages, product_id, brand, known_brands=()):
            # No branded_shelf / shelf_brand keys → the discovery-time brand of
            # the competitor facet URL (OGX) must be preserved, not wiped.
            return {
                "found": len(pages), "missing": 0, "invalid": 0,
                "details": {
                    p["url"]: {
                        "visibility": True, "discoverability": True,
                        "organic": True, "sponsored": False,
                        "placement_rank": None, "placements": [],
                        "brand": True, "product": True, "page": 1,
                    }
                    for p in pages
                },
            }

        mock_check.side_effect = fake_check
        await _call_check_shelf(state, {"max_pages": 5})

        pools_own, pools_comp, pools_gen = state._partition_pools(state.found_pages)
        assert [sr.page_url for sr in pools_own] == [PRODUCT_BRAND_URL]
        assert [sr.page_url for sr in pools_comp] == [COMPETITOR_FACET_URL]
        assert [sr.page_url for sr in pools_gen] == [GENERIC_URL]

        report = state.to_final_report()
        rows = {sr["url"]: sr for sr in report["shelf_results"]}
        assert set(rows) == {PRODUCT_BRAND_URL, COMPETITOR_FACET_URL, GENERIC_URL}
        assert rows[COMPETITOR_FACET_URL]["shelf_brand"] == "OGX"
        assert rows[PRODUCT_BRAND_URL]["shelf_brand"] == BRAND
        assert rows[GENERIC_URL]["is_branded_shelf"] is False

    @patch("app.tools.shelf_checker.check_shelf_visibility")
    async def test_nonown_candidate_checked_before_extra_own_brand(self, mock_check):
        # An own-brand result is already admitted. A higher-ranked own-brand
        # candidate and a lower-ranked competitor candidate remain unchecked;
        # only one more may be checked. The competitor must win the fetch.
        state = _make_state(include_branded=True)
        state.recommended_result_count = 5
        state.found_pages = [_own(OWN_A, 0.99, 0)]   # own already represented
        state.pages_discovered = [
            BrowsePage(url=OWN_B, keyword="head & shoulders", relevance_score=0.95,
                       is_branded=True, shelf_brand=BRAND),
            BrowsePage(url=COMP_A, keyword="dandruff shampoo", relevance_score=0.30,
                       is_branded=True, shelf_brand="OGX"),
        ]

        captured = {}

        async def fake_check(pages, product_id, brand, known_brands=()):
            captured["urls"] = [p["url"] for p in pages]
            return {"found": 0, "missing": len(pages), "invalid": 0, "details": {
                p["url"]: {
                    "visibility": False, "discoverability": False,
                    "organic": False, "sponsored": False,
                    "placement_rank": None, "placements": [],
                    "brand": None, "product": False, "page": 0,
                } for p in pages}}

        mock_check.side_effect = fake_check
        await _call_check_shelf(state, {"max_pages": 1})

        # The competitor was fetched first despite its lower relevance, because
        # the analyzed brand was already represented.
        assert captured["urls"] == [COMP_A]
