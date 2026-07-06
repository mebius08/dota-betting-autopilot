from app.collectors.match_collector import normalize_team_name


def test_normalize_team_name_strips_and_lowercases() -> None:
    assert normalize_team_name(" TEAM SPIRIT ") == "team spirit"


def test_normalize_team_name_collapses_internal_whitespace() -> None:
    assert normalize_team_name("Team   Spirit") == "team spirit"
    assert normalize_team_name("Team\tSpirit") == "team spirit"
    assert normalize_team_name("Team\nSpirit") == "team spirit"


def test_normalize_team_name_keeps_empty_string_empty() -> None:
    assert normalize_team_name("") == ""
    assert normalize_team_name("   \t\n  ") == ""
