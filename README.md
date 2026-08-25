# RPT Outreach Agent

Local-first, production-shaped Python service for the Rausch PT 14-day outreach cadence. The current
runtime uses real Vapi outbound calling and real Twilio messaging, with deterministic local mocks for
Stride booking and the Keap-team handoff. Production cadence spreading is unchanged; accelerated time applies only to
rows explicitly marked `is_test=true`.

## Project layout

```text
src/rpt_agent/
  api.py                 FastAPI assembly and trace middleware
  routes/                Vapi, Twilio, and health HTTP boundaries
  services/              Booking, lead-state, and integration workflows
  providers.py           Real/mock provider adapters
  worker.py              Cadence claim, dispatch, settlement, and sweepers
  mock_server.py         Deterministic Stride/Twilio/Keap/Vapi fixtures
  usage_report.py        Real test-provider cost ledger refresh and client report
  config.py              Environment-driven runtime configuration
  db.py                  Hosted Supabase Postgres pool and transactions
supabase/migrations/      Ordered, idempotent schema migrations
config/                   Vapi tool schemas and assistant prompt
scripts/                  Local start and Vapi synchronization utilities
tests/                    Contract, unit, scenario, and optional DB tests
```

This adopts the useful API/service/config separation from the reference repository without copying its
duplicated scheduler implementations or AWS-only runtime complexity.

## First local start

1. Copy `.env.example` to `.env`. Use only a hosted **development** Supabase database.
2. Set `SUPABASE_DB_URL` and the Vapi values. For the current hybrid setup use:

   ```dotenv
   APP_ENV=development
   VAPI_MODE=real
   TWILIO_MODE=real
   STRIDE_MODE=mock
   KEAP_MODE=mock
   TEST_MODE=true
   TEST_CADENCE_DAY_MINUTES=1
   ```

3. Apply/verify the schema and seed the cadence:

   ```powershell
   docker compose run --rm api rpt migrate
   docker compose run --rm api rpt seed
   docker compose run --rm api rpt verify
   ```

4. Start and verify everything:

   ```powershell
   docker compose up --build -d
   Invoke-RestMethod http://localhost:8000/health
   Invoke-RestMethod http://localhost:8000/ready
   Invoke-RestMethod http://localhost:9000/health
   docker compose logs -f api worker mock-provider
   ```

The API docs are at `http://localhost:8000/docs`; mock-provider docs are at
`http://localhost:9000/docs`. JSON step logs are also rotated under `logs/`. `X-Trace-ID` correlates
API, provider, worker, and webhook activity, and PHI/secrets are redacted.

## ngrok and Vapi

Follow [docs/LOCAL_VAPI_NGROK.md](docs/LOCAL_VAPI_NGROK.md). In short: keep Compose running, start
`ngrok http 8000`, set `PUBLIC_BASE_URL`, and run:

```powershell
$env:PYTHONPATH = "src"
python scripts/sync_vapi.py
```

That command idempotently synchronizes the three current synchronous custom tools and assistant webhook.
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

Vapi makes the real phone call. The assistant's availability and appointment tools tunnel through ngrok
to this API, which calls the local Stride mock. Twilio sends real SMS and authenticated status callbacks;
the Keap handoff remains mocked.

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

The `.env` file is git-ignored and excluded from the Docker build context. Since credentials pasted into
chat should be treated as exposed, rotate them before production use and rerun the Vapi sync after updating
the credential in Vapi.
