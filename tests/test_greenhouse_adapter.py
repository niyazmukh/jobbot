import json
from pathlib import Path

from jobbot.discovery.greenhouse.adapter import parse_greenhouse_board_payload


def test_parse_greenhouse_board_payload_from_fixture():
    payload = json.loads(
        Path("fixtures/discovery/greenhouse/board_jobs_sample.json").read_text(encoding="utf-8")
    )

    batch = parse_greenhouse_board_payload(
        company_name="Example Corp",
        board_url="https://boards.greenhouse.io/example",
        payload=payload,
    )

    assert batch.source.value == "greenhouse"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "12345"
    assert str(batch.jobs[0].canonical_url) == "https://boards.greenhouse.io/example/jobs/12345"
    assert batch.jobs[0].remote_type == "remote"
    assert batch.jobs[1].remote_type == "onsite"
