from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from .db import transaction
from .observability import WorkflowTrace
from .providers import ProviderClients, ProviderError
from .security import sign_slot, verify_slot
from .vapi_contract import extract_vapi_context

VALID_OUTCOMES = {
    "booked", "not_interested", "no_answer", "voicemail", "callback", "transferred", "manual"
}


def record_status(conn, lead_id: str, old: str | None, new: str, source: str, reason: str) -> None:
    conn.execute(
        "insert into lead_status_history (lead_id,from_status,to_status,source,reason) values (%s,%s,%s,%s,%s)",
        (lead_id, old, new, source, reason),
    )


def mark_booked(conn, lead_id: str, source: str) -> None:
    lead = conn.execute("select status from leads where id=%s for update", (lead_id,)).fetchone()
    if not lead:
        raise ValueError("lead not found")
    conn.execute(
        "update outreach_events set status='skipped',updated_at=now() where lead_id=%s and status='planned'",
        (lead_id,),
    )
    conn.execute(
        "update leads set status='booked',cadence_state='completed',last_call_outcome='booked',"
        "status_changed_at=now() where id=%s",
        (lead_id,),
    )
    if lead["status"] != "booked":
        record_status(conn, lead_id, lead["status"], "booked", source, "appointment confirmed")


def apply_call_outcome(
    trace: WorkflowTrace, *, lead_id: str, event_id: int, outcome: str, source: str = "tool"
) -> str:
    trace.log("validation_started", lead_id=lead_id, event_id=event_id)
    if outcome not in VALID_OUTCOMES:
        trace.log("validation_failed", reason="invalid_outcome")
        raise ValueError("invalid call outcome")
    with transaction() as conn:
        trace.log("database_operation_started", operation="lock_lead_event")
        event = conn.execute(
            "select id,lead_id,status from outreach_events where id=%s for update", (event_id,)
        ).fetchone()
        lead = conn.execute(
            "select id,status,phone_e164 from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead or not event or str(event["lead_id"]) != str(lead_id):
            trace.log("validation_failed", reason="lead_event_mismatch")
            raise ValueError("lead and outreach event do not match")
        if event["status"] in {"delivered", "failed", "skipped"}:
            trace.log("state_transition_skipped", current_status=event["status"])
            return "already settled"
        conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by=%s,outcome=%s "
            "where id=%s and status not in ('delivered','failed','skipped')",
            (source, outcome, event_id),
        )
        if outcome == "booked":
            mark_booked(conn, lead_id, source)
        elif outcome == "not_interested":
            conn.execute(
                "update leads set status='declined',cadence_state='terminated',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'", (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "declined", source, "not interested")
        elif outcome == "callback":
            conn.execute(
                "update leads set status='callback_scheduled',cadence_state='active',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            record_status(conn, lead_id, lead["status"], "callback_scheduled", source, "callback requested")
        elif outcome in {"no_answer", "voicemail"}:
            conn.execute("update leads set last_call_outcome=%s where id=%s", (outcome, lead_id))
        else:
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',last_call_outcome=%s,"
                "needs_review=true,review_reason=%s,review_flagged_at=now(),status_changed_at=now() where id=%s",
                (outcome, f"call outcome: {outcome}", lead_id),
            )
            record_status(conn, lead_id, lead["status"], "needs_attention", source, outcome)
        trace.log("database_operation_completed", operation="settle_call")
    trace.log("state_transition_applied", outcome=outcome)
    return "recorded"


def explicit_opt_out(trace: WorkflowTrace, phone: str, channel: str, source: str) -> None:
    if channel not in {"sms", "call"}:
        raise ValueError("channel must be sms or call")
    with transaction() as conn:
        leads = conn.execute(
            "select id,status,call_opt_out,sms_opt_out from leads where phone_e164=%s for update", (phone,)
        ).fetchall()
        for lead in leads:
            column = "sms_opt_out" if channel == "sms" else "call_opt_out"
            other_opted_out = lead["call_opt_out"] if channel == "sms" else lead["sms_opt_out"]
            conn.execute(
                f"update leads set {column}=true,status=case when %s then 'do_not_contact' else status end,"
                "cadence_state=case when %s then 'terminated' else cadence_state end,"
                "status_changed_at=case when %s then now() else status_changed_at end where id=%s",
                (other_opted_out, other_opted_out, other_opted_out, lead["id"]),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel=%s and status='planned'",
                (lead["id"], channel),
            )
            if other_opted_out and lead["status"] != "do_not_contact":
                record_status(
                    conn, str(lead["id"]), lead["status"], "do_not_contact", source,
                    "all outreach channels opted out",
                )
    trace.log("state_transition_applied", transition="explicit_opt_out", channel=channel)


class BookingService:
    def __init__(self, providers: ProviderClients | None = None):
        self.providers = providers or ProviderClients()

    def availability(self, trace: WorkflowTrace, lead_id: str, start: date, days: int = 7) -> dict[str, Any]:
        trace.log("request_parsed", lead_id=lead_id, start_date=start.isoformat(), days=days)
        with transaction() as conn:
            row = conn.execute(
                "select l.id,ps.stride_location_id,ps.stride_clinician_ids,"
                "ps.stride_default_duration_mins from leads l join practice_settings ps "
                "on ps.practice_id=l.practice_id where l.id=%s", (lead_id,),
            ).fetchone()
        if not row:
            raise ValueError("lead not found")
        slots = self.providers.stride_availability(
            trace, location=row["stride_location_id"], duration=row["stride_default_duration_mins"],
            clinician_ids=row["stride_clinician_ids"], start_date=start,
            end_date=min(start + timedelta(days=max(1, days) - 1), start + timedelta(days=30)),
        )[:5]
        expires = int(time.time()) + 300
        values = []
        for slot in slots:
            payload = json.dumps({"lead_id": lead_id, "clinician_id": slot.clinician_id,
                                  "date": slot.local_date, "time": slot.local_time,
                                  "timezone": slot.timezone}, separators=(",", ":"))
            display_time = datetime.strptime(slot.local_time, "%H:%M:%S").replace(
                tzinfo=UTC
            ).strftime("%I:%M %p").lstrip("0")
            values.append({"date": slot.local_date, "time": slot.local_time,
                           "spoken_time": display_time, "timezone": slot.timezone,
                           "slot_token": sign_slot(payload, expires)})
        trace.log("availability_prepared", slot_count=len(values))
        return {"status": "ok", "slots": values}

    def book(self, trace: WorkflowTrace, lead_id: str, event_id: int | None, slot_token: str) -> dict[str, Any]:
        trace.log("request_parsed", lead_id=lead_id, event_id=event_id)
        payload_text, _ = verify_slot(slot_token)
        slot = json.loads(payload_text)
        if slot.get("lead_id") != lead_id:
            raise ValueError("slot token belongs to another lead")
        with transaction() as conn:
            lead = conn.execute(
                "select l.*,ps.stride_location_id,ps.stride_appointment_type_id,"
                "ps.stride_default_duration_mins,ps.stride_case_title,ps.stride_location_timezone from leads l "
                "join practice_settings ps on ps.practice_id=l.practice_id where l.id=%s for update of l",
                (lead_id,),
            ).fetchone()
            if not lead:
                raise ValueError("lead not found")
            if not lead["first_name"] or not lead["last_name"] or not lead["date_of_birth"]:
                return {"status": "missing_patient_data"}
            if event_id is not None:
                event = conn.execute(
                    "select lead_id from outreach_events where id=%s", (event_id,)
                ).fetchone()
                if not event or str(event["lead_id"]) != lead_id:
                    raise ValueError("lead and outreach event do not match")
            existing = conn.execute(
                "select id,state,stride_appointment_id from appointments where lead_id=%s "
                "and state in ('booking','scheduled','unknown') order by id desc limit 1", (lead_id,),
            ).fetchone()
            if existing:
                return {"status": "already_booked", "appointment_id": existing["stride_appointment_id"]}
            booking_key = f"{lead_id}:{slot['date']}:{slot['time']}:{lead['stride_appointment_type_id']}"
            appointment = conn.execute(
                "insert into appointments(lead_id,practice_id,outreach_event_id,booking_source,state,booking_key,"
                "clinician_id,location_id,appointment_type_id) values(%s,%s,%s,'voice_agent','booking',%s,%s,%s,%s) "
                "returning id",
                (lead_id, lead["practice_id"], event_id, booking_key, slot["clinician_id"],
                 lead["stride_location_id"], lead["stride_appointment_type_id"]),
            ).fetchone()
            local_id = appointment["id"]
            trace.log("booking_reserved", appointment_id=local_id)

        try:
            live = self.providers.stride_availability(
                trace, location=lead["stride_location_id"], duration=lead["stride_default_duration_mins"],
                clinician_ids=str(slot["clinician_id"]), start_date=date.fromisoformat(slot["date"]),
                end_date=date.fromisoformat(slot["date"]),
            )
            if not any(s.local_date == slot["date"] and s.local_time == slot["time"] for s in live):
                self._mark_booking(local_id, "failed", "slot unavailable")
                return {"status": "slot_unavailable"}
            patient_id = lead["stride_patient_id"]
            if not patient_id:
                patient_id = self.providers.stride_create(trace, "patients", {
                    "first_name": lead["first_name"], "last_name": lead["last_name"],
                    "date_of_birth": lead["date_of_birth"].isoformat(),
                    "contact_info": {"mobile_phone_number": lead["phone_e164"],
                                     "personal_email": lead["email"] or "",
                                     "preferred_contact_method": "P"},
                    "primary_address": {},
                })
            case_id = lead["stride_case_id"]
            if not case_id:
                case_id = self.providers.stride_create(
                    trace, "cases", {"patient_id": patient_id, "title": lead["stride_case_title"]}
                )
            start_utc, end_utc = self._slot_utc(slot, lead["stride_default_duration_mins"])
            stride_id = self.providers.stride_create(trace, "appointments", {
                "case_id": case_id, "primary_attendee": slot["clinician_id"],
                "location": lead["stride_location_id"],
                "appointment_type": lead["stride_appointment_type_id"],
                "start_date_utc": start_utc.isoformat(), "end_date_utc": end_utc.isoformat(),
                "is_pending": True, "appointment_status": "O",
            })
        except ProviderError as exc:
            state = "unknown" if exc.ambiguous else "failed"
            self._mark_booking(local_id, state, str(exc))
            if exc.code == "400" and "already exists" in str(exc).lower():
                self._flag_review(lead_id, "Stride duplicate patient requires mapping")
                return {"status": "manual_review"}
            if exc.ambiguous:
                self._flag_review(lead_id, "ambiguous Stride booking result; do not retry")
                return {"status": "manual_review"}
            return {"status": "failed"}

        with transaction() as conn:
            conn.execute(
                "update leads set stride_patient_id=%s,stride_case_id=%s where id=%s",
                (patient_id, case_id, lead_id),
            )
            conn.execute(
                "update appointments set state='scheduled',stride_appointment_id=%s,start_utc=%s,end_utc=%s,"
                "confirmed_at=now(),updated_at=now() where id=%s",
                (stride_id, start_utc, end_utc, local_id),
            )
            if event_id is not None:
                conn.execute(
                    "update outreach_events set status='delivered',settled_at=now(),settled_by='tool',"
                    "outcome='booked' where id=%s and lead_id=%s and status not in ('delivered','failed','skipped')",
                    (event_id, lead_id),
                )
            mark_booked(conn, lead_id, "stride_booking")
            conn.execute(
                "insert into notification_log(lead_id,appointment_id,notification_type,channel,status,payload) "
                "values(%s,%s,'sms_appointment_booked','sms','queued',%s) on conflict do nothing",
                (lead_id, local_id, json.dumps({"start_utc": start_utc.isoformat()})),
            )
            event_payload = {
                "event_type": "appointment.booked.v1", "event_id": str(uuid4()), "lead_id": lead_id,
                "first_name": lead["first_name"], "last_name": lead["last_name"], "email": lead["email"],
                "phone": lead["phone_e164"], "birthday": lead["date_of_birth"].isoformat(),
                "appointment_type_id": lead["stride_appointment_type_id"],
                "appointment_start_utc": start_utc.isoformat(), "provider_id": slot["clinician_id"],
                "stride_appointment_id": stride_id,
            }
            conn.execute(
                "insert into integration_outbox(event_id,event_type,aggregate_id,payload,status) "
                "values(%s,'appointment.booked.v1',%s,%s,'pending') on conflict(event_id) do nothing",
                (event_payload["event_id"], str(local_id), json.dumps(event_payload)),
            )
        trace.log("booking_confirmed", appointment_id=local_id, stride_appointment_id=stride_id)
        local_display = datetime.fromisoformat(f"{slot['date']}T{slot['time']}").strftime(
            "%A, %B %d at %I:%M %p"
        ).replace(" 0", " ")
        return {"status": "confirmed", "appointment_id": stride_id,
                "spoken_confirmation": f"Your appointment is confirmed for {local_display}."}

    @staticmethod
    def _slot_utc(slot: dict[str, Any], duration: int) -> tuple[datetime, datetime]:
        from zoneinfo import ZoneInfo

        local = datetime.fromisoformat(f"{slot['date']}T{slot['time']}").replace(
            tzinfo=ZoneInfo(slot["timezone"])
        )
        start = local.astimezone(UTC)
        return start, start + timedelta(minutes=duration)

    @staticmethod
    def _mark_booking(appointment_id: int, state: str, error: str) -> None:
        with transaction() as conn:
            conn.execute(
                "update appointments set state=%s,stride_error=%s,needs_staff_review=%s,updated_at=now() where id=%s",
                (state, error[:500], state == "unknown", appointment_id),
            )

    @staticmethod
    def _flag_review(lead_id: str, reason: str) -> None:
        with transaction() as conn:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                (reason, lead_id),
            )


def process_pending_integrations(trace: WorkflowTrace, providers: ProviderClients | None = None) -> dict[str, int]:
    providers = providers or ProviderClients()
    counts = {"sms": 0, "handoff": 0, "failed": 0}
    with transaction() as conn:
        notifications = conn.execute(
            "select n.id,n.lead_id,n.appointment_id,n.payload,l.phone_e164,l.first_name,a.start_utc,"
            "ps.stride_location_timezone from notification_log n join leads l on l.id=n.lead_id "
            "join appointments a on a.id=n.appointment_id "
            "join practice_settings ps on ps.practice_id=l.practice_id "
            "where n.status='queued' order by n.id limit 20 for update of n skip locked"
        ).fetchall()
        for row in notifications:
            conn.execute(
                "update notification_log set status='sending',updated_at=now() where id=%s", (row["id"],)
            )
        outbox = conn.execute(
            "select id,payload from integration_outbox where status='pending' and next_attempt_at<=now() "
            "order by id limit 20 for update skip locked"
        ).fetchall()
        for row in outbox:
            conn.execute(
                "update integration_outbox set status='sending',attempts=attempts+1,updated_at=now() where id=%s",
                (row["id"],),
            )
    for row in notifications:
        try:
            from zoneinfo import ZoneInfo

            local_start = row["start_utc"].astimezone(ZoneInfo(row["stride_location_timezone"]))
            when = local_start.strftime("%A, %B %d at %I:%M %p").replace(" 0", " ")
            greeting = f"Hi {row['first_name']}, " if row["first_name"] else ""
            body = (
                f"{greeting}your Rausch PT appointment is confirmed for {when}. "
                "Call 949-276-5401 with questions. Reply STOP to opt out."
            )
            sid = providers.send_sms(trace, row["phone_e164"], body)
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='sent',provider_ref=%s,sent_at=now(),updated_at=now() "
                    "where id=%s",
                    (sid, row["id"]),
                )
            counts["sms"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                status = "unknown" if exc.ambiguous else "failed"
                conn.execute(
                    "update notification_log set status=%s,error=%s,updated_at=now() where id=%s",
                    (status, str(exc), row["id"]),
                )
                if exc.ambiguous:
                    conn.execute(
                        "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                        ("ambiguous confirmation SMS; do not retry", row["lead_id"]),
                    )
            counts["failed"] += 1
    for row in outbox:
        try:
            providers.deliver_handoff(trace, row["payload"])
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='delivered',delivered_at=now(),updated_at=now() "
                    "where id=%s", (row["id"],)
                )
            counts["handoff"] += 1
        except ProviderError as exc:
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='pending',last_error=%s,"
                    "next_attempt_at=now()+make_interval(secs=>least(3600,power(2,attempts)::int)),"
                    "updated_at=now() where id=%s",
                    (str(exc), row["id"]),
                )
            counts["failed"] += 1
    trace.log("integration_batch_completed", **counts)
    return counts


def reprocess_failed_vapi_events(trace: WorkflowTrace) -> int:
    """Retry webhook processing only after the original payload is durable."""
    with transaction() as conn:
        rows = conn.execute(
            "select id,payload from provider_events where provider='vapi' and processed_at is null "
            "and processing_error is not null and next_attempt_at<=now() and processing_attempts<5 "
            "order by id limit 20"
        ).fetchall()
    completed = 0
    for row in rows:
        try:
            body = row["payload"]
            message = body.get("message") if isinstance(body, dict) else {}
            message = message if isinstance(message, dict) else {}
            context = extract_vapi_context(body)
            lead_id = str(context.get("lead_id") or "")
            event_id = context.get("outreach_event_id")
            ended = str(message.get("endedReason") or "")
            outcome = "voicemail" if "voicemail" in ended.lower() else (
                "no_answer" if "answer" in ended.lower() or "silence" in ended.lower() else "manual"
            )
            if not lead_id or not event_id:
                raise ValueError("webhook cannot be associated with a lead and event")
            apply_call_outcome(
                trace, lead_id=lead_id, event_id=int(event_id), outcome=outcome, source="webhook"
            )
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processed_at=now(),processing_error=null where id=%s",
                    (row["id"],),
                )
            completed += 1
            trace.log("webhook_reprocessed", provider_event_id=row["id"])
        except Exception as exc:  # noqa: BLE001 - isolate malformed durable webhook records
            with transaction() as conn:
                conn.execute(
                    "update provider_events set processing_attempts=processing_attempts+1,processing_error=%s,"
                    "next_attempt_at=now()+make_interval(mins=>least(60,power(2,processing_attempts)::int)) "
                    "where id=%s", (str(exc)[:500], row["id"]),
                )
            trace.log("webhook_reprocess_failed", provider_event_id=row["id"],
                      error_category=type(exc).__name__)
    return completed
