"""Roster-need weighting and recommendation ranking."""

from draft_assistant.draft_state import compute_needs, tier_cliff
from draft_assistant.recommend import RecommendConfig, recommend, score_player
from tests.conftest import rp


def _rec(available, roster_positions, rules, current_round=1, picks_left=16, horizon=10, roster=None, cfg=None):
    roster = roster if roster is not None else [rp(500 + i, f"mine{i}", pos) for i, pos in enumerate(roster_positions)]
    needs = compute_needs([p.position for p in roster], rules)
    return recommend(available, roster, rules, needs, current_round, 16, picks_left, horizon, cfg or RecommendConfig())


def test_open_starter_beats_slightly_better_rank_at_filled_position(rules_std):
    # I already have 3 WR + 2 FLEX filled by WR; RB starters open.
    roster_pos = ["WR", "WR", "WR", "WR", "WR"]
    board = [rp(10, "WR X", "WR", tier=2), rp(13, "RB Y", "RB", tier=2), rp(30, "TE Z", "TE", tier=3)]
    rec = _rec(board, roster_pos, rules_std, current_round=6, picks_left=11)
    assert rec.take.player.name == "RB Y"
    assert "open RB starter" in rec.why


def test_kicker_blocked_when_league_has_no_k_slot(rules_std):
    board = [rp(1, "K A", "K"), rp(2, "DEF A", "DEF"), rp(3, "WR A", "WR")]
    rec = _rec(board, [], rules_std, current_round=15, picks_left=2)
    scored = {s.player.name: s for s in rec.scored}
    assert scored["K A"].blocked and scored["K A"].score < -500
    assert rec.take.player.name != "K A"


def test_def_penalized_before_threshold_and_boosted_when_must_fill(rules_std):
    board = [rp(1, "DEF A", "DEF", tier=1), rp(2, "WR A", "WR", tier=1)]
    early = _rec(board, [], rules_std, current_round=3, picks_left=14)
    assert early.take.player.name == "WR A"
    assert {s.player.name: s for s in early.scored}["DEF A"].blocked
    # Round 15 with the DEF slot still open and only 2 picks left: DEF must be filled.
    roster_pos = ["QB", "RB", "RB", "WR", "WR", "WR", "TE", "RB", "WR"]
    late = _rec(board, roster_pos, rules_std, current_round=15, picks_left=1)
    assert late.take.player.name == "DEF A"
    assert any("running out of picks" in r for r in late.take.reasons)


def test_third_qb_blocked_in_one_qb_league_but_not_in_superflex(rules_std, rules_sf):
    board = [rp(5, "QB C", "QB", tier=1), rp(40, "RB Z", "RB", tier=4)]
    one_qb = _rec(board, ["QB", "QB"], rules_std, current_round=9, picks_left=8)
    assert one_qb.take.player.name == "RB Z"
    assert {s.player.name: s for s in one_qb.scored}["QB C"].blocked
    sf = _rec(board, ["QB", "QB"], rules_sf, current_round=9, picks_left=8)
    assert not {s.player.name: s for s in sf.scored}["QB C"].blocked


def test_superflex_boosts_second_qb(rules_sf):
    board = [rp(20, "QB B", "QB", tier=2), rp(18, "WR B", "WR", tier=2)]
    rec = _rec(board, ["QB", "RB", "RB", "WR", "WR", "TE", "RB"], rules_sf, current_round=4, picks_left=11)
    assert rec.take.player.name == "QB B"
    assert "SUPER_FLEX" in rec.why


def test_second_te_penalized(rules_std):
    board = [rp(25, "TE B", "TE", tier=2), rp(27, "WR B", "WR", tier=3)]
    rec = _rec(board, ["TE", "RB", "RB", "WR", "WR", "WR", "QB", "RB", "WR"], rules_std, current_round=8, picks_left=9)
    assert rec.take.player.name == "WR B"


def test_bye_week_stacking_penalty(rules_std):
    roster = [rp(50, "mine RB", "RB", bye=7), rp(51, "mine WR", "WR", bye=7)]
    same_bye = rp(20, "RB same", "RB", bye=7)
    other_bye = rp(20, "RB other", "RB", bye=9)
    needs = compute_needs(["RB", "WR"], rules_std)
    tiers = {"RB": tier_cliff([same_bye, other_bye], "RB", 5)}
    cfg = RecommendConfig()
    a = score_player(same_bye, needs, rules_std, roster, 3, 16, 14, tiers, cfg)
    b = score_player(other_bye, needs, rules_std, roster, 3, 16, 14, tiers, cfg)
    assert b.score - a.score == 5.0  # 2 shared byes (-3) plus same-position (-2)
    assert any("bye" in r for r in a.reasons)


def test_tier_cliff_bonus_and_adp_value(rules_std):
    board = [rp(10, "RB last", "RB", tier=2, adp=8), rp(11, "WR mid", "WR", tier=2, adp=-8), rp(12, "RB next", "RB", tier=3)]
    needs = compute_needs([], rules_std)
    tiers = {pos: tier_cliff(board, pos, 3) for pos in ("RB", "WR")}
    cfg = RecommendConfig()
    a = score_player(board[0], needs, rules_std, [], 2, 16, 15, tiers, cfg)
    b = score_player(board[1], needs, rules_std, [], 2, 16, 15, tiers, cfg)
    assert any("last player in RB tier 2" in r for r in a.reasons)
    assert any("value" in r for r in a.reasons) and any("reach" in r for r in b.reasons)
    assert a.score > b.score + 5


def test_recommendation_structure(rules_std):
    board = [rp(i, f"P{i}", pos, tier=1 + i // 5) for i, pos in enumerate(["RB", "WR", "WR", "RB", "QB", "TE", "WR", "RB", "WR", "RB", "WR", "QB"], start=1)]
    rec = _rec(board, [], rules_std)
    assert [p.rank for p in rec.top_overall] == list(range(1, 11))
    assert rec.best_by_position["QB"].name == "P5" and rec.best_by_position["TE"].name == "P6"
    assert rec.best_by_position["DEF"] is None and "K" not in rec.best_by_position
    assert rec.take.player.name == "P1" and rec.backup.player.name != "P1"
    assert rec.why.endswith(".")


def test_roster_balance_prefers_needed_wr_over_fifth_rb(rules_std):
    # 4 RB already (2 starters + both FLEX) and 1 WR: a WR ranked 15 spots worse should still win.
    roster_pos = ["RB", "RB", "RB", "RB", "WR", "QB", "TE"]
    board = [rp(40, "RB fifth", "RB", tier=4), rp(55, "WR needed", "WR", tier=5), rp(58, "WR needed2", "WR", tier=5)]
    rec = _rec(board, roster_pos, rules_std, current_round=6, picks_left=9)
    assert rec.take.player.name == "WR needed"
    assert "open WR starter" in rec.why
    wr = {s.player.name: s for s in rec.scored}["WR needed"]
    assert any("behind pace at WR" in r for r in wr.reasons)
    # With 5 RB already, a 6th is over the plan and penalized; a 7th more so.
    rec2 = _rec(board, roster_pos + ["RB"], rules_std, current_round=7, picks_left=8)
    sixth = {s.player.name: s for s in rec2.scored}["RB fifth"]
    assert any("over your RB plan" in r for r in sixth.reasons)
    rec3 = _rec(board, roster_pos + ["RB", "RB"], rules_std, current_round=8, picks_left=7)
    seventh = {s.player.name: s for s in rec3.scored}["RB fifth"]
    assert seventh.score < sixth.score


def test_roster_targets_balanced_plan(rules_std, rules_sf):
    from draft_assistant.draft_state import roster_targets
    t = roster_targets(rules_std, 16)
    assert t == {"QB": 2, "RB": 5, "WR": 6, "TE": 2, "K": 0, "DEF": 1}
    assert sum(t.values()) == 16
    t2 = roster_targets(rules_sf, 14)
    assert t2["QB"] == 3 and t2["K"] == 1 and sum(t2.values()) == 14


def test_pinned_upside_bust_and_injury(rules_std):
    from draft_assistant.recommend import Weights
    needs = compute_needs([], rules_std)
    tiers = {}
    base = rp(30, "Base", "WR")
    pinned = rp(30, "Pinned", "WR"); pinned.sleeper_id = "pin1"
    upside = rp(30, "Upside", "WR"); upside.upside = 5; upside.bust = 1
    bust = rp(30, "Bust", "WR"); bust.bust = 5
    common = rp(30, "Common", "WR"); common.upside = 4; common.bust = 4  # the norm: no effect
    out = rp(30, "Out", "WR"); out.injury = "Out"
    q = rp(30, "Q", "WR"); q.injury = "Questionable"
    cfg = RecommendConfig(pinned_ids={"pin1"})
    sc = {p.name: score_player(p, needs, rules_std, [], 3, 16, 14, tiers, cfg) for p in (base, pinned, upside, bust, out, q, common)}
    assert sc["Pinned"].score - sc["Base"].score == Weights().pinned and "on your target list" in sc["Pinned"].reasons
    assert sc["Upside"].score - sc["Base"].score == 2.0
    assert sc["Base"].score - sc["Bust"].score == 2.0
    assert sc["Common"].score == sc["Base"].score  # 4-out-of-5 is the norm, so it is ignored
    assert not any("boom" in r or "bust" in r for r in sc["Common"].reasons)
    assert sc["Base"].score - sc["Out"].score == 25.0 and any("injury" in r for r in sc["Out"].reasons)
    assert sc["Base"].score - sc["Q"].score == 2.0


def test_upside_bust_columns_parse(tmp_path):
    from draft_assistant.players import read_rankings_csv
    f = tmp_path / "r.csv"
    f.write_text('"RK",TIERS,"PLAYER NAME",TEAM,"POS","BYE WEEK","UPSIDE ","BUST ","SOS SEASON","ECR VS. ADP"\n'
                 '"1",1,"Jahmyr Gibbs",DET,"RB1","6","5 out of 5","1 out of 5","5 out of 5 stars","+3"\n')
    p = read_rankings_csv(f)[0]
    assert p.upside == 5 and p.bust == 1 and p.ecr_vs_adp == 3 and p.bye == 6


def test_sleeper_gap_is_weighted_symmetrically(rules_std):
    from draft_assistant.draft_state import compute_needs
    from draft_assistant.recommend import RecommendConfig, Weights, score_player
    w = Weights()
    needs, tiers, cfg = compute_needs([], rules_std), {}, RecommendConfig()
    base = rp(80, "Base", "WR")
    sleeper = rp(80, "Sleeper", "WR"); sleeper.proj_gap = 40
    fade = rp(80, "Fade", "WR"); fade.proj_gap = -40
    small = rp(80, "Small", "WR"); small.proj_gap = 10      # under the noise floor
    huge = rp(80, "Huge", "WR"); huge.proj_gap = 200        # clipped
    sc = {p.name: score_player(p, needs, rules_std, [], 5, 16, 12, tiers, cfg)
          for p in (base, sleeper, fade, small, huge)}
    assert sc["Sleeper"].score - sc["Base"].score == 40 * w.sleeper_gap
    assert sc["Base"].score - sc["Fade"].score == 40 * w.sleeper_gap
    assert sc["Small"].score == sc["Base"].score
    assert sc["Huge"].score - sc["Base"].score == w.sleeper_gap_clip * w.sleeper_gap
    assert any("sleeper:" in r for r in sc["Sleeper"].reasons)
    assert any("caution:" in r for r in sc["Fade"].reasons)


def test_sleeper_gap_cannot_outrank_a_much_better_player(rules_std):
    # A 40-spot gap is worth 10 rank points, so it breaks near-ties, not blowouts.
    board = [rp(40, "Clearly Better", "WR", tier=3), rp(80, "Sleeper", "WR", tier=5)]
    board[1].proj_gap = 40
    rec = _rec(board, [], rules_std, current_round=4, picks_left=13)
    assert rec.take.player.name == "Clearly Better"
