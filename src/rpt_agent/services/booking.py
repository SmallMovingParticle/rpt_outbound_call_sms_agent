from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

from ..db import transaction
from ..observability import WorkflowTrace
from ..providers import ProviderClients, ProviderError
from ..security import sign_slot, verify_slot
from .lead_status import mark_booked


class BookingService:
    def __init__(self, providers: ProviderClients | None = None):
        self.providers = providers or ProviderClients()

    def availability(
        self, trace: WorkflowTrace, lead_id: str, start: date, days: int = 7
    ) -> dict[str, Any]:
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
            trace,
            location=row["stride_location_id"],
            duration=row["stride_default_duration_mins"],
            clinician_ids=row["stride_clinician_ids"],
            start_date=start,
            end_date=min(start + timedelta(days=max(1, days) - 1), start + timedelta(days=30)),
        )[:5]
        expires = int(time.time()) + 300
        values = []
        for slot in slots:
            payload = json.dumps(
                {
                    "lead_id": lead_id,
                    "clinician_id": slot.clinician_id,
                    "date": slot.local_date,
                    "time": slot.local_time,
                    "timezone": slot.timezone,
                },
                separators=(",", ":"),
            )
            display_time = datetime.strptime(slot.local_time, "%H:%M:%S").replace(
                tzinfo=UTC
            ).strftime("%I:%M %p").lstrip("0")
            values.append({
                "date": slot.local_date,
                "time": slot.local_time,
                "spoken_time": display_time,
                "timezone": slot.timezone,
                "slot_token": sign_slot(payload, expires),
            })
        trace.log("availability_prepared", slot_count=len(values))
        return {"status": "ok", "slots": values}

    def book(
        self,
        trace: WorkflowTrace,
        lead_id: str,
        event_id: int | None,
        slot_token: str,
        patient_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        trace.log("request_parsed", lead_id=lead_id, event_id=event_id)
        payload_text, _ = verify_slot(slot_token)
        slot = json.loads(payload_text)
        if slot.get("lead_id") != lead_id:
            raise ValueError("slot token belongs to another lead")
        with transaction() as conn:
            lead = conn.execute(
                "select l.*,ps.stride_location_id,ps.stride_appointment_type_id,"
                "ps.stride_default_duration_mins,ps.stride_case_title,ps.stride_location_timezone "
                "from leads l join practice_settings ps on ps.practice_id=l.practice_id "
                "where l.id=%s for update of l",
                (lead_id,),
            ).fetchone()
            if not lead:
                raise ValueError("lead not found")
            patient_data = patient_data or {}
            first_name = lead["first_name"] or patient_data.get("first_name", "").strip()
            last_name = lead["last_name"] or patient_data.get("last_name", "").strip()
            date_of_birth = lead["date_of_birth"]
            if not date_of_birth and patient_data.get("date_of_birth"):
                date_of_birth = date.fromisoformat(patient_data["date_of_birth"])
            if not first_name or not last_name or not date_of_birth:
                return {"status": "missing_patient_data"}
            if (
                first_name != lead["first_name"]
                or last_name != lead["last_name"]
                or date_of_birth != lead["date_of_birth"]
            ):
                conn.execute(
                    "update leads set first_name=%s,last_name=%s,date_of_birth=%s,"
                    "full_name=trim(%s || ' ' || %s) where id=%s",
                    (first_name, last_name, date_of_birth, first_name, last_name, lead_id),
                )
                lead["first_name"] = first_name
                lead["last_name"] = last_name
                lead["date_of_birth"] = date_of_birth
            if event_id is not None:
                event = conn.execute(
                    "select lead_id,channel from outreach_events where id=%s", (event_id,)
                ).fetchone()
                if not event or str(event["lead_id"]) != lead_id or event["channel"] != "call":
                    raise ValueError("lead and call outreach event do not match")
            existing = conn.execute(
                "select id,state,stride_appointment_id from appointments where lead_id=%s "
                "and state in ('booking','scheduled','unknown') order by id desc limit 1", (lead_id,),
            ).fetchone()
            if existing:
                return {
                    "status": "already_booked",
                    "appointment_id": existing["stride_appointment_id"],
                }
            booking_key = (
                f"{lead_id}:{slot['date']}:{slot['time']}:{lead['stride_appointment_type_id']}"
            )
            appointment = conn.execute(
                "insert into appointments(lead_id,practice_id,outreach_event_id,booking_source,state,"
                "booking_key,clinician_id,location_id,appointment_type_id) "
                "values(%s,%s,%s,'voice_agent','booking',%s,%s,%s,%s) returning id",
                (
                    lead_id,
                    lead["practice_id"],
                    event_id,
                    booking_key,
                    slot["clinician_id"],
                    lead["stride_location_id"],
                    lead["stride_appointment_type_id"],
                ),
            ).fetchone()
            local_id = appointment["id"]
            trace.log("booking_reserved", appointment_id=local_id)

        try:
            live = self.providers.stride_availability(
                trace,
                location=lead["stride_location_id"],
                duration=lead["stride_default_duration_mins"],
                clinician_ids=str(slot["clinician_id"]),
                start_date=date.fromisoformat(slot["date"]),
                end_date=date.fromisoformat(slot["date"]),
            )
            if not any(
                item.local_date == slot["date"] and item.local_time == slot["time"] for item in live
            ):
                self._mark_booking(local_id, "failed", "slot unavailable")
                return {"status": "slot_unavailable"}
            patient_id = lead["stride_patient_id"]
            if not patient_id:
                patient_id = self.providers.stride_create(trace, "patients", {
                    "first_name": lead["first_name"],
                    "last_name": lead["last_name"],
                    "date_of_birth": lead["date_of_birth"].isoformat(),
                    "contact_info": {
                        "mobile_phone_number": lead["phone_e164"],
                        "personal_email": lead["email"] or "",
                        "preferred_contact_method": "P",
                    },
                    "primary_address": {},
                })
            case_id = lead["stride_case_id"]
            if not case_id:
                case_id = self.providers.stride_create(
                    trace, "cases", {"patient_id": patient_id, "title": lead["stride_case_title"]}
                )
            start_utc, end_utc = self._slot_utc(slot, lead["stride_default_duration_mins"])
            stride_id = self.providers.stride_create(trace, "appointments", {
                "case_id": case_id,
                "primary_attendee": slot["clinician_id"],
                "location": lead["stride_location_id"],
                "appointment_type": lead["stride_appointment_type_id"],
                "start_date_utc": start_utc.isoformat(),
                "end_date_utc": end_utc.isoformat(),
                "is_pending": True,
                "appointment_status": "O",
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
                "update appointments set state='scheduled',stride_appointment_id=%s,start_utc=%s,"
                "end_utc=%s,confirmed_at=now(),updated_at=now() where id=%s",
                (stride_id, start_utc, end_utc, local_id),
            )
            if event_id is not None:
                conn.execute(
                    "update outreach_events set status='delivered',settled_at=now(),settled_by='tool',"
                    "outcome='booked' where id=%s and lead_id=%s "
                    "and status not in ('delivered','failed','skipped')",
                    (event_id, lead_id),
                )
            mark_booked(conn, lead_id, "stride_booking")
            conn.execute(
                "insert into notification_log(lead_id,appointment_id,notification_type,channel,status,payload) "
                "values(%s,%s,'sms_appointment_booked','sms','queued',%s) on conflict do nothing",
                (lead_id, local_id, json.dumps({"start_utc": start_utc.isoformat()})),
            )
            event_payload = {
                "event_type": "appointment.booked.v1",
                "event_id": str(uuid4()),
                "lead_id": lead_id,
                "first_name": lead["first_name"],
                "last_name": lead["last_name"],
                "email": lead["email"],
                "phone": lead["phone_e164"],
                "birthday": lead["date_of_birth"].isoformat(),
                "appointment_type_id": lead["stride_appointment_type_id"],
                "appointment_start_utc": start_utc.isoformat(),
                "provider_id": slot["clinician_id"],
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
        return {
            "status": "confirmed",
            "appointment_id": stride_id,
            "spoken_confirmation": f"Your appointment is confirmed for {local_display}.",
        }

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
                "update appointments set state=%s,stride_error=%s,needs_staff_review=%s,"
                "updated_at=now() where id=%s",
                (state, error[:500], state == "unknown", appointment_id),
            )

    @staticmethod
    def _flag_review(lead_id: str, reason: str) -> None:
        with transaction() as conn:
            conn.execute(
                "update leads set needs_review=true,review_reason=%s,review_flagged_at=now() "
                "where id=%s",
                (reason, lead_id),
            )
