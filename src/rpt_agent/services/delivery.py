from __future__ import annotations

from zoneinfo import ZoneInfo

from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..vapi_contract import extract_vapi_context
from .lead_status import apply_call_outcome


def process_pending_integrations(
    trace: WorkflowTrace, providers: ProviderClients | None = None
) -> dict[str, int]:
    providers = providers or ProviderClients()
    counts = {"sms": 0, "handoff": 0, "failed": 0}
    with transaction() as conn:
        notifications = conn.execute(
            "select n.id,n.lead_id,n.appointment_id,n.payload,l.phone_e164,l.first_name,a.start_utc,"
            "coalesce(ps.stride_location_timezone,l.timezone,'America/Los_Angeles') "
            "as stride_location_timezone from notification_log n join leads l on l.id=n.lead_id "
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
                "update integration_outbox set status='sending',attempts=attempts+1,updated_at=now() "
                "where id=%s",
                (row["id"],),
            )
    for row in notifications:
        try:
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
                    "update notification_log set status='sent',provider_ref=%s,sent_at=now(),"
                    "updated_at=now() where id=%s",
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
                        "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                        "where id=%s",
                        ("ambiguous confirmation SMS; do not retry", row["lead_id"]),
                    )
            counts["failed"] += 1
        except Exception as exc:  # noqa: BLE001 - an accepted SMS cannot be retried safely
            trace.log(
                "integration_delivery_failed",
                provider="twilio",
                notification_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                conn.execute(
                    "update notification_log set status='unknown',error=%s,updated_at=now() where id=%s",
                    (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                    "where id=%s",
                    ("confirmation SMS delivery requires review", row["lead_id"]),
                )
            counts["failed"] += 1
    for row in outbox:
        try:
            providers.deliver_handoff(trace, row["payload"])
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='delivered',delivered_at=now(),"
                    "updated_at=now() where id=%s", (row["id"],)
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
        except Exception as exc:  # noqa: BLE001 - outbox event_id makes retries idempotent
            trace.log(
                "integration_delivery_failed",
                provider="keap",
                outbox_id=row["id"],
                error_category=type(exc).__name__,
            )
            with transaction() as conn:
                conn.execute(
                    "update integration_outbox set status='pending',last_error=%s,"
                    "next_attempt_at=now()+interval '1 minute',updated_at=now() where id=%s",
                    (f"unexpected delivery error: {type(exc).__name__}", row["id"]),
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
                    "update provider_events set processing_attempts=processing_attempts+1,"
                    "processing_error=%s,next_attempt_at=now()+make_interval("
                    "mins=>least(60,power(2,processing_attempts)::int)) where id=%s",
                    (str(exc)[:500], row["id"]),
                )
            trace.log(
                "webhook_reprocess_failed",
                provider_event_id=row["id"],
                error_category=type(exc).__name__,
            )
    return completed
