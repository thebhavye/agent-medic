from bisect_agent.metrics import Metrics, classify, detect, from_cases


def m(pass_rate=1.0, error_rate=0.0, cost=0.01, tool_calls=3.0):
    return Metrics(8, pass_rate, error_rate, cost, 5000, tool_calls)


def test_detects_each_signature():
    base = m()
    assert [r.signature for r in detect(base, m(error_rate=0.5, pass_rate=0.4))] == ["errors", "quality"]
    assert [r.signature for r in detect(base, m(cost=0.03, tool_calls=7))] == ["cost", "tool_calls"]
    assert detect(base, m(pass_rate=0.9, cost=0.012)) == []     # noise, not a regression


def test_classify_splits_between_endpoints():
    assert classify("quality", 0.875, good_value=1.0, bad_value=0.25) == "good"
    assert classify("quality", 0.5, good_value=1.0, bad_value=0.25) == "bad"
    # cost compares on a log scale: 0.019 is closer to 0.01 than to 0.04
    assert classify("cost", 0.019, good_value=0.01, bad_value=0.04) == "good"
    assert classify("cost", 0.021, good_value=0.01, bad_value=0.04) == "bad"


def test_tool_errors_count_as_errors():
    case = {"passed": False, "status": "ok", "tool_errors": 2, "cost_usd": 0.01,
            "input_tokens": 100, "output_tokens": 10, "tool_calls": 3}
    assert from_cases([case]).error_rate == 1.0
