"""Regression coverage for complete shelf URLs in diagnostic logs."""

import logging
from unittest.mock import patch

import pytest

from app.agents.orchestrator import _call_search
from app.models.session_state import ProductInfo, SessionState


BRANDED_URL = (
    "https://www.walmart.com/browse/beauty/dandruff-shampoo/leader/"
    "1085666_3147628_5752434_2927912_4339403/YnJhbmQ6TGVhZGVy"
)
GENERIC_URL = (
    "https://www.walmart.com/browse/beauty/dandruff-shampoo/"
    "1085666_3147628_5752434_2927912_4339403"
)


@pytest.mark.asyncio
async def test_search_logs_complete_rejected_and_admitted_urls(caplog) -> None:
    state = SessionState(include_branded=False)
    state.product = ProductInfo(
        title="Dandruff Shampoo",
        brand="Head & Shoulders",
        product_id="12345",
    )
    state.keywords_pending = ["dandruff shampoo"]

    raw_pages = [
        {
            "url": BRANDED_URL,
            "keyword": "dandruff shampoo",
            "position": 1,
            "title": "Leader Dandruff Shampoo - Walmart.com",
        },
        {
            "url": GENERIC_URL,
            "keyword": "dandruff shampoo",
            "position": 2,
            "title": "Dandruff Shampoo - Walmart.com",
        },
    ]

    caplog.set_level(logging.INFO, logger="app.agents.orchestrator")
    with patch(
        "app.agents.search_agent.find_browse_pages", return_value=raw_pages
    ):
        result = await _call_search(
            state, {"keywords": ["dandruff shampoo"]}
        )

    assert result.success
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.endswith(BRANDED_URL)
        and "Rejected branded shelf" in message
        for message in messages
    )
    assert any(
        message.endswith(f"url={GENERIC_URL}")
        and "BrowsePage created" in message
        for message in messages
    )
