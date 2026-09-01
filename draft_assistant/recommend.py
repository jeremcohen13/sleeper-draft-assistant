"""Scoring and recommendation logic.

Every available player gets a score that starts from their overall rank and is
adjusted for roster need, positional rules (superflex, K/DEF timing, QB/TE
depth), bye-week stacking, tier cliffs, and value versus ADP. The breakdown is
kept so the UI can explain the pick in one sentence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .draft_state import Demand, RosterNeeds, RosterRules, TierInfo, roster_targets, tier_cliff
from .players import FANTASY_POSITIONS, RankedPlayer


@dataclass(frozen=True)
class Weights:
    """Tunable scoring weights, in "rank points" (1.0 == one spot of overall rank)."""

    open_starter: float = 15.0
    over_target: float = -8.0  # per player beyond the roster-plan target at that position, compounding
    behind_pace: float = 4.0  # per player the position is behind its planned pace
    open_flex: float = 6.0
    open_superflex_qb: float = 12.0
    open_superflex_other: float = 3.0
    must_fill: float = 30.0  # remaining picks <= open starters
    no_slot: float = -1000.0
    early_k_def: float = -150.0
    extra_k_def: float = -150.0
    second_qb_no_sf: float = -25.0
    second_qb_no_sf_early: float = -40.0  # before round 8
    third_qb_no_sf: float = -150.0
    third_qb_sf: float = -25.0
    fourth_qb_sf: float = -150.0
    second_te: float = -20.0
    third_te: float = -100.0
    bye_same: float = -1.5
    bye_same_pos: float = -2.0
    bye_cap: float = -8.0
    tier_last_chance: float = 5.0
    tier_last_player: float = 3.0
    adp_value: float = 0.4
    adp_clip: float = 10.0
    pinned: float = 6.0
    upside_per_point: float = 1.0  # per point above 3 on FantasyPros' 1-5 upside scale
    bust_per_point: float = -1.0  # per point above 3 on the bust scale
    injury_out: float = -25.0  # Out / IR / PUP / Suspended
    injury_doubtful: float = -8.0
    injury_questionable: float = -2.0
    run: float = 3.0  # position is being run on and this player is the best left there
    scoring_tilt: float = 0.5  # per rank spot your league's scoring moves him vs generic half-PPR
    scoring_tilt_clip: float = 20.0
    late_round_threshold: int = 8


@dataclass
class RecommendConfig:
    """Settings that come from config.toml / league rules."""

    k_def_round_threshold: int = 12
    weights: Weights = field(default_factory=Weights)
    pinned_ids: set[str] = field(default_factory=set)


@dataclass
class Scored:
    """A player with its score and the reasons behind it."""

    player: RankedPlayer
    score: float
    reasons: list[str]
    blocked: bool = False  # True when the player should not be drafted at all


@dataclass
class Recommendation:
    """Everything the recommendation panel shows."""

    top_overall: list[RankedPlayer]
    best_by_position: dict[str, RankedPlayer | None]
    tiers: dict[str, TierInfo]
    demand: dict[str, Demand]
    scored: list[Scored]
    take: Scored | None
    backup: Scored | None
    why: str


def _bye_penalty(player: RankedPlayer, roster: list[RankedPlayer], w: Weights) -> tuple[float, str | None]:
    if player.bye is None:
        return 0.0, None
    same = [r for r in roster if r.bye == player.bye]
    if not same:
        return 0.0, None
    pen = w.bye_same * len(same)
    pen += w.bye_same_pos * sum(1 for r in same if r.position == player.position)
    pen = max(pen, w.bye_cap)
    return pen, f"shares week-{player.bye} bye with {len(same)} of your players"


def score_player(
    player: RankedPlayer,
    needs: RosterNeeds,
    rules: RosterRules,
    roster: list[RankedPlayer],
    current_round: int,
    total_rounds: int,
    my_picks_left: int,
    tiers: dict[str, TierInfo],
    cfg: RecommendConfig,
    demand: dict[str, Demand] | None = None,
    best_at_pos: bool = False,
) -> Scored:
    """Score one available player for my next pick."""
    w = cfg.weights
    pos = player.position
    score = 200.0 - float(player.rank)
    reasons: list[str] = []
    blocked = False
    count = needs.counts.get(pos, 0)

    if not rules.has_slot_for(pos):
        score += w.no_slot
        return Scored(player, score, [f"no {pos} slot in this league"], blocked=True)

    # Roster need.
    open_here = needs.open_starters.get(pos, 0)
    if open_here > 0:
        score += w.open_starter
        reasons.append(
            f"fills 1 of your {open_here} open {pos} starter slots" if open_here > 1 else f"fills your open {pos} starter slot"
        )
        if my_picks_left <= needs.open_starter_total:
            score += w.must_fill
            reasons.append("you are running out of picks to fill starters")
    elif needs.open_flex_for(pos) > 0:
        if pos == "QB":
            score += w.open_superflex_qb
            reasons.append("QB fills your open SUPER_FLEX slot")
        elif needs.open_flex.get("SUPER_FLEX", 0) > 0 and needs.open_flex_for(pos) == needs.open_flex.get("SUPER_FLEX", 0):
            score += w.open_superflex_other
            reasons.append("could start in SUPER_FLEX")
        else:
            score += w.open_flex
            reasons.append("fills an open FLEX slot")

    # Roster-plan balance: compare this position's count with its planned share.
    targets = roster_targets(rules, total_rounds)
    target = targets.get(pos, 0)
    picks_made = max(0, total_rounds - my_picks_left)
    if target > 0 and total_rounds > 0:
        over = count + 1 - target
        if over > 0:
            score += w.over_target * over
            reasons.append(f"over your {pos} plan ({count} rostered, plan calls for {target})")
        else:
            expected = target * picks_made / total_rounds
            deficit = expected - count
            if deficit >= 0.75:
                score += w.behind_pace * deficit
                reasons.append(f"behind pace at {pos} ({count} of a planned {target})")

    # Positional depth rules.
    if pos in ("K", "DEF"):
        if current_round < cfg.k_def_round_threshold and my_picks_left > needs.open_starter_total:
            score += w.early_k_def
            reasons.append(f"too early for {pos} (before round {cfg.k_def_round_threshold})")
            blocked = True
        if count >= rules.starters.get(pos, 0):
            score += w.extra_k_def
            reasons.append(f"you already have a {pos}")
            blocked = True
    elif pos == "QB":
        if rules.is_superflex:
            if count >= 3:
                score += w.fourth_qb_sf
                blocked = True
                reasons.append("4th QB is a wasted pick")
            elif count == 2:
                score += w.third_qb_sf
                reasons.append("3rd QB in superflex")
        else:
            if count >= 2:
                score += w.third_qb_no_sf
                blocked = True
                reasons.append("3rd QB in a 1-QB league")
            elif count == 1:
                score += w.second_qb_no_sf_early if current_round < w.late_round_threshold else w.second_qb_no_sf
                reasons.append("2nd QB in a 1-QB league")
    elif pos == "TE":
        if count >= 2:
            score += w.third_te
            blocked = True
            reasons.append("3rd TE")
        elif count == 1 and needs.open_flex_for("TE") == 0:
            score += w.second_te
            reasons.append("2nd TE with no open flex")
        elif count == 1:
            score += w.second_te / 2
            reasons.append("2nd TE")

    # Bye-week stacking.
    bye_pen, bye_reason = _bye_penalty(player, roster, w)
    if bye_pen:
        score += bye_pen
        if bye_reason:
            reasons.append(bye_reason)

    # Tier cliff.
    info = tiers.get(pos)
    if info and info.tier is not None and player.tier == info.tier:
        if info.at_risk:
            score += w.tier_last_chance
            reasons.append(
                f"last chance at {pos} tier {info.tier} ({info.remaining_in_tier} left, "
                f"{info.picks_until_next} picks until your next turn)"
            )
        if info.remaining_in_tier == 1:
            score += w.tier_last_player
            reasons.append(f"last player in {pos} tier {info.tier}")

    # Value vs ADP.
    if player.ecr_vs_adp:
        delta = max(-w.adp_clip, min(w.adp_clip, player.ecr_vs_adp))
        score += w.adp_value * delta
        if delta >= 5:
            reasons.append(f"value: usually drafted {int(delta)} spots later")
        elif delta <= -5:
            reasons.append(f"reach: usually drafted {int(-delta)} spots earlier")

    # Your league's scoring vs the generic half-PPR the rankings assume.
    if player.proj_tilt is not None and player.proj_tilt != 0:
        tilt = max(-w.scoring_tilt_clip, min(w.scoring_tilt_clip, float(player.proj_tilt)))
        score += w.scoring_tilt * tilt
        if tilt >= 5:
            reasons.append(f"your scoring moves him up {int(tilt)} spots vs generic half-PPR")
        elif tilt <= -5:
            reasons.append(f"your scoring moves him down {int(-tilt)} spots vs generic half-PPR")

    # Positional run in progress.
    dm = (demand or {}).get(pos)
    if dm and dm.is_run and best_at_pos:
        score += w.run
        reasons.append(f"{pos} run: {dm.recent_count} of the last {dm.recent_window} picks")

    # Your target list.
    if player.sleeper_id and player.sleeper_id in cfg.pinned_ids:
        score += w.pinned
        reasons.append("on your target list")

    # FantasyPros upside / bust flags.
    if player.upside is not None and player.upside > 3:
        score += w.upside_per_point * (player.upside - 3)
        if player.upside >= 5:
            reasons.append("max upside rating")
    if player.bust is not None and player.bust > 3:
        score += w.bust_per_point * (player.bust - 3)
        if player.bust >= 5:
            reasons.append("high bust risk")

    # Injury status from Sleeper.
    inj = (player.injury or "").lower()
    if inj:
        if inj in ("out", "ir", "pup", "sus", "suspended", "nfi", "cov"):
            score += w.injury_out
            reasons.append(f"injury status: {player.injury}")
        elif inj.startswith("doubt"):
            score += w.injury_doubtful
            reasons.append(f"injury status: {player.injury}")
        elif inj.startswith("quest"):
            score += w.injury_questionable

    return Scored(player, score, reasons, blocked=blocked)


def _why(take: Scored, needs: RosterNeeds) -> str:
    p = take.player
    lead = f"{p.name} is the best-ranked {p.position} on the board (#{p.rank}"
    lead += f", tier {p.tier})" if p.tier is not None else ")"
    extras = [r for r in take.reasons if not r.startswith("too early") and not r.startswith("no ")]
    if extras:
        return lead + " and " + "; ".join(extras[:2]) + "."
    return lead + "."


def recommend(
    available: list[RankedPlayer],
    my_roster: list[RankedPlayer],
    rules: RosterRules,
    needs: RosterNeeds,
    current_round: int,
    total_rounds: int,
    my_picks_left: int,
    picks_until_next: int,
    cfg: RecommendConfig,
    top_n: int = 10,
    demand: dict[str, Demand] | None = None,
) -> Recommendation:
    """Build the full recommendation for my next pick.

    ``available`` must be sorted best-first by overall rank. ``picks_until_next``
    is the number of opponent picks between my upcoming pick and the one after
    it (the tier-cliff horizon).
    """
    positions = [pos for pos in FANTASY_POSITIONS if rules.has_slot_for(pos) or any(p.position == pos for p in available)]
    demand = demand or {}
    tiers = {
        pos: tier_cliff(available, pos, picks_until_next, demand[pos].expected if pos in demand else None)
        for pos in positions
    }
    best_by_pos: dict[str, RankedPlayer | None] = {}
    for pos in positions:
        best_by_pos[pos] = next((p for p in available if p.position == pos), None)
    best_ids = {p.sleeper_id for p in best_by_pos.values() if p}

    scored = [
        score_player(
            p, needs, rules, my_roster, current_round, total_rounds, my_picks_left, tiers, cfg,
            demand=demand, best_at_pos=p.sleeper_id in best_ids,
        )
        for p in available[:150]
    ]
    scored.sort(key=lambda s: (-s.score, s.player.rank))
    draftable = [s for s in scored if not s.blocked] or scored
    take = draftable[0] if draftable else None
    backup = draftable[1] if len(draftable) > 1 else None
    why = _why(take, needs) if take else "No ranked players left."
    return Recommendation(
        top_overall=available[:top_n],
        best_by_position=best_by_pos,
        tiers=tiers,
        demand=demand,
        scored=scored,
        take=take,
        backup=backup,
        why=why,
    )
