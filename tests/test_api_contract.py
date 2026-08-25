import json

from fastapi.testclient import TestClient

import rpt_agent.api as api_module


class FakeBookingService:
    def availability(self, trace, lead_id, start, days=7):
        trace.log("fake_availability", lead_id=lead_id)
        return {"status": "ok", "slots": []}

    def book(self, trace, lead_id, event_id, slot_token):
        return {"status": "confirmed", "appointment_id": 123}


def test_vapi_tools_require_auth(monkeypatch):
    client = TestClient(api_module.app)
    response = client.post("/api/v1/vapi/tools", json={})
    assert response.status_code == 401


def test_vapi_results_preserve_order_and_business_errors_use_200(monkeypatch):
    monkeypatch.setattr(api_module, "BookingService", FakeBookingService)
    client = TestClient(api_module.app)
    payload = {"message": {"toolCallList": [
        {"id": "one", "name": "availability", "arguments": {"lead_id": "lead-1"}},
        {"id": "two", "name": "unknown", "arguments": {}},
    ]}}
    response = client.post(
        "/api/v1/vapi/tools", json=payload,
        headers={"Authorization": "Bearer local-vapi-secret", "X-Trace-ID": "trace-api"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert [item["toolCallId"] for item in results] == ["one", "two"]
    assert json.loads(results[0]["result"])["status"] == "ok"
    assert "error" in results[1]
    assert "\n" not in results[1]["error"]
    assert response.headers["X-Trace-ID"] == "trace-api"

