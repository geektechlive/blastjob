from pydantic import BaseModel, Field, field_validator


def _flatten_to_strings(value: object) -> list[str]:
    """Coerce a skills field into a flat list of strings. The ingest model
    sometimes returns skills grouped as a dict (e.g. {category: {skill: count}}),
    which would fail list validation; flatten dict keys and list/scalar values."""
    out: list[str] = []

    def walk(v: object) -> None:
        if isinstance(v, dict):
            for k, sub in v.items():
                if isinstance(k, str) and k.strip():
                    out.append(k.strip())
                if isinstance(sub, (dict, list)):
                    walk(sub)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, (str, int, float)) and str(v).strip():
            out.append(str(v).strip())

    walk(value)
    # de-dupe preserving order
    seen: set[str] = set()
    return [s for s in out if not (s in seen or seen.add(s))]


class DateRange(BaseModel):
    start: str = ""
    end: str = ""


class Experience(BaseModel):
    title: str = ""
    company: str = ""
    dates: DateRange = Field(default_factory=DateRange)
    location: str = ""
    bullets: list[str] = Field(default_factory=list)


class Education(BaseModel):
    degree: str = ""
    institution: str = ""
    dates: DateRange = Field(default_factory=DateRange)
    details: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    description: str = ""
    bullets: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    name: str = ""
    issuer: str = ""
    date: str = ""


class MasterHistory(BaseModel):
    name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    linkedin: str = ""
    github: str = ""
    summary: str = ""
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)

    @field_validator("skills", mode="before")
    @classmethod
    def _coerce_skills(cls, v: object) -> list[str]:
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return v
        return _flatten_to_strings(v)
