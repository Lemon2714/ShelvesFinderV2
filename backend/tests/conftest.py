"""
Shared fixtures and example Walmart product URLs for ShelvesFinder tests.

Use these URLs for manual UI testing and as expected values in unit tests.
"""

import pytest

# ---------------------------------------------------------------------------
# Example product URLs (from user / docs)
# ---------------------------------------------------------------------------

HTIGEA_DRESS_URL = (
    "https://www.walmart.com/ip/Htigea-Wedding-Guest-Midi-Dress-for-Women-"
    "Long-Sleeve-V-Neck-Tie-Waist-Bodycon-Dresses-Elegant-Formal-Party-"
    "Cocktail-Dress-Black-XL/19307773882"
)
HTIGEA_PRODUCT_ID = "19307773882"
HTIGEA_SLUG_TITLE = (
    "Htigea Wedding Guest Midi Dress for Women Long Sleeve V Neck Tie Waist "
    "Bodycon Dresses Elegant Formal Party Cocktail Dress Black XL"
)

SOURDOUGH_URL = (
    "https://www.walmart.com/ip/Essential-Hawaiian-Sliced-Sourdough-Loaf-"
    "Non-GMO-16-oz/12928764204?classType=REGULAR&adsRedirect=true"
)
SOURDOUGH_PRODUCT_ID = "12928764204"
SOURDOUGH_SLUG_TITLE = (
    "Essential Hawaiian Sliced Sourdough Loaf Non GMO 16 oz"
)

BROCCOLI_URL = (
    "https://www.walmart.com/ip/Simplot-IQF-Broccoli-Florets-32-oz-package-"
    "12-packages-per-case/504628592?classType=REGULAR&from=/search"
)
BROCCOLI_PRODUCT_ID = "504628592"
BROCCOLI_SLUG_TITLE = (
    "Simplot IQF Broccoli Florets 32 oz package 12 packages per case"
)

# Invalid / edge-case URLs for negative tests
INVALID_URLS = [
    ("not-a-url", "malformed"),
    ("https://www.amazon.com/dp/B000", "wrong retailer"),
    ("https://www.walmart.com/", "no product path"),
    ("https://business.walmart.com/ip/foo/123", "disallowed host on browse"),
]

# Browse URL examples for search / evaluate tests
VALID_BROWSE_URL = (
    "https://www.walmart.com/browse/food/frozen-foods/frozen-vegetables/broccoli/1234"
)
INVALID_BROWSE_URLS = [
    "https://www.walmart.com/ip/Simplot-Broccoli/504628592",
    "https://business.walmart.com/browse/food/broccoli/1",
    "https://www.walmart.com/search?q=broccoli",
]

# Expected keyword *style* (manual QA — not exact LLM output)
EXAMPLE_KEYWORD_HINTS = {
    "dress": ["cocktail dresses", "womens dresses", "party dresses"],
    "sourdough": ["sourdough bread", "bread", "bakery bread"],
    "broccoli": ["frozen broccoli", "frozen vegetables", "broccoli"],
}


@pytest.fixture
def htigea_dress_url() -> str:
    return HTIGEA_DRESS_URL


@pytest.fixture
def sourdough_url() -> str:
    return SOURDOUGH_URL


@pytest.fixture
def broccoli_url() -> str:
    return BROCCOLI_URL


@pytest.fixture
def sample_browse_urls() -> list[dict]:
    return [
        {
            "url": "https://www.walmart.com/browse/clothing/womens-dresses/cocktail-dresses/1234",
            "keyword": "cocktail dresses",
            "position": 2,
        },
        {
            "url": "https://www.walmart.com/browse/clothing/womens-clothing/dresses/5678",
            "keyword": "womens dresses",
            "position": 5,
        },
    ]


@pytest.fixture(params=[
    pytest.param(
        (HTIGEA_DRESS_URL, HTIGEA_PRODUCT_ID, HTIGEA_SLUG_TITLE),
        id="htigea-dress",
    ),
    pytest.param(
        (SOURDOUGH_URL, SOURDOUGH_PRODUCT_ID, SOURDOUGH_SLUG_TITLE),
        id="sourdough",
    ),
    pytest.param(
        (BROCCOLI_URL, BROCCOLI_PRODUCT_ID, BROCCOLI_SLUG_TITLE),
        id="broccoli",
    ),
])
def example_product(request):
    """Parametrized (url, product_id, slug_title) for all example SKUs."""
    return request.param
