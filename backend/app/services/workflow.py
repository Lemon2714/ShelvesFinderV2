from app.models.response_models import AnalyzeResponse
from app.agents.planner import execute_plan

def run_analysis_workflow(url: str) -> AnalyzeResponse:
    """
    Runs the agent workflow and constructs the final response model.
    """
    results = execute_plan(url)
    
    return AnalyzeResponse(
        product_title=results["product_title"],
        product_brand=results.get("product_brand", ""),
        product_id=results.get("product_id", ""),
        product_image=results.get("product_image", ""),
        product_price=results.get("product_price", ""),
        keywords=results["keywords"],
        branded_keywords=results.get("branded_keywords", []),
        unbranded_keywords=results.get("unbranded_keywords", []),
        browse_pages=results["browse_pages"],
        confidence_score=results["confidence_score"],
        openai_cost=results.get("openai_cost", 0.0),
        shelf_stats=results.get("shelf_stats", {}),
    )
