from pydantic import BaseModel, field_validator

STATUSES: tuple[str, ...] = (
    "drafted",
    "applied",
    "screen",
    "interview",
    "offer",
    "rejected",
    "ghosted",
    "withdrawn",
)


class TrackingRecord(BaseModel):
    model_config = {"extra": "ignore"}

    status: str = "drafted"
    applied_at: str | None = None
    next_action: str = ""
    next_action_due: str | None = None
    notes: str = ""

    @field_validator("status")
    @classmethod
    def _validate_status(cls, v: str) -> str:
        if v not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {v!r}")
        return v
