from jobbot.execution.site_profiles import required_fields_for_site, selector_overlay_for_site


def test_selector_overlay_for_supported_sites_returns_expected_selectors():
    greenhouse_selectors, greenhouse_gate, greenhouse_manual = selector_overlay_for_site("greenhouse", "email")
    lever_selectors, lever_gate, lever_manual = selector_overlay_for_site("lever", "resume_upload")
    workday_selectors, workday_gate, workday_manual = selector_overlay_for_site("workday", "first_name")

    assert "input[name='email']" in greenhouse_selectors
    assert greenhouse_gate >= 0.9
    assert greenhouse_manual is False
    assert "input[type='file'][name='resume']" in lever_selectors
    assert lever_gate >= 0.9
    assert lever_manual is False
    assert "input[name='firstName']" in workday_selectors
    assert workday_gate >= 0.9
    assert workday_manual is False


def test_selector_overlay_and_required_fields_fallback_for_unknown_site():
    selectors, gate, manual = selector_overlay_for_site("unknown_vendor", "custom_field")
    required = required_fields_for_site("unknown_vendor")

    assert selectors == ["[data-jobbot-field='custom_field']"]
    assert gate == 0.7
    assert manual is True
    assert required == []
