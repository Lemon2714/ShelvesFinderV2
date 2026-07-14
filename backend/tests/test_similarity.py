"""Cosine similarity and keyword fallback scoring."""

from unittest.mock import MagicMock, patch

import pytest

from app.tools.similarity import (
    cosine_similarity,
    keyword_match_score,
    score_relevance,
)


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self) -> None:
        v = [1.0, 0.0, 1.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors_score_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_zero_vector_returns_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


class TestKeywordMatchScore:
    def test_overlapping_words_score_higher(self) -> None:
        product = "organic almond butter 16oz"
        good = "nut butters almond spreads"
        poor = "living room sofas"
        assert keyword_match_score(product, good) > keyword_match_score(product, poor)

    def test_empty_string_returns_zero(self) -> None:
        assert keyword_match_score("", "nut butters") == 0.0


class TestScoreRelevance:
    @patch("app.tools.similarity.get_openai_client")
    def test_keyword_fallback_when_no_openai_client(self, mock_client) -> None:
        mock_client.return_value = None

        product = "wedding guest midi dress cocktail formal"
        pages = ["clothing womens dresses cocktail dresses", "snacks crackers chips"]

        ranked, cost = score_relevance(product, pages)

        assert cost == 0.0
        assert len(ranked) == 2
        assert ranked[0][0] == "clothing womens dresses cocktail dresses"
        assert ranked[0][1] >= ranked[1][1]

    @patch("app.tools.similarity.get_embedding")
    @patch("app.tools.similarity.get_openai_client")
    def test_uses_embeddings_when_client_available(
        self, mock_client, mock_embedding
    ) -> None:
        mock_client.return_value = MagicMock()
        mock_embedding.side_effect = [
            ([1.0, 0.0], 0.001),
            ([1.0, 0.0], 0.001),
            ([0.0, 1.0], 0.001),
        ]

        ranked, cost = score_relevance("dress", ["formal dress", "crackers"])

        assert ranked[0][0] == "formal dress"
        assert ranked[0][1] == pytest.approx(1.0)
        assert cost > 0
