"""
Tests for the configurable "Recommended Category Page Results" setting
(recommended_result_count): endpoint validation, SessionState wiring,
orchestrator stopping behavior, final-row selection, and report contract.

All external calls (LLM, Serper search, Walmart scraping) are mocked.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.agents.evaluation_agent import RELEVANCE_SCORE_KEY
from app.agents.orchestrator import (
    _call_check_shelf,
    _check_stopping_conditions,
    _is_noop_choice,
    _rule_based_decision,
    run_react_loop,
)
from app.models.session_state import (
    BrowsePage,
    SessionState,
    ShelfResult,
    RECOMMENDED_RESULT_COUNT_DEFAULT,
    RECOMMENDED_RESULT_COUNT_MAX,
    RECOMMENDED_RESULT_COUNT_MIN,
)
from tests.conftest import assert_ranked_contract

BROWSE = "https://www.walmart.com/browse/food/snacks"


def _shelf_result(
    idx: int,
    found: bool = False,
    url: str | None = None,
    keyword: str = "kw",
    position: int = 0,
    relevance: float = 0.0,
) -> ShelfResult:
    return ShelfResult(
        page_url=url or f"{BROWSE}/{idx}",
        product_found=found,
        page_number_found=1 if found else 0,
        visibility=found,
        discoverability=found,
        keyword=keyword,
        position=position,
        relevance_score=relevance,
        discovery_index=idx,
    )


# ===========================================================================
# A. Defaults and endpoint validation
# ===========================================================================

class TestEndpointValidation:
    """/v2/analyze/stream must enforce the 3-10 integer contract."""

    def _client_with_stub(self, monkeypatch, captured: dict) -> TestClient:
        async def fake_workflow(url, **kwargs):
            captured.update(kwargs)
            yield {"event": "complete", "data": {}}

        import app.services.workflow_v2 as wf
        monkeypatch.setattr(wf, "run_v2_workflow", fake_workflow)
        return TestClient(app)

    def test_omitted_defaults_to_five(self, monkeypatch):
        captured: dict = {}
        client = self._client_with_stub(monkeypatch, captured)
        resp = client.get("/v2/analyze/stream", params={"url": "https://www.walmart.com/ip/x/1"})
        assert resp.status_code == 200
        assert captured["recommended_result_count"] == 5

    @pytest.mark.parametrize("value", [3, 5, 10])
    def test_valid_values_accepted(self, monkeypatch, value):
        captured: dict = {}
        client = self._client_with_stub(monkeypatch, captured)
        resp = client.get(
            "/v2/analyze/stream",
            params={"url": "https://www.walmart.com/ip/x/1", "recommended_result_count": value},
        )
        assert resp.status_code == 200
        assert captured["recommended_result_count"] == value

    @pytest.mark.parametrize("value", ["0", "-1", "2", "11", "100", "4.5", "abc", ""])
    def test_invalid_values_rejected(self, monkeypatch, value):
        captured: dict = {}
        client = self._client_with_stub(monkeypatch, captured)
        resp = client.get(
            "/v2/analyze/stream",
            params={"url": "https://www.walmart.com/ip/x/1", "recommended_result_count": value},
        )
        assert resp.status_code == 422
        assert captured == {}  # workflow never invoked


class TestWorkflowValidationAndState:
    """run_v2_workflow guards the value and stores it on SessionState."""

    async def _run_workflow(self, monkeypatch, **kwargs):
        import app.services.workflow_v2 as wf

        captured: dict = {}

        async def fake_loop(state):
            captured["state"] = state
            yield {"event": "complete", "data": state.to_final_report()}

        monkeypatch.setattr(wf, "run_react_loop", fake_loop)
        monkeypatch.setattr(wf, "get_initial_keywords", lambda state, provider=None: (["kw"], 0.0))
        monkeypatch.setattr(wf, "_persist_results", AsyncMock())
        monkeypatch.setattr(
            "app.tools.scraper.fetch_product_content",
            lambda url: {
                "title": "Test Product", "brand": "Acme", "id": "123",
                "description": "", "features": [], "image": "", "price": "",
            },
        )

        events = []
        async for ev in wf.run_v2_workflow(url="https://www.walmart.com/ip/x/123", **kwargs):
            events.append(ev)
        return events, captured

    async def test_setting_reaches_session_state(self, monkeypatch):
        _, captured = await self._run_workflow(monkeypatch, recommended_result_count=7)
        assert captured["state"].recommended_result_count == 7

    async def test_default_is_five(self, monkeypatch):
        _, captured = await self._run_workflow(monkeypatch)
        assert captured["state"].recommended_result_count == RECOMMENDED_RESULT_COUNT_DEFAULT == 5

    async def test_loop_start_announces_target(self, monkeypatch):
        events, _ = await self._run_workflow(monkeypatch, recommended_result_count=10)
        loop_start = next(e for e in events if e["event"] == "loop_start")
        assert loop_start["config"]["recommended_result_count"] == 10
        assert "Target: 10 recommended category pages" in loop_start["message"]

    @pytest.mark.parametrize("bad", [2, 11, 0, -3, 4.5, "7", None, True])
    async def test_invalid_values_yield_error(self, monkeypatch, bad):
        events, captured = await self._run_workflow(monkeypatch, recommended_result_count=bad)
        assert events[0]["event"] == "error"
        assert "recommended_result_count" in events[0]["message"]
        assert "state" not in captured  # loop never started

    def test_constants(self):
        assert RECOMMENDED_RESULT_COUNT_MIN == 3
        assert RECOMMENDED_RESULT_COUNT_DEFAULT == 5
        assert RECOMMENDED_RESULT_COUNT_MAX == 10


# ===========================================================================
# B. Exact result limits and selection
# ===========================================================================

class TestSelectionAndReport:
    def test_report_caps_rows_at_requested_count(self):
        state = SessionState(recommended_result_count=3)
        for i in range(7):
            (state.found_pages if i % 2 else state.missing_pages).append(_shelf_result(i))

        report = state.to_final_report()
        assert len(report["shelf_results"]) == 3
        assert report["recommended_result_count_requested"] == 3
        assert report["recommended_result_count_returned"] == 3

    def test_ten_rows_returned_when_available(self):
        state = SessionState(recommended_result_count=10)
        for i in range(12):
            state.missing_pages.append(_shelf_result(i))

        report = state.to_final_report()
        assert len(report["shelf_results"]) == 10
        assert report["recommended_result_count_returned"] == 10

    def test_duplicate_urls_count_once(self):
        state = SessionState(recommended_result_count=5)
        state.found_pages.append(_shelf_result(0, found=True, url=f"{BROWSE}/1"))
        state.missing_pages.append(_shelf_result(1, url=f"{BROWSE}/1"))       # exact dup
        state.missing_pages.append(_shelf_result(2, url=f"{BROWSE}/1/"))      # trailing slash dup
        state.missing_pages.append(_shelf_result(3, url=f"{BROWSE}/2"))

        assert state.eligible_result_count == 2
        report = state.to_final_report()
        assert len(report["shelf_results"]) == 2
        urls = {r["url"].rstrip("/") for r in report["shelf_results"]}
        assert urls == {f"{BROWSE}/1", f"{BROWSE}/2"}

    def test_repeated_keywords_with_distinct_urls_all_count(self):
        state = SessionState(recommended_result_count=5)
        for i in range(4):
            state.missing_pages.append(_shelf_result(i, keyword="same keyword"))
        assert state.eligible_result_count == 4
        assert len(state.to_final_report()["shelf_results"]) == 4

    def test_brand_filtered_counterpart_is_not_a_second_row(self):
        # One ShelfResult carries BOTH shelf views (base visibility +
        # brand-filtered discoverability); it must count exactly once.
        state = SessionState(recommended_result_count=5)
        sr = _shelf_result(0, found=True)
        sr.visibility = True
        sr.discoverability = True
        sr.brand_found = True
        state.found_pages.append(sr)

        assert state.eligible_result_count == 1
        report = state.to_final_report()
        assert len(report["shelf_results"]) == 1
        row = report["shelf_results"][0]
        assert row["visibility"] is True and row["discoverability"] is True

    def test_selection_prefers_relevance_then_position_then_discovery(self):
        state = SessionState(recommended_result_count=3)
        state.missing_pages.append(_shelf_result(0, url=f"{BROWSE}/low", relevance=0.2, position=1))
        state.found_pages.append(_shelf_result(1, found=True, url=f"{BROWSE}/high", relevance=0.9, position=8))
        state.missing_pages.append(_shelf_result(2, url=f"{BROWSE}/mid-pos2", relevance=0.5, position=2))
        state.missing_pages.append(_shelf_result(3, url=f"{BROWSE}/mid-pos1", relevance=0.5, position=1))
        state.missing_pages.append(_shelf_result(4, url=f"{BROWSE}/mid-pos9", relevance=0.5, position=9))

        report = state.to_final_report()
        urls = [r["url"] for r in report["shelf_results"]]
        # relevance desc, then position asc; the 0.2 row and pos-9 row are cut
        assert urls == [f"{BROWSE}/high", f"{BROWSE}/mid-pos1", f"{BROWSE}/mid-pos2"]

    def test_selected_order_preserved_not_regrouped_by_found(self):
        # A missing page with better relevance must stay ahead of a found page.
        state = SessionState(recommended_result_count=2)
        state.found_pages.append(_shelf_result(0, found=True, url=f"{BROWSE}/found", relevance=0.4))
        state.missing_pages.append(_shelf_result(1, url=f"{BROWSE}/missing", relevance=0.8))

        rows = state.to_final_report()["shelf_results"]
        assert [r["url"] for r in rows] == [f"{BROWSE}/missing", f"{BROWSE}/found"]
        assert [r["found"] for r in rows] == [False, True]


# ===========================================================================
# C/D. Orchestrator behavior
# ===========================================================================

def _make_state(count: int = 5, **kwargs) -> SessionState:
    state = SessionState(recommended_result_count=count, **kwargs)
    state.product.title = "Test Product"
    state.product.brand = "Acme"
    state.product.product_id = "123"
    return state


class TestStoppingConditions:
    def test_stops_when_requested_count_reached(self):
        state = _make_state(count=3)
        for i in range(3):
            state.missing_pages.append(_shelf_result(i))
        should_stop, reason = _check_stopping_conditions(state)
        assert should_stop is True
        assert reason.startswith("recommended_result_count_reached")

    def test_does_not_stop_on_raw_discovered_pages(self):
        # 8 raw candidates discovered but zero checked rows → keep going.
        state = _make_state(count=5, max_rounds=10)
        state.keywords_pending = ["kw"]
        for i in range(8):
            state.pages_discovered.append(BrowsePage(url=f"{BROWSE}/{i}"))
        should_stop, _ = _check_stopping_conditions(state)
        assert should_stop is False

    def test_missing_target_alone_no_longer_stops(self):
        # Old behavior stopped at target_missing_count; now only the row
        # target (or limits/exhaustion) completes the run.
        state = _make_state(count=8, target_missing_count=3, max_rounds=10)
        state.keywords_pending = ["kw"]
        for i in range(3):
            state.missing_pages.append(_shelf_result(i))
        should_stop, _ = _check_stopping_conditions(state)
        assert should_stop is False

    def test_round_limit_still_stops(self):
        state = _make_state(count=10, max_rounds=2)
        state.round_number = 2
        should_stop, reason = _check_stopping_conditions(state)
        assert should_stop is True and reason.startswith("round_limit")

    def test_budget_limit_still_stops(self):
        state = _make_state(count=10, max_rounds=10)
        state.keywords_pending = ["kw"]
        state.budget_limit = 0.10
        state.total_openai_cost = 0.11
        should_stop, reason = _check_stopping_conditions(state)
        assert should_stop is True and reason.startswith("budget_limit")

    def test_keyword_exhaustion_still_stops(self):
        state = _make_state(count=10, max_rounds=10)
        state.keyword_expansion_level = 3
        state.keywords_pending = []
        should_stop, reason = _check_stopping_conditions(state)
        assert should_stop is True and reason.startswith("keywords_exhausted")

    def test_rule_based_check_shelf_requests_remaining_rows(self):
        state = _make_state(count=9)
        for i in range(12):
            state.pages_discovered.append(
                BrowsePage(url=f"{BROWSE}/{i}", relevance_score=0.5)
            )
        for i in range(4):
            state.missing_pages.append(_shelf_result(i))
        tool, args = _rule_based_decision(state)
        assert tool == "check_shelf"
        assert args["max_pages"] == 5  # 9 requested - 4 ready


class TestCheckShelfBatching:
    def _stats_for(self, pages, valid=True, found=False):
        return {
            "found": 0,
            "missing": len(pages),
            "details": {
                p["url"]: (
                    {
                        "product": found, "brand": None, "page": 1 if found else 0,
                        "visibility": found, "discoverability": found, "placements": [],
                    }
                    if valid else None
                )
                for p in pages
            },
        }

    async def test_batch_can_exceed_five(self):
        state = _make_state(count=10)
        for i in range(10):
            state.pages_discovered.append(
                BrowsePage(url=f"{BROWSE}/{i}", keyword="kw", relevance_score=0.9 - i * 0.01)
            )

        captured: dict = {}

        async def fake_check(pages, product_id, brand, known_brands=()):
            captured["pages"] = pages
            return self._stats_for(pages)

        with patch("app.tools.shelf_checker.check_shelf_visibility", new=fake_check):
            result = await _call_check_shelf(state, {})  # no max_pages → remaining needed

        assert result.success is True
        assert len(captured["pages"]) == 10  # not capped at 5
        assert state.eligible_result_count == 10

    async def test_invalid_pages_do_not_count(self):
        state = _make_state(count=5)
        for i in range(5):
            state.pages_discovered.append(
                BrowsePage(url=f"{BROWSE}/{i}", keyword="kw", relevance_score=0.5)
            )

        async def fake_check(pages, product_id, brand, known_brands=()):
            stats = self._stats_for(pages)
            # First two pages don't exist on Walmart
            for p in pages[:2]:
                stats["details"][p["url"]] = None
            return stats

        with patch("app.tools.shelf_checker.check_shelf_visibility", new=fake_check):
            result = await _call_check_shelf(state, {"max_pages": 5})

        assert result.success is True
        assert result.data["invalid"] == 2
        assert state.eligible_result_count == 3
        should_stop, _ = _check_stopping_conditions(state)
        assert should_stop is False  # 3 of 5 → keep going


class TestFullLoop:
    """End-to-end run_react_loop with the rule-based fallback (LLM mocked out)."""

    def _run(self, state, search_pages, valid_urls, found_urls=frozenset()):
        async def collect():
            events = []

            def fake_search(keywords):
                return search_pages

            def fake_evaluate(product, pages):
                """
                Stand-in for evaluate_and_rank. Mirrors the real return
                contract exactly — {url, keyword, position, title,
                relevance_score}, every candidate present, sorted by score
                descending — and self-checks against the same shared assertion
                the real function is held to, so this double cannot drift away
                from production again.
                """
                ranked = [
                    {
                        "url": p["url"],
                        "keyword": p.get("keyword", ""),
                        "position": p.get("position"),
                        "title": p.get("title", ""),
                        RELEVANCE_SCORE_KEY: round(0.9 - i * 0.05, 4),
                    }
                    for i, p in enumerate(pages)
                ]
                ranked.sort(key=lambda r: -r[RELEVANCE_SCORE_KEY])
                assert_ranked_contract(ranked, len(pages))
                return ranked, 0.9, 0.0

            async def fake_check(pages, product_id, brand, known_brands=()):
                details = {}
                for p in pages:
                    url = p["url"]
                    if url not in valid_urls:
                        details[url] = None
                        continue
                    found = url in found_urls
                    details[url] = {
                        "product": found, "brand": True, "page": 1 if found else 0,
                        "visibility": found, "discoverability": found, "placements": [],
                    }
                return {"found": 0, "missing": 0, "details": details}

            with patch("app.agents.orchestrator.call_llm", return_value=None), \
                 patch("app.agents.search_agent.find_browse_pages", new=fake_search), \
                 patch("app.agents.evaluation_agent.evaluate_and_rank", new=fake_evaluate), \
                 patch("app.tools.shelf_checker.check_shelf_visibility", new=fake_check):
                async for ev in run_react_loop(state):
                    events.append(ev)
            return events

        return collect()

    async def test_reaches_requested_count_of_eight(self):
        # More than five raw candidates; agent must not stop at five rows.
        state = _make_state(count=8, max_rounds=10, target_missing_count=3)
        state.keywords_pending = ["kw"]
        pages = [
            {"url": f"{BROWSE}/{i}", "keyword": "kw", "position": i + 1}
            for i in range(12)
        ]
        events = await self._run(state, pages, valid_urls={p["url"] for p in pages})

        complete = events[-1]
        assert complete["event"] == "complete"
        assert complete["stop_reason"].startswith("recommended_result_count_reached")
        report = complete["data"]
        assert len(report["shelf_results"]) == 8
        assert report["recommended_result_count_returned"] == 8
        assert complete["rows_returned"] == 8
        assert complete["rows_requested"] == 8

    async def test_continues_past_invalid_candidates(self):
        # 5 of the best-ranked candidates are invalid; the loop must keep
        # checking further candidates until 5 valid rows are collected.
        state = _make_state(count=5, max_rounds=15, target_missing_count=3)
        state.keywords_pending = ["kw"]
        pages = [
            {"url": f"{BROWSE}/{i}", "keyword": "kw", "position": i + 1}
            for i in range(12)
        ]
        invalid = {f"{BROWSE}/{i}" for i in range(5)}
        valid = {p["url"] for p in pages} - invalid

        events = await self._run(state, pages, valid_urls=valid)

        complete = events[-1]
        assert complete["stop_reason"].startswith("recommended_result_count_reached")
        report = complete["data"]
        assert len(report["shelf_results"]) == 5
        assert not invalid & {r["url"] for r in report["shelf_results"]}

    async def test_shortfall_returns_available_rows_with_reason(self):
        # Only 4 valid pages exist and keywords are exhausted → return 4 of 10.
        state = _make_state(count=10, max_rounds=15)
        state.keywords_pending = ["kw"]
        state.keyword_expansion_level = 3  # no further expansion possible
        pages = [
            {"url": f"{BROWSE}/{i}", "keyword": "kw", "position": i + 1}
            for i in range(4)
        ]
        events = await self._run(state, pages, valid_urls={p["url"] for p in pages})

        complete = events[-1]
        assert complete["stop_reason"].startswith("keywords_exhausted")
        report = complete["data"]
        assert len(report["shelf_results"]) == 4
        assert report["recommended_result_count_requested"] == 10
        assert report["recommended_result_count_returned"] == 4
        # No duplicated rows to pad the shortfall
        urls = [r["url"] for r in report["shelf_results"]]
        assert len(urls) == len(set(urls))
        assert "Returned 4 of 10 requested category pages" in complete["message"]

    async def test_round_limit_stops_loop(self):
        state = _make_state(count=10, max_rounds=1)
        state.keywords_pending = ["kw"]
        events = await self._run(state, [], valid_urls=set())
        complete = events[-1]
        assert complete["event"] == "complete"
        assert complete["stop_reason"].startswith("round_limit")

    async def test_budget_limit_stops_loop(self):
        state = _make_state(count=10, max_rounds=10)
        state.keywords_pending = ["kw"]
        state.budget_limit = 0.05
        state.total_openai_cost = 0.06
        events = await self._run(state, [], valid_urls=set())
        complete = events[-1]
        assert complete["stop_reason"].startswith("budget_limit")

    async def test_goal_check_reports_rows_ready(self):
        state = _make_state(count=3, max_rounds=10)
        state.keywords_pending = ["kw"]
        pages = [
            {"url": f"{BROWSE}/{i}", "keyword": "kw", "position": i + 1}
            for i in range(5)
        ]
        events = await self._run(state, pages, valid_urls={p["url"] for p in pages})
        goal_events = [e for e in events if e["event"] == "goal_check"]
        assert goal_events
        assert all(e["rows_target"] == 3 for e in goal_events)
        assert goal_events[-1]["rows_ready"] == 3
        assert "category pages ready" in goal_events[-1]["message"]

    async def test_branded_on_still_respects_maximum(self):
        # With Include Branded Results on, branded + generic rows may both
        # count but the combined set must not exceed the requested maximum.
        state = _make_state(count=5, max_rounds=10)
        state.include_branded = True
        state.keywords_pending = ["generic kw", "Acme branded kw"]
        pages = [
            {"url": f"{BROWSE}/generic-{i}", "keyword": "generic kw", "position": i + 1}
            for i in range(4)
        ] + [
            {"url": f"{BROWSE}/branded-{i}", "keyword": "Acme branded kw", "position": i + 1}
            for i in range(4)
        ]
        events = await self._run(
            state, pages,
            valid_urls={p["url"] for p in pages},
            found_urls={f"{BROWSE}/branded-0"},
        )
        complete = events[-1]
        report = complete["data"]
        assert len(report["shelf_results"]) == 5
        assert report["recommended_result_count_returned"] == 5


class TestNoopGuard:
    """The orchestrator must not let the LLM loop on tools with no work."""

    def test_is_noop_choice_detection(self):
        state = _make_state()
        # evaluate with no unranked pages is a no-op
        assert _is_noop_choice(state, "evaluate", {}) is True
        state.pages_discovered.append(BrowsePage(url=f"{BROWSE}/1"))
        assert _is_noop_choice(state, "evaluate", {}) is False
        # check_shelf needs unchecked pages (the one above is unchecked)
        assert _is_noop_choice(state, "check_shelf", {}) is False
        state.pages_discovered[0].checked = True
        assert _is_noop_choice(state, "check_shelf", {}) is True
        # search needs keywords in the tool args
        assert _is_noop_choice(state, "search", {}) is True
        assert _is_noop_choice(state, "search", {"keywords": ["kw"]}) is False
        # expand_keywords is a no-op once every level is exhausted
        state.keyword_expansion_level = 3
        state.keywords_pending = []
        assert _is_noop_choice(state, "expand_keywords", {}) is True
        # stop is never a no-op
        assert _is_noop_choice(state, "stop", {}) is False

    async def test_llm_stuck_on_evaluate_is_overridden(self):
        # Reproduces the reported loop: every page is already ranked, but the
        # LLM keeps choosing 'evaluate'. The guard must reroute each round to
        # the rule-based choice so the run still completes with real rows.
        state = _make_state(count=3, max_rounds=10)
        for i in range(5):
            state.pages_discovered.append(
                BrowsePage(url=f"{BROWSE}/{i}", keyword="kw", relevance_score=0.9 - i * 0.05)
            )

        class _StuckOnEvaluate:
            tool_name = "evaluate"
            tool_args = {"reasoning": "There are 29 unranked pages..."}

            def cost_usd(self):
                return 0.0

        async def fake_check(pages, product_id, brand, known_brands=()):
            return {
                "found": 0, "missing": len(pages),
                "details": {
                    p["url"]: {
                        "product": False, "brand": True, "page": 0,
                        "visibility": False, "discoverability": False,
                        "placements": [],
                    }
                    for p in pages
                },
            }

        events = []
        with patch("app.agents.orchestrator.call_llm", return_value=_StuckOnEvaluate()), \
             patch("app.tools.shelf_checker.check_shelf_visibility", new=fake_check):
            async for ev in run_react_loop(state):
                events.append(ev)

        complete = events[-1]
        assert complete["event"] == "complete"
        assert complete["stop_reason"].startswith("recommended_result_count_reached")
        assert len(complete["data"]["shelf_results"]) == 3
        # The wasted-round pattern is gone: check_shelf ran in round 1
        first_tool = next(e for e in events if e["event"] == "tool_selected")
        assert first_tool["tool"] == "check_shelf"
        assert "No-op override" in first_tool["reasoning"]

    def test_summary_exposes_unranked_and_ready_counts(self):
        state = _make_state()
        state.pages_discovered.append(BrowsePage(url=f"{BROWSE}/1"))
        state.pages_discovered.append(BrowsePage(url=f"{BROWSE}/2", relevance_score=0.7))
        summary = state.to_summary_dict()
        assert summary["pages_unranked"] == 1
        assert summary["pages_ranked_unchecked"] == 1


# ===========================================================================
# E. Reporting consistency
# ===========================================================================

class TestReportContract:
    def test_returned_count_equals_len_and_stats_use_selected_rows(self):
        state = SessionState(recommended_result_count=3)
        for i in range(6):
            state.found_pages.append(
                _shelf_result(i, found=True, relevance=0.9 - i * 0.1)
            )
        for i in range(6, 9):
            state.missing_pages.append(_shelf_result(i, relevance=0.9 - i * 0.1))

        report = state.to_final_report()
        rows = report["shelf_results"]
        assert len(rows) == 3
        assert report["recommended_result_count_returned"] == len(rows)
        assert len(rows) <= report["recommended_result_count_requested"]
        # Dashboard totals derive from the same 3 selected rows
        stats = report["shelf_stats"]
        assert stats["total"] == 3
        assert stats["found"] == sum(1 for r in rows if r["discoverability"])
        assert stats["missing"] == 3 - stats["found"]
        assert set(stats["details"]) <= {r["url"] for r in rows}

    def test_persistence_uses_selected_rows(self):
        # _persist_results reads report["shelf_results"]; verify the URLs it
        # would save match the truncated selection exactly.
        state = SessionState(recommended_result_count=3)
        for i in range(7):
            state.missing_pages.append(_shelf_result(i, relevance=1.0 - i * 0.1))
        report = state.to_final_report()
        persisted_urls = [sr["url"] for sr in report["shelf_results"]]
        assert len(persisted_urls) == 3
        assert persisted_urls == [f"{BROWSE}/0", f"{BROWSE}/1", f"{BROWSE}/2"]

    def test_empty_state_reports_zero_returned(self):
        state = SessionState(recommended_result_count=5)
        report = state.to_final_report()
        assert report["shelf_results"] == []
        assert report["recommended_result_count_returned"] == 0
        assert report["recommended_result_count_requested"] == 5
