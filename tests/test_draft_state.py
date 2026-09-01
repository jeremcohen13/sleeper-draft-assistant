"""Snake/linear order, pick ownership, keepers, and turn math."""

from draft_assistant.draft_state import (
    DraftSettings,
    DraftState,
    RosterRules,
    compute_needs,
    pick_label,
    picks_for_slot,
    round_of_pick,
    slot_for_pick,
)
from tests.conftest import rp


def test_snake_order_first_two_rounds(snake12):
    assert [slot_for_pick(n, snake12) for n in range(1, 13)] == list(range(1, 13))
    assert [slot_for_pick(n, snake12) for n in range(13, 25)] == list(range(12, 0, -1))
    assert slot_for_pick(25, snake12) == 1
    assert round_of_pick(25, snake12) == 3
    assert pick_label(25, snake12) == "3.01"


def test_snake_pick_ownership_slot_9(snake12):
    mine = picks_for_slot(9, snake12)
    assert mine[:4] == [9, 16, 33, 40]
    assert len(mine) == 16
    assert all(slot_for_pick(n, snake12) == 9 for n in mine)


def test_linear_order():
    s = DraftSettings(teams=10, rounds=3, draft_type="linear")
    assert picks_for_slot(1, s) == [1, 11, 21]
    assert picks_for_slot(10, s) == [10, 20, 30]


def test_third_round_reversal():
    s = DraftSettings(teams=4, rounds=4, draft_type="snake", reversal_round=3)
    # Round 1 fwd, round 2 rev, round 3 rev (again), round 4 fwd.
    assert [slot_for_pick(n, s) for n in range(1, 17)] == [1, 2, 3, 4, 4, 3, 2, 1, 4, 3, 2, 1, 1, 2, 3, 4]


def _raw(pick_no, slot, pid, pos, by=None, keeper=False):
    return {
        "pick_no": pick_no,
        "round": (pick_no - 1) // 12 + 1,
        "draft_slot": slot,
        "player_id": pid,
        "picked_by": by,
        "is_keeper": keeper,
        "metadata": {"first_name": "P", "last_name": pid, "position": pos, "team": "XX"},
    }


def test_current_pick_skips_keepers_and_counts_until_my_turn(snake12, rules_std):
    st = DraftState(snake12, rules_std, my_slot=9, my_user_id="me")
    # Keeper at my round-4 pick (#40) already used, plus picks 1-7 made.
    raw = [_raw(40, 9, "k1", "RB", by="me", keeper=True)] + [_raw(n, n, f"p{n}", "WR") for n in range(1, 8)]
    new, removed = st.update_picks(raw)
    assert len(new) == 8 and removed == []
    assert st.current_pick_no == 8
    assert st.on_the_clock_slot == 8
    assert st.picks_until_my_turn == 1 and st.is_on_deck and not st.is_my_turn
    assert st.my_next_pick_no == 9
    # Between #9 and #16 there are 6 open picks (10..15).
    assert st.picks_between_my_next_two == 6
    assert [p.player_id for p in st.my_roster] == ["k1"]
    assert 40 not in st.my_open_picks


def test_on_the_clock_and_removed_pick(snake12, rules_std):
    st = DraftState(snake12, rules_std, my_slot=1)
    st.update_picks([])
    assert st.is_my_turn and st.picks_until_my_turn == 0
    st.update_picks([_raw(1, 1, "a", "RB")])
    assert st.picks_until_my_turn == 22  # picks 2..23 before my #24
    new, removed = st.update_picks([])
    assert [p.pick_no for p in removed] == [1] and new == []


def test_complete_and_available(snake12, rules_std):
    s = DraftSettings(teams=2, rounds=2)
    st = DraftState(s, rules_std, my_slot=1)
    st.update_picks([_raw(n, 1, f"id{n}", "RB") for n in range(1, 5)])
    assert st.is_complete and st.current_pick_no is None and st.picks_until_my_turn is None
    ranked = [rp(1, "A", "RB"), rp(9, "B", "RB")]
    assert [p.name for p in st.available(ranked)] == ["B"]


def test_roster_rules_parse(rules_std, rules_sf):
    assert rules_std.starters == {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "K": 0, "DEF": 1}
    assert rules_std.flex == {"FLEX": 2} and rules_std.bench == 6
    assert not rules_std.has_slot_for("K") and rules_std.has_slot_for("DEF")
    assert not rules_std.is_superflex and rules_sf.is_superflex
    assert rules_std.total_starters == 10


def test_compute_needs_fills_flex_greedily(rules_std):
    needs = compute_needs(["RB", "RB", "RB", "WR", "TE"], rules_std)
    assert needs.open_starters["RB"] == 0 and needs.open_starters["WR"] == 2
    assert needs.open_flex["FLEX"] == 1  # third RB took one flex
    assert needs.surplus["RB"] == 0
    needs2 = compute_needs(["RB"] * 5 + ["WR"] * 3 + ["TE"] * 2 + ["QB"], rules_std)
    assert needs2.open_starter_total == 1  # only DEF open
    assert needs2.surplus == {"QB": 0, "RB": 1, "WR": 0, "TE": 1, "K": 0, "DEF": 0}


def test_superflex_needs(rules_sf):
    needs = compute_needs(["QB", "QB"], rules_sf)
    assert needs.open_starters["QB"] == 0
    assert needs.open_flex["SUPER_FLEX"] == 0 and needs.open_flex["FLEX"] == 1
    assert needs.open_flex_for("QB") == 0


def test_is_mine_prefers_roster_id_then_user_then_slot(snake12, rules_std):
    st = DraftState(snake12, rules_std, my_slot=9, my_user_id="me", my_roster_id=9)
    raw = [
        {**_raw(9, 9, "a", "RB", by="commish"), "roster_id": 9},     # commissioner picked for me -> mine (roster)
        {**_raw(10, 10, "b", "RB", by="me"), "roster_id": 10},       # my user id but another roster -> not mine
        _raw(11, 9, "c", "RB", by=None),                            # no ids at all -> falls back to slot
        _raw(12, 4, "d", "RB", by="someone"),
    ]
    st.update_picks(raw)
    assert [p.player_id for p in st.my_roster] == ["a", "c"]
