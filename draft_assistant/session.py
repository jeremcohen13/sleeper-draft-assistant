"""Shared bootstrapping and per-poll logic used by both draft.py and web.py.

A :class:`DraftSession` owns the draft state, the ranked player pool, the pick
source (live API or simulator) and produces recommendations and JSON snapshots.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import Config
from .draft_state import FLEX_ELIGIBLE, DraftSettings, DraftState, Pick, RosterRules, compute_needs, demand_forecast, pick_label, slot_for_pick
from .players import PlayerDB, RankedPlayer, RankingsError, load_overrides, match_rankings, read_rankings_csv
from .projections import ProjectionSet
from .recommend import RecommendConfig, Recommendation, recommend
from .simulate import DraftSimulator
from .sleeper import SleeperAPIError, SleeperClient, pick_active_draft


class PickSource(Protocol):
    """Anything that can supply the draft and its picks."""

    def fetch_draft(self) -> dict[str, Any]: ...

    def fetch_picks(self) -> list[dict[str, Any]]: ...


class LiveSource:
    """Real Sleeper API source."""

    def __init__(self, client: SleeperClient, draft_id: str) -> None:
        self.client = client
        self.draft_id = draft_id

    def fetch_draft(self) -> dict[str, Any]:
        """Current draft payload."""
        draft = self.client.get_draft(self.draft_id)
        if not draft:
            raise SleeperAPIError(f"draft {self.draft_id} not found")
        return draft

    def fetch_picks(self) -> list[dict[str, Any]]:
        """All picks so far."""
        return self.client.get_draft_picks(self.draft_id)


class SessionError(Exception):
    """Startup failed (bad config, API down, unknown slot, missing rankings)."""


@dataclass
class PollResult:
    """What changed during one poll."""

    status: str
    new: list[Pick]
    removed: list[Pick]
    status_changed: bool


@dataclass
class DraftSession:
    """Everything a UI needs to follow the draft."""

    cfg: Config
    league: dict[str, Any]
    draft: dict[str, Any]
    settings: DraftSettings
    rules: RosterRules
    state: DraftState
    ranked: list[RankedPlayer]
    slot_names: dict[int, str]
    my_slot: int
    my_user_id: str | None
    source: PickSource
    rec_cfg: RecommendConfig
    logger: logging.Logger
    simulator: DraftSimulator | None = None
    unmatched: int = 0
    scoring_notes: list[str] = field(default_factory=list)
    projected: int = 0
    slot_changed: bool = False
    status: str = "starting"
    polls: int = 0
    last_error: str | None = None
    updated_at: float = 0.0
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.ranked_by_id = {p.sleeper_id: p for p in self.ranked if p.sleeper_id}
        self._arrival: dict[int, int] = {}  # pick_no -> order in which we first saw it
        self.targets_path = self.cfg.config_path.parent / "targets.json"
        self.load_targets()

    # ------------------------------------------------------------- targets
    def load_targets(self) -> None:
        """Read targets.json (list of names or Sleeper ids) into ``rec_cfg.pinned_ids``."""
        pinned: set[str] = set()
        if self.targets_path.exists():
            try:
                data = json.loads(self.targets_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                self.logger.warning("targets.json is not valid JSON: %s", exc)
                data = []
            items = data.get("targets", []) if isinstance(data, dict) else data
            by_name = {p.norm_name: p.sleeper_id for p in self.ranked}
            for item in items:
                key = str(item)
                if key in self.ranked_by_id:
                    pinned.add(key)
                else:
                    from .players import normalize_name

                    pid = by_name.get(normalize_name(key))
                    if pid:
                        pinned.add(pid)
                    else:
                        self.logger.warning("target %r not found in rankings", key)
        self.rec_cfg.pinned_ids = pinned

    def save_targets(self) -> None:
        """Write the pinned list back to targets.json (as names, so it stays readable)."""
        names = [self.ranked_by_id[pid].name for pid in sorted(self.rec_cfg.pinned_ids, key=lambda i: self.ranked_by_id[i].rank) if pid in self.ranked_by_id]
        self.targets_path.write_text(json.dumps({"targets": names}, indent=2), encoding="utf-8")

    def set_pin(self, sleeper_id: str, pinned: bool) -> None:
        """Pin or unpin a player and persist."""
        with self.lock:
            if sleeper_id not in self.ranked_by_id:
                raise ValueError("Unknown player")
            if pinned:
                self.rec_cfg.pinned_ids.add(sleeper_id)
            else:
                self.rec_cfg.pinned_ids.discard(sleeper_id)
            self.save_targets()
            self.logger.info("target %s: %s", "pinned" if pinned else "unpinned", self.ranked_by_id[sleeper_id].label)

    def target_advice(self) -> list[dict[str, Any]]:
        """For each pinned player: availability and whether to take him now or wait."""
        st = self.state
        mine = st.my_open_picks
        nxt = mine[0] if mine else None
        following = mine[1] if len(mine) > 1 else None
        taken_by = {p.player_id: p for p in st.picks.values()}
        out = []
        for pid in sorted(self.rec_cfg.pinned_ids, key=lambda i: self.ranked_by_id[i].rank if i in self.ranked_by_id else 9999):
            p = self.ranked_by_id.get(pid)
            if p is None:
                continue
            adp = p.rank + (p.ecr_vs_adp or 0)
            d = self.player_json(p)
            d["adp_est"] = round(adp)
            if pid in taken_by:
                pk = taken_by[pid]
                d["status"] = "gone"
                d["advice"] = f"Drafted {pick_label(pk.pick_no, self.settings)} by {self.slot_names.get(pk.slot, 'slot ' + str(pk.slot))}"
            elif nxt is None:
                d["status"] = "gone"
                d["advice"] = "You have no picks left"
            elif following is None:
                d["status"] = "now"
                d["advice"] = f"Last pick — take him at #{nxt} if you want him"
            elif adp < following - 3:
                d["status"] = "now"
                d["advice"] = f"Take at #{nxt}: usually gone by ~#{round(adp)}, before your next pick #{following}"
            elif adp <= following + 3:
                d["status"] = "risky"
                d["advice"] = f"Coin flip: ADP ~#{round(adp)} vs your next pick #{following}"
            else:
                d["status"] = "wait"
                d["advice"] = f"Should last until #{following} (ADP ~#{round(adp)}); you can wait"
            out.append(d)
        return out

    # ------------------------------------------------------------- polling
    def poll(self) -> PollResult:
        """Fetch draft + picks once and merge. Raises ``SleeperAPIError`` on failure."""
        with self.lock:
            self.polls += 1
            try:
                draft = self.source.fetch_draft()
                raw = self.source.fetch_picks()
            except SleeperAPIError as exc:
                self.last_error = str(exc)
                self.logger.warning("poll %d failed: %s", self.polls, exc)
                raise
            self.last_error = None
            self._sync_slot(draft)
            status = str(draft.get("status") or "unknown")
            changed = status != self.status
            if changed:
                self.logger.info("status %s -> %s", self.status, status)
            self.status = status
            new, removed = self.state.update_picks(raw)
            for p in new:
                self._arrival.setdefault(p.pick_no, len(self._arrival))
            for p in removed:
                self.logger.warning("pick removed: #%d %s", p.pick_no, p.name)
            for p in new:
                self.logger.info(
                    "PICK %s #%d slot %d %s %s %s by %s%s", pick_label(p.pick_no, self.settings), p.pick_no, p.slot,
                    p.name, p.position, p.team, self.slot_names.get(p.slot, p.picked_by), " (keeper)" if p.is_keeper else "",
                )
            st = self.state
            self.logger.info(
                "poll %d status=%s picks=%d current=#%s on_clock=%s until_me=%s new=%d",
                self.polls, status, len(st.picks), st.current_pick_no, self.owner_name(st.on_the_clock_slot), st.picks_until_my_turn, len(new),
            )
            self.updated_at = time.time()
            return PollResult(status, new, removed, changed)

    def _sync_slot(self, draft: dict[str, Any]) -> None:
        """Follow draft_order changes (commissioner re-randomized) without a restart."""
        order = {str(k): int(v) for k, v in (draft.get("draft_order") or {}).items()}
        if not order or not self.my_user_id:
            return
        new_slot = order.get(self.my_user_id)
        if new_slot is None or new_slot == self.my_slot:
            return
        self.logger.warning("draft order changed: my slot %s -> %s", self.my_slot, new_slot)
        old_name = self.slot_names.get(self.my_slot, "")
        names_by_uid = {uid: self.slot_names.get(slot, uid) for uid, slot in {u: sl for u, sl in order.items()}.items()}
        self.slot_names = {slot: names_by_uid.get(uid, uid) for uid, slot in order.items()}
        if old_name and new_slot not in self.slot_names:
            self.slot_names[new_slot] = old_name
        self.my_slot = new_slot
        self.state.my_slot = new_slot
        self.slot_changed = True

    def owner_name(self, slot: int | None) -> str:
        """Display name for a slot."""
        if slot is None:
            return "-"
        return self.slot_names.get(slot, f"slot {slot}")

    # ------------------------------------------------------------- recommendation
    def my_roster_ranked(self) -> list[RankedPlayer]:
        """My picks as RankedPlayer objects (stubs for unranked keepers)."""
        out: list[RankedPlayer] = []
        for p in self.state.my_roster:
            r = self.ranked_by_id.get(p.player_id)
            out.append(r if r else RankedPlayer(rank=999, name=p.name, position=p.position or "?", team=p.team))
        return out

    def recommendation(self) -> Recommendation:
        """Recommendation for my next pick given the current board."""
        with self.lock:
            st = self.state
            available = st.available(self.ranked)
            demand = demand_forecast(st, available, st.picks_between_my_next_two)
            return recommend(
                demand=demand,
                available=available,
                my_roster=self.my_roster_ranked(),
                rules=st.rules,
                needs=st.my_needs(),
                current_round=st.current_round,
                total_rounds=st.settings.rounds,
                my_picks_left=len(st.my_open_picks),
                picks_until_next=st.picks_between_my_next_two,
                cfg=self.rec_cfg,
            )

    def log_recommendation(self, rec: Recommendation) -> None:
        """Write the recommendation to the log file."""
        st = self.state
        take = rec.take.player.label if rec.take else "-"
        backup = rec.backup.player.label if rec.backup else "-"
        self.logger.info("RECOMMEND round %d pick #%s | TAKE %s | Backup %s | Why: %s", st.current_round, st.my_next_pick_no, take, backup, rec.why)
        self.logger.info("  top10: " + "; ".join(f"{p.rank}.{p.name} {p.position}" for p in rec.top_overall))
        self.logger.info("  best by pos: " + "; ".join(f"{pos}={p.name if p else '-'}" for pos, p in rec.best_by_position.items()))
        for pos, info in rec.tiers.items():
            if info.at_risk:
                self.logger.info("  tier cliff: %s tier %s has %d left, expect %d taken in %d picks", pos, info.tier, info.remaining_in_tier, info.expected_taken, info.picks_until_next)
        for sc in rec.scored[:10]:
            self.logger.debug("  score %.1f %s %s", sc.score, sc.player.label, "; ".join(sc.reasons))

    def submit_pick(self, sleeper_id: str) -> Pick:
        """(dry-run) make my pick. Raises ``ValueError`` when not allowed."""
        if self.simulator is None:
            raise ValueError("Picks can only be submitted in dry-run mode; use the Sleeper app for the real draft")
        with self.lock:
            player = self.ranked_by_id.get(sleeper_id)
            if player is None:
                raise ValueError("Unknown player")
            raw = self.simulator.submit_pick(player)
            self.logger.info("MY PICK (dry-run) #%s %s", raw["pick_no"], player.label)
            return Pick.from_raw(raw, self.settings)

    # ------------------------------------------------------------- lineup / JSON
    def lineup(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Assign my picks to starting slots (dedicated first, then flex). Returns (starters, bench)."""
        picks = list(self.state.my_roster)
        slots: list[dict[str, Any]] = []
        assigned: set[int] = set()
        order = [str(s).upper() for s in self.league.get("roster_positions") or []]
        dedicated = [s for s in order if s in self.rules.starters]
        flex = [s for s in order if s in FLEX_ELIGIBLE]
        for slot in dedicated + flex:
            eligible = FLEX_ELIGIBLE.get(slot, frozenset({slot}))
            chosen = next((p for p in picks if p.pick_no not in assigned and (p.position or "") in eligible), None)
            if chosen:
                assigned.add(chosen.pick_no)
            slots.append({"slot": slot, "player": self.pick_json(chosen) if chosen else None})
        # Restore league order for display.
        rank = {s: i for i, s in enumerate(order)}
        slots.sort(key=lambda d: rank.get(d["slot"], 99))
        bench = [self.pick_json(p) for p in picks if p.pick_no not in assigned]
        return slots, bench

    def player_json(self, p: RankedPlayer, score: float | None = None, blocked: bool = False, reasons: list[str] | None = None) -> dict[str, Any]:
        """Serialize a ranked player."""
        d: dict[str, Any] = {
            "id": p.sleeper_id, "rank": p.rank, "tier": p.tier, "name": p.name, "pos": p.position,
            "team": p.team, "bye": p.bye, "adp": p.ecr_vs_adp, "upside": p.upside, "bust": p.bust,
            "injury": p.injury, "rookie": p.rookie, "pinned": bool(p.sleeper_id and p.sleeper_id in self.rec_cfg.pinned_ids),
            "proj": p.proj_pts, "tilt": p.proj_tilt, "vor": p.vor,
            "sleeper": bool(
                p.position not in ("K", "DEF") and p.rank >= 50
                and (self.sleeper_gap(p) or 0) >= 20
                and self._vor_ranks().get(p.sleeper_id or "", 9999) <= 150
            ),
        }
        if score is not None:
            d["fit"] = round(score, 1)
            d["blocked"] = blocked
            d["reasons"] = reasons or []
        return d

    def pick_json(self, p: Pick) -> dict[str, Any]:
        """Serialize a made pick."""
        r = self.ranked_by_id.get(p.player_id)
        return {
            "pick_no": p.pick_no, "label": pick_label(p.pick_no, self.settings), "round": p.round, "slot": p.slot,
            "owner": self.slot_names.get(p.slot, f"slot {p.slot}"), "name": p.name, "pos": p.position, "team": p.team,
            "rank": r.rank if r else None, "tier": r.tier if r else None, "bye": r.bye if r else None,
            "mine": self.state.is_mine(p), "keeper": p.is_keeper, "id": p.player_id,
        }

    def _vor_ranks(self) -> dict[str, int]:
        """Rank the draftable universe by value over replacement (cached)."""
        if getattr(self, "_vor_rank_cache", None) is None:
            depth = max(200, self.settings.total_picks + 60)
            universe = [
                p for p in self.ranked
                if p.rank <= depth and p.vor is not None
                and p.position not in ("K", "DEF") and self.rules.has_slot_for(p.position)
            ]
            by_vor = sorted(universe, key=lambda p: -(p.vor or 0))
            self._vor_rank_cache = {p.sleeper_id: i for i, p in enumerate(by_vor, start=1) if p.sleeper_id}
        return self._vor_rank_cache

    def sleeper_gap(self, player: RankedPlayer) -> int | None:
        """How many spots the projections rate him above his rankings slot."""
        v = self._vor_ranks().get(player.sleeper_id or "")
        return None if v is None else player.rank - v

    def sleepers(self, limit: int = 10, min_gap: int = 20, min_rank: int = 50, max_proj_rank: int = 150) -> list[dict[str, Any]]:
        """Still-available players the projections like far more than the rankings do.

        Three filters keep the list actionable: kickers and defenses are out
        (rankings push them late on purpose, so the gap is a convention rather
        than an edge), the player must be ranked outside the early rounds, and
        the projections must see him as a startable player rather than a deep
        flier with a large but meaningless gap.
        """
        out: list[dict[str, Any]] = []
        for p in self.state.available(self.ranked):
            if p.position in ("K", "DEF") or p.rank < min_rank:
                continue
            gap = self.sleeper_gap(p)
            if gap is None or gap < min_gap:
                continue
            if self._vor_ranks()[p.sleeper_id] > max_proj_rank:
                continue
            d = self.player_json(p)
            d["gap"] = gap
            d["proj_rank"] = self._vor_ranks()[p.sleeper_id]
            d["why"] = f"projections rate him {d['proj_rank']}th overall; your rankings have him {p.rank}th"
            out.append(d)
        out.sort(key=lambda d: -d["gap"])
        return out[:limit]

    def trends(self, rec: Recommendation) -> dict[str, Any]:
        """Recent positional runs and what the teams picking before me still need."""
        st = self.state
        window = st.settings.teams
        recent = [p for p in sorted(st.picks.values(), key=lambda p: p.pick_no) if not p.is_keeper][-window:]
        counts: dict[str, int] = {}
        for p in recent:
            counts[p.position or "?"] = counts.get(p.position or "?", 0) + 1
        mine = st.my_open_picks
        upcoming: list[dict[str, Any]] = []
        if mine:
            end = mine[1] if len(mine) > 1 else st.settings.total_picks + 1
            for n in st.open_picks():
                if mine[0] < n < end:
                    slot = slot_for_pick(n, st.settings)
                    needs = compute_needs([p.position or "" for p in st.roster_for_slot(slot)], st.rules)
                    open_pos = [pos for pos in ("QB", "RB", "WR", "TE", "DEF", "K") if needs.open_starters.get(pos, 0) > 0]
                    upcoming.append({"pick_no": n, "label": pick_label(n, st.settings), "owner": self.slot_names.get(slot, f"slot {slot}"), "needs": open_pos})
        forecast = {pos: {"expected": d.expected, "board": d.board, "run": d.run, "need": d.need, "is_run": d.is_run, "recent": d.recent_count} for pos, d in rec.demand.items()}
        return {"window": len(recent), "recent": counts, "runs": [pos for pos, d in rec.demand.items() if d.is_run], "upcoming": upcoming, "forecast": forecast}

    def snapshot(self, rec: Recommendation | None = None) -> dict[str, Any]:
        """Full JSON view for the web UI."""
        with self.lock:
            st = self.state
            rec = rec or self.recommendation()
            needs = st.my_needs()
            cur = st.current_pick_no
            nxt = st.my_next_pick_no
            fit = {sc.player.sleeper_id: sc for sc in rec.scored}
            available = []
            for p in st.available(self.ranked)[:80]:
                sc = fit.get(p.sleeper_id)
                available.append(self.player_json(p, sc.score if sc else None, sc.blocked if sc else False, sc.reasons if sc else None))
            best = {}
            for pos, p in rec.best_by_position.items():
                info = rec.tiers.get(pos)
                best[pos] = {
                    "player": self.player_json(p) if p else None,
                    "tier": info.tier if info else None,
                    "left": info.remaining_in_tier if info else 0,
                    "expected": info.expected_taken if info else 0,
                    "at_risk": bool(info and info.at_risk),
                    "thinning": bool(info and info.tier is not None and not info.at_risk and info.remaining_in_tier <= info.expected_taken + 1),
                }
            starters, bench = self.lineup()
            picks = sorted(st.picks.values(), key=lambda p: (-self._arrival.get(p.pick_no, -1), -p.pick_no))
            return {
                "league": self.league.get("name"),
                "dry_run": self.simulator is not None,
                "status": self.status,
                "teams": self.settings.teams,
                "rounds": self.settings.rounds,
                "draft_type": self.settings.draft_type,
                "my_slot": self.my_slot,
                "my_name": self.slot_names.get(self.my_slot, "you"),
                "slot_changed": self.slot_changed,
                "current_pick": cur,
                "current_label": pick_label(cur, self.settings) if cur else None,
                "round": st.current_round,
                "on_clock": self.owner_name(st.on_the_clock_slot),
                "on_clock_slot": st.on_the_clock_slot,
                "picks_until_me": st.picks_until_my_turn,
                "my_next_pick": nxt,
                "my_next_label": pick_label(nxt, self.settings) if nxt else None,
                "my_picks_left": len(st.my_open_picks),
                "is_my_turn": st.is_my_turn,
                "is_on_deck": st.is_on_deck,
                "horizon": st.picks_between_my_next_two,
                "draft_start_ms": self.draft.get("start_time"),
                "k_def_round": self.rec_cfg.k_def_round_threshold,
                "picks": [self.pick_json(p) for p in picks],
                "starters": starters,
                "bench": bench,
                "bench_size": self.rules.bench,
                "needs": {"open_starters": needs.open_starters, "open_flex": needs.open_flex, "counts": needs.counts},
                "rec": {
                    "take": self.player_json(rec.take.player, rec.take.score, rec.take.blocked, rec.take.reasons) if rec.take else None,
                    "backup": self.player_json(rec.backup.player, rec.backup.score, rec.backup.blocked, rec.backup.reasons) if rec.backup else None,
                    "why": rec.why,
                    "top": [self.player_json(p, fit[p.sleeper_id].score if p.sleeper_id in fit else None, fit[p.sleeper_id].blocked if p.sleeper_id in fit else False) for p in rec.top_overall],
                    "best": best,
                    "scored": [self.player_json(sc.player, sc.score, sc.blocked, sc.reasons) for sc in rec.scored[:25]],
                },
                "available": available,
                "targets": self.target_advice(),
                "trends": self.trends(rec),
                "sleepers": self.sleepers(),
                "polls": self.polls,
                "error": self.last_error,
                "updated": self.updated_at,
                "unmatched": self.unmatched,
                "scoring_notes": self.scoring_notes,
                "projected": self.projected,
            }


def build_session(
    cfg: Config,
    logger: logging.Logger,
    dry_run: bool = False,
    slot_override: int = 0,
    seed: int | None = None,
    rankings_override: str | None = None,
    client: SleeperClient | None = None,
) -> DraftSession:
    """Resolve league/draft/players/rankings and construct a session.

    Raises:
        SessionError: with a user-facing message on any startup problem.
    """
    client = client or SleeperClient()
    try:
        league = client.get_league(cfg.league_id)
        if not league:
            raise SessionError(f"League {cfg.league_id} not found")
        if league.get("draft_id") and cfg.draft_id and str(league["draft_id"]) != str(cfg.draft_id):
            logger.warning("league's current draft is %s, config had %s; following the league's", league["draft_id"], cfg.draft_id)
            cfg.draft_id = str(league["draft_id"])
        draft = _resolve_draft(client, cfg)
        users = client.get_league_users(cfg.league_id)
        rosters = client.get_league_rosters(cfg.league_id)
        existing_picks = client.get_draft_picks(str(draft["draft_id"]))
        db = PlayerDB.load(client, cfg.cache_path / "players.json")
    except SleeperAPIError as exc:
        raise SessionError(f"Sleeper API error during startup: {exc}") from exc

    settings = DraftSettings.from_draft(draft)
    rules = RosterRules.from_positions(league.get("roster_positions") or [])
    order = {str(k): int(v) for k, v in (draft.get("draft_order") or {}).items()}
    names = {str(u["user_id"]): u.get("display_name") or str(u["user_id"]) for u in users}
    slot_names = {slot: names.get(uid, uid) for uid, slot in order.items()}
    slot_users = {slot: uid for uid, slot in order.items()}
    my_user_id = cfg.user_id or (client.get_user(cfg.username) or {}).get("user_id")
    my_slot = order.get(str(my_user_id)) if my_user_id else None
    if dry_run and slot_override:
        my_slot = slot_override
    if my_slot is None:
        raise SessionError(
            "Your draft slot is unknown (draft order not set, or you are not in it). "
            "Re-run setup.py once the order is set, or rehearse with --dry-run --slot <n>."
        )
    slot_names.setdefault(my_slot, cfg.username or "you")

    rankings_file = Path(rankings_override) if rankings_override else cfg.rankings_file
    try:
        rankings = read_rankings_csv(rankings_file)
        result = match_rankings(rankings, db, load_overrides(cfg.overrides_file))
    except RankingsError as exc:
        raise SessionError(str(exc)) from exc
    if result.unmatched:
        logger.warning("unmatched rankings rows: %s", "; ".join(u.player.label for u in result.unmatched))
    ranked = sorted(result.matched, key=lambda p: p.rank)

    scoring = {k: float(v) for k, v in (league.get("scoring_settings") or {}).items() if v is not None}
    scoring_notes: list[str] = []
    projected = 0
    proj = ProjectionSet.load(int(league.get("season") or cfg.season), scoring, cfg.cache_path / f"projections_{league.get('season') or cfg.season}.json", session=client.session)
    if proj is not None:
        proj.compute_vor(rules, settings.teams)
        projected = proj.attach(ranked)
        scoring_notes = proj.scoring_summary(scoring)
        logger.info("projections attached to %d/%d ranked players; scoring vs generic half-PPR: %s", projected, len(ranked), "; ".join(scoring_notes) or "identical")

    my_roster_id = next((int(r["roster_id"]) for r in rosters if str(r.get("owner_id")) == str(my_user_id)), None)
    state = DraftState(settings, rules, my_slot, str(my_user_id) if my_user_id else None, my_roster_id=my_roster_id)
    rec_cfg = RecommendConfig(k_def_round_threshold=cfg.k_def_round_threshold)
    simulator: DraftSimulator | None = None
    source: PickSource
    if dry_run:
        simulator = DraftSimulator(
            draft, rules, ranked, db, my_slot, str(my_user_id) if my_user_id else None, slot_users,
            existing_picks=existing_picks, rng=random.Random(seed),
        )
        source = simulator
    else:
        source = LiveSource(client, str(draft["draft_id"]))
    logger.info(
        "league=%s teams=%d rounds=%d type=%s my_slot=%s ranked=%d existing_picks=%d dry_run=%s",
        league.get("name"), settings.teams, settings.rounds, settings.draft_type, my_slot, len(ranked), len(existing_picks), dry_run,
    )
    return DraftSession(
        cfg=cfg, league=league, draft=draft, settings=settings, rules=rules, state=state, ranked=ranked,
        slot_names=slot_names, my_slot=my_slot, my_user_id=str(my_user_id) if my_user_id else None,
        source=source, rec_cfg=rec_cfg, logger=logger, simulator=simulator, unmatched=len(result.unmatched),
        scoring_notes=scoring_notes, projected=projected,
    )


def _resolve_draft(client: SleeperClient, cfg: Config) -> dict[str, Any]:
    if cfg.draft_id:
        draft = client.get_draft(cfg.draft_id)
        if draft:
            return draft
    drafts = client.get_league_drafts(cfg.league_id)
    draft = pick_active_draft(drafts)
    if not draft:
        raise SleeperAPIError("league has no drafts")
    return client.get_draft(str(draft["draft_id"])) or draft
