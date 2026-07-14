from pydantic import BaseModel
from typing import List

class AnalyzeRequest(BaseModel):
    url: str

class AnalyzeResponse(BaseModel):
    product_title: str
    product_brand: str = ""
    product_id: str = ""
    product_image: str = ""
    product_price: str = ""
    keywords: List[str]
    branded_keywords: List[str] = []
    unbranded_keywords: List[str] = []
    browse_pages: List[dict]
    confidence_score: float
    openai_cost: float = 0.0
    shelf_stats: dict = {}

# Step-by-step API Models

class ScrapeResponse(BaseModel):
    title: str
    description: str
    features: List[str]
    brand: str = ""
    id: str = ""
    image: str = ""
    price: str = ""

class KeywordsRequest(BaseModel):
    product_info: dict

class KeywordsResponse(BaseModel):
    keywords: List[str]
    branded_keywords: List[str] = []
    unbranded_keywords: List[str] = []
    cost: float = 0.0

class SearchRequest(BaseModel):
    keywords: List[str]
    product_title: str

class SearchResponse(BaseModel):
    browse_pages: List[dict]

class EvaluateRequest(BaseModel):
    product_info: dict
    browse_pages: List[dict]
    
class EvaluateResponse(BaseModel):
    browse_pages: List[dict]
    confidence_score: float
    cost: float = 0.0

class VisibilityRequest(BaseModel):
    product_id: str
    product_brand: str
    browse_pages: List[dict]

class VisibilityResponse(BaseModel):
    shelf_stats: dict

class SaveRequest(BaseModel):
    url: str
    product_title: str
    product_brand: str = ""
    product_id: str = ""
    keywords: List[str]
    branded_keywords: List[str] = []
    unbranded_keywords: List[str] = []
    browse_pages: List[str]
    openai_cost: float = 0.0
    shelf_stats: dict = {}

class SaveResponse(BaseModel):
    status: str
    message: str

class EmailRequest(BaseModel):
    emails: List[str]
    data: dict

class EmailResponse(BaseModel):
    status: str
    message: str
