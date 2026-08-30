# RPT Outreach Agent

Pre-production Python service for the Rausch PT 14-day outreach cadence. Vapi, Twilio, Stride, Supabase,
and the Keap-team handoff have real provider adapters; mocks remain available only through an explicit
Compose profile for automated/local testing. Production cadence spreading is unchanged.

<p>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/FastAPI-API-009688?logo=fastapi&logoColor=white">
  <img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-Supabase-4169E1?logo=postgresql&logoColor=white">
  <img alt="Docker" src="https://img.shields.io/badge/Docker-Compose-2496ED?logo=docker&logoColor=white">
  <img alt="Tests" src="https://img.shields.io/badge/tests-56%20passed-22C55E">
</p>

## What it delivers

- Executes a durable 14-day voice and SMS outreach cadence with safe retries and idempotent settlement.
- Lets a Vapi voice agent check live Stride availability, book an evaluation, and update lead outcomes.
- Sends Twilio confirmations, publishes a signed Keap handoff, and keeps provider failures reviewable.
- Powers an authenticated operations CRM for lead intake, cadence monitoring, appointments, and analytics.

## Architecture

```mermaid
flowchart LR
    Staff[Authorized staff] --> CRM[Next.js CRM]
    CRM --> API[FastAPI dashboard API]
    Worker[Cadence worker] --> API
    API <--> DB[(Supabase Postgres)]
    API --> Vapi[Vapi voice agent]
    API --> Twilio[Twilio SMS]
    API --> Stride[Stride scheduling]
    API --> Keap[Signed Keap handoff]
```

## CRM preview

These are synthetic design-reference screens; no patient data is shown.

| Outreach operations | Lead pipeline |
| --- | --- |
| ![Outreach operations dashboard](dashboard/salesforce-concept-v4-final/01-outreach-home.jpg) | ![Lead pipeline board](dashboard/salesforce-concept-v4-final/02-leads-board.jpg) |

The companion frontend lives in
[`SmallMovingParticle/rpt_frontend`](https://github.com/SmallMovingParticle/rpt_frontend). Its hosted deployment
is access-protected and is not a public demo.

## Project layout

```text
src/rpt_agent/
  api.py                 FastAPI assembly and trace middleware
  routes/
    availability.py      POST /api/v1/tools/check-availability
    appointments.py      POST /api/v1/tools/create-appointment
    leads.py             POST /api/v1/webhooks/vapi/lead-status
    vapi.py              Compatibility tool endpoint and durable end report
    twilio.py             Signed inbound/status callbacks
    dashboard.py          Authenticated CRM read/write boundary
  services/
    booking.py            Stride booking workflow and idempotency
    lead_status.py        Lead/call transitions and duplicate guard
    delivery.py           SMS/outbox/webhook delivery
    stride_service.py     Stride HTTP contract
    vapi_service.py       Vapi outbound-call contract
    twilio_service.py     Twilio message contract
    keap_service.py       Signed Keap-team handoff contract
    provider_http.py      Shared bounded HTTP/retry behavior
  providers.py           Small compatibility facade over provider services
  worker.py              Cadence claim, dispatch, settlement, and sweepers
  mock_server.py         Deterministic Stride/Twilio/Keap/Vapi fixtures
  usage_report.py        Real test-provider cost ledger refresh and client report
  config.py              Environment-driven runtime configuration
  db.py                  Hosted Supabase Postgres pool and transactions
supabase/migrations/      Ordered, idempotent schema migrations
config/                   Vapi tool schemas and assistant prompt
scripts/                  Local start and Vapi synchronization utilities
tests/                    Contract, unit, scenario, and optional DB tests
frontend/                 Separate Next.js/Vinext CRM application and environment
dashboard/                Approved dashboard concept images (design reference only)
```

This keeps the useful API/service/config separation from the reference repository without its duplicated
schedulers, fixed timezone offsets, raw request logging, or AWS-only runtime complexity.

## First local start

1. Copy `.env.example` to `.env` and fill every blank/placeholder from the pre-production secret store.
2. Use the real provider profile:

   ```dotenv
   APP_ENV=preproduction
   VAPI_MODE=real
   TWILIO_MODE=real
   STRIDE_MODE=real
   KEAP_MODE=real
   TEST_MODE=false
   ```

3. Apply/verify the schema. `rpt seed` is for a fresh sandbox only; it intentionally leaves
   `stride_booking_enabled=false`.

   ```powershell
   docker compose run --rm api rpt migrate
   docker compose run --rm api rpt verify
   ```

4. Start and verify everything:

   ```powershell
   docker compose up --build -d
   Invoke-RestMethod http://localhost:8000/health
   Invoke-RestMethod http://localhost:8000/ready
   docker compose logs -f api worker
   ```

The API docs are at `http://localhost:8000/docs`. JSON step logs are also rotated under `logs/`.
`X-Trace-ID` correlates
API, provider, worker, and webhook activity, and PHI/secrets are redacted.

Before enabling real Stride appointment creation, verify the location ID, clinician IDs, numeric Initial
Evaluation appointment-type ID, duration, and IANA booking timezone in `practice_settings`, then set
`stride_booking_enabled=true`. Availability remains usable while this gate is false. The supplied Stride
material did not include the numeric appointment-type ID, so the repository does not guess it.

After confirming that value in Stride, enable only the intended practice:

```sql
update public.practice_settings ps
set stride_appointment_type_id = <confirmed_numeric_id>,
    stride_booking_enabled = true
from public.practices p
where p.id = ps.practice_id and p.slug = 'rausch-pt';
```

For deterministic tests only, start the mock provider explicitly:

```powershell
docker compose --profile mock up --build
```

## ngrok and Vapi

Follow [docs/LOCAL_VAPI_NGROK.md](docs/LOCAL_VAPI_NGROK.md). In short: keep Compose running, start
`ngrok http 8000`, set `PUBLIC_BASE_URL`, and run:

```powershell
$env:PYTHONPATH = "src"
python scripts/sync_vapi.py
```

That command idempotently synchronizes the three synchronous tools to their direct endpoints and the
assistant end-report webhook.
It preserves Vapi's built-in transfer/end-call tools. Tool requests accept the current
`message.toolCallList` contract and a narrow legacy compatibility shape; authenticated business failures
still return HTTP 200 with ordered, single-line results using the exact `toolCallId`.

## Consented synthetic cadence test

Do not use an arbitrary or production patient number. Once the owner of a working number has explicitly
consented, create a marked test lead:

```powershell
docker compose run --rm api rpt test-lead `
  --phone "+1XXXXXXXXXX" `
  --first-name "Synthetic" `
  --last-name "Tester" `
  --dob "1990-01-01" `
  --consent-reference "written-test-consent-2026-08-25"
```

With `TEST_MODE=true`, day 0 starts immediately and each cadence day is one minute. Only `is_test=true`
leads bypass production legal-hour/contact-cap gates for this synthetic test. Normal leads keep the
documented schedule. Reusing the same normalized first and last name automatically removes only the older
`is_test=true` / `synthetic_test` lead and its test workflow records before creating the new run. Replacement
is refused while a call is active and is disabled outside development/test environments. Monitor with:

```powershell
docker compose logs -f worker api
Get-Content logs/rpt-agent-worker.jsonl -Wait
```

This accelerated synthetic flow is retained only as an explicit development tool. Do not enable it in the
pre-production runtime profile.

## Test usage and client cost report

Every accepted real Vapi call and real Twilio SMS for an `is_test=true` lead is written to the durable
`test_usage_ledger`. Mock operations are excluded. The database retains the protected recipient number so
the audit survives test-lead cleanup, while the shareable report contains only the last four digits and a
stable one-way fingerprint.

Refresh provider statuses/prices and rebuild the client report before sharing it:

```powershell
$env:PYTHONPATH = "src"
python scripts/generate_test_usage_report.py
```

The generated report is [testing_updates/CLIENT_TEST_USAGE.md](testing_updates/CLIENT_TEST_USAGE.md).
Provider invoices remain the accounting source of truth because prices can settle or be adjusted later.

## Commands and verification

- `rpt migrate`, `rpt verify`, `rpt seed` — database lifecycle.
- `rpt test-lead ...` — create a consented, accelerated synthetic lead.
- `rpt tick` — run one worker tick for debugging.
- `rpt demo` — fully mocked demo; intentionally refuses to run if any provider is real.
- `pytest -q` — unit/contract suite. Database tests require `TEST_DATABASE_URL` and are skipped otherwise.
- `ruff check .` — static checks.

Provider modes are independent. Changing `VAPI_MODE`, `TWILIO_MODE`, `STRIDE_MODE`, or `KEAP_MODE`
switches adapters without modifying cadence or booking logic.

## CRM frontend

The production-oriented CRM is a separate application under `frontend/`. Browser requests go through its
same-origin `/api/dashboard/*` proxy; the dashboard API token remains server-side and is never placed in a
`NEXT_PUBLIC_*` variable. For local development, make the root and frontend `DASHBOARD_API_TOKEN` values
match, then run the backend and frontend separately:

```powershell
Copy-Item frontend\.env.example frontend\.env
npm --prefix frontend install
npm --prefix frontend run dev
```

Open `http://localhost:3000`. The local synthetic preview is enabled only when
`DASHBOARD_ALLOW_LOCAL_DEMO=true`; production fails closed unless ChatGPT Sites authentication, a Rausch
staff email allowlist enforced on every page and API request, an HTTPS backend origin, and the shared
server-side API token are configured. See
[frontend/README.md](frontend/README.md) for the route map and deployment requirements.

The Add Lead form persists through the authenticated dashboard API. It requires a phone, date of birth,
lead type, location, owner, and explicit contact-consent confirmation; successful creation is audited and
atomically materializes the active outreach cadence. In a development/test environment with `TEST_MODE=true`,
new dashboard leads are marked synthetic and each cadence day uses `TEST_CADENCE_DAY_MINUTES` (currently one
minute). Provider credentials remain backend-only.

The Keap integration is the signed, event-ID-deduplicated webhook owned by the Keap team. It is real when
`KEAP_MODE=real` and `KEAP_HANDOFF_URL` points to that receiver. Direct Keap REST/OAuth contact mutation is
not implemented because no Keap application/OAuth contract was supplied.

## Retry and reconciliation

Transient, safely repeatable provider failures use bounded exponential backoff with jitter. HTTP 429 honors
`Retry-After`; idempotent reads and the event-ID-deduplicated Keap handoff may be retried. Credential,
validation, and other permanent failures stop immediately. Ambiguous Vapi call, Twilio SMS, and Stride
creation results are never blindly repeated because the provider may already have accepted them; those rows
become `unknown` and require provider reconciliation.

Durable retries default to five attempts, starting at 60 seconds and capped at one hour unless a longer
provider `Retry-After` must be honored. Short in-request
retries default to three attempts and are limited to safe operations and brief waits. Configure them with
`RETRY_MAX_ATTEMPTS`, `RETRY_BASE_SECONDS`, `RETRY_MAX_SECONDS`, `HTTP_RETRY_ATTEMPTS`, and
`HTTP_RETRY_BASE_SECONDS`. Run `rpt migrate` before starting the updated worker so the retry,
integration-audit, outcome-source, and Stride booking-gate schema from migrations 013-015 is present.

The `.env` file is git-ignored and excluded from the Docker build context. Since credentials pasted into
chat should be treated as exposed, rotate them before production use and rerun the Vapi sync after updating
the credential in Vapi.
