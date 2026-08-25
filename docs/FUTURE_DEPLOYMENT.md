# Future deployment notes

The current target is local execution. The existing container images can later run on EC2 or a container
service. A deployment will need public HTTPS for the API, one API process, exactly one cadence worker while
rate governors are process-local, health checks, persistent log shipping, and secrets supplied outside the
image. Required secrets are the Supabase database URL and real provider credentials listed in `.env.example`.
Do not expose the mock provider in production. Configure Vapi authentication, Twilio request validation,
database TLS, backups, monitoring, and the required healthcare vendor agreements before handling PHI.

## Mandatory production cleanup

Before a production release:

- Set `APP_ENV=production` and `TEST_MODE=false`.
- Remove `supabase/dev/reset_test_lead_by_name.sql` and the automatic replacement call from
  `create_test_lead` in `src/rpt_agent/cli.py`, or exclude the entire synthetic `test-lead` command from the
  production image.
- Add a release check that fails if `TEST_MODE=true`, any `is_test=true` lead exists in the target database,
  or `supabase/dev/` is packaged into the production artifact.
