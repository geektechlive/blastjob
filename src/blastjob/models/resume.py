from pydantic import BaseModel, Field


class ResumeSection(BaseModel):
    heading: str = ""
    content: str = ""


class TailoredResume(BaseModel):
    name: str = ""
    contact: str = ""
    summary: str = ""
    sections: list[ResumeSection] = Field(default_factory=list)
    raw_markdown: str = ""
