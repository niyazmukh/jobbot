import json
from pathlib import Path

from jobbot.discovery.workday.adapter import parse_workday_search_payload


def test_parse_workday_search_payload_from_fixture():
    payload = json.loads(
        Path("fixtures/discovery/workday/search_jobs_sample.json").read_text(encoding="utf-8")
    )

    batch = parse_workday_search_payload(
        company_name="Example Corp",
        base_url="https://example.wd5.myworkdayjobs.com",
        site_id="Careers",
        payload=payload,
    )

    assert batch.source.value == "workday"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "JR-9001"
    assert str(batch.jobs[0].canonical_url) == (
        "https://example.wd5.myworkdayjobs.com/Careers/job/Toronto-ON/"
        "Senior-Data-Engineer_JR-9001"
    )
    assert batch.jobs[0].employment_type == "Full time"
    assert batch.jobs[0].remote_type == "onsite"
    assert batch.jobs[1].location_normalized == "remote united states"
    assert batch.jobs[1].remote_type == "remote"
