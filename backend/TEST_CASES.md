# ShelvesFinder — Test Cases & Examples

> **Excel workbook (manual testing):** open [`ShelvesFinder_Test_Cases.xlsx`](ShelvesFinder_Test_Cases.xlsx) in Excel.
> Fill the yellow columns (Actual Result, Pass/Fail, Date, Tester, Notes).
> Regenerate after edits: `python scripts/generate_test_excel.py`

This document is your **manual QA checklist** and maps to **automated tests** in `backend/tests/`.

---

## How to run automated tests

From the `backend` folder:

```bash
pip install pytest pytest-asyncio
python -m pytest tests/ -v
```

Run only fast unit tests (no live APIs):

```bash
python -m pytest tests/ -v -m "not integration"
```

Run the full mocked pipeline test:

```bash
python -m pytest tests/test_pipeline_htigea.py -v
```

---

## Example product URLs

Use these in the UI (**paste into the URL box**) or in API calls.

| ID | Product | URL | Expected product ID |
|----|---------|-----|---------------------|
| TC-URL-01 | Htigea dress | `https://www.walmart.com/ip/Htigea-Wedding-Guest-Midi-Dress-for-Women-Long-Sleeve-V-Neck-Tie-Waist-Bodycon-Dresses-Elegant-Formal-Party-Cocktail-Dress-Black-XL/19307773882` | `19307773882` |
| TC-URL-02 | Essential sourdough | `https://www.walmart.com/ip/Essential-Hawaiian-Sliced-Sourdough-Loaf-Non-GMO-16-oz/12928764204?classType=REGULAR&adsRedirect=true` | `12928764204` |
| TC-URL-03 | Simplot broccoli | `https://www.walmart.com/ip/Simplot-IQF-Broccoli-Florets-32-oz-package-12-packages-per-case/504628592?classType=REGULAR&from=/search` | `504628592` |

**Note:** Walmart often returns **“Robot or human?”** for automated access. The app should still extract **ID from the URL** and use the **slug as fallback title**.

---

## Manual test cases — Basic (v1)

### TC-V1-AUTO-01 — Full auto pipeline (broccoli)

| Field | Value |
|-------|--------|
| **Mode** | Basic |
| **Auto** | ON |
| **URL** | TC-URL-03 (Simplot broccoli) |
| **Preconditions** | Server running; `SERPER_API_KEY` and LLM key set in `backend/.env` |

**Steps**

1. Paste URL → click **Analyze**.
2. Watch stepper: Scrape → Keywords → Search → Evaluation → Visibility.

**Expected**

| Step | Expected result |
|------|-----------------|
| Scrape | Completes; title contains “Broccoli” or slug fallback; ID = `504628592` |
| Keywords | 6–8 unbranded phrases (e.g. frozen broccoli, frozen vegetables) — no long case-pack title |
| Search | At least 0 browse URLs (0 is OK if Serper quota empty; with key, expect some `/browse/` links) |
| Evaluation | Ranked list; confidence score 0–1 |
| Visibility | Shelf stats: found + missing + total; results table populated if pages were checked |

---

### TC-V1-AUTO-02 — Sourdough with query params

| Field | Value |
|-------|--------|
| **URL** | TC-URL-02 |
| **Goal** | Confirm `?classType=REGULAR&adsRedirect=true` does not break ID parsing |

**Expected:** Product ID = `12928764204` in scrape output and shelf check.

---

### TC-V1-MANUAL-01 — Step-by-step with edit

| Field | Value |
|-------|--------|
| **Mode** | Basic |
| **Auto** | OFF |
| **URL** | TC-URL-01 (dress) |

**Steps**

1. Run scrape → **Continue**.
2. On keywords step, edit unbranded keywords to: `cocktail dresses`, `party dresses` → **Continue**.
3. Complete search → evaluate → visibility.

**Expected:** Search uses your edited keywords; evaluate ranks browse pages related to dresses.

---

### TC-V1-NEG-01 — Invalid URL

| Field | Value |
|-------|--------|
| **URL** | `https://www.amazon.com/dp/B000` or empty |

**Expected:** UI shows validation error; no analysis starts.

---

## Manual test cases — Advance (v2)

### TC-V2-01 — Default agent run

| Field | Value |
|-------|--------|
| **Mode** | Advance |
| **URL** | TC-URL-03 (broccoli) |
| **Settings** | Max rounds=5, Target missing=3, Budget=$0.50 |

**Steps**

1. Analyze and watch v2 panel (reasoning log, rounds, cost).
2. Wait for **complete** event.

**Expected**

- Setup: scrape + keywords.
- Loop: mix of `search`, `evaluate`, `check_shelf` in log.
- Stops when **3 missing** shelves found OR rounds/budget exhausted.
- Final stats: missing count, discovered pages, cost ≤ budget (approx).

---

### TC-V2-02 — Agent context (instructions)

| Field | Value |
|-------|--------|
| **URL** | TC-URL-03 |
| **Agent context** | `Focus on frozen vegetables and frozen broccoli aisles only. Skip snacks and dairy.` |

**Expected:** Keywords and searches lean toward frozen produce (subjective — verify in reasoning log).

---

### TC-V2-03 — Branded keywords

| Field | Value |
|-------|--------|
| **URL** | TC-URL-03 |
| **Branded keywords** | ON |

**Expected:** Keyword list includes brand-style terms (e.g. Simplot + product type) in addition to unbranded shelf terms.

---

### TC-V2-04 — Low budget stop

| Field | Value |
|-------|--------|
| **Budget** | $0.01 |
| **Max rounds** | 20 |

**Expected:** Run stops early due to budget; `goal_check` or stop reason mentions budget.

---

## API test cases (curl / Postman)

Start server: `uvicorn app.main:app --reload` from `backend`.

### TC-API-01 — Scrape only

```bash
curl -X POST http://localhost:8000/analyze/step/scrape \
  -H "Content-Type: application/json" \
  -d "{\"url\": \"https://www.walmart.com/ip/Simplot-IQF-Broccoli-Florets-32-oz-package-12-packages-per-case/504628592\"}"
```

**Expected JSON fields:** `title`, `id` (`504628592`), `brand` (may be empty if bot-blocked).

---

### TC-API-02 — v1 stream (SSE)

```bash
curl -N "http://localhost:8000/analyze/stream?url=https://www.walmart.com/ip/Essential-Hawaiian-Sliced-Sourdough-Loaf-Non-GMO-16-oz/12928764204"
```

**Expected:** SSE events with `step`: scraping, keywords, search, evaluation, visibility, done.

---

### TC-API-03 — v2 health

```bash
curl http://localhost:8000/v2/health
```

**Expected:** 200 OK.

---

## Automated test map

| Manual ID | Automated test file | What it verifies |
|-----------|---------------------|------------------|
| TC-URL-01–03 | `test_product_urls.py`, `test_scraper_url.py` | ID + slug from URL |
| Browse filter | `test_browse_url_validation.py`, `test_search_api.py` | Only valid `/browse/` URLs |
| Search dedup | `test_search_agent.py` | Best rank kept per URL |
| Similarity | `test_similarity.py` | Cosine + keyword fallback |
| Shelf check | `test_shelf_checker.py` | Found / missing / 404 |
| Evaluate | `test_evaluation_agent.py` | Ranking + confidence |
| Pipeline | `test_pipeline_htigea.py` | End-to-end mocked flow |

---

## Environment checklist (live testing)

| Variable | Needed for |
|----------|------------|
| `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` | Keywords, v2 orchestrator |
| `SERPER_API_KEY` | Search step (browse discovery) |
| `WEBSCRAPING_API_KEY` | Optional; better scrape/shelf fetch vs bot block |

---

## Test result log (fill in when you run manual tests)

| Test ID | Date | Pass/Fail | Notes |
|---------|------|-----------|-------|
| TC-V1-AUTO-01 | | | |
| TC-V1-AUTO-02 | | | |
| TC-V1-MANUAL-01 | | | |
| TC-V2-01 | | | |
| TC-V2-02 | | | |
