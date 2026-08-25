from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..db import transaction
from ..observability import WorkflowTrace

VALID_OUTCOMES = {
    "booked", "not_interested", "no_answer", "voicemail", "callback", "transferred", "manual",
    "call_opt_out", "do_not_contact",
}


def record_status(conn, lead_id: str, old: str | None, new: str, source: str, reason: str) -> None:
    conn.execute(
        "insert into lead_status_history (lead_id,from_status,to_status,source,reason) "
        "values (%s,%s,%s,%s,%s)",
        (lead_id, old, new, source, reason),
    )


def mark_booked(conn, lead_id: str, source: str) -> None:
    lead = conn.execute("select status from leads where id=%s for update", (lead_id,)).fetchone()
    if not lead:
        raise ValueError("lead not found")
    conn.execute(
        "update outreach_events set status='skipped',updated_at=now() "
        "where lead_id=%s and status='planned'",
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
    trace: WorkflowTrace,
    *,
    lead_id: str,
    event_id: int,
    outcome: str,
    source: str = "tool",
    callback_requested_at: datetime | None = None,
    callback_notes: str | None = None,
) -> str:
    trace.log("validation_started", lead_id=lead_id, event_id=event_id)
    if outcome not in VALID_OUTCOMES:
        trace.log("validation_failed", reason="invalid_outcome")
        raise ValueError("invalid call outcome")
    with transaction() as conn:
        trace.log("database_operation_started", operation="lock_lead_event")
        event = conn.execute(
            "select id,lead_id,channel,status,outcome from outreach_events where id=%s for update",
            (event_id,),
        ).fetchone()
        lead = conn.execute(
            "select id,status,phone_e164 from leads where id=%s for update", (lead_id,)
        ).fetchone()
        if not lead or not event or str(event["lead_id"]) != str(lead_id):
            trace.log("validation_failed", reason="lead_event_mismatch")
            raise ValueError("lead and outreach event do not match")
        if event["channel"] != "call":
            trace.log("validation_failed", reason="event_is_not_call")
            raise ValueError("outreach event is not a call")
        if event["status"] in {"delivered", "failed", "skipped"}:
            if event["status"] == "delivered" and event["outcome"] == outcome:
                trace.log("state_transition_skipped", current_status=event["status"])
                return "already recorded"
            trace.log("validation_failed", reason="conflicting_terminal_outcome")
            raise ValueError("outreach event is already settled with a different outcome")
        if event["status"] not in {"in_flight", "attempted"}:
            trace.log("validation_failed", reason="event_not_dispatched")
            raise ValueError("call outcome cannot be recorded before dispatch")
        if outcome == "booked":
            appointment = conn.execute(
                "select id from appointments where lead_id=%s and state='scheduled' limit 1",
                (lead_id,),
            ).fetchone()
            if not appointment:
                trace.log("validation_failed", reason="booked_without_appointment")
                raise ValueError("booked requires a confirmed appointment")
        callback_utc = None
        if outcome == "callback":
            if callback_requested_at is None or callback_requested_at.tzinfo is None:
                trace.log("validation_failed", reason="callback_time_required")
                raise ValueError("callback requires a timezone-aware callback_requested_at")
            now = datetime.now(UTC)
            callback_utc = callback_requested_at.astimezone(UTC)
            if callback_utc <= now or callback_utc > now + timedelta(days=30):
                trace.log("validation_failed", reason="callback_time_out_of_range")
                raise ValueError("callback time must be in the next 30 days")
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
                "callback_requested_at=%s,callback_notes=%s,status_changed_at=now() where id=%s",
                (outcome, callback_utc, (callback_notes or "")[:500] or None, lead_id),
            )
            conn.execute(
                "insert into outreach_events(lead_id,channel,scheduled_for,status) "
                "values(%s,'call',%s,'planned')",
                (lead_id, callback_utc),
            )
            record_status(
                conn, lead_id, lead["status"], "callback_scheduled", source, "callback requested"
            )
        elif outcome in {"no_answer", "voicemail"}:
            conn.execute("update leads set last_call_outcome=%s where id=%s", (outcome, lead_id))
        elif outcome == "transferred":
            conn.execute(
                "update leads set status='transferred_human',cadence_state='completed',last_call_outcome=%s,"
                "status_changed_at=now() where id=%s", (outcome, lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'", (lead_id,),
            )
            record_status(conn, lead_id, lead["status"], "transferred_human", source, outcome)
        elif outcome == "call_opt_out":
            conn.execute(
                "update leads set call_opt_out=true,last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly opted out of calls", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and channel='call' and status='planned'",
                (lead_id,),
            )
        elif outcome == "do_not_contact":
            conn.execute(
                "update leads set call_opt_out=true,sms_opt_out=true,status='do_not_contact',"
                "cadence_state='terminated',last_call_outcome=%s,status_reason=%s,"
                "status_changed_at=now() where id=%s",
                (outcome, "caller explicitly requested no contact", lead_id),
            )
            conn.execute(
                "update outreach_events set status='skipped',updated_at=now() "
                "where lead_id=%s and status='planned'",
                (lead_id,),
            )
            conn.execute(
                "insert into suppressed_numbers(phone_e164,reason,source,list_type) "
                "values(%s,'explicit do-not-contact request',%s,'internal') "
                "on conflict(phone_e164) do update set reason=excluded.reason,source=excluded.source,"
                "last_verified_at=now()",
                (lead["phone_e164"], source),
            )
            record_status(conn, lead_id, lead["status"], "do_not_contact", source, outcome)
        else:
            conn.execute(
                "update leads set status='needs_attention',cadence_state='paused',last_call_outcome=%s,"
                "needs_review=true,review_reason=%s,review_flagged_at=now(),status_changed_at=now() "
                "where id=%s",
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
            "select id,status,call_opt_out,sms_opt_out from leads where phone_e164=%s for update",
            (phone,),
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
                    conn,
                    str(lead["id"]),
                    lead["status"],
                    "do_not_contact",
                    source,
                    "all outreach channels opted out",
                )
    trace.log("state_transition_applied", transition="explicit_opt_out", channel=channel)
