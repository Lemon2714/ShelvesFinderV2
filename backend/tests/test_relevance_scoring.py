"""
Regression tests for the relevance score reaching the final ranking.

The bug these pin: evaluate_and_rank computed a cosine-similarity score per
candidate but returned dicts carrying only url/keyword/position. The
orchestrator read it back with `rp.get("relevance_score", rp.get("score", 0.5))`
— neither key existed, so every page was assigned the literal fallback 0.5.
With every score tied, selected_shelf_results()'s sort key collapsed onto its
first tie-breaker, `position`: the report was ordered by raw Google/Serper rank
with no relevance input at all, and _call_check_shelf picked which shelves to
even fetch on the same relevance-blind ordering.
"""

from unittest.mock import patch

import pytest

from app.agents.evaluation_agent import RELEVANCE_SCORE_KEY
from app.agents.orchestrator import _call_check_shelf, _call_evaluate
from app.models.session_state import (
    BrowsePage,
    SessionState,
    ShelfResult,
    relevance_sort_key,
)

BROWSE = "https://www.walmart.com/browse"
BREAD = f"{BROWSE}/food/bread/1111"
TV = f"{BROWSE}/electronics/tv/2222"


def _state(pages: list[BrowsePage], **kwargs) -> SessionState:
    state = SessionState(**kwargs)
    state.product.title = "Sourdough Loaf"
    state.product.description = "Sliced sourdough bread"
    state.pages_discovered = list(pages)
    return state


def _evaluate_with(scores: list[tuple[str, float]]):
    """Run the REAL evaluate_and_rank with score_relevance stubbed."""
    from app.agents.evaluation_agent import evaluate_and_rank

    def fake_eval(product, pages):
        with patch("app.agents.evaluation_agent.score_relevance") as m:
            m.return_value = (scores, 0.0)
            return evaluate_and_rank(product, pages)

    return patch("app.agents.evaluation_agent.evaluate_and_rank", new=fake_eval)


class TestScoreSurvivesToBrowsePage:
    """The 0.5 collapse cannot come back."""

    async def test_distinct_scores_produce_distinct_relevance_scores(self) -> None:
        state = _state([
            BrowsePage(url=BREAD, keyword="bread", position=1, title="Bread"),
            BrowsePage(url=TV, keyword="bread", position=2, title="TVs"),
        ])

        with _evaluate_with([("Bread", 0.91), ("TVs", 0.12)]):
            result = await _call_evaluate(state, {})

        assert result.success is True
        scores = {p.url: p.relevance_score for p in state.pages_discovered}
        assert scores[BREAD] == pytest.approx(0.91)
        assert scores[TV] == pytest.approx(0.12)
        assert len(set(scores.values())) == 2, "scores must not collapse to one value"
        assert 0.5 not in scores.values(), "0.5 is the old silent fallback"

    async def test_real_search_result_title_is_scored_not_url_slug(self) -> None:
        """
        _call_evaluate used to build its payload as {url, keyword, position},
        dropping BrowsePage.title, so the evaluator scored a title
        reconstructed from the URL slug.
        """
        state = _state([
            BrowsePage(url=BREAD, keyword="bread", position=1,
                       title="Artisan Sourdough Bread"),
        ])
        seen: dict = {}

        def capture(product, pages):
            seen["pages"] = pages
            return [{**pages[0], RELEVANCE_SCORE_KEY: 0.7}], 0.7, 0.0

        with patch("app.agents.evaluation_agent.evaluate_and_rank", new=capture):
            await _call_evaluate(state, {})

        assert seen["pages"][0]["title"] == "Artisan Sourdough Bread"
        assert state.pages_discovered[0].relevance_score == pytest.approx(0.7)

    async def test_pages_discovered_sorted_best_first(self) -> None:
        state = _state([
            BrowsePage(url=TV, keyword="bread", position=1, title="TVs"),
            BrowsePage(url=BREAD, keyword="bread", position=2, title="Bread"),
        ])

        with _evaluate_with([("Bread", 0.91), ("TVs", 0.12)]):
            await _call_evaluate(state, {})

        assert [p.url for p in state.pages_discovered] == [BREAD, TV]


class TestUnscoredVsGenuineZero:
    """A measured 0.0 is not the same as 'never scored'."""

    def test_genuine_zero_is_not_unranked(self) -> None:
        state = _state([
            BrowsePage(url=BREAD, relevance_score=0.0),   # measured irrelevant
            BrowsePage(url=TV),                           # never scored
        ])
        assert [p.url for p in state.unranked_pages] == [TV]

    async def test_zero_scored_page_is_not_re_evaluated(self) -> None:
        """
        Under `relevance_score == 0.0` meaning 'unscored', a page the evaluator
        genuinely scored 0.0 would re-enter the evaluate queue every round,
        forever.
        """
        state = _state([
            BrowsePage(url=BREAD, keyword="bread", position=1, title="Bread"),
            BrowsePage(url=TV, keyword="bread", position=2, title="TVs"),
        ])

        with _evaluate_with([("Bread", 0.91), ("TVs", 0.0)]):
            await _call_evaluate(state, {})

        assert state.pages_discovered[-1].relevance_score == 0.0
        assert state.unranked_pages == [], "a scored 0.0 must not re-queue"

    def test_unscored_sorts_after_a_scored_zero(self) -> None:
        assert relevance_sort_key(0.9) < relevance_sort_key(0.0)
        assert relevance_sort_key(0.0) < relevance_sort_key(None)


class TestMissingKeyIsNotSilentlyDefaulted:
    async def test_missing_key_leaves_page_unscored_and_warns(self, caplog) -> None:
        """A missing score must never be replaced by something score-shaped."""
        state = _state([
            BrowsePage(url=BREAD, keyword="bread", position=1, title="Bread"),
            BrowsePage(url=TV, keyword="bread", position=2, title="TVs"),
        ])

        def no_score(product, pages):
            # One row scored, one row missing the key entirely.
            return (
                [
                    {"url": BREAD, "keyword": "bread", "position": 1,
                     RELEVANCE_SCORE_KEY: 0.8},
                    {"url": TV, "keyword": "bread", "position": 2},
                ],
                0.8,
                0.0,
            )

        with patch("app.agents.evaluation_agent.evaluate_and_rank", new=no_score):
            with caplog.at_level("WARNING"):
                result = await _call_evaluate(state, {})

        scores = {p.url: p.relevance_score for p in state.pages_discovered}
        assert scores[BREAD] == pytest.approx(0.8)
        assert scores[TV] is None, "must stay unscored, not get a fake number"
        assert result.success is True
        assert result.data["unscored_count"] == 1
        assert RELEVANCE_SCORE_KEY in caplog.text

    async def test_all_rows_missing_key_reports_tool_failure(self) -> None:
        """
        If nothing carries a score the evaluator is violating its contract.
        Report the tool as failed rather than leaving every page unscored,
        which would make the agent re-pick 'evaluate' until the round limit.
        """
        state = _state([
            BrowsePage(url=BREAD, keyword="bread", position=1, title="Bread"),
        ])

        def no_score(product, pages):
            return [{"url": BREAD, "keyword": "bread", "position": 1}], 0.0, 0.02

        with patch("app.agents.evaluation_agent.evaluate_and_rank", new=no_score):
            result = await _call_evaluate(state, {})

        assert result.success is False
        assert RELEVANCE_SCORE_KEY in (result.error or "")
        assert state.pages_discovered[0].relevance_score is None
        # Embedding spend still happened and is still accounted for.
        assert state.total_openai_cost == pytest.approx(0.02)


class TestRankingUsesRelevance:
    """The point of the fix: ordering is relevance-driven, not position-driven."""

    def _result(self, idx, url, position, relevance) -> ShelfResult:
        return ShelfResult(
            page_url=url,
            product_found=True,
            visibility=True,
            discoverability=True,
            keyword="kw",
            position=position,
            relevance_score=relevance,
            discovery_index=idx,
        )

    def test_relevance_outranks_search_position(self) -> None:
        state = SessionState(recommended_result_count=3)
        # Google rank is the exact inverse of true relevance.
        state.found_pages = [
            self._result(0, f"{BROWSE}/a", position=1, relevance=0.10),
            self._result(1, f"{BROWSE}/b", position=2, relevance=0.55),
            self._result(2, f"{BROWSE}/c", position=3, relevance=0.95),
        ]

        order = [sr.page_url for sr in state.selected_shelf_results()]
        assert order == [f"{BROWSE}/c", f"{BROWSE}/b", f"{BROWSE}/a"]

    def test_position_still_breaks_genuine_ties(self) -> None:
        state = SessionState(recommended_result_count=3)
        state.found_pages = [
            self._result(0, f"{BROWSE}/a", position=9, relevance=0.5),
            self._result(1, f"{BROWSE}/b", position=2, relevance=0.5),
        ]
        order = [sr.page_url for sr in state.selected_shelf_results()]
        assert order == [f"{BROWSE}/b", f"{BROWSE}/a"]

    def test_low_relevance_sorts_last_but_is_never_dropped(self) -> None:
        """Relevance ranks; it never filters."""
        state = SessionState(recommended_result_count=10)
        state.found_pages = [
            self._result(0, f"{BROWSE}/a", position=1, relevance=0.99),
            self._result(1, f"{BROWSE}/b", position=2, relevance=0.0),
            self._result(2, f"{BROWSE}/c", position=3, relevance=0.01),
        ]

        selected = state.selected_shelf_results()
        assert len(selected) == 3, "no row may be filtered out by low relevance"
        assert selected[-1].page_url == f"{BROWSE}/b"

    async def test_check_shelf_fetches_most_relevant_first(self) -> None:
        """Which shelves get fetched was relevance-blind for the same reason."""
        state = _state([
            BrowsePage(url=f"{BROWSE}/a", keyword="kw", position=1, relevance_score=0.1),
            BrowsePage(url=f"{BROWSE}/b", keyword="kw", position=2, relevance_score=0.9),
            BrowsePage(url=f"{BROWSE}/c", keyword="kw", position=3, relevance_score=0.5),
        ], recommended_result_count=5)
        captured: dict = {}

        async def fake_check(pages, product_id, brand, known_brands=()):
            captured["urls"] = [p["url"] for p in pages]
            return {"found": 0, "missing": len(pages), "details": {
                p["url"]: {"product": False, "brand": None, "page": 0,
                           "visibility": False, "discoverability": False,
                           "placements": []}
                for p in pages
            }}

        with patch("app.tools.shelf_checker.check_shelf_visibility", new=fake_check):
            await _call_check_shelf(state, {"max_pages": 2})

        assert captured["urls"] == [f"{BROWSE}/b", f"{BROWSE}/c"]


class TestReportSurfacesScore:
    def test_final_report_rows_carry_relevance_score(self) -> None:
        state = SessionState(recommended_result_count=5)
        state.found_pages = [
            ShelfResult(page_url=f"{BROWSE}/a", product_found=True, visibility=True,
                        discoverability=True, position=1, relevance_score=0.87,
                        discovery_index=0),
        ]
        state.missing_pages = [
            ShelfResult(page_url=f"{BROWSE}/b", product_found=False, position=2,
                        relevance_score=None, discovery_index=1),
        ]

        rows = state.to_final_report()["shelf_results"]

        assert [r["relevance_score"] for r in rows] == [0.87, None]
