"""Placement-level workflow and report regression tests."""

from unittest.mock import AsyncMock, patch

from app.agents.orchestrator import _call_check_shelf
from app.models.session_state import (
    BrowsePage,
    ProductPlacement,
    SessionState,
    ShelfResult,
)


def _mixed_placements() -> list[ProductPlacement]:
    return [
        ProductPlacement(
            placement_index=1,
            placement_rank=3,
            visibility=True,
            discoverability=False,
            sponsored=True,
            organic=False,
        ),
        ProductPlacement(
            placement_index=2,
            placement_rank=9,
            visibility=True,
            discoverability=True,
            sponsored=False,
            organic=True,
        ),
    ]


def test_final_report_counts_organic_and_sponsored_impressions_independently() -> None:
    state = SessionState()
    state.found_pages.append(ShelfResult(
        page_url="https://www.walmart.com/browse/example/1",
        product_found=True,
        visibility=True,
        discoverability=True,
        organic=True,
        sponsored=True,
        placement_rank=3,
        placements=_mixed_placements(),
    ))

    report = state.to_final_report()

    result = report["shelf_results"][0]
    assert result["organic"] is True
    assert result["sponsored"] is True
    assert result["placement_rank"] == 3
    assert result["placements"][0]["placement_rank"] == 3
    assert len(result["placements"]) == 2
    assert report["shelf_stats"]["placements"] == 2
    assert report["shelf_stats"]["organic"] == 1
    assert report["shelf_stats"]["sponsored"] == 1
    assert report["shelf_stats"]["organic_pages"] == 1
    assert report["shelf_stats"]["sponsored_pages"] == 1


async def test_orchestrator_does_not_make_placement_types_mutually_exclusive() -> None:
    url = "https://www.walmart.com/browse/example/1"
    state = SessionState()
    state.product.product_id = "123"
    state.product.brand = "Brand"
    state.pages_discovered.append(BrowsePage(
        url=url,
        keyword="example",
        relevance_score=1.0,
    ))
    placements = [placement.to_dict() for placement in _mixed_placements()]
    stats = {
        "found": 1,
        "missing": 0,
        "details": {
            url: {
                "product": True,
                "brand": True,
                "page": 1,
                "visibility": True,
                "discoverability": True,
                "organic": True,
                "sponsored": True,
                "placement_rank": 3,
                "placements": placements,
            }
        },
    }

    with patch(
        "app.tools.shelf_checker.check_shelf_visibility",
        new=AsyncMock(return_value=stats),
    ):
        result = await _call_check_shelf(state, {"max_pages": 1})

    assert result.success is True
    assert len(state.found_pages) == 1
    shelf_result = state.found_pages[0]
    assert shelf_result.organic is True
    assert shelf_result.sponsored is True
    assert shelf_result.placement_rank == 3
    assert len(shelf_result.placements) == 2
