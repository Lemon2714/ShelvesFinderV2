"""Controlled live diagnostics for ShelfChecker fetch-path reliability.

This script does not alter shelf-check behavior. It captures the structured
fetch observations emitted by ``_fetch_html`` while replaying a stable set of
category URLs previously returned for Walmart item 408353826.
"""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.tools import shelf_checker


PRODUCT_ID = "408353826"
BRAND = "Head & Shoulders"

# Ten non-brand-path shelves drawn from persisted runs for the target product.
SHELF_URLS = [
    "https://www.walmart.com/browse/beauty/dandruff-shampoo/1085666_3147628_5752434_2927912_4339403",
    "https://www.walmart.com/browse/beauty/shampoo/1085666_3147628_5752434",
    "https://www.walmart.com/browse/beauty/shampoo-conditioner/1085666_3147628_8825321",
    "https://www.walmart.com/browse/personal-care/mens-hair-care/1005862_1056884_8219670",
    "https://www.walmart.com/browse/beauty/travel-size-hair-care/1085666_8097138_1650011",
    "https://www.walmart.com/browse/beauty/hair-styling-products/1085666_3147628_7768896",
    "https://www.walmart.com/browse/beauty/scalp-scrubs-treatments/1085666_3147628_8428896_5309236",
    "https://www.walmart.com/browse/beauty/top-rated-hair-care/1085666_1307057_6702299",
    "https://www.walmart.com/browse/premium-beauty/premium-hair-care/7924299_8655252",
    "https://www.walmart.com/browse/personal-care/1005862",
]


class ObservationHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict] = []
        self.parse_records: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        if str(record.msg).startswith("[ShelfChecker] Parse observation"):
            structured_results, url = record.args
            with self._lock:
                self.parse_records.append({
                    "structured_results": structured_results,
                    "url": url,
                })
            return
        if not str(record.msg).startswith("[ShelfChecker] Fetch observation"):
            return
        path, status, response_bytes, next_data, url = record.args
        observation = {
            "path": path,
            "status": status,
            "bytes": response_bytes,
            "next_data": next_data,
            "url": url,
        }
        with self._lock:
            self.records.append(observation)


def run(args: argparse.Namespace) -> dict:
    urls = SHELF_URLS[: args.pages]
    original_key = settings.webscraping_api_key
    if args.direct_only:
        settings.webscraping_api_key = None

    handler = ObservationHandler()
    original_level = shelf_checker.logger.level
    shelf_checker.logger.addHandler(handler)
    shelf_checker.logger.setLevel(logging.INFO)

    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            results = list(
                executor.map(
                    lambda url: shelf_checker.fetch_shelf_sync(
                        url, PRODUCT_ID, BRAND
                    ),
                    urls,
                )
            )
    finally:
        elapsed = time.perf_counter() - started
        shelf_checker.logger.removeHandler(handler)
        shelf_checker.logger.setLevel(original_level)
        settings.webscraping_api_key = original_key

    terminal = [
        item
        for item in handler.records
        if item["path"] in {"api", "direct_fallback", "total_failure"}
    ]
    by_page: list[dict] = []
    for url, result in zip(urls, results):
        page_fetches = [
            item for item in terminal
            if item["url"].split("?")[0] == url.split("?")[0]
        ]
        verified_fetches = sum(
            1 for item in page_fetches if item["next_data"] == "present"
        )
        by_page.append({
            "url": url,
            "reported_missing": isinstance(result, dict)
            and not result.get("discoverability", False),
            "terminal_fetches": len(page_fetches),
            "verified_fetches": verified_fetches,
            "negative_fully_verified": len(page_fetches) == 2
            and verified_fetches == 2,
            "result_is_invalid": result is None,
        })

    return {
        "pages": len(urls),
        "concurrency": args.concurrency,
        "direct_only": args.direct_only,
        "elapsed_seconds": round(elapsed, 2),
        "attempt_path_distribution": dict(
            Counter(item["path"] for item in handler.records)
        ),
        "terminal_path_distribution": dict(
            Counter(item["path"] for item in terminal)
        ),
        "terminal_status_distribution": dict(
            Counter(str(item["status"]) for item in terminal)
        ),
        "terminal_next_data_distribution": dict(
            Counter(item["next_data"] for item in terminal)
        ),
        "api_failure_status_distribution": dict(
            Counter(
                str(item["status"])
                for item in handler.records
                if item["path"] == "api_failure"
            )
        ),
        "reported_missing": sum(item["reported_missing"] for item in by_page),
        "verified_missing": sum(
            item["reported_missing"] and item["negative_fully_verified"]
            for item in by_page
        ),
        "unverified_missing": sum(
            item["reported_missing"] and not item["negative_fully_verified"]
            for item in by_page
        ),
        "structured_result_distribution": dict(
            Counter(
                str(item["structured_results"])
                for item in handler.parse_records
            )
        ),
        "observations": handler.records,
        "parse_observations": handler.parse_records,
        "by_page": by_page,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, choices=range(1, 11), default=10)
    parser.add_argument("--concurrency", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--direct-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run(args), indent=2))


if __name__ == "__main__":
    main()
