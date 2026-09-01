"""Projection scoring under league rules, tilt vs generic half-PPR, and VOR."""

from draft_assistant.draft_state import RosterRules
from draft_assistant.projections import BASELINE_HALF_PPR, ProjectionSet, league_points
from tests.conftest import rp


def _row(pid, pos, **stats):
    return {"player_id": pid, "player": {"position": pos}, "stats": {"gp": 17, **stats}}


def test_league_points_uses_every_scored_stat():
    scoring = {"pass_yd": 0.04, "pass_td": 6, "pass_int": -1, "rush_yd": 0.1, "rec": 0.5, "bonus_rec_te": 1.0}
    stats = {"pass_yd": 4000, "pass_td": 30, "pass_int": 10, "rush_yd": 300, "rec": 0, "unused": 999}
    assert league_points(stats, scoring) == 160 + 180 - 10 + 30


def test_tilt_moves_qbs_up_with_six_point_passing_tds():
    six = {**BASELINE_HALF_PPR, "pass_td": 6.0, "pass_int": -1.0}
    rows = [
        _row("qb", "QB", pass_yd=4000, pass_td=30, pass_int=10),   # baseline 160+120-20=260 ; league 160+180-10=330
        _row("rb", "RB", rush_yd=1500, rush_td=12, rec=40, rec_yd=300),  # 150+72+20+30=272 both
        _row("wr", "WR", rec=100, rec_yd=1300, rec_td=8),  # 50+130+48=228 both
    ]
    ps = ProjectionSet(rows, six)
    assert ps.by_id["qb"].rank_baseline == 2 and ps.by_id["qb"].rank_league == 1
    assert ps.by_id["qb"].tilt == 1 and ps.by_id["rb"].tilt == -1
    ranked = [rp(5, "QB Guy", "QB"), rp(6, "RB Guy", "RB")]
    ranked[0].sleeper_id, ranked[1].sleeper_id = "qb", "rb"
    assert ps.attach(ranked) == 2
    assert ranked[0].proj_pts == 330.0 and ranked[0].proj_tilt == 1
    notes = ps.scoring_summary(six)
    assert any("pass_td: 6" in n for n in notes) and any("pass_int: -1" in n for n in notes)


def test_vor_uses_starter_count_and_flex_share():
    rules = RosterRules.from_positions(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "DEF", "BN"])
    rows = [_row(f"rb{i}", "RB", rush_yd=1000 - i * 20) for i in range(40)]
    ps = ProjectionSet(rows, BASELINE_HALF_PPR)
    ps.compute_vor(rules, teams=12)
    # replacement index = 12 * (2 + 0.45*2) = 35 -> 35th RB has VOR 0, the best RB is above it.
    vors = sorted(((p.player_id, p.vor) for p in ps.by_id.values()), key=lambda kv: -kv[1])
    assert vors[0][0] == "rb0" and vors[0][1] > 0
    assert abs(ps.by_id["rb34"].vor) < 1e-9


def test_scoring_tilt_changes_recommendation(rules_std):
    from draft_assistant.draft_state import compute_needs
    from draft_assistant.recommend import RecommendConfig, recommend
    qb = rp(20, "QB Up", "QB", tier=2); qb.proj_tilt = 12
    wr = rp(19, "WR Flat", "WR", tier=2); wr.proj_tilt = 0
    needs = compute_needs([], rules_std)
    rec = recommend([wr, qb], [], rules_std, needs, 2, 16, 15, 10, RecommendConfig())
    assert rec.take.player.name == "QB Up"
    assert any("your scoring moves him up 12" in r for r in rec.take.reasons)


def test_vor_based_tilt_respects_positional_scarcity(rules_std):
    six = {**BASELINE_HALF_PPR, "pass_td": 6.0}
    rows = [_row(f"qb{i}", "QB", pass_yd=4000, pass_td=30 - i) for i in range(20)]  # every QB gains under 6-pt TDs
    rows += [_row(f"rb{i}", "RB", rush_yd=1500 - i * 25, rush_td=10) for i in range(40)]
    ps = ProjectionSet(rows, six)
    ps.compute_vor(rules_std, teams=12)
    # Replacement QB also gains, so the top QB's VOR-based move is small (a few spots), not a wholesale leap.
    top_qb = ps.by_id["qb0"]
    assert top_qb.tilt is not None and 0 < top_qb.tilt <= 12
    assert ps.by_id["rb0"].tilt is not None and ps.by_id["rb0"].tilt <= 0
    assert not any("rush_yd" in n for n in ps.scoring_summary({**six, "rush_yd": 0.10000000149}))


def test_attach_gaps_ranks_only_the_draftable_universe():
    from draft_assistant.projections import ProjectionSet
    rows = [_row(f"w{i}", "WR", rec=100 - i, rec_yd=1200 - i * 30, rec_td=8) for i in range(12)]
    ps = ProjectionSet(rows, BASELINE_HALF_PPR)
    ranked = []
    for i in range(12):
        r = rp(12 - i, f"WR{i}", "WR")       # rankings order is the reverse of projections
        r.sleeper_id = f"w{i}"
        ranked.append(r)
    ranked.append(rp(500, "Deep Flier", "WR"))   # outside the depth cap
    ranked[-1].sleeper_id = "none"
    from draft_assistant.draft_state import RosterRules
    ps.compute_vor(RosterRules.from_positions(["WR", "WR", "BN"]), teams=2)
    ps.attach(ranked)                      # copies vor onto the ranked players
    n = ps.attach_gaps(ranked, depth=100)
    assert n == 12
    assert ranked[-1].proj_gap is None            # never ranked, so no gap
    best_proj = next(r for r in ranked if r.sleeper_id == "w0")  # top projection, worst ranking
    assert best_proj.rank == 12 and best_proj.proj_gap == 11
    worst_proj = next(r for r in ranked if r.sleeper_id == "w11")
    assert worst_proj.proj_gap == -11


def test_kickers_and_defenses_never_get_a_gap():
    from draft_assistant.draft_state import RosterRules
    from draft_assistant.projections import ProjectionSet
    rows = [_row("k1", "K", fgm_20_29=20, xpm=40), _row("d1", "DEF", sack=40, int=15)]
    ps = ProjectionSet(rows, {**BASELINE_HALF_PPR, "fgm_20_29": 3, "xpm": 1, "sack": 1, "int": 2})
    ps.compute_vor(RosterRules.from_positions(["K", "DEF", "BN"]), teams=2)
    ranked = [rp(200, "Kicker", "K"), rp(210, "Defense", "DEF")]
    ranked[0].sleeper_id, ranked[1].sleeper_id = "k1", "d1"
    ps.attach(ranked)
    assert all(r.vor is not None for r in ranked)   # they do have projections...
    ps.attach_gaps(ranked, depth=300)
    assert all(r.proj_gap is None for r in ranked)  # ...but are deliberately skipped
