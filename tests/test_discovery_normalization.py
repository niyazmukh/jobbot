from jobbot.discovery.normalization import canonicalize_job_url, normalize_company_name, normalize_location


def test_canonicalize_job_url_removes_tracking_and_fragment():
    url = "https://boards.greenhouse.io/example/jobs/12345/?gh_src=tracker&utm_source=test#apply"

    assert canonicalize_job_url(url) == "https://boards.greenhouse.io/example/jobs/12345"


def test_normalize_company_name_and_location():
    assert normalize_company_name("  Example   Corp ") == "example corp"
    assert normalize_location(" Remote   - Canada ") == "remote canada"


def test_normalize_location_expands_region_abbreviations():
    assert normalize_location("Redmond, WA") == "redmond, washington"
    assert normalize_location("Toronto, ON") == "toronto, ontario"
