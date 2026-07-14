"""Browse URL acceptance rules (search_api + search_agent host guard)."""

import pytest

from app.agents.search_agent import _is_allowed_host
from app.tools.search_api import _is_valid_walmart_browse_url
from tests.conftest import INVALID_BROWSE_URLS, VALID_BROWSE_URL


class TestBrowseUrlValidation:
    def test_valid_browse_url_accepted(self) -> None:
        assert _is_valid_walmart_browse_url(VALID_BROWSE_URL) is True
        assert _is_allowed_host(VALID_BROWSE_URL) is True

    @pytest.mark.parametrize("url", INVALID_BROWSE_URLS)
    def test_invalid_browse_urls_rejected(self, url: str) -> None:
        assert _is_valid_walmart_browse_url(url) is False
