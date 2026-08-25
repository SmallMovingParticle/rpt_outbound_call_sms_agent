import httpx

from rpt_agent.config import Settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderClients, ProviderError


def test_mock_adapter_propagates_trace_and_requires_provider_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["trace"] = request.headers["X-Trace-ID"]
        return httpx.Response(201, json={"id": "mock-call-123"})

    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    call_id = clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-provider"), {})
    assert call_id == "mock-call-123"
    assert seen["trace"] == "trace-provider"


def test_missing_provider_id_is_failure():
    clients = ProviderClients(
        Settings(provider_mode="mock", mock_base_url="http://mock.test"),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(201, json={}))),
    )
    try:
        clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-missing"), {})
    except ProviderError as exc:
        assert exc.code == "missing_id"
    else:
        raise AssertionError("missing provider id must fail")
