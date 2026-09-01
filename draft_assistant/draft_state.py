"""Pure draft logic: pick order, ownership, roster needs, availability, tiers.

Nothing in this module talks to the network, so it is fully unit-testable.
Picks are tracked by pick number rather than by count so keepers slotted into
later rounds are handled: the pick on the clock is simply the lowest pick
number that has not been made yet.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .players import FANTASY_POSITIONS, RankedPlayer

FLEX_ELIGIBLE: dict[str, frozenset[str]] = {
    "FLEX": frozenset({"RB", "WR", "TE"}),
    "SUPER_FLEX": frozenset({"QB", "RB", "WR", "TE"}),
    "REC_FLEX": frozenset({"WR", "TE"}),
    "WRRB_FLEX": frozenset({"RB", "WR"}),
}


@dataclass(frozen=True)
class DraftSettings:
    """The parts of a Sleeper draft object that determine pick order."""

    teams: int
    rounds: int
    draft_type: str = "snake"  # "snake" | "linear"
    reversal_round: int = 0  # Sleeper "3RR": rounds >= this reverse an extra time

    @classmethod
    def from_draft(cls, draft: dict[str, Any]) -> "DraftSettings":
        """Build from a raw ``GET /draft/{id}`` payload."""
        settings = draft.get("settings") or {}
        return cls(
            teams=int(settings.get("teams") or len(draft.get("draft_order") or {}) or 0),
            rounds=int(settings.get("rounds") or 0),
            draft_type=str(draft.get("type") or "snake"),
            reversal_round=int(settings.get("reversal_round") or 0),
        )

    @property
    def total_picks(self) -> int:
        """Number of picks in the whole draft."""
        return self.teams * self.rounds


def round_of_pick(pick_no: int, settings: DraftSettings) -> int:
    """1-based round for an overall pick number."""
    return (pick_no - 1) // settings.teams + 1


def slot_for_pick(pick_no: int, settings: DraftSettings) -> int:
    """Draft slot (1..teams) that owns overall pick ``pick_no``."""
    rnd = round_of_pick(pick_no, settings)
    idx = (pick_no - 1) % settings.teams
    if settings.draft_type != "snake":
        return idx + 1
    reversed_round = rnd % 2 == 0
    if settings.reversal_round and rnd >= settings.reversal_round:
        reversed_round = not reversed_round
    return settings.teams - idx if reversed_round else idx + 1


def picks_for_slot(slot: int, settings: DraftSettings) -> list[int]:
    """Every overall pick number owned by ``slot``."""
    return [p for p in range(1, settings.total_picks + 1) if slot_for_pick(p, settings) == slot]


def pick_label(pick_no: int, settings: DraftSettings) -> str:
    """``"3.05"`` style label."""
    rnd = round_of_pick(pick_no, settings)
    within = (pick_no - 1) % settings.teams + 1
    return f"{rnd}.{within:02d}"


@dataclass
class Pick:
    """A made pick."""

    pick_no: int
    round: int
    slot: int
    player_id: str
    picked_by: str | None
    name: str
    position: str | None
    team: str | None
    is_keeper: bool = False
    roster_id: int | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, Any], settings: DraftSettings) -> "Pick":
        """Build from a ``GET /draft/{id}/picks`` element."""
        meta = raw.get("metadata") or {}
        pick_no = int(raw["pick_no"])
        first = meta.get("first_name") or ""
        last = meta.get("last_name") or ""
        return cls(
            pick_no=pick_no,
            round=int(raw.get("round") or round_of_pick(pick_no, settings)),
            slot=int(raw.get("draft_slot") or slot_for_pick(pick_no, settings)),
            player_id=str(raw.get("player_id") or meta.get("player_id") or ""),
            picked_by=str(raw["picked_by"]) if raw.get("picked_by") else None,
            name=f"{first} {last}".strip() or str(raw.get("player_id")),
            position=meta.get("position"),
            team=meta.get("team"),
            is_keeper=bool(raw.get("is_keeper")),
            roster_id=int(raw["roster_id"]) if raw.get("roster_id") is not None else None,
        )


# ------------------------------------------------------------------ roster rules
@dataclass(frozen=True)
class RosterRules:
    """Starting-lineup structure derived from ``roster_positions``."""

    starters: dict[str, int]  # dedicated starter slots per position
    flex: dict[str, int]  # flex slot name -> count
    bench: int

    @classmethod
    def from_positions(cls, roster_positions: Iterable[str]) -> "RosterRules":
        """Parse Sleeper's ``roster_positions`` list."""
        starters = {pos: 0 for pos in FANTASY_POSITIONS}
        flex: dict[str, int] = {}
        bench = 0
        for slot in roster_positions:
            slot = str(slot).upper()
            if slot in starters:
                starters[slot] += 1
            elif slot in FLEX_ELIGIBLE:
                flex[slot] = flex.get(slot, 0) + 1
            elif slot in ("BN", "IR", "TAXI"):
                bench += 1 if slot == "BN" else 0
            # IDP and other slots are ignored.
        return cls(starters=starters, flex=flex, bench=bench)

    @property
    def is_superflex(self) -> bool:
        """True if a SUPER_FLEX slot exists."""
        return self.flex.get("SUPER_FLEX", 0) > 0

    def has_slot_for(self, pos: str) -> bool:
        """True if ``pos`` can start somewhere (dedicated or flex)."""
        if self.starters.get(pos, 0) > 0:
            return True
        return any(pos in FLEX_ELIGIBLE[name] for name, n in self.flex.items() if n > 0)

    @property
    def total_starters(self) -> int:
        """Dedicated plus flex starting slots."""
        return sum(self.starters.values()) + sum(self.flex.values())


@dataclass
class RosterNeeds:
    """What is still open on my roster after greedily filling starters."""

    counts: dict[str, int]
    open_starters: dict[str, int]
    open_flex: dict[str, int]
    surplus: dict[str, int]  # players beyond every starting slot, per position

    @property
    def open_starter_total(self) -> int:
        """All open starting slots, dedicated and flex."""
        return sum(self.open_starters.values()) + sum(self.open_flex.values())

    def open_flex_for(self, pos: str) -> int:
        """Open flex slots that ``pos`` could fill."""
        return sum(n for name, n in self.open_flex.items() if pos in FLEX_ELIGIBLE[name])


def compute_needs(positions: Iterable[str], rules: RosterRules) -> RosterNeeds:
    """Greedy lineup fill: dedicated slots first, then flex slots in declared order."""
    counts: dict[str, int] = {pos: 0 for pos in FANTASY_POSITIONS}
    for pos in positions:
        if pos in counts:
            counts[pos] += 1
    open_starters = {pos: max(0, rules.starters.get(pos, 0) - counts[pos]) for pos in FANTASY_POSITIONS}
    leftover = {pos: max(0, counts[pos] - rules.starters.get(pos, 0)) for pos in FANTASY_POSITIONS}
    open_flex: dict[str, int] = {}
    # Fill the most restrictive flex types first so a QB is not wasted in SUPER_FLEX
    # before an RB fills FLEX, etc.
    order = sorted(rules.flex.items(), key=lambda kv: len(FLEX_ELIGIBLE[kv[0]]))
    for name, n in order:
        remaining = n
        for pos in ("QB", "RB", "WR", "TE"):
            if pos not in FLEX_ELIGIBLE[name]:
                continue
            use = min(remaining, leftover[pos])
            leftover[pos] -= use
            remaining -= use
            if remaining == 0:
                break
        open_flex[name] = remaining
    return RosterNeeds(counts=counts, open_starters=open_starters, open_flex=open_flex, surplus=leftover)


def roster_targets(rules: RosterRules, total_rounds: int) -> dict[str, int]:
    """Planned end-of-draft count per position for a balanced roster.

    QB/TE get one backup when the bench is deep enough (two QBs in superflex
    get a third), K/DEF exactly their starters, and every remaining pick is
    split between RB and WR in proportion to their starter + shared-flex load.
    """
    targets = {pos: rules.starters.get(pos, 0) for pos in FANTASY_POSITIONS}
    sf = rules.flex.get("SUPER_FLEX", 0)
    targets["QB"] += sf + (1 if rules.bench >= 5 and targets["QB"] + sf > 0 else 0)
    if targets["TE"] and rules.bench >= 6:
        targets["TE"] += 1
    fixed = targets["QB"] + targets["TE"] + targets["K"] + targets["DEF"]
    remaining = max(0, total_rounds - fixed)
    shared = sum(n for name, n in rules.flex.items() if name != "SUPER_FLEX")
    rb_w = rules.starters.get("RB", 0) + shared / 2
    wr_w = rules.starters.get("WR", 0) + shared / 2
    if rb_w + wr_w == 0:
        return targets
    rb = int(round(remaining * rb_w / (rb_w + wr_w)))
    targets["RB"] = rb
    targets["WR"] = remaining - rb
    return targets


# ------------------------------------------------------------------ tiers
@dataclass
class TierInfo:
    """Tier-cliff summary for one position."""

    position: str
    tier: int | None
    remaining_in_tier: int
    expected_taken: int
    picks_until_next: int

    @property
    def at_risk(self) -> bool:
        """True if the current tier will probably be gone by my next pick."""
        return self.tier is not None and self.remaining_in_tier <= self.expected_taken


@dataclass
class Demand:
    """Why we expect N players of a position to go before my next pick."""

    position: str
    board: int  # from the board alone (next `horizon` by rank)
    run: int  # extra from a positional run in recent picks
    need: int  # extra from upcoming teams' open starter slots
    recent_count: int  # picks at this position in the recent window
    recent_window: int

    @property
    def expected(self) -> int:
        """Combined forecast (never more than the picks in the window)."""
        return self.board + self.run + self.need

    @property
    def is_run(self) -> bool:
        """True when the position is being drafted well above its board share."""
        return self.run > 0


def demand_forecast(state: "DraftState", available: list[RankedPlayer], horizon: int) -> dict[str, Demand]:
    """Forecast how many players per position go before my next pick.

    Board share: positions among the next ``horizon`` players by rank.
    Run: if a position took a clearly larger share of the last ``teams`` picks
    than the board would predict, add the excess (scaled to the horizon).
    Need: each opponent picking before my next turn who still has an open
    dedicated starter at the position adds half a pick of demand.
    """
    horizon = max(0, horizon)
    window = state.settings.teams
    recent = [p for p in sorted(state.picks.values(), key=lambda p: p.pick_no) if not p.is_keeper][-window:]
    recent_counts = Counter(p.position or "?" for p in recent)
    board_counts = Counter(p.position for p in available[:horizon])
    board_window = Counter(p.position for p in available[:window]) if window else Counter()
    # Which slots pick before my next turn (after the pick I'm about to make, if any)?
    mine = state.my_open_picks
    if not mine:
        upcoming_slots: list[int] = []
    else:
        start = mine[0] if not state.is_my_turn else mine[0]
        end = mine[1] if len(mine) > 1 else state.settings.total_picks + 1
        upcoming_slots = [slot_for_pick(n, state.settings) for n in state.open_picks() if start < n < end]
    out: dict[str, Demand] = {}
    for pos in FANTASY_POSITIONS:
        board = board_counts.get(pos, 0)
        run = 0
        if recent and horizon:
            expected_recent = board_window.get(pos, 0) * len(recent) / max(1, window)
            excess = recent_counts.get(pos, 0) - expected_recent
            if excess >= 2:
                run = int(round(excess * horizon / max(1, window)))
        need = 0
        if pos in ("QB", "RB", "WR", "TE") and state.current_round > 2:
            hungry = 0
            for slot in upcoming_slots:
                needs = compute_needs([p.position or "" for p in state.roster_for_slot(slot)], state.rules)
                open_here = needs.open_starters.get(pos, 0)
                others = max(needs.open_starters.get(o, 0) for o in ("QB", "RB", "WR", "TE") if o != pos)
                if open_here > 0 and open_here >= others:
                    hungry += 1
            need = min(hungry // 2, max(1, horizon // 4))
        board = min(board, horizon)
        out[pos] = Demand(pos, board, run, need, recent_counts.get(pos, 0), len(recent))
    return out


def tier_cliff(available: list[RankedPlayer], position: str, picks_until_next: int, expected_override: int | None = None) -> TierInfo:
    """Compare players left in ``position``'s best remaining tier against demand.

    ``expected_taken`` counts how many of the next ``picks_until_next`` players
    by overall rank play this position, i.e. how many the other teams will take
    if they draft roughly to the board.
    """
    pos_players = [p for p in available if p.position == position]
    if not pos_players:
        return TierInfo(position, None, 0, 0, picks_until_next)
    top_tier = pos_players[0].tier
    remaining = sum(1 for p in pos_players if p.tier == top_tier) if top_tier is not None else len(pos_players)
    horizon = available[: max(0, picks_until_next)]
    expected = sum(1 for p in horizon if p.position == position)
    if expected_override is not None:
        expected = max(expected, expected_override)
    return TierInfo(position, top_tier, remaining, expected, picks_until_next)


# ------------------------------------------------------------------ state
@dataclass
class DraftState:
    """Everything known about the draft so far, keyed by pick number."""

    settings: DraftSettings
    rules: RosterRules
    my_slot: int | None
    my_user_id: str | None = None
    picks: dict[int, Pick] = field(default_factory=dict)
    my_roster_id: int | None = None

    def update_picks(self, raw_picks: Iterable[dict[str, Any]]) -> tuple[list[Pick], list[Pick]]:
        """Merge the latest picks payload. Returns ``(new_picks, removed_picks)``."""
        incoming: dict[int, Pick] = {}
        for raw in raw_picks:
            if raw.get("pick_no") is None:
                continue
            pick = Pick.from_raw(raw, self.settings)
            incoming[pick.pick_no] = pick
        new = [incoming[n] for n in sorted(incoming) if n not in self.picks]
        removed = [self.picks[n] for n in sorted(self.picks) if n not in incoming]
        self.picks = incoming
        return new, removed

    @property
    def taken_ids(self) -> set[str]:
        """Sleeper ids already drafted."""
        return {p.player_id for p in self.picks.values() if p.player_id}

    @property
    def current_pick_no(self) -> int | None:
        """Lowest pick number not yet made, or ``None`` when the draft is over."""
        for n in range(1, self.settings.total_picks + 1):
            if n not in self.picks:
                return n
        return None

    @property
    def is_complete(self) -> bool:
        """All picks are in."""
        return self.current_pick_no is None

    @property
    def current_round(self) -> int:
        """Round of the pick on the clock (last round if complete)."""
        cur = self.current_pick_no
        return round_of_pick(cur, self.settings) if cur else self.settings.rounds

    @property
    def on_the_clock_slot(self) -> int | None:
        """Slot that owns the current pick."""
        cur = self.current_pick_no
        return slot_for_pick(cur, self.settings) if cur else None

    def open_picks(self) -> list[int]:
        """Pick numbers not yet made, ascending."""
        return [n for n in range(1, self.settings.total_picks + 1) if n not in self.picks]

    @property
    def my_pick_numbers(self) -> list[int]:
        """All pick numbers my slot owns (including ones already used)."""
        return picks_for_slot(self.my_slot, self.settings) if self.my_slot else []

    @property
    def my_open_picks(self) -> list[int]:
        """My pick numbers still to be made."""
        mine = set(self.my_pick_numbers)
        return [n for n in self.open_picks() if n in mine]

    @property
    def my_next_pick_no(self) -> int | None:
        """My next pick number, or ``None`` if I have none left."""
        mine = self.my_open_picks
        return mine[0] if mine else None

    @property
    def picks_until_my_turn(self) -> int | None:
        """Open picks before mine (0 = on the clock). ``None`` if I have no picks left."""
        nxt = self.my_next_pick_no
        if nxt is None:
            return None
        return sum(1 for n in self.open_picks() if n < nxt)

    @property
    def picks_between_my_next_two(self) -> int:
        """Open picks strictly between my next pick and the one after it."""
        mine = self.my_open_picks
        if len(mine) < 2:
            if len(mine) == 1:
                return sum(1 for n in self.open_picks() if n > mine[0])
            return 0
        return sum(1 for n in self.open_picks() if mine[0] < n < mine[1])

    @property
    def is_my_turn(self) -> bool:
        """I am on the clock."""
        return self.picks_until_my_turn == 0

    @property
    def is_on_deck(self) -> bool:
        """Exactly one pick before mine."""
        return self.picks_until_my_turn == 1

    def is_mine(self, pick: Pick) -> bool:
        """True if the pick belongs to me: roster id first, then user id, then slot."""
        if self.my_roster_id is not None and pick.roster_id is not None:
            return pick.roster_id == self.my_roster_id
        if self.my_user_id and pick.picked_by:
            return pick.picked_by == self.my_user_id
        return self.my_slot is not None and pick.slot == self.my_slot

    @property
    def my_roster(self) -> list[Pick]:
        """My picks so far, including keepers."""
        return [p for p in sorted(self.picks.values(), key=lambda p: p.pick_no) if self.is_mine(p)]

    def roster_for_slot(self, slot: int) -> list[Pick]:
        """Picks made by a given slot."""
        return [p for p in sorted(self.picks.values(), key=lambda p: p.pick_no) if p.slot == slot]

    def my_needs(self) -> RosterNeeds:
        """Roster needs given my current picks."""
        return compute_needs([p.position or "" for p in self.my_roster], self.rules)

    def available(self, ranked: list[RankedPlayer]) -> list[RankedPlayer]:
        """Ranked players not yet drafted, best first."""
        taken = self.taken_ids
        return [p for p in ranked if p.sleeper_id and p.sleeper_id not in taken]

    def position_counts(self) -> Counter[str]:
        """Drafted-so-far counts by position across the league."""
        return Counter(p.position or "?" for p in self.picks.values())

    def rounds_remaining_for_me(self) -> int:
        """How many picks I still get."""
        return len(self.my_open_picks)


def expected_picks_at_position(available: list[RankedPlayer], horizon: int) -> dict[str, int]:
    """Position counts among the next ``horizon`` best available players."""
    out: dict[str, int] = {pos: 0 for pos in FANTASY_POSITIONS}
    for p in available[: max(0, horizon)]:
        out[p.position] = out.get(p.position, 0) + 1
    return out


def ceil_div(a: int, b: int) -> int:
    """Integer ceiling division helper."""
    return int(math.ceil(a / b)) if b else 0
