"""Browse page ranking via evaluate_and_rank."""

from unittest.mock import patch

import pytest

from app.agents.evaluation_agent import evaluate_and_rank, RELEVANCE_SCORE_KEY
from tests.conftest import assert_ranked_contract


class TestEvaluateAndRank:
    @patch("app.agents.evaluation_agent.score_relevance")
    def test_ranks_by_relevance_and_computes_confidence(
        self, mock_score, sample_browse_urls
    ) -> None:
        # Titles must match evaluate_and_rank URL → page_title extraction
        mock_score.return_value = (
            [
                ("clothing womens dresses cocktail dresses 1234", 0.9),
                ("clothing womens clothing dresses 5678", 0.7),
            ],
            0.01,
        )

        product_info = {
            "title": "Wedding Guest Midi Dress Cocktail Formal",
            "description": "Elegant black dress for parties.",
        }

        ranked, confidence, cost = evaluate_and_rank(product_info, sample_browse_urls)

        assert len(ranked) == 2
        assert ranked[0]["url"] == sample_browse_urls[0]["url"]
        assert ranked[0]["keyword"] == "cocktail dresses"
        assert confidence == pytest.approx(0.8)  # avg of 0.9 and 0.7
        assert cost == 0.01

    def test_empty_candidates_returns_zeros(self) -> None:
        ranked, confidence, cost = evaluate_and_rank(
            {"title": "Dress", "description": ""}, []
        )
        assert ranked == []
        assert confidence == 0.0
        assert cost == 0.0


PRODUCT = {"title": "Sourdough Loaf", "description": "Sliced sourdough bread"}


class TestReturnContract:
    """
    Pins the shape evaluate_and_rank returns.

    This is the seam where the relevance score used to be dropped: the function
    computed scores into a local list and returned dicts carrying only
    url/keyword/position, so the orchestrator's lookup fell through to a literal
    0.5 for every page and the final ranking silently collapsed onto raw search
    position. Any test double standing in for evaluate_and_rank must satisfy
    these same assertions — see
    test_recommended_result_count.py::fake_evaluate.
    """

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_every_ranked_row_carries_the_documented_score_key(self, mock_score) -> None:
        mock_score.return_value = ([("Bread", 0.8), ("TVs", 0.2)], 0.0)

        ranked, _, _ = evaluate_and_rank(
            PRODUCT,
            [
                {"url": "https://www.walmart.com/browse/food/bread/1", "title": "Bread"},
                {"url": "https://www.walmart.com/browse/electronics/tv/2", "title": "TVs"},
            ],
        )

        assert RELEVANCE_SCORE_KEY == "relevance_score"
        # Same assertion every test double is held to (tests/conftest.py), so
        # the real function and its stand-ins cannot drift apart.
        assert_ranked_contract(ranked, candidate_count=2)
        assert {"url", "keyword", "position", "title", RELEVANCE_SCORE_KEY} == set(
            ranked[0]
        )
        for row in ranked:
            assert isinstance(row[RELEVANCE_SCORE_KEY], float)

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_scores_are_distinct_and_sorted_descending(self, mock_score) -> None:
        mock_score.return_value = ([("Bread", 0.8), ("TVs", 0.2)], 0.0)

        ranked, _, _ = evaluate_and_rank(
            PRODUCT,
            [
                {"url": "https://www.walmart.com/browse/electronics/tv/2", "title": "TVs"},
                {"url": "https://www.walmart.com/browse/food/bread/1", "title": "Bread"},
            ],
        )

        scores = [row[RELEVANCE_SCORE_KEY] for row in ranked]
        assert scores == [0.8, 0.2]
        assert ranked[0]["url"].endswith("/bread/1")
        assert len(set(scores)) == 2, "distinct inputs must stay distinct"

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_no_candidate_is_dropped_when_scorer_returns_partial(
        self, mock_score
    ) -> None:
        """
        A dropped candidate keeps relevance_score=None forever, so the agent
        re-queues it as 'unranked' every round until the round limit. Ranking
        must return every candidate it was given.
        """
        mock_score.return_value = ([("Bread", 0.8)], 0.0)   # TVs omitted

        ranked, _, _ = evaluate_and_rank(
            PRODUCT,
            [
                {"url": "https://www.walmart.com/browse/food/bread/1", "title": "Bread"},
                {"url": "https://www.walmart.com/browse/electronics/tv/2", "title": "TVs"},
            ],
        )

        assert len(ranked) == 2
        by_url = {row["url"]: row[RELEVANCE_SCORE_KEY] for row in ranked}
        assert by_url["https://www.walmart.com/browse/food/bread/1"] == 0.8
        # Unscored is None — never a substituted number, and sorted last.
        assert by_url["https://www.walmart.com/browse/electronics/tv/2"] is None
        assert ranked[-1]["url"].endswith("/tv/2")


class TestTitlePairing:
    @patch("app.agents.evaluation_agent.score_relevance")
    def test_colliding_titles_pair_scores_to_distinct_urls(self, mock_score) -> None:
        """
        Two distinct URLs can derive the same title (trailing slash, differing
        scheme, or a genuinely shared search-result title). Pairing scores back
        by title string matched whichever candidate was found first; pairing is
        by index so each URL keeps its own row and no URL is lost.
        """
        mock_score.return_value = (
            [("Bread", 0.9), ("Bread", 0.3)],
            0.0,
        )

        a = "https://www.walmart.com/browse/food/bread/3811"
        b = "https://www.walmart.com/browse/food/bread/3811/"
        ranked, _, _ = evaluate_and_rank(
            PRODUCT,
            [{"url": a, "title": "Bread"}, {"url": b, "title": "Bread"}],
        )

        assert len(ranked) == 2
        assert {row["url"] for row in ranked} == {a, b}
        # Both URLs survive with their own score; neither is overwritten or dropped.
        assert sorted(row[RELEVANCE_SCORE_KEY] for row in ranked) == [0.3, 0.9]

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_real_title_preferred_over_url_slug(self, mock_score) -> None:
        """The real search-result title is scored, not the URL slug."""
        mock_score.return_value = ([("Sourdough Bread Loaves", 0.9)], 0.0)

        evaluate_and_rank(
            PRODUCT,
            [{
                "url": "https://www.walmart.com/browse/food/bread/3811",
                "title": "Sourdough Bread Loaves",
            }],
        )

        scored_titles = mock_score.call_args[0][1]
        assert scored_titles == ["Sourdough Bread Loaves"]
        assert "3811" not in scored_titles[0]

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_falls_back_to_slug_when_title_absent(self, mock_score) -> None:
        mock_score.return_value = ([("food bread 3811", 0.9)], 0.0)

        evaluate_and_rank(
            PRODUCT,
            [{"url": "https://www.walmart.com/browse/food/bread/3811"}],
        )

        assert mock_score.call_args[0][1] == ["food bread 3811"]

    @patch("app.agents.evaluation_agent.score_relevance")
    def test_blank_title_falls_back_to_slug(self, mock_score) -> None:
        mock_score.return_value = ([("food bread 3811", 0.9)], 0.0)

        evaluate_and_rank(
            PRODUCT,
            [{"url": "https://www.walmart.com/browse/food/bread/3811", "title": "  "}],
        )

        assert mock_score.call_args[0][1] == ["food bread 3811"]
