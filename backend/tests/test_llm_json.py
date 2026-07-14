"""Tests for clean_json_text — robust JSON extraction from LLM responses.

Claude (and occasionally other models) wrap JSON in markdown code fences even
when told not to, which previously broke keyword generation (json.loads failed
and the agent silently fell back to rule-based title splitting).
"""

import json

from app.services.llm import clean_json_text


class TestCleanJsonText:
    def test_plain_json_unchanged(self) -> None:
        raw = '{"unbranded_keywords": ["walking shoes"]}'
        assert json.loads(clean_json_text(raw)) == {"unbranded_keywords": ["walking shoes"]}

    def test_strips_json_code_fence(self) -> None:
        raw = '```json\n{"unbranded_keywords": ["walking shoes", "sneakers"]}\n```'
        assert json.loads(clean_json_text(raw)) == {
            "unbranded_keywords": ["walking shoes", "sneakers"]
        }

    def test_strips_bare_code_fence(self) -> None:
        raw = '```\n{"keywords": ["a", "b"]}\n```'
        assert json.loads(clean_json_text(raw)) == {"keywords": ["a", "b"]}

    def test_strips_leading_prose(self) -> None:
        raw = 'Here is the JSON you requested:\n{"keywords": ["x"]}'
        assert json.loads(clean_json_text(raw)) == {"keywords": ["x"]}

    def test_handles_array_payload(self) -> None:
        raw = '```json\n["a", "b", "c"]\n```'
        assert json.loads(clean_json_text(raw)) == ["a", "b", "c"]

    def test_empty_string(self) -> None:
        assert clean_json_text("") == ""
