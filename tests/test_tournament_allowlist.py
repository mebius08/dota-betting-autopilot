from __future__ import annotations

import pytest

from app.tournament_allowlist import (
    TournamentAllowlistError,
    load_tournament_allowlist,
)


EXPECTED_LEAGUE_IDS = {
    17765,
    17795,
    17891,
    17907,
    18058,
    18107,
    18111,
    18304,
    18305,
    18306,
    18307,
    18308,
    18309,
    18324,
    18358,
    18375,
    18433,
    18633,
    18920,
    18988,
    19099,
    19101,
    19239,
    19269,
    19422,
    19435,
    19543,
    19656,
    19696,
    19719,
    19785,
    19890,
    19891,
    19892,
    19893,
    19894,
    20009,
    20053,
}


def test_versioned_allowlist_contains_exact_approved_league_ids() -> None:
    allowlist = load_tournament_allowlist()

    assert allowlist.version == 1
    assert set(allowlist.league_ids) == EXPECTED_LEAGUE_IDS
    assert len(allowlist.entries) == len(EXPECTED_LEAGUE_IDS)


def test_ti_regional_qualifiers_are_the_only_qualifier_stage() -> None:
    allowlist = load_tournament_allowlist()
    qualifier_ids = {
        entry.league_id
        for entry in allowlist.entries
        if entry.stage == "ti_closed_qualifier"
    }

    assert qualifier_ids == {
        18304,
        18305,
        18306,
        18307,
        18308,
        18309,
        19890,
        19891,
        19892,
        19893,
        19894,
    }


def test_ti_qualifier_ids_map_to_exact_region_editions() -> None:
    allowlist = load_tournament_allowlist()
    qualifier_editions = {
        entry.league_id: entry.edition
        for entry in allowlist.entries
        if entry.stage == "ti_closed_qualifier"
    }

    assert qualifier_editions == {
        19890: "2026 Regional Qualifier North America",
        19891: "2026 Regional Qualifier South America",
        19892: "2026 Regional Qualifier Europe",
        19893: "2026 Regional Qualifier China",
        19894: "2026 Regional Qualifier Southeast Asia",
        18304: "2025 Regional Qualifier Eastern Europe",
        18305: "2025 Regional Qualifier South America",
        18306: "2025 Regional Qualifier China",
        18307: "2025 Regional Qualifier North America",
        18308: "2025 Regional Qualifier Southeast Asia",
        18309: "2025 Regional Qualifier Western Europe",
    }


@pytest.mark.parametrize(
    "league_id",
    [
        19842,
        19532,
        18681,
        19539,
        19268,
        999999,
    ],
    ids=[
        "ti-2026-open-qualifier-china",
        "dreamleague-division-2-season-4",
        "ti-2025-practice",
        "blast-slam-vii-qualifier",
        "fissure-universe-episode-8-open-qualifier",
        "arbitrary-unknown-positive-id",
    ],
)
def test_excluded_and_unknown_league_ids_remain_default_denied(
    league_id: int,
) -> None:
    allowlist = load_tournament_allowlist()

    with pytest.raises(
        TournamentAllowlistError,
        match=rf"league_id_not_allowlisted league_id={league_id}",
    ):
        allowlist.require(league_id)
