import json
from pathlib import Path

from jobbot.discovery.lever.adapter import parse_lever_postings_payload


def test_parse_lever_postings_payload_from_fixture():
    payload = json.loads(
        Path("fixtures/discovery/lever/postings_sample.json").read_text(encoding="utf-8")
    )

    batch = parse_lever_postings_payload(
        company_name="Example Corp",
        postings_url="https://jobs.lever.co/example",
        payload=payload,
    )

    assert batch.source.value == "lever"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "lever-001"
    assert batch.jobs[0].location_normalized == "new york city"
    assert batch.jobs[0].remote_type == "hybrid"
    assert batch.jobs[1].location_normalized == "remote canada"
    assert batch.jobs[1].remote_type == "remote"
