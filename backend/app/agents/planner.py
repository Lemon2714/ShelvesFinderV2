import logging
import asyncio
from app.tools.scraper import fetch_product_content, _is_mostly_non_english, _slug_from_url
from app.agents.keyword_agent import extract_keywords
from app.agents.search_agent import find_browse_pages
from app.agents.evaluation_agent import evaluate_and_rank
from app.tools.shelf_checker import check_shelf_visibility

logger = logging.getLogger(__name__)

async def stream_plan(url: str, llm_provider: str = ""):
    """
    Orchestrates the steps asynchronously and yields progress updates
    for Server-Sent Events (SSE).
    """
    # --- PHASE 1: SCRAPING ---
    total_cost = 0.0
    yield {"step": "scraping", "status": "running", "message": f"Scraping product data from URL..."}
    await asyncio.sleep(0.5) # Slight artificial delay for UI UX 
    
    product_info = fetch_product_content(url)

    if not product_info.get("title") or "robot or human" in product_info.get("title", "").lower():
        yield {"step": "scraping", "status": "warning", "message": "Anti-bot detected. Falling back to URL parsing."}
        parts = url.rstrip('/').split('/')
        slug = parts[-2] if len(parts) >= 2 and parts[-2] != "ip" else parts[-1]
        product_info["title"] = slug.replace('-', ' ').title()
        product_info["description"] = ""
        product_info["features"] = []
        product_info["id"] = product_info.get("id") or parts[-1].split('?')[0]
        product_info["brand"] = product_info.get("brand") or ""

    # Non-English title guard — same fix as v2
    if _is_mostly_non_english(product_info.get("title", "")):
        slug_title = _slug_from_url(url)
        if slug_title:
            logger.warning(
                f"[Scrape Agent] Non-English title detected: '{product_info['title'][:50]}' "
                f"→ using URL slug: '{slug_title}'"
            )
            product_info["title"] = slug_title.title()
            yield {"step": "scraping", "status": "warning",
                   "message": f"Non-English title replaced with English slug: '{product_info['title']}'"}

    yield {"step": "scraping", "status": "complete", "message": f"Found product: '{product_info.get('title')}'"}
    logger.info(f"[Scrape Agent] Successfully scraped - URL: {url} | Title: {product_info.get('title', '')[:30]}... | Brand: {product_info.get('brand', '')}")
    await asyncio.sleep(0.5)

    # --- PHASE 2: KEYWORDS ---
    yield {"step": "keywords", "status": "running", "message": "AI Keyword Agent extracting search terms..."}
    keywords, branded_keywords, unbranded_keywords, k_cost = extract_keywords(product_info)
    total_cost += k_cost
    
    if not keywords:
        keywords = [product_info["title"]]
        branded_keywords = []
        unbranded_keywords = [product_info["title"]]
        yield {"step": "keywords", "status": "warning", "message": "Keyword extraction failed, using title instead."}
    else:
        yield {"step": "keywords", "status": "complete", "message": f"Generated {len(keywords)} optimal search phrases."}
        
    logger.info(f"[Keyword Agent] Extracted {len(keywords)} optimal search phrases. OpenAICost: ${k_cost:.6f}")
    await asyncio.sleep(0.5)
        
    # --- PHASE 3: SEARCH ---
    yield {"step": "search", "status": "running", "message": f"Search Agent querying Walmart for {len(keywords)} phrases..."}
    browse_pages = find_browse_pages(keywords)
    
    if not browse_pages:
        yield {"step": "search", "status": "warning", "message": "No specific browse pages found. Attempting broader search..."}
        words = product_info["title"].split()
        if words:
            browse_pages = find_browse_pages([words[0]])
            
    yield {"step": "search", "status": "complete", "message": f"Discovered {len(browse_pages)} candidate category URLs."}
    logger.info(f"[Search Agent] Discovered {len(browse_pages)} candidate category URLs")
    await asyncio.sleep(0.5)
            
    # --- PHASE 4: EVALUATION ---
    yield {"step": "evaluation", "status": "running", "message": f"Evaluation Agent ranking {len(browse_pages)} candidates via similarity scoring..."}
    ranked_pages, confidence_score, e_cost = evaluate_and_rank(product_info, browse_pages)
    total_cost += e_cost
    
    top_results = ranked_pages[:10]
    yield {"step": "evaluation", "status": "complete", "message": "Ranking complete."}
    logger.info(f"[Evaluation Agent] Evaluated similarity via embeddings. Confidence: {confidence_score}%. OpenAICost: ${e_cost:.6f}")
    await asyncio.sleep(0.5)
    
    # --- PHASE 5: SHELF CHECK ---
    yield {"step": "visibility", "status": "running", "message": f"Checking visibility of '{product_info.get('id')}' on {len(top_results)} shelves..."}
    shelf_stats = await check_shelf_visibility(top_results, product_info.get("id"), product_info.get("brand"))
    yield {"step": "visibility", "status": "complete", "message": f"Shelf check complete. Found on {shelf_stats.get('found', 0)} shelves."}
    logger.info(f"[Visibility Agent] Scan complete. Found {shelf_stats.get('found', 0)} / {shelf_stats.get('total', 0)} shelves. Score: {shelf_stats.get('score', 0.0)}%")
    await asyncio.sleep(0.5)

    # --- FINAL RESULT ---
    yield {
        "step": "done",
        "status": "complete",
        "data": {
            "product_title": product_info.get("title", ""),
            "product_brand": product_info.get("brand", ""),
            "product_id": product_info.get("id", ""),
            "product_image": product_info.get("image", ""),
            "product_price": product_info.get("price", ""),
            "keywords": keywords,
            "branded_keywords": branded_keywords,
            "unbranded_keywords": unbranded_keywords,
            "browse_pages": top_results,
            "confidence_score": confidence_score,
            "openai_cost": round(total_cost * 1000, 6),
            "shelf_stats": shelf_stats
        }
    }

# Keeping the original synchronous function for backward compatibility with the POST endpoint
def execute_plan(url: str) -> dict:
    total_cost = 0.0
    logger.info(f"Phase 1: Scraping {url}")
    product_info = fetch_product_content(url)

    if not product_info.get("title") or "robot or human" in product_info.get("title", "").lower():
        logger.warning("Scraping returned no title or was blocked. Proceeding with URL parsing fallback.")
        parts = url.rstrip('/').split('/')
        slug = parts[-2] if len(parts) >= 2 and parts[-2] != "ip" else parts[-1]
        product_info["title"] = slug.replace('-', ' ').title()
        product_info["description"] = ""
        product_info["features"] = []
        product_info["id"] = product_info.get("id") or parts[-1].split('?')[0]
        product_info["brand"] = product_info.get("brand") or ""

    # Non-English title guard — same fix as v2
    if _is_mostly_non_english(product_info.get("title", "")):
        slug_title = _slug_from_url(url)
        if slug_title:
            logger.warning(
                f"[execute_plan] Non-English title detected: '{product_info['title'][:50]}' "
                f"→ using URL slug: '{slug_title}'"
            )
            product_info["title"] = slug_title.title()

    logger.info("Phase 2: Extracting keywords")
    keywords, branded_keywords, unbranded_keywords, k_cost = extract_keywords(product_info)
    total_cost += k_cost
    
    if not keywords:
        logger.info("No keywords extracted, using title.")
        keywords = [product_info["title"]]
        branded_keywords = []
        unbranded_keywords = [product_info["title"]]
        
    logger.info(f"Phase 3: Searching Walmart browse pages with {len(keywords)} keywords")
    browse_pages = find_browse_pages(keywords)
    
    if not browse_pages:
        logger.info("No browse pages found. Attempting a broader search...")
        words = product_info["title"].split()
        if words:
            browse_pages = find_browse_pages([words[0]])
            
    logger.info(f"Phase 4: Evaluating {len(browse_pages)} candidates.")
    ranked_pages, confidence_score, e_cost = evaluate_and_rank(product_info, browse_pages)
    total_cost += e_cost
    
    top_results = ranked_pages[:10]
    
    logger.info(f"Phase 5: Checking visibility on {len(top_results)} shelves")
    import asyncio
    shelf_stats = asyncio.run(check_shelf_visibility(top_results, product_info.get("id"), product_info.get("brand")))
    
    return {
        "product_title": product_info.get("title", ""),
        "product_brand": product_info.get("brand", ""),
        "product_id": product_info.get("id", ""),
        "product_image": product_info.get("image", ""),
        "product_price": product_info.get("price", ""),
        "keywords": keywords,
        "branded_keywords": branded_keywords,
        "unbranded_keywords": unbranded_keywords,
        "browse_pages": top_results,
        "confidence_score": confidence_score,
        "openai_cost": round(total_cost * 1000, 6),
        "shelf_stats": shelf_stats
    }
