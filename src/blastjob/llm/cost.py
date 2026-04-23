from dataclasses import dataclass, field

from blastjob.models.config import PricingConfig


@dataclass
class CallCost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

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
    )


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
    def session_summary(self) -> str:
        return f"${self.total_cost:.4f}  {self.total_tokens:,} tok"

    def reset(self) -> None:
        self.calls.clear()
