from datetime import date

from rpt_agent.worker import Job, compute_send_time, format_phone, render_sms_template


def test_phone_normalization():
    assert format_phone("(949) 555-1212") == "+19495551212"
    assert format_phone("bad") is None


def test_existing_scheduler_stays_inside_business_day():
    settings = {
        "timezone": "America/Los_Angeles", "holidays": [],
        "business_hours": {str(day): {"open": "09:00", "close": "17:00"} for day in range(1, 6)},
    }
    result = compute_send_time(settings, "lead-1", date(2026, 8, 24), 0)
    local = result.astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles"))
    assert 9 <= local.hour <= 16


def test_sms_template_renders_name_and_booking_link():
    job = Job(
        event_id=1,
        lead_id="lead-1",
        channel="sms",
        phone="+19495551212",
        name="Synthetic Patient",
        body="Hi {name}, book here: {link}",
        booking_link_url="https://example.test/book",
        day_offset=1,
        vapi_assistant_id=None,
        vapi_phone_number_id=None,
    )
    assert render_sms_template(job) == "Hi Synthetic, book here: https://example.test/book"
