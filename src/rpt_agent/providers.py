from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from .config import Settings, get_settings
from .observability import WorkflowTrace


class ProviderError(RuntimeError):
    def __init__(self, provider: str, code: str, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class Slot:
    clinician_id: int
    timezone: str
    local_date: str
    local_time: str


class ProviderClients:
    def __init__(self, settings: Settings | None = None, client: httpx.Client | None = None):
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=self.settings.request_timeout_seconds)

    def _request(self, trace: WorkflowTrace, provider: str, method: str, url: str, **kwargs: Any) -> httpx.Response:
        trace.log("provider_request_started", provider=provider, method=method, url=url)
        headers = dict(kwargs.pop("headers", {}))
        headers["X-Trace-ID"] = trace.trace_id
        if self.settings.mode(provider) == "mock":
            headers["X-Mock-Scenario"] = self.settings.mock_scenario
        try:
            response = self.client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            trace.log("provider_timeout", provider=provider)
            raise ProviderError(provider, "timeout", f"{provider} request timed out", ambiguous=True) from exc
        except httpx.HTTPError as exc:
            trace.log("provider_transport_error", provider=provider, error_category=type(exc).__name__)
            raise ProviderError(provider, "transport", f"{provider} request failed", ambiguous=True) from exc
        trace.log(
            "provider_response_received", provider=provider, status_code=response.status_code,
            request_id=response.headers.get("x-request-id", ""),
        )
        return response

    def create_vapi_call(self, trace: WorkflowTrace, payload: dict[str, Any]) -> str:
        base = self.settings.provider_url("vapi")
        path = "/calls" if self.settings.mode("vapi") == "mock" else "/call"
        headers = (
            {"Authorization": f"Bearer {self.settings.vapi_api_key}"}
            if self.settings.mode("vapi") == "real" else {}
        )
        response = self._request(trace, "vapi", "POST", base + path, json=payload, headers=headers)
        if response.status_code not in (200, 201):
            raise ProviderError("vapi", str(response.status_code), response.text[:200])
        call_id = response.json().get("id")
        if not call_id:
            raise ProviderError("vapi", "missing_id", "Vapi response did not include a call id")
        return str(call_id)

    def send_sms(self, trace: WorkflowTrace, to: str, body: str) -> str:
        base = self.settings.provider_url("twilio")
        data = {"To": to, "From": self.settings.twilio_from_number, "Body": body}
        if self.settings.mode("twilio") == "mock":
            url = base + "/messages"
        else:
            url = f"{base}/2010-04-01/Accounts/{self.settings.twilio_account_sid}/Messages.json"
            if self.settings.public_base_url:
                data["StatusCallback"] = (
                    f"{self.settings.public_base_url.rstrip('/')}/api/v1/twilio/message-status"
                )
        response = self._request(
            trace, "twilio", "POST", url,
            data=data,
            auth=None if self.settings.mode("twilio") == "mock" else (
                self.settings.twilio_account_sid, self.settings.twilio_auth_token
            ),
        )
        if response.status_code not in (200, 201):
            raise ProviderError("twilio", str(response.status_code), response.text[:200])
        sid = response.json().get("sid")
        if not sid:
            raise ProviderError("twilio", "missing_id", "Twilio response did not include a message sid")
        return str(sid)

    def stride_availability(
        self, trace: WorkflowTrace, *, location: int, duration: int,
        clinician_ids: str, start_date: date, end_date: date,
    ) -> list[Slot]:
        response = self._request(
            trace, "stride", "GET", self.settings.provider_url("stride") + "/v1/scheduling/availabilities/",
            params={"location": location, "duration": duration, "clinician_ids": clinician_ids,
                    "start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            headers=(
                {"Authorization": f"Token {self.settings.stride_api_token}"}
                if self.settings.mode("stride") == "real" else {}
            ),
        )
        if response.status_code != 200:
            raise ProviderError("stride", str(response.status_code), response.text[:200])
        slots: list[Slot] = []
        for clinician in response.json():
            for day, times in clinician.items():
                if day in {"timezone", "clinician_id"}:
                    continue
                slots.extend(
                    Slot(int(clinician["clinician_id"]), clinician["timezone"], day, value)
                    for value in times
                )
        return slots

    def stride_create(self, trace: WorkflowTrace, resource: str, payload: dict[str, Any]) -> int:
        response = self._request(
            trace, "stride", "POST", self.settings.provider_url("stride") + f"/v1/{resource}/",
            json=payload,
            headers=(
                {"Authorization": f"Token {self.settings.stride_api_token}"}
                if self.settings.mode("stride") == "real" else {}
            ),
        )
        if response.status_code != 200:
            detail = response.json().get("detail", response.text[:200]) if response.content else "empty response"
            raise ProviderError("stride", str(response.status_code), str(detail), ambiguous=response.status_code >= 500)
        resource_id = response.json().get("id")
        if not resource_id:
            raise ProviderError("stride", "missing_id", f"Stride {resource} response omitted id")
        return int(resource_id)

    def deliver_handoff(self, trace: WorkflowTrace, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        from .security import sign_handoff

        response = self._request(
            trace, "keap", "POST", self.settings.keap_handoff_url,
            content=body, headers={"Content-Type": "application/json", "X-RPT-Signature": sign_handoff(body)},
        )
        if response.status_code not in (200, 201, 202, 204):
            raise ProviderError("keap_handoff", str(response.status_code), response.text[:200])
