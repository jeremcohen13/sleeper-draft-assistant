"""Name normalization and rankings matching edge cases."""

from draft_assistant.players import (
    RankedPlayer,
    match_player,
    match_rankings,
    normalize_name,
    normalize_position,
    normalize_team,
)


def test_normalize_name_suffixes_and_punctuation():
    assert normalize_name("Marvin Harrison Jr.") == "marvinharrison"
    assert normalize_name("Kenneth Walker III") == normalize_name("Kenneth Walker")
    assert normalize_name("Odell Beckham Jr") == "odellbeckham"
    assert normalize_name("Amon-Ra St. Brown") == "amonrastbrown"
    assert normalize_name("Ja'Marr Chase") == "jamarrchase"
    assert normalize_name("Ka’imi Fairbairn") == "kaimifairbairn"
    assert normalize_name("D.J. Moore") == "djmoore"
    assert normalize_name("José Ramírez") == "joseramirez"
    assert normalize_name("  Michael  Pittman Sr. ") == "michaelpittman"
    assert normalize_name("Derrick Kelly II") == "derrickkelly"
    assert normalize_name("V") == "v"  # lone suffix-looking token survives


def test_normalize_team_and_position():
    assert normalize_team("JAC") == "JAX" and normalize_team("WSH") == "WAS" and normalize_team("LA") == "LAR"
    assert normalize_team("kc") == "KC" and normalize_team("FA") is None and normalize_team("") is None
    assert normalize_position("WR12") == "WR" and normalize_position("DST3") == "DEF"
    assert normalize_position("D/ST") == "DEF" and normalize_position("PK") == "K" and normalize_position(None) is None


def test_basic_and_suffix_match(db):
    pid, note = match_player(RankedPlayer(1, "Marvin Harrison Jr.", "WR", "ARI"), db)
    assert pid == "1" and note is None
    pid, _ = match_player(RankedPlayer(2, "Kenneth Walker III", "RB", "SEA"), db)
    assert pid == "4"
    pid, _ = match_player(RankedPlayer(3, "Jose Ramirez", "RB", "WAS"), db)
    assert pid == "12"


def test_same_name_disambiguated_by_position_then_team(db):
    pid, note = match_player(RankedPlayer(5, "Josh Allen", "QB", "BUF"), db)
    assert pid == "5" and note is None
    pid, note = match_player(RankedPlayer(50, "Mike Williams", "WR", "NYJ"), db)
    assert pid == "8" and note is None
    pid, note = match_player(RankedPlayer(51, "Mike Williams", "WR", "LAC"), db)
    assert pid == "7"
    pid, note = match_player(RankedPlayer(200, "Tyler Davis", "K", "JAC"), db)
    assert pid == "9"
    pid, note = match_player(RankedPlayer(201, "Tyler Davis", "TE", "GB"), db)
    assert pid == "10"
    # No team given and two same-position candidates -> best by search_rank, with a note.
    pid, note = match_player(RankedPlayer(52, "Mike Williams", "WR", None), db)
    assert pid == "7" and "ambiguous" in note


def test_team_mismatch_still_matches_with_note(db):
    pid, note = match_player(RankedPlayer(30, "Brian Thomas Jr.", "WR", "JAC"), db)
    assert pid == "13" and note is None
    pid, note = match_player(RankedPlayer(34, "Travis Etienne Jr.", "RB", "JAC"), db)
    assert pid == "15" and "team mismatch" in note


def test_defense_mapping(db):
    for name, team in (("Philadelphia Eagles", "PHI"), ("Eagles", None), ("PHI D/ST", None), ("Jacksonville Jaguars", "JAC"), ("Washington Commanders", "WSH"), ("San Francisco 49ers", None)):
        pid, note = match_player(RankedPlayer(150, name, "DEF", team), db)
        assert pid in ("PHI", "JAX", "WAS", "SF"), (name, team, note)
    assert match_player(RankedPlayer(150, "Jacksonville Jaguars", "DEF", "JAC"), db)[0] == "JAX"
    assert match_player(RankedPlayer(150, "Commanders", "DEF", None), db)[0] == "WAS"


def test_overrides_by_id_name_and_team(db):
    ov = {"Hollywood Harrison": "1", "Some Guy": "Kenneth Walker", "Philly D": "PHI"}
    assert match_player(RankedPlayer(1, "Hollywood Harrison", "WR", None), db, ov)[0] == "1"
    assert match_player(RankedPlayer(2, "Some Guy", "RB", None), db, ov)[0] == "4"
    assert match_player(RankedPlayer(3, "Philly D", "DEF", None), db, ov)[0] == "PHI"
    pid, note = match_player(RankedPlayer(4, "Nobody", "WR", None), db, {"Nobody": "does-not-exist"})
    assert pid is None and "override" in note


def test_match_rankings_reports_unmatched_with_suggestions_and_dupes(db):
    rows = [
        RankedPlayer(1, "Ja'Marr Chase", "WR", "CIN"),
        RankedPlayer(2, "Ja'marr Chase Jr.", "WR", "CIN"),  # same player, different spelling -> duplicate
        RankedPlayer(3, "Marvin Harrisson Jr.", "WR", "ARI"),  # typo -> unmatched w/ suggestion
        RankedPlayer(4, "Kenneth Walker III", "RB", "SEA"),
    ]
    res = match_rankings(rows, db)
    assert [p.name for p in res.matched] == ["Ja'Marr Chase", "Kenneth Walker III"]
    assert res.matched[0].sleeper_id == "3"
    names = {u.player.name: u for u in res.unmatched}
    assert "duplicate" in names["Ja'marr Chase Jr."].reason
    assert any("Marvin Harrison" in s for s in names["Marvin Harrisson Jr."].suggestions)
    assert res.by_sleeper_id["4"].name == "Kenneth Walker III"
