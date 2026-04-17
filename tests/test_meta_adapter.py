import json
from pathlib import Path

from jobbot.discovery.custom_sites.meta import parse_meta_search_payload


def test_parse_meta_search_payload_from_fixture():
    payload = json.loads(
        Path("fixtures/discovery/meta/search_jobs_sample.json").read_text(encoding="utf-8")
    )

    batch = parse_meta_search_payload(
        company_name="Meta",
        search_url="https://www.metacareers.com/jobsearch",
        payload=payload,
    )

    assert batch.source.value == "custom_site"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "meta-1001"
    assert str(batch.jobs[0].canonical_url) == "https://www.metacareers.com/profile/job_details/meta-1001"
    assert batch.jobs[0].location_normalized == "menlo park, california"
    assert batch.jobs[0].remote_type == "onsite"
    assert batch.jobs[1].location_normalized == "remote united states"
    assert batch.jobs[1].remote_type == "remote"
    assert batch.jobs[1].ats_vendor == "meta-careers"
