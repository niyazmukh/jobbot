from jobbot.discovery.normalization import normalize_location


def test_normalize_location_handles_common_aliases():
    assert normalize_location("NYC") == "new york city"
    assert normalize_location("New York, NY") == "new york city"
    assert normalize_location("Remote - Canada") == "remote canada"
    assert normalize_location("Menlo Park, CA") == "menlo park, california"
