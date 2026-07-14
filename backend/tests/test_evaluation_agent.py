"""Browse page ranking via evaluate_and_rank."""

from unittest.mock import patch

import pytest

from app.agents.evaluation_agent import evaluate_and_rank


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
