import json
from pathlib import Path

from jobbot.discovery.custom_sites.microsoft import parse_microsoft_search_payload


def test_parse_microsoft_search_payload_from_fixture():
    payload = json.loads(
        Path("fixtures/discovery/microsoft/search_positions_sample.json").read_text(
            encoding="utf-8"
        )
    )

    batch = parse_microsoft_search_payload(
        company_name="Microsoft",
        search_url="https://apply.careers.microsoft.com/careers/search",
        payload=payload,
    )

    assert batch.source.value == "custom_site"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "1772801"
    assert str(batch.jobs[0].canonical_url) == "https://apply.careers.microsoft.com/careers/job/1772801"
    assert batch.jobs[0].location_normalized == "redmond, washington, united states"
    assert batch.jobs[0].remote_type == "onsite"
    assert batch.jobs[1].remote_type == "remote"
    assert batch.jobs[1].ats_vendor == "microsoft-careers"
