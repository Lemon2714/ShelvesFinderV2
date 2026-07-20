from fastapi import FastAPI, HTTPException, Query, Request, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sse_starlette.sse import EventSourceResponse
from app.models.response_models import (
    AnalyzeRequest, AnalyzeResponse,
    ScrapeResponse, KeywordsRequest, KeywordsResponse,
    SearchRequest, SearchResponse,
    EvaluateRequest, EvaluateResponse,
    SaveRequest, SaveResponse,
    VisibilityRequest, VisibilityResponse,
    EmailRequest, EmailResponse,
)
from app.services.workflow import run_analysis_workflow
from app.agents.planner import stream_plan
from app.tools.scraper import fetch_product_content, _is_mostly_non_english, _slug_from_url
from app.agents.keyword_agent import extract_keywords
from app.agents.search_agent import find_browse_pages
from app.agents.evaluation_agent import evaluate_and_rank
from app.tools.shelf_checker import check_shelf_visibility
from app.services.persistence import save_result_to_csv, upload_csv_to_drive, append_result_to_sheet
import logging
import json
import os

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ShelvesFinder",
    description="Agentic AI for Walmart browse page discovery — v1 + v2.",
    version="2.0.0",
)


@app.on_event("startup")
async def log_startup_config():
    from app.config import settings
    from app.services.llm import get_provider_info, get_openai_client, get_anthropic_client

    info = get_provider_info()
    sep = "=" * 60
    logger.info(sep)
    logger.info("  ShelvesFinder v2 — startup")
    logger.info(sep)
    logger.info(f"  LLM provider      : {info['llm_provider'].upper()}")
    logger.info(f"  Chat model        : {info['chat_model']}")
    logger.info(f"  Orchestrator model: {info['orchestrator_model']}")

    # Eagerly initialise both clients so failures are visible in startup logs
    openai_client = get_openai_client()
    claude_client = get_anthropic_client()
    logger.info(f"  OpenAI client     : {'✓ ready' if openai_client else '✗ unavailable (check OPENAI_API_KEY)'}")
    logger.info(f"  Claude client     : {'✓ ready' if claude_client else '✗ unavailable (check ANTHROPIC_API_KEY)'}")
    logger.info(f"  Embeddings        : OpenAI ({settings.openai_embedding_model})")
    logger.info(f"  v2 max rounds     : {settings.v2_max_rounds}")
    logger.info(f"  v2 budget limit   : ${settings.v2_budget_limit}")
    logger.info(sep)

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# v1 Endpoints (kept for full backward compatibility)
# ===========================================================================

@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze_url(request: AnalyzeRequest):
    logger.info(f"[v1] Sync analyze: {request.url}")
    try:
        return run_analysis_workflow(request.url)
    except Exception as e:
        logger.error(f"[v1] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/analyze/stream")
async def analyze_stream(request: Request, url: str, llm_provider: str = ""):
    logger.info(f"[v1] Stream analyze: {url} (provider={llm_provider or 'default'})")
    if not url:
        raise HTTPException(status_code=400, detail="URL parameter is required")

    async def event_generator():
        try:
            async for update in stream_plan(url, llm_provider=llm_provider):
                if await request.is_disconnected():
                    break
                yield json.dumps(update)
        except Exception as e:
            logger.error(f"[v1] Stream error: {e}", exc_info=True)
            yield json.dumps({"step": "error", "status": "failed", "message": str(e)})

    return EventSourceResponse(event_generator())


@app.post("/analyze/step/scrape", response_model=ScrapeResponse)
async def step_scrape(request: AnalyzeRequest):
    try:
        product_info = fetch_product_content(request.url)
        if not product_info.get("title") or "robot or human" in product_info.get("title", "").lower():
            parts = request.url.rstrip("/").split("/")
            slug = parts[-2] if len(parts) >= 2 and parts[-2] != "ip" else parts[-1]
            product_info["title"] = slug.replace("-", " ").title()
            product_info["description"] = f"Walmart product listing for {product_info['title']}."
            words = [w for w in product_info["title"].split() if len(w) > 3]
            product_info["features"] = [f"Product includes: {w}" for w in words[:3]]
            product_info["id"] = product_info.get("id") or parts[-1].split("?")[0]
            product_info["brand"] = product_info.get("brand") or ""
        # Non-English title guard
        if _is_mostly_non_english(product_info.get("title", "")):
            slug_title = _slug_from_url(request.url)
            if slug_title:
                logger.warning(f"[v1/scrape] Non-English title replaced: '{product_info['title'][:50]}' → '{slug_title}'")
                product_info["title"] = slug_title.title()
        logger.info(f"[v1/scrape] {product_info.get('title', '')[:40]}")
        return ScrapeResponse(**product_info)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/step/keywords", response_model=KeywordsResponse)
async def step_keywords(request: KeywordsRequest):
    try:
        keywords, branded_keys, unbranded_keys, cost = extract_keywords(request.product_info)
        if not keywords:
            keywords = [request.product_info.get("title", "")]
            unbranded_keys = keywords
            branded_keys = []
        return KeywordsResponse(
            keywords=keywords,
            branded_keywords=branded_keys,
            unbranded_keywords=unbranded_keys,
            cost=cost * 1000,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/step/search", response_model=SearchResponse)
async def step_search(request: SearchRequest):
    try:
        browse_pages = find_browse_pages(request.keywords)
        if not browse_pages:
            words = request.product_title.split()
            if words:
                browse_pages = find_browse_pages([words[0]])
        return SearchResponse(browse_pages=browse_pages)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/step/evaluate", response_model=EvaluateResponse)
async def step_evaluate(request: EvaluateRequest):
    try:
        ranked_pages, confidence_score, t_cost = evaluate_and_rank(
            request.product_info, request.browse_pages
        )
        return EvaluateResponse(
            browse_pages=ranked_pages[:10],
            confidence_score=confidence_score,
            cost=t_cost * 1000,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/step/visibility", response_model=VisibilityResponse)
async def step_visibility(request: VisibilityRequest):
    try:
        shelf_stats = await check_shelf_visibility(
            request.browse_pages, request.product_id, request.product_brand
        )
        return VisibilityResponse(shelf_stats=shelf_stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/analyze/email", response_model=EmailResponse)
@limiter.limit("3/minute")
async def email_analysis(request: Request, payload: EmailRequest):
    if len(payload.emails) > 3:
        raise HTTPException(status_code=400, detail="Maximum of 3 recipients allowed.")
    try:
        from app.agents.email_agent import send_results_email
        send_results_email(payload.emails, payload.data)
        return EmailResponse(status="success", message="Email sent successfully")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to send email: {str(e)}")


@app.post("/analyze/save", response_model=SaveResponse)
async def save_analysis(request: SaveRequest, background_tasks: BackgroundTasks):
    try:
        from app.config import settings
        if settings.use_google_sheets:
            success = append_result_to_sheet(
                url=request.url, title=request.product_title,
                brand=request.product_brand, product_id=request.product_id,
                keywords=request.keywords, branded_keywords=request.branded_keywords,
                unbranded_keywords=request.unbranded_keywords,
                browse_pages=request.browse_pages, openai_cost=request.openai_cost,
            )
            if not success:
                raise HTTPException(status_code=500, detail="Failed to write to Google Sheet")
            return SaveResponse(status="success", message="Data appended to Google Sheet")
        else:
            success = save_result_to_csv(
                url=request.url, title=request.product_title,
                brand=request.product_brand, product_id=request.product_id,
                keywords=request.keywords, branded_keywords=request.branded_keywords,
                unbranded_keywords=request.unbranded_keywords,
                browse_pages=request.browse_pages, openai_cost=request.openai_cost,
            )
            if not success:
                raise HTTPException(status_code=500, detail="Failed to write to CSV")
            background_tasks.add_task(upload_csv_to_drive)
            return SaveResponse(status="success", message="Data saved to CSV")
    except Exception as e:
        logger.error(f"[v1/save] {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


# ===========================================================================
# v2 Endpoints — ReAct agentic loop
# ===========================================================================

from pydantic import BaseModel, Field
from typing import Optional

from app.models.session_state import (
    RECOMMENDED_RESULT_COUNT_MIN,
    RECOMMENDED_RESULT_COUNT_DEFAULT,
    RECOMMENDED_RESULT_COUNT_MAX,
)


class V2ConfigRequest(BaseModel):
    max_rounds: int = 5
    target_missing_count: int = 3
    budget_limit: float = 0.50
    recommended_result_count: int = Field(
        default=RECOMMENDED_RESULT_COUNT_DEFAULT,
        ge=RECOMMENDED_RESULT_COUNT_MIN,
        le=RECOMMENDED_RESULT_COUNT_MAX,
    )


class V2AnalyzeRequest(BaseModel):
    url: str
    max_rounds: Optional[int] = None
    target_missing_count: Optional[int] = None
    budget_limit: Optional[float] = None
    recommended_result_count: Optional[int] = Field(
        default=None,
        ge=RECOMMENDED_RESULT_COUNT_MIN,
        le=RECOMMENDED_RESULT_COUNT_MAX,
    )


@app.get("/v2/analyze/stream")
async def v2_analyze_stream(
    request: Request,
    url: str,
    max_rounds: int = 5,
    target_missing_count: int = 3,
    budget_limit: float = 0.50,
    user_instructions: str = "",
    include_branded: bool = False,
    llm_provider: str = "",
    recommended_result_count: int = Query(
        default=RECOMMENDED_RESULT_COUNT_DEFAULT,
        ge=RECOMMENDED_RESULT_COUNT_MIN,
        le=RECOMMENDED_RESULT_COUNT_MAX,
        description=(
            "Number of final category-page rows to return under "
            "'Recommended Category Pages' (integer, 3-10)"
        ),
    ),
):
    """
    Full agentic ReAct loop streamed via SSE.

    Query params:
      url                  — Walmart product URL (required)
      max_rounds           — max ReAct iterations (default 5)
      target_missing_count — stop when N missing pages found (default 3)
      budget_limit         — max OpenAI spend in USD (default $0.50)
      user_instructions    — optional free-text context injected into agent prompts
      recommended_result_count — final category-page rows to return (3-10, default 5);
                                 invalid values are rejected with 422

    SSE event types:
      setup_start | setup_scraping | setup_keywords | loop_start
      agent_reasoning | tool_selected | tool_result | goal_check
      complete | error
    """
    if not url:
        raise HTTPException(status_code=400, detail="url parameter is required")

    logger.info(
        f"[v2] Stream analyze: {url} "
        f"(max_rounds={max_rounds}, target_missing={target_missing_count}, "
        f"budget=${budget_limit}, result_count={recommended_result_count}, "
        f"provider={llm_provider or 'default'})"
    )
    if user_instructions:
        logger.info(f"[v2] User instructions: {user_instructions[:100]}")

    from app.services.workflow_v2 import run_v2_workflow

    async def event_generator():
        try:
            async for event in run_v2_workflow(
                url=url,
                max_rounds=max_rounds,
                target_missing_count=target_missing_count,
                budget_limit=budget_limit,
                user_instructions=user_instructions,
                include_branded=include_branded,
                llm_provider=llm_provider,
                recommended_result_count=recommended_result_count,
            ):
                if await request.is_disconnected():
                    logger.info("[v2] Client disconnected, stopping stream")
                    break
                yield json.dumps(event)
        except Exception as e:
            logger.error(f"[v2] Stream error: {e}", exc_info=True)
            yield json.dumps({"event": "error", "message": str(e)})

    return EventSourceResponse(event_generator())


@app.post("/v2/analyze/stop")
async def v2_stop_session(session_id: str):
    """
    Signal a running session to stop at the next opportunity.
    (For future use when sessions are tracked server-side.)
    """
    # In the current in-memory model, disconnect from the SSE stream
    # to stop the generator. This endpoint is a placeholder for future
    # server-side session tracking.
    return {"status": "ok", "message": f"Stop signal sent for session {session_id}"}


@app.get("/api/default-provider")
async def get_default_provider():
    """Returns the default LLM provider configured in .env."""
    from app.config import settings
    return {"provider": settings.llm_provider}


@app.get("/v2/health")
async def v2_health():
    """Health check for v2 endpoints."""
    from app.config import settings
    from app.services.llm import get_provider_info
    return {
        "status": "ok",
        "version": "2.0.0",
        "llm": get_provider_info(),
        "v2_max_rounds": settings.v2_max_rounds,
        "v2_target_missing_count": settings.v2_target_missing_count,
        "v2_budget_limit": settings.v2_budget_limit,
    }


# ===========================================================================
# Static frontend (must be last — catch-all)
# ===========================================================================

frontend_path = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend"
)
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")
else:
    logger.warning(f"Frontend not found at {frontend_path}. UI will not load.")
