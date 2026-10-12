import httpx
import pytest
from app.metering.probe import UsageProbe


class BrokenBody(httpx.SyncByteStream):
    def __iter__(self):
        yield b'{"usage":'
        raise httpx.ReadTimeout("interrupted response")


def test_probe_preserves_original_read_error_instead_of_masking_as_stream_consumed():
    probe=UsageProbe()
    with probe.collecting() as attempts, httpx.Client(event_hooks={"response":[probe.hook]},
            transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={"content-type":"application/json"},stream=BrokenBody()))) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.get("https://provider.invalid")
    assert attempts==[{"status":200}]


def test_malformed_json_remains_nonfatal_and_response_body_is_readable():
    probe=UsageProbe()
    with probe.collecting() as attempts, httpx.Client(event_hooks={"response":[probe.hook]},
            transport=httpx.MockTransport(lambda req:httpx.Response(200,headers={"content-type":"application/json"},content=b'invalid'))) as client:
        response=client.get("https://provider.invalid")
    assert response.text=="invalid" and attempts==[{"status":200}]
