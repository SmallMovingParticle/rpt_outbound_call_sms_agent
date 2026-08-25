from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .providers import ProviderClients, ProviderError
from .services import process_pending_integrations, reprocess_failed_vapi_events


@dataclass(frozen=True)
class Job:
    event_id: int
    lead_id: str
    channel: str
    phone: str
    name: str
    body: str | None
    booking_link_url: str | None
    day_offset: int | None
    vapi_assistant_id: str | None
    vapi_phone_number_id: str | None


def format_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d+]", "", str(raw).strip())
    digits = cleaned.replace("+", "")
    if len(digits) < 10 or len(digits) > 15:
        return None
    if cleaned.startswith("+"):
        return cleaned if len(digits) >= 11 else None
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return "+" + digits


def compute_send_time(settings: dict, lead_id: str, start_on: date, day_offset: int) -> datetime:
    """Existing cadence spreading logic, intentionally unchanged for this milestone."""
    import random

    hours = settings["business_hours"] or {}
    holidays = _parse_holidays(settings["holidays"])
    tz = ZoneInfo(settings["timezone"] or "America/Los_Angeles")
    target = start_on + timedelta(days=day_offset)
    for _ in range(21):
        window = hours.get(str(target.isoweekday()))
        holiday = holidays.get(target)
        if window and holiday is not False:
            open_t = _parse_time(window["open"])
            close_t = _parse_time(holiday) if isinstance(holiday, str) else _parse_time(window["close"])
            open_dt = datetime.combine(target, open_t, tzinfo=tz)
            close_dt = datetime.combine(target, close_t, tzinfo=tz)
            usable = max((close_dt - open_dt).total_seconds() - 1800, 0)
            slot = (hash(str(lead_id)) % 10_000) / 10_000
            offset = max(0, min(slot * usable + random.uniform(-120, 120), usable))
            return (open_dt + timedelta(seconds=offset)).astimezone(ZoneInfo("UTC"))
        target += timedelta(days=1)
    return datetime.combine(target, dtime(9, 0), tzinfo=tz).astimezone(ZoneInfo("UTC"))


def _parse_holidays(raw) -> dict:
    out = {}
    for item in raw or []:
        if isinstance(item, str):
            out[date.fromisoformat(item)] = False
        elif isinstance(item, dict) and "date" in item:
            out[date.fromisoformat(item["date"])] = item.get("close", False)
    return out


def _parse_time(value: str) -> dtime:
    hours, minutes = value.split(":")[:2]
    return dtime(int(hours), int(minutes))


def materialize_cadence(conn, lead_id: str, practice_id: int, start_on: date) -> int:
    """Materialize the configured cadence using the existing scheduler."""
    settings = conn.execute(
        "select ps.business_hours,ps.holidays,p.timezone from practice_settings ps "
        "join practices p on p.id=ps.practice_id where ps.practice_id=%s", (practice_id,),
    ).fetchone()
    if not settings:
        raise ValueError("practice settings not found")
    steps = conn.execute(
        "select id,day_offset,channel from cadence_steps where practice_id=%s and is_active "
        "order by day_offset,step_order", (practice_id,),
    ).fetchall()
    for step in steps:
        conn.execute(
            "insert into outreach_events(lead_id,cadence_step_id,channel,day_offset,scheduled_for,status) "
            "values(%s,%s,%s,%s,%s,'planned')",
            (lead_id, step["id"], step["channel"], step["day_offset"],
             compute_send_time(settings, lead_id, start_on, step["day_offset"])),
        )
    conn.execute(
        "update leads set cadence_started_on=%s,cadence_state='active',status='in_progress',"
        "status_changed_at=now() where id=%s", (start_on, lead_id),
    )
    return len(steps)


CLAIM_SQL = """
with due as (
 select oe.id from outreach_events oe
 join leads l on l.id=oe.lead_id
 join practices p on p.id=l.practice_id
 join practice_settings ps on ps.practice_id=l.practice_id
 where oe.status='planned' and oe.scheduled_for<=now() and l.cadence_state='active'
 and l.status not in ('booked','declined','do_not_contact','invalid_phone')
 and ((oe.channel='call' and not l.call_opt_out) or (oe.channel='sms' and not l.sms_opt_out))
 and (oe.channel<>'call' or coalesce(l.line_type,'unknown')<>'mobile' or l.consent_captured_at is not null)
 and not exists(select 1 from suppressed_numbers s where s.phone_e164=l.phone_e164)
 and (now() at time zone coalesce(l.timezone,p.timezone))::time >= time '08:00'
 and (now() at time zone coalesce(l.timezone,p.timezone))::time < time '21:00'
 and (
   oe.channel<>'call' or (
     select count(*) from outreach_events day_event
     where day_event.lead_id=l.id and day_event.channel='call' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_calls_per_lead_per_day
 )
 and (
   oe.channel<>'sms' or (
     select count(*) from outreach_events day_event
     where day_event.lead_id=l.id and day_event.channel='sms' and day_event.executed_at is not null
     and day_event.executed_at >= (
       date_trunc('day',now() at time zone coalesce(l.timezone,p.timezone))
       at time zone coalesce(l.timezone,p.timezone)
     )
   ) < ps.max_sms_per_lead_per_day
 )
 order by oe.scheduled_for limit %s for update of oe skip locked
)
update outreach_events oe set status='in_flight',updated_at=now()
from due where oe.id=due.id
returning oe.id,oe.lead_id,oe.channel,oe.cadence_step_id,oe.day_offset
"""


def claim_jobs(trace: WorkflowTrace, limit: int = 20) -> list[Job]:
    trace.log("database_operation_started", operation="claim_due_events")
    with transaction() as conn:
        rows = conn.execute(CLAIM_SQL, (limit,)).fetchall()
        jobs = []
        for row in rows:
            context = conn.execute(
                "select l.full_name,l.phone_e164,ps.vapi_assistant_id,ps.vapi_phone_number_id,"
                "ps.booking_link_url,mt.body from leads l "
                "join practice_settings ps on ps.practice_id=l.practice_id "
                "left join message_templates mt on mt.cadence_step_id=%s and mt.is_active where l.id=%s limit 1",
                (row["cadence_step_id"], row["lead_id"]),
            ).fetchone()
            jobs.append(Job(
                row["id"], str(row["lead_id"]), row["channel"], context["phone_e164"],
                context["full_name"], context["body"], context["booking_link_url"], row["day_offset"],
                context["vapi_assistant_id"], context["vapi_phone_number_id"],
            ))
    trace.log("database_operation_completed", operation="claim_due_events", job_count=len(jobs))
    return jobs


def render_sms_template(job: Job) -> str:
    first_name = job.name.split()[0] if job.name else ""
    return (job.body or "").replace("{name}", first_name).replace(
        "{link}", job.booking_link_url or ""
    ).strip()


def dispatch_job(trace: WorkflowTrace, job: Job, providers: ProviderClients) -> tuple[Job, str, str]:
    child = WorkflowTrace("outreach_dispatch", "worker", trace.trace_id)
    try:
        if job.channel == "call":
            ref = providers.create_vapi_call(child, {
                "assistantId": job.vapi_assistant_id, "phoneNumberId": job.vapi_phone_number_id,
                "customer": {"number": job.phone}, "assistantOverrides": {"variableValues": {
                    "lead_id": job.lead_id, "outreach_event_id": str(job.event_id), "patient_name": job.name,
                    "booking_link": job.booking_link_url or "", "day_offset": job.day_offset,
                }},
            })
        else:
            body = render_sms_template(job)
            if not body:
                raise ProviderError("twilio", "missing_template", "SMS template is missing")
            ref = providers.send_sms(child, job.phone, body)
        child.complete(provider_ref=ref)
        return job, "accepted", ref
    except ProviderError as exc:
        child.fail(exc)
        return job, "unknown" if exc.ambiguous else "failed", str(exc)


def run_safety_checks(trace: WorkflowTrace) -> dict[str, int]:
    """Flag ambiguous work for review; never blindly resend it."""
    counts = {
        "stuck_dispatches": 0,
        "orphaned_calls": 0,
        "stuck_notifications": 0,
        "stuck_handoffs": 0,
        "exhausted_leads": 0,
    }
    with transaction() as conn:
        stuck = conn.execute(
            "update outreach_events set status='unknown',settled_at=now(),settled_by='sweeper',"
            "failure_reason='worker stopped before dispatch result was recorded' "
            "where status='in_flight' and updated_at<now()-interval '15 minutes' returning lead_id"
        ).fetchall()
        orphaned = conn.execute(
            "update outreach_events set status='delivered',settled_at=now(),settled_by='sweeper',outcome='manual',"
            "failure_reason='call outcome was not reported' where status='attempted' "
            "and executed_at<now()-interval '2 hours' returning lead_id"
        ).fetchall()
        for row in stuck:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                ("ambiguous provider dispatch; do not retry", row["lead_id"]),
            )
        for row in orphaned:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                ("call outcome not reported", row["lead_id"]),
            )
        stuck_notifications = conn.execute(
            "update notification_log set status='unknown',error=%s,updated_at=now() "
            "where status='sending' and updated_at<now()-interval '15 minutes' returning lead_id",
            ("worker stopped after SMS send began; do not retry",),
        ).fetchall()
        for row in stuck_notifications:
            if row["lead_id"]:
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    ("ambiguous confirmation SMS; do not retry", row["lead_id"]),
                )
        stuck_handoffs = conn.execute(
            "update integration_outbox set status='pending',next_attempt_at=now(),last_error=%s,updated_at=now() "
            "where status='sending' and updated_at<now()-interval '15 minutes' returning id",
            ("worker stopped during delivery; retry with the same event_id",),
        ).fetchall()
        exhausted = conn.execute(
            "update leads l set status='closed_no_response',cadence_state='completed',status_changed_at=now() "
            "where l.cadence_state='active' and l.cadence_started_on+14<=current_date "
            "and not exists(select 1 from outreach_events oe where oe.lead_id=l.id "
            "and oe.status in ('planned','in_flight','attempted')) returning id"
        ).fetchall()
        counts.update(
            stuck_dispatches=len(stuck),
            orphaned_calls=len(orphaned),
            stuck_notifications=len(stuck_notifications),
            stuck_handoffs=len(stuck_handoffs),
            exhausted_leads=len(exhausted),
        )
    trace.log("safety_checks_completed", **counts)
    return counts


def run_tick() -> dict[str, int]:
    trace = WorkflowTrace("worker_tick", "worker")
    providers = ProviderClients()
    run_safety_checks(trace)
    reprocess_failed_vapi_events(trace)
    jobs = claim_jobs(trace)
    results = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(dispatch_job, trace, job, providers) for job in jobs]
        results.extend(future.result() for future in as_completed(futures))
    counts = {"accepted": 0, "failed": 0, "unknown": 0}
    with transaction() as conn:
        for job, state, value in results:
            if state == "accepted" and job.channel == "call":
                conn.execute(
                    "update outreach_events set status='attempted',executed_at=now(),provider='vapi',"
                    "provider_ref=%s,vapi_call_id=%s where id=%s and status='in_flight'", (value, value, job.event_id),
                )
                conn.execute(
                    "update leads set call_attempts=call_attempts+1,last_contacted_at=now() where id=%s",
                    (job.lead_id,),
                )
            elif state == "accepted":
                conn.execute(
                    "update outreach_events set status='delivered',executed_at=now(),settled_at=now(),"
                    "settled_by='worker',provider='twilio',provider_ref=%s where id=%s and status='in_flight'",
                    (value, job.event_id),
                )
                conn.execute(
                    "insert into sms_messages(lead_id,outreach_event_id,direction,body,occurred_at,delivery_status,"
                    "provider_message_id) values(%s,%s,'outbound',%s,now(),'queued',%s) "
                    "on conflict(provider_message_id) do nothing",
                    (job.lead_id, job.event_id, render_sms_template(job), value),
                )
                conn.execute(
                    "update leads set last_contacted_at=now() where id=%s", (job.lead_id,)
                )
            else:
                conn.execute(
                    "update outreach_events set status=%s,executed_at=now(),settled_at=now(),settled_by='worker',"
                    "failure_reason=%s where id=%s and status='in_flight'",
                    ("failed" if state == "failed" else "unknown", value[:500], job.event_id),
                )
                conn.execute(
                    "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() where id=%s",
                    (f"ambiguous dispatch: {value}" if state == "unknown" else f"dispatch failed: {value}", job.lead_id),
                )
            counts[state] += 1
    process_pending_integrations(trace, providers)
    trace.complete(**counts)
    return counts


def main() -> None:
    configure_logging("worker")
    settings = get_settings()
    errors = settings.runtime_errors("worker")
    if errors:
        raise RuntimeError("; ".join(errors))
    interval = settings.worker_poll_seconds
    logging.getLogger(__name__).info("worker_started", extra={"event": "worker_started"})
    while True:
        started = time.monotonic()
        try:
            run_tick()
        except Exception:
            logging.getLogger(__name__).exception("worker_tick_failed", extra={"event": "worker_tick_failed"})
        time.sleep(max(0, interval - (time.monotonic() - started)))


if __name__ == "__main__":
    main()
