from pathlib import Path

from jobbot.discovery.custom_sites.google import parse_google_results_html


def test_parse_google_results_html_from_fixture():
    html = Path("fixtures/discovery/google/results_page_sample.html").read_text(encoding="utf-8")

    batch = parse_google_results_html(
        company_name="Google",
        page_url="https://www.google.com/about/careers/applications/jobs/results/",
        html=html,
    )

    assert batch.source.value == "custom_site"
    assert len(batch.jobs) == 2
    assert batch.jobs[0].external_job_id == "123456789"
    assert str(batch.jobs[0].canonical_url) == (
        "https://www.google.com/about/careers/applications/jobs/results/"
        "123456789-software-engineer?location=Tel+Aviv"
    )
    assert batch.jobs[0].ats_vendor == "google-careers"
    assert batch.jobs[1].title == "Data Scientist"
