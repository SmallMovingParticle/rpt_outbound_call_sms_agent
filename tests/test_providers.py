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


def test_real_vapi_uses_current_call_endpoint_and_bearer_auth():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(201, json={"id": "real-call-123"})

    clients = ProviderClients(
        Settings(
            provider_mode="mock",
            vapi_mode="real",
            vapi_base_url="https://api.vapi.test",
            vapi_api_key="test-key",
        ),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    call_id = clients.create_vapi_call(WorkflowTrace("provider", "test", "trace-real"), {})
    assert call_id == "real-call-123"
    assert seen == {
        "url": "https://api.vapi.test/call",
        "authorization": "Bearer test-key",
    }


def test_provider_modes_can_mix_real_vapi_with_mock_stride():
    settings = Settings(
        provider_mode="mock",
        vapi_mode="real",
        stride_mode="mock",
        mock_base_url="http://mock.test",
    )
    assert settings.provider_url("vapi") == "https://api.vapi.ai"
    assert settings.provider_url("stride") == "http://mock.test/mock/stride"


def test_real_twilio_message_includes_public_status_callback():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = dict(
            item.split("=", 1) for item in request.content.decode().split("&")
        )
        return httpx.Response(201, json={"sid": "SM-test"})

    settings = Settings(
        provider_mode="mock",
        twilio_mode="real",
        twilio_account_sid="AC-test",
        twilio_auth_token="token",
        twilio_from_number="+15005550006",
        public_base_url="https://public.example.test",
    )
    clients = ProviderClients(
        settings,
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert clients.send_sms(
        WorkflowTrace("provider", "test", "trace-twilio"),
        "+15555550123",
        "synthetic message",
    ) == "SM-test"
    assert seen["url"].endswith("/Accounts/AC-test/Messages.json")
    assert seen["form"]["StatusCallback"] == (
        "https%3A%2F%2Fpublic.example.test%2Fapi%2Fv1%2Ftwilio%2Fmessage-status"
    )
