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
    # Set by the posting-closure lifecycle (core/closure.py): the posting closed
    # before the user applied, or closed and stayed closed 21 days after they did.
    "posting_closed",
    "filled",
)


# How an application started (#80). Labels are what the cockpit shows; a missing
# value (NULL) is "unknown" and is never shown as cold.
APPLICATION_CHANNELS: dict[str, str] = {
    "cold": "Cold application",
    "referral": "Referral",
    "recruiter": "Recruiter",
    "inbound": "Inbound",
}
MAX_CHANNEL_VIA_CHARS = 120


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
