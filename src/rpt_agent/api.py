from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .db import transaction
from .observability import WorkflowTrace, configure_logging, trace_id_var
from .security import require_twilio_auth, require_vapi_auth
from .services import BookingService, apply_call_outcome, explicit_opt_out
from .vapi_contract import extract_vapi_context, parse_tool_calls, tool_error, tool_success

configure_logging("api")
app = FastAPI(title="RPT Agent API", version="0.1.0")


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    trace_id = request.headers.get("x-trace-id") or uuid4().hex
    token = trace_id_var.set(trace_id)
    try:
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response
    finally:
        trace_id_var.reset(token)


@app.get("/health")
def health():
    return {"status": "ok", "service": "rpt-agent-api"}


@app.get("/ready")
def ready():
    from .config import get_settings

    errors = get_settings().runtime_errors("api")
    if not errors:
        try:
            with transaction() as conn:
                conn.execute("select 1").fetchone()
        except Exception as exc:  # noqa: BLE001 - readiness reports dependency failure
            errors.append(f"database unavailable: {type(exc).__name__}")
    if errors:
        return JSONResponse(status_code=503, content={"status": "not_ready", "errors": errors})
    return {"status": "ready", "service": "rpt-agent-api"}


@app.post("/api/v1/vapi/tools")
async def vapi_tools(request: Request):
    await require_vapi_auth(request)
    trace = WorkflowTrace("vapi_tools", "api", trace_id_var.get())
    try:
        body = await request.json()
        calls = parse_tool_calls(body)
        trace.log("request_parsed", tool_count=len(calls))
        if not calls:
            raise HTTPException(status_code=400, detail="no valid Vapi tool calls")
        results = []
        service = BookingService()
        for call in calls:
            try:
                if call.name in {"get_available_slots", "availability"}:
                    value = service.availability(
                        trace, str(call.arguments["lead_id"]),
                        date.fromisoformat(
                            call.arguments.get("start_date") or datetime.now(UTC).date().isoformat()
                        ),
                        int(call.arguments.get("days", 7)),
                    )
                elif call.name in {"book_appointment", "book"}:
                    value = service.book(
                        trace, str(call.arguments["lead_id"]),
                        int(call.arguments["outreach_event_id"]) if call.arguments.get("outreach_event_id") else None,
                        str(call.arguments["slot_token"]),
                    )
                elif call.name in {"update_lead_status", "record_call_outcome"}:
                    value = {"status": apply_call_outcome(
                        trace, lead_id=str(call.arguments["lead_id"]),
                        event_id=int(call.arguments["outreach_event_id"]),
                        outcome=str(call.arguments["outcome"]),
                    )}
                else:
                    raise ValueError(f"unsupported tool: {call.name}")
                results.append(tool_success(call.tool_call_id, value, call.name))
            except (KeyError, TypeError, ValueError) as exc:
                trace.log("tool_call_failed", tool=call.name, error_category=type(exc).__name__)
                results.append(tool_error(call.tool_call_id, str(exc), call.name))
            except Exception as exc:  # noqa: BLE001 - isolate one failed call in a Vapi batch
                trace.log("tool_call_failed", tool=call.name, error_category=type(exc).__name__)
                results.append(tool_error(call.tool_call_id, "The request could not be completed.", call.name))
        trace.complete(result_count=len(results))
        return {"results": results}
    except HTTPException:
        raise
    except Exception as exc:
        trace.fail(exc)
        raise HTTPException(status_code=400, detail="invalid tool request") from exc


@app.post("/api/v1/vapi/webhook")
async def vapi_webhook(request: Request):
    await require_vapi_auth(request)
    trace = WorkflowTrace("vapi_webhook", "api", trace_id_var.get())
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - invalid webhook JSON is durably reported as ignored
        body = {}
    message = body.get("message") if isinstance(body, dict) else {}
    message = message if isinstance(message, dict) else {}
    call = message.get("call") if isinstance(message.get("call"), dict) else {}
    call_id = str(call.get("id") or body.get("id") or "")
    event_type = str(message.get("type") or "unknown")
    if not call_id:
        trace.log("validation_failed", reason="missing_call_id")
        return {"ok": True, "ignored": "missing_call_id"}
    with transaction() as conn:
        inserted = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) values('vapi',%s,%s,%s) "
            "on conflict(provider,event_id) do nothing returning id",
            (call_id, event_type, json.dumps(body)),
        ).fetchone()
    if not inserted:
        trace.complete(outcome="duplicate")
        return {"ok": True, "duplicate": True}
    trace.log("webhook_persisted", provider_event_id=inserted["id"], event_type=event_type)
    try:
        if event_type == "end-of-call-report":
            context = extract_vapi_context(body)
            lead_id = str(context.get("lead_id") or "")
            event_id = context.get("outreach_event_id")
            ended = str(message.get("endedReason") or "")
            outcome = "voicemail" if "voicemail" in ended.lower() else (
                "no_answer" if "answer" in ended.lower() or "silence" in ended.lower() else "manual"
            )
            if lead_id and event_id:
                apply_call_outcome(trace, lead_id=lead_id, event_id=int(event_id), outcome=outcome, source="webhook")
            with transaction() as conn:
                lead_exists = conn.execute("select 1 from leads where id=%s", (lead_id,)).fetchone() if lead_id else None
                if lead_exists:
                    answer_state = outcome if outcome in {"voicemail", "no_answer"} else "human"
                    conn.execute(
                        "insert into call_logs(lead_id,vapi_call_id,dialed_at,ended_at,answer_state,ended_reason) "
                        "values(%s,%s,coalesce(%s::timestamptz,now()),%s::timestamptz,%s,%s) "
                        "on conflict(vapi_call_id) do nothing",
                        (lead_id, call_id, message.get("startedAt"), message.get("endedAt"), answer_state, ended),
                    )
                conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
        else:
            with transaction() as conn:
                conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
        trace.complete()
    except Exception as exc:  # noqa: BLE001 - webhook receipt must remain durable on processing failure
        trace.fail(exc, provider_event_id=inserted["id"])
        with transaction() as conn:
            conn.execute(
                "update provider_events set processing_error=%s,processing_attempts=processing_attempts+1,"
                "next_attempt_at=now()+interval '1 minute' where id=%s",
                (str(exc)[:500], inserted["id"]),
            )
    return {"ok": True, "persisted": True}


@app.post("/api/v1/twilio/inbound-sms")
async def twilio_inbound_sms(request: Request):
    form_data = {str(k): str(v) for k, v in (await request.form()).items()}
    await require_twilio_auth(request, form_data)
    trace = WorkflowTrace("twilio_inbound_sms", "api", trace_id_var.get())
    phone = form_data.get("From", "").strip()
    text = form_data.get("Body", "").strip()
    sid = form_data.get("MessageSid") or f"missing-{uuid4().hex}"
    if not phone:
        raise HTTPException(status_code=400, detail="missing From")
    with transaction() as conn:
        lead = conn.execute(
            "select id from leads where phone_e164=%s order by created_at desc limit 1", (phone,)
        ).fetchone()
        conn.execute(
            "insert into sms_messages(lead_id,direction,body,occurred_at,delivery_status,provider_message_id) "
            "values(%s,'inbound',%s,now(),'received',%s) on conflict(provider_message_id) do nothing",
            (lead["id"] if lead else None, text, sid),
        )
    command = text.lower()
    if command in {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}:
        explicit_opt_out(trace, phone, "sms", "twilio_inbound")
    elif command == "call" and lead:
        with transaction() as conn:
            conn.execute(
                "update leads set status='callback_scheduled',callback_requested_at=now(),status_changed_at=now() "
                "where id=%s", (lead["id"],),
            )
        trace.log("callback_requested", lead_id=str(lead["id"]))
    trace.complete()
    return JSONResponse({"ok": True})


@app.post("/api/v1/twilio/message-status")
async def twilio_message_status(request: Request):
    form_data = {str(k): str(v) for k, v in (await request.form()).items()}
    await require_twilio_auth(request, form_data)
    trace = WorkflowTrace("twilio_message_status", "api", trace_id_var.get())
    sid = form_data.get("MessageSid", "")
    status = form_data.get("MessageStatus", "").lower()
    mapped = status if status in {"queued", "sent", "delivered", "undelivered", "failed"} else "sent"
    with transaction() as conn:
        receipt_id = f"message-status:{sid}:{mapped}"
        inserted = conn.execute(
            "insert into provider_events(provider,event_id,event_type,payload) "
            "values('twilio',%s,'message-status',%s) on conflict(provider,event_id) do nothing returning id",
            (receipt_id, json.dumps(form_data)),
        ).fetchone()
        if not inserted:
            trace.complete(outcome="duplicate")
            return {"ok": True, "duplicate": True}
        conn.execute(
            "update sms_messages set delivery_status=%s,delivered_at=case when %s='delivered' then now() "
            "else delivered_at end,updated_at=now() where provider_message_id=%s",
            (mapped, mapped, sid),
        )
        conn.execute("update provider_events set processed_at=now() where id=%s", (inserted["id"],))
    trace.complete(message_status=mapped)
    return {"ok": True}
