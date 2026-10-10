"""Turn eval results into metrics, detect regressions, and classify a commit as good or bad."""

import math
from dataclasses import asdict, dataclass


@dataclass
class Metrics:
    n: int
    pass_rate: float
    error_rate: float
    cost_per_run: float
    tokens_per_run: float
    tool_calls_per_run: float
    source: str = "local"           # "neatlogs" when cost/tokens/tool calls came from traces

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Signature:
    name: str
    metric: str
    higher_is_worse: bool
    ratio: bool          # compare as a ratio (cost-like) instead of an absolute difference (rates)
    threshold: float     # minimum change that counts as a regression


# Checked in this order; the first regression found is investigated first.
SIGNATURES = {
    "errors": Signature("errors", "error_rate", True, False, 0.15),
    "quality": Signature("quality", "pass_rate", False, False, 0.15),
    "cost": Signature("cost", "cost_per_run", True, True, 1.5),
    "tool_calls": Signature("tool_calls", "tool_calls_per_run", True, True, 1.5),
}


@dataclass
class Regression:
    signature: str
    good_value: float
    bad_value: float

    def describe(self) -> str:
        sig = SIGNATURES[self.signature]
        if sig.ratio:
            return (f"{sig.metric} {self.good_value:.4g} -> {self.bad_value:.4g} "
                    f"({self.bad_value / max(self.good_value, 1e-12):.1f}x)")
        return f"{sig.metric} {self.good_value:.0%} -> {self.bad_value:.0%}"


def from_cases(cases: list[dict]) -> Metrics:
    n = len(cases)
    if n == 0:
        raise ValueError("eval produced no cases")
    return Metrics(
        n=n,
        pass_rate=sum(c["passed"] for c in cases) / n,
        # A run "has errors" if the agent failed or any tool call raised.
        error_rate=sum(c["status"] != "ok" or c.get("tool_errors", 0) > 0 for c in cases) / n,
        cost_per_run=sum(c["cost_usd"] for c in cases) / n,
        tokens_per_run=sum(c["input_tokens"] + c["output_tokens"] for c in cases) / n,
        tool_calls_per_run=sum(c["tool_calls"] for c in cases) / n,
    )


def _regressed(sig: Signature, good: float, bad: float) -> bool:
    if sig.ratio:
        if good <= 0:
            return False
        change = bad / good if sig.higher_is_worse else good / max(bad, 1e-12)
        return change >= sig.threshold
    delta = bad - good if sig.higher_is_worse else good - bad
    return delta >= sig.threshold


def detect(good: Metrics, bad: Metrics, only: list[str] | None = None) -> list[Regression]:
    found = []
    for name, sig in SIGNATURES.items():
        if only and name not in only:
            continue
        g, b = getattr(good, sig.metric), getattr(bad, sig.metric)
        if _regressed(sig, g, b):
            found.append(Regression(name, g, b))
    return found


def classify(signature: str, value: float, good_value: float, bad_value: float) -> str:
    """Is a commit's value closer to the known-good or the known-bad endpoint?

    Splitting at the midpoint between the two endpoints (geometric for cost-like metrics)
    keeps bisection stable when the LLM's run-to-run noise is smaller than the regression.
    """
    sig = SIGNATURES[signature]
    if sig.ratio:
        cut = math.sqrt(max(good_value, 1e-12) * max(bad_value, 1e-12))
    else:
        cut = (good_value + bad_value) / 2
    worse = value >= cut if sig.higher_is_worse else value <= cut
    return "bad" if worse else "good"
