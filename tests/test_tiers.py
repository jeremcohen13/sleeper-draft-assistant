"""Tier-cliff calculation."""

from draft_assistant.draft_state import tier_cliff
from tests.conftest import rp


def _board():
    return [
        rp(1, "RB A", "RB", tier=1), rp(2, "WR A", "WR", tier=1), rp(3, "RB B", "RB", tier=1),
        rp(4, "WR B", "WR", tier=1), rp(5, "RB C", "RB", tier=2), rp(6, "TE A", "TE", tier=1),
        rp(7, "WR C", "WR", tier=2), rp(8, "QB A", "QB", tier=1), rp(9, "RB D", "RB", tier=2),
    ]


def test_tier_cliff_at_risk_when_demand_exceeds_supply():
    info = tier_cliff(_board(), "RB", picks_until_next=5)
    assert info.tier == 1 and info.remaining_in_tier == 2
    assert info.expected_taken == 3  # RB A, RB B, RB C among the next 5
    assert info.at_risk


def test_tier_cliff_safe_with_short_horizon():
    info = tier_cliff(_board(), "WR", picks_until_next=1)
    assert info.remaining_in_tier == 2 and info.expected_taken == 0 and not info.at_risk


def test_tier_cliff_single_te_is_last_in_tier_and_safe_if_nobody_wants_it():
    info = tier_cliff(_board(), "TE", picks_until_next=3)
    assert info.remaining_in_tier == 1 and info.expected_taken == 0 and not info.at_risk


def test_tier_cliff_empty_position():
    info = tier_cliff(_board(), "DEF", picks_until_next=4)
    assert info.tier is None and info.remaining_in_tier == 0 and not info.at_risk


def test_tier_cliff_without_tiers_uses_whole_position():
    board = [rp(1, "A", "RB"), rp(2, "B", "RB")]
    info = tier_cliff(board, "RB", 3)
    assert info.tier is None and info.remaining_in_tier == 2 and not info.at_risk


def test_demand_forecast_detects_run_and_upcoming_needs(rules_std):
    from draft_assistant.draft_state import DraftSettings, DraftState, demand_forecast
    settings = DraftSettings(teams=4, rounds=6)
    st = DraftState(settings, rules_std, my_slot=1)
    # Picks 1-8 made; last 4 picks (round 2) were all RB although the board is WR-heavy.
    raw = []
    for n in range(1, 9):
        pos = "RB" if n >= 5 else "WR"
        raw.append({"pick_no": n, "round": (n - 1) // 4 + 1, "draft_slot": 0, "player_id": f"x{n}", "picked_by": None,
                    "metadata": {"first_name": "P", "last_name": str(n), "position": pos, "team": "XX"}})
    st.update_picks(raw)
    assert st.my_next_pick_no == 9 and st.is_my_turn
    board = [rp(10 + i, f"WR{i}", "WR", tier=1) for i in range(8)] + [rp(30 + i, f"RB{i}", "RB", tier=2) for i in range(4)]
    horizon = st.picks_between_my_next_two  # picks 10..15 -> 6 picks
    assert horizon == 6
    d = demand_forecast(st, board, horizon)
    assert d["WR"].board == 6 and d["RB"].board == 0
    assert d["RB"].is_run and d["RB"].run >= 3  # 4 RB in the window vs ~0 expected
    assert not d["WR"].is_run
    # Every upcoming team still has open RB/WR starters -> need demand for both.
    assert d["WR"].need >= 1  # WR is every upcoming team's biggest hole (3 open vs 2 RB)
    assert d["RB"].expected >= 3


def test_recommend_uses_demand_for_tier_cliff(rules_std):
    from draft_assistant.draft_state import Demand, compute_needs
    from draft_assistant.recommend import RecommendConfig, recommend
    board = [rp(1, "RB A", "RB", tier=1), rp(2, "RB B", "RB", tier=1), rp(3, "WR A", "WR", tier=1), rp(4, "WR B", "WR", tier=1)]
    needs = compute_needs([], rules_std)
    quiet = recommend(board, [], rules_std, needs, 1, 16, 16, 1, RecommendConfig())
    assert not quiet.tiers["RB"].at_risk
    run = {"RB": Demand("RB", board=1, run=3, need=0, recent_count=6, recent_window=12)}
    hot = recommend(board, [], rules_std, needs, 1, 16, 16, 1, RecommendConfig(), demand=run)
    assert hot.tiers["RB"].expected_taken == 4 and hot.tiers["RB"].at_risk
    assert any("RB run" in r for r in hot.take.reasons)
