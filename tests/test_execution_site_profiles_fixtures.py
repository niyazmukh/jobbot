import json
from pathlib import Path

from jobbot.execution.site_profiles import required_fields_for_site, selector_overlay_for_site


def _load_fixture(name: str) -> dict:
    fixture_path = (
        Path(__file__).resolve().parent.parent
        / "fixtures"
        / "execution"
        / "ats_profiles"
        / name
    )
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _assert_profile_fixture(payload: dict) -> None:
    site_vendor = payload["site_vendor"]
    expected_required = payload["required_fields"]

    assert required_fields_for_site(site_vendor) == expected_required

    for case in payload["cases"]:
        selectors, gate, manual = selector_overlay_for_site(site_vendor, case["field_key"])
        expected_selectors = case["expected_profile_selectors"]
        assert gate >= case["min_confidence_gate"]
        assert manual is case["expected_manual_review"]
        assert all(selector in selectors for selector in expected_selectors)


def test_greenhouse_selector_profile_fixture_regression():
    payload = _load_fixture("greenhouse_field_variants.json")
    _assert_profile_fixture(payload)


def test_lever_selector_profile_fixture_regression():
    payload = _load_fixture("lever_field_variants.json")
    _assert_profile_fixture(payload)
