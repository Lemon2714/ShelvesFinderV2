"""Search agent deduplication and host guard."""

from unittest.mock import patch

from app.agents.search_agent import find_browse_pages, _is_allowed_host


class TestIsAllowedHost:
    def test_accepts_www_and_bare_walmart(self) -> None:
        assert _is_allowed_host("https://www.walmart.com/browse/x")
        assert _is_allowed_host("https://walmart.com/browse/x")

    def test_rejects_business_subdomain(self) -> None:
        assert not _is_allowed_host("https://business.walmart.com/browse/x")


class TestFindBrowsePages:
    @patch("app.agents.search_agent.search_walmart_browse")
    def test_deduplicates_url_keeps_best_position(self, mock_search) -> None:
        shared = "https://www.walmart.com/browse/clothing/dresses/1"
        mock_search.side_effect = [
            [{"url": shared, "position": 5}],
            [{"url": shared, "position": 2}],
        ]

        pages = find_browse_pages(["cocktail dresses", "party dresses"])

        assert len(pages) == 1
        assert pages[0]["url"] == shared
        assert pages[0]["position"] == 2
        assert pages[0]["keyword"] == "party dresses"

    @patch("app.agents.search_agent.search_walmart_browse")
    def test_retains_result_title_metadata(self, mock_search) -> None:
        mock_search.return_value = [
            {"url": "https://www.walmart.com/browse/beauty/shampoo/1",
             "position": 1, "title": "Shampoo - Walmart.com"},
        ]

        pages = find_browse_pages(["shampoo"])

        assert pages[0]["title"] == "Shampoo - Walmart.com"

    @patch("app.agents.search_agent.search_walmart_browse")
    def test_drops_disallowed_host_after_search(self, mock_search) -> None:
        mock_search.return_value = [
            {"url": "https://business.walmart.com/browse/x", "position": 1},
            {"url": "https://www.walmart.com/browse/clothing/dresses/1", "position": 2},
        ]

        pages = find_browse_pages(["dresses"])

        assert len(pages) == 1
        assert "www.walmart.com" in pages[0]["url"]
