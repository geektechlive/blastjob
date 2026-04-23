from pydantic import BaseModel, Field


class CompanyResearch(BaseModel):
    company_name: str = ""
    overview: str = ""
    culture_values: str = ""
    recent_news: str = ""
    products_services: str = ""
    keywords: list[str] = Field(default_factory=list)
    raw_markdown: str = ""
