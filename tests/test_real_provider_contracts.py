import os
from datetime import UTC, datetime, timedelta

import pytest

from rpt_agent.config import Settings
from rpt_agent.observability import WorkflowTrace
from rpt_agent.providers import ProviderClients

pytestmark = pytest.mark.integration


def test_real_stride_availability_contract():
    if os.getenv("RUN_REAL_PROVIDER_TESTS") != "1":
        pytest.skip("real provider contract tests are disabled")
    settings = Settings(provider_mode="real")
    if not settings.stride_api_token:
        pytest.skip("STRIDE_API_TOKEN is not configured")
    start = datetime.now(UTC).date()
    slots = ProviderClients(settings).stride_availability(
        WorkflowTrace("real_stride_contract", "test"), location=3, duration=60,
        clinician_ids="42", start_date=start, end_date=start + timedelta(days=1),
    )
    assert isinstance(slots, list)
