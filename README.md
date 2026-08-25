# RPT AI Agent

Local-first FastAPI and worker implementation for the Rausch PT outreach cadence.
Provider integrations can run entirely against deterministic mocks while the real APIs are completed.

## Quick start

1. Copy `.env.example` to `.env` and set `SUPABASE_DB_URL` to a **development** Supabase project.
2. Run `docker compose run --rm api rpt migrate`.
3. Run `docker compose run --rm api rpt seed`.
4. Run `docker compose up --build`.
5. Open `http://localhost:8000/docs` and `http://localhost:9000/docs`.

Without Docker:

```powershell
./scripts/dev.ps1 setup
./scripts/dev.ps1 migrate
./scripts/dev.ps1 seed
./scripts/dev.ps1 start
```

Use only synthetic patient data in local and demo environments. Detailed redacted JSON logs are written to
service-owned files such as `logs/rpt-agent-api.jsonl` and `logs/rpt-agent-worker.jsonl`;
`X-Trace-ID` links API, worker, provider, and webhook activity. `/health` is liveness-only, while `/ready`
also verifies configuration and the database connection.

## Commands

- `rpt migrate` - apply migrations from `supabase/migrations`.
- `rpt verify` - show the applied migration set.
- `rpt seed` - seed the practice, provider settings, cadence, and message templates.
- `rpt demo` - execute a synthetic mocked booking workflow after the services are running.
- `pytest` - run the local test suite.

Set `PROVIDER_MODE=real` and the relevant credentials to swap provider adapters without changing workflow code.

Vapi may authenticate with either `Authorization: Bearer <VAPI_WEBHOOK_SECRET>` or an HMAC credential using
`X-Vapi-Timestamp` and `X-Vapi-Signature`. The signature is lowercase SHA-256 over
`<timestamp>.<raw-request-body>` using `VAPI_HMAC_SECRET`, and timestamps older than five minutes are rejected.

Mock behavior is selected with `MOCK_SCENARIO` or the `X-Mock-Scenario` request header. Supported common
scenarios are `success`, `malformed`, `rate_limit`, `provider_error`, `delay`, `timeout`,
`duplicate_patient`, `unavailable_slot`, and `overlap`. Synthetic Vapi and Twilio callback payloads are
available from the mock server's documented GET endpoints.
