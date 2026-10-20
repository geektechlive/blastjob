from collections.abc import Sequence
from dataclasses import dataclass, field

from blastjob.models.config import PricingConfig


@dataclass
class CallCost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    # Whether a provider actually reported usage for this call. False means the
    # numeric fields above are meaningless placeholders, NOT "this call was
    # free" — the claude-cli path sets this when it cannot parse a usage figure
    # from the subprocess output. Every reader must check `known` before
    # treating `cost_usd` / `cache_hit_ratio` as real; a fabrication is worse
    # than a gap. Defaults to True so every pre-existing call site (which never
    # set this) keeps behaving exactly as before.
    known: bool = True

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def cache_hit_ratio(self) -> float:
        denom = self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_tokens / denom


def cost_from_usage(usage, pricing: PricingConfig) -> CallCost:
    """Build a CallCost from a provider's usage object.

    `usage=None` means the provider genuinely could not report token counts —
    e.g. the claude-cli path when it fails to parse a usage figure out of the
    subprocess output. That must produce an UNKNOWN CallCost, never a
    fabricated zero-cost one, so callers (and metadata.json, and the cockpit)
    can tell "we don't know" apart from "this call was free".
    """
    if usage is None:
        return CallCost(known=False)

    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

    cost = (
        inp * pricing.input_per_mtok / 1_000_000
        + out * pricing.output_per_mtok / 1_000_000
        + cache_write * pricing.cache_write_per_mtok / 1_000_000
        + cache_read * pricing.cache_read_per_mtok / 1_000_000
    )
    return CallCost(
        input_tokens=inp,
        output_tokens=out,
        cache_creation_tokens=cache_write,
        cache_read_tokens=cache_read,
        cost_usd=cost,
        known=True,
    )


def aggregate_calls(calls: Sequence[CallCost]) -> CallCost:
    """Combine several calls (e.g. every LLM call made by one build) into one
    CallCost representing their total.

    `known` is True only when every constituent call's usage was known — a
    build with one untracked call must report its total as unknown rather
    than as the partial sum of the calls it could measure. An empty sequence
    is a build that made zero calls, which is a known (zero) total, not an
    unknown one.
    """
    if not calls:
        return CallCost(known=True)
    return CallCost(
        input_tokens=sum(c.input_tokens for c in calls),
        output_tokens=sum(c.output_tokens for c in calls),
        cache_creation_tokens=sum(c.cache_creation_tokens for c in calls),
        cache_read_tokens=sum(c.cache_read_tokens for c in calls),
        cost_usd=sum(c.cost_usd for c in calls),
        known=all(c.known for c in calls),
    )


def describe_cost(call: CallCost) -> str:
    """A cost for people to read: "$0.0123", or "unknown" when any call's usage was
    unknown (a partial sum is not a cost). Docs: docs/how-it-works/llm.md."""
    return f"${call.cost_usd:.4f}" if call.known else "unknown"


@dataclass
class CostTracker:
    calls: list[CallCost] = field(default_factory=list)

    def record(self, call: CallCost) -> None:
        self.calls.append(call)

    @property
    def total_cost(self) -> float:
        return sum(c.cost_usd for c in self.calls)

    @property
    def total_tokens(self) -> int:
        return sum(c.total_tokens for c in self.calls)

    @property
    def total_call_cost(self) -> CallCost:
        """Every recorded call combined into one CallCost. `.known` is False
        if any recorded call's usage was unknown — use this (not `total_cost`
        alone) whenever "is this total trustworthy" matters."""
        return aggregate_calls(self.calls)
