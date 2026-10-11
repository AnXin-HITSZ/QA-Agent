import httpx
import pytest
from scripts.evalkit.billing import exhausted, install


def test_billing_codes_not_transient_rate_limit():
    assert exhausted(402, {})
    assert exhausted(403, {"code": "Arrearage"})
    assert exhausted(403, {"code": "InsufficientBalance"})
    assert exhausted(403, {"code": "AllocationQuota.FreeTierOnly"})
    assert exhausted(429, {"error": {"code": "insufficient_quota"}})
    assert not exhausted(429, {"code": "RateLimitExceeded"})
    assert not exhausted(200, {"text": "余额不足"})


def test_http_refusal_stops_before_degradation_and_keeps_safe_marker(tmp_path, monkeypatch):
    original = httpx.Client.send
    monkeypatch.setattr(httpx.Client, "send", original)
    marker = tmp_path / "stop.json"
    install(marker)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(
            402, json={"secret": "sensitive"}))) as client:
        with pytest.raises(SystemExit, match="余额"):
            client.get("https://provider.invalid")
    assert "sensitive" not in marker.read_text()
    monkeypatch.setattr(httpx.Client, "send", original)
