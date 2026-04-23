from blastjob.llm.cost import CallCost, CostTracker, cost_from_usage
from blastjob.models.config import PricingConfig


class FakeUsage:
    def __init__(self, inp, out, cache_write=0, cache_read=0):
        self.input_tokens = inp
        self.output_tokens = out
        self.cache_creation_input_tokens = cache_write
        self.cache_read_input_tokens = cache_read


def test_cost_zero():
    pricing = PricingConfig()
    usage = FakeUsage(0, 0)
    cost = cost_from_usage(usage, pricing)
    assert cost.cost_usd == 0.0


def test_cost_calculation():
    pricing = PricingConfig(
        input_per_mtok=3.0,
        output_per_mtok=15.0,
        cache_write_per_mtok=3.75,
        cache_read_per_mtok=0.30,
    )
    usage = FakeUsage(inp=1_000_000, out=1_000_000)
    cost = cost_from_usage(usage, pricing)
    assert abs(cost.cost_usd - 18.0) < 0.001


def test_cache_hit_ratio():
    call = CallCost(input_tokens=100, cache_read_tokens=900)
    assert abs(call.cache_hit_ratio - 0.9) < 0.001


def test_tracker_accumulates():
    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=0.01))
    tracker.record(CallCost(cost_usd=0.02))
    assert abs(tracker.total_cost - 0.03) < 0.0001


def test_tracker_reset():
    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=1.0))
    tracker.reset()
    assert tracker.total_cost == 0.0


def test_call_cost_total_tokens():
    call = CallCost(input_tokens=300, output_tokens=200)
    assert call.total_tokens == 500


def test_cache_hit_ratio_zero_denom():
    call = CallCost()
    assert call.cache_hit_ratio == 0.0


def test_tracker_total_tokens():
    tracker = CostTracker()
    tracker.record(CallCost(input_tokens=100, output_tokens=50))
    tracker.record(CallCost(input_tokens=200, output_tokens=100))
    assert tracker.total_tokens == 450


def test_tracker_session_summary():
    tracker = CostTracker()
    tracker.record(CallCost(cost_usd=0.05, input_tokens=1000, output_tokens=500))
    summary = tracker.session_summary
    assert "$0.0500" in summary
    assert "1,500" in summary
