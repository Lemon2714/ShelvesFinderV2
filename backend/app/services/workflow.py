from app.models.response_models import AnalyzeResponse
from app.agents.planner import execute_plan

def run_analysis_workflow(url: str) -> AnalyzeResponse:
    """
    Runs the agent workflow and constructs the final response model.
    """
    results = execute_plan(url)
    
    return AnalyzeResponse(
        product_title=results["product_title"],
        keywords=results["keywords"],
        browse_pages=results["browse_pages"],
        confidence_score=results["confidence_score"]
    )
