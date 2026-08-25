from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from .config import get_settings
from .db import transaction
from .observability import WorkflowTrace, configure_logging
from .providers import ProviderClients
from .services import process_pending_integrations
from .sftp_fixtures import load_stride_fixtures
from .worker import materialize_cadence

ROOT = Path(__file__).resolve().parents[2]


def migrate() -> None:
    trace = WorkflowTrace("database_migration", "cli")
    with transaction() as conn:
        conn.execute(
            "create table if not exists public.schema_migrations(version text primary key,applied_at timestamptz not null default now())"
        )
        applied = {row["version"] for row in conn.execute("select version from public.schema_migrations")}
        for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
            if path.name in applied:
                trace.log("migration_skipped", migration=path.name)
                continue
            trace.log("migration_started", migration=path.name)
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into public.schema_migrations(version) values(%s)", (path.name,))
            trace.log("migration_completed", migration=path.name)
    trace.complete()


def verify() -> None:
    with transaction() as conn:
        rows = conn.execute("select version,applied_at from public.schema_migrations order by version").fetchall()
    print(json.dumps(rows, indent=2, default=str))


def seed() -> None:
    trace = WorkflowTrace("database_seed", "cli")
    with transaction() as conn:
        conn.execute((ROOT / "supabase" / "seed.sql").read_text(encoding="utf-8"))
    trace.complete()


def demo() -> None:
    trace = WorkflowTrace("local_demo", "cli")
    with transaction() as conn:
        practice = conn.execute("select id from practices where slug='rausch-pt'").fetchone()
        if not practice:
            raise RuntimeError("run `rpt seed` first")
        referral = f"demo-{uuid4().hex[:8]}"
        lead = conn.execute(
            "insert into leads(practice_id,source_system,external_referral_id,first_name,last_name,full_name,"
            "phone_e164,email,date_of_birth,timezone,status,cadence_state) "
            "values(%s,'demo',%s,'Synthetic','Patient','Synthetic Patient','+15555550123',"
            "'synthetic@example.test','1990-01-01','America/Los_Angeles','in_progress','active') returning id",
            (practice["id"], referral),
        ).fetchone()
        materialize_cadence(conn, str(lead["id"]), practice["id"], datetime.now(UTC).date())
        call_event = conn.execute(
            "select id from outreach_events where lead_id=%s and channel='call' order by day_offset limit 1",
            (lead["id"],),
        ).fetchone()
    providers = ProviderClients()
    call_id = providers.create_vapi_call(trace, {
        "assistantId": "mock-assistant", "phoneNumberId": "mock-phone",
        "customer": {"number": "+15555550123"}, "assistantOverrides": {"variableValues": {
            "lead_id": str(lead["id"]), "outreach_event_id": str(call_event["id"]),
        }},
    })
    with transaction() as conn:
        conn.execute(
            "update outreach_events set status='attempted',executed_at=now(),provider='vapi',provider_ref=%s,"
            "vapi_call_id=%s where id=%s", (call_id, call_id, call_event["id"]),
        )
    base = get_settings().api_base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {get_settings().vapi_webhook_secret}", "X-Trace-ID": trace.trace_id}
    availability_request = {"message": {"type": "tool-calls", "toolCallList": [{
        "id": "demo-availability", "name": "get_available_slots",
        "arguments": {"lead_id": str(lead["id"])},
    }]}}
    response = httpx.post(base + "/api/v1/vapi/tools", json=availability_request, headers=headers, timeout=20)
    response.raise_for_status()
    availability = json.loads(response.json()["results"][0]["result"])
    if not availability["slots"]:
        raise RuntimeError("mock returned no slots")
    booking_request = {"message": {"type": "tool-calls", "toolCallList": [{
        "id": "demo-book", "name": "book_appointment", "arguments": {
            "lead_id": str(lead["id"]), "outreach_event_id": call_event["id"],
            "slot_token": availability["slots"][0]["slot_token"]
        },
    }]}}
    booked = httpx.post(base + "/api/v1/vapi/tools", json=booking_request, headers=headers, timeout=30)
    booked.raise_for_status()
    process_pending_integrations(trace, providers)
    trace.complete(lead_id=str(lead["id"]))
    print(json.dumps({"lead_id": str(lead["id"]), "availability": availability,
                      "booking": json.loads(booked.json()["results"][0]["result"])}, indent=2))


def main() -> None:
    configure_logging("cli")
    parser = argparse.ArgumentParser(prog="rpt")
    parser.add_argument("command", choices=("migrate", "verify", "seed", "demo", "fixtures"))
    args = parser.parse_args()
    {"migrate": migrate, "verify": verify, "seed": seed, "demo": demo,
     "fixtures": lambda: print(json.dumps(load_stride_fixtures(WorkflowTrace("sftp_fixture_import", "cli")), indent=2))}[args.command]()


if __name__ == "__main__":
    main()
