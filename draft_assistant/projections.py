"""Season projections scored under the league's exact scoring settings.

Sleeper publishes season-long stat projections per player (pass_yd, pass_td,
rec, ... ) keyed by Sleeper player id. Multiplying those by the league's
``scoring_settings`` gives projected points under *your* rules, which lets us
see where your league's scoring disagrees with generic half-PPR rankings
(6-point passing TDs, for example, lift QBs).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests

from .draft_state import RosterRules
from .players import FANTASY_POSITIONS, RankedPlayer

log = logging.getLogger("draft.projections")

PROJECTIONS_URL = (
    "https://api.sleeper.app/projections/nfl/{season}?season_type=regular"
    "&position[]=QB&position[]=RB&position[]=WR&position[]=TE&position[]=K&position[]=DEF"
)

# Generic half-PPR baseline, i.e. what FantasyPros' half-PPR ranks assume.
BASELINE_HALF_PPR: dict[str, float] = {
    "pass_yd": 0.04, "pass_td": 4.0, "pass_int": -2.0, "pass_2pt": 2.0,
    "rush_yd": 0.1, "rush_td": 6.0, "rush_2pt": 2.0,
    "rec": 0.5, "rec_yd": 0.1, "rec_td": 6.0, "rec_2pt": 2.0,
    "fum_lost": -2.0,
}

# Rough share of FLEX-type slots each position ends up occupying.
FLEX_SHARE: dict[str, float] = {"RB": 0.45, "WR": 0.45, "TE": 0.10, "QB": 0.0}


def league_points(stats: dict[str, Any], scoring: dict[str, float]) -> float:
    """Sum ``stat * weight`` over every stat the league scores."""
    total = 0.0
    for key, weight in scoring.items():
        val = stats.get(key)
        if val and weight:
            total += float(val) * float(weight)
    return total


@dataclass
class Projection:
    """Projected season points for one player under two scoring systems."""

    player_id: str
    position: str
    pts_league: float
    pts_baseline: float
    stats: dict[str, float] = field(default_factory=dict)
    rank_league: int | None = None  # rank among all fantasy players by league points
    rank_baseline: int | None = None
    vor: float | None = None  # value over replacement at position, league scoring
    vor_baseline: float | None = None
    vor_rank_league: int | None = None
    vor_rank_baseline: int | None = None

    @property
    def tilt(self) -> int | None:
        """Rank spots your scoring moves him vs generic half-PPR (+ = better for you).

        Uses value-over-replacement ranks when available so positional scarcity
        is respected; falls back to raw-points ranks.
        """
        if self.vor_rank_league is not None and self.vor_rank_baseline is not None:
            return self.vor_rank_baseline - self.vor_rank_league
        if self.rank_league is None or self.rank_baseline is None:
            return None
        return self.rank_baseline - self.rank_league


class ProjectionSet:
    """All projections for a season, cached to disk for 24h."""

    def __init__(self, rows: list[dict[str, Any]], scoring: dict[str, float]) -> None:
        self.by_id: dict[str, Projection] = {}
        for row in rows:
            stats = row.get("stats") or {}
            pos = (row.get("player") or {}).get("position")
            pid = str(row.get("player_id") or "")
            if not pid or pos not in FANTASY_POSITIONS or not stats.get("gp"):
                continue
            self.by_id[pid] = Projection(
                player_id=pid, position=pos,
                pts_league=league_points(stats, scoring),
                pts_baseline=league_points(stats, BASELINE_HALF_PPR),
                stats={k: float(v) for k, v in stats.items() if isinstance(v, (int, float))},
            )
        self._rank()

    @classmethod
    def load(cls, season: int, scoring: dict[str, float], cache_path: Path | str, max_age_hours: float = 24.0,
             session: requests.Session | None = None) -> "ProjectionSet | None":
        """Load from cache or download. Returns ``None`` (with a log line) if unavailable."""
        cache_path = Path(cache_path)
        rows: list[dict[str, Any]] | None = None
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if time.time() - float(cached.get("fetched_at", 0)) <= max_age_hours * 3600:
                    rows = cached.get("rows")
            except (json.JSONDecodeError, OSError, ValueError) as exc:
                log.warning("ignoring bad projections cache: %s", exc)
        if rows is None:
            try:
                resp = (session or requests).get(PROJECTIONS_URL.format(season=season), timeout=60)
                resp.raise_for_status()
                rows = resp.json()
                if not isinstance(rows, list) or not rows:
                    raise ValueError("empty projections payload")
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({"fetched_at": time.time(), "rows": rows}), encoding="utf-8")
                log.info("downloaded %d projection rows", len(rows))
            except (requests.RequestException, ValueError) as exc:
                log.warning("projections unavailable (%s); recommendations will use rankings only", exc)
                if cache_path.exists():
                    try:
                        rows = json.loads(cache_path.read_text(encoding="utf-8")).get("rows")
                    except (json.JSONDecodeError, OSError):
                        rows = None
                if not rows:
                    return None
        return cls(rows, scoring)

    def _rank(self) -> None:
        skill = [p for p in self.by_id.values() if p.position in ("QB", "RB", "WR", "TE")]
        for i, p in enumerate(sorted(skill, key=lambda p: -p.pts_league), start=1):
            p.rank_league = i
        for i, p in enumerate(sorted(skill, key=lambda p: -p.pts_baseline), start=1):
            p.rank_baseline = i

    def compute_vor(self, rules: RosterRules, teams: int) -> None:
        """Value over replacement: points above the last starter-quality player at the position."""
        flex_slots = sum(n for name, n in rules.flex.items() if name != "SUPER_FLEX")
        sf = rules.flex.get("SUPER_FLEX", 0)
        for pos in FANTASY_POSITIONS:
            starters = rules.starters.get(pos, 0)
            share = FLEX_SHARE.get(pos, 0.0) * flex_slots
            if pos == "QB":
                share += sf * 0.8
            n = int(round(teams * (starters + share)))
            pool = [p for p in self.by_id.values() if p.position == pos]
            if not pool:
                continue
            idx = min(max(n, 1), len(pool)) - 1
            repl_league = sorted(pool, key=lambda p: -p.pts_league)[idx].pts_league
            repl_base = sorted(pool, key=lambda p: -p.pts_baseline)[idx].pts_baseline
            for p in pool:
                p.vor = p.pts_league - repl_league
                p.vor_baseline = p.pts_baseline - repl_base
        skill = [p for p in self.by_id.values() if p.position in ("QB", "RB", "WR", "TE") and p.vor is not None]
        for i, p in enumerate(sorted(skill, key=lambda p: -(p.vor or 0)), start=1):
            p.vor_rank_league = i
        for i, p in enumerate(sorted(skill, key=lambda p: -(p.vor_baseline or 0)), start=1):
            p.vor_rank_baseline = i

    def attach(self, ranked: list[RankedPlayer]) -> int:
        """Copy projections onto ranked players; returns how many got one."""
        hit = 0
        for r in ranked:
            p = self.by_id.get(r.sleeper_id or "")
            if p is None:
                continue
            r.proj_pts = round(p.pts_league, 1)
            r.proj_tilt = p.tilt
            r.vor = round(p.vor, 1) if p.vor is not None else None
            hit += 1
        return hit

    def scoring_summary(self, scoring: dict[str, float]) -> list[str]:
        """Human-readable lines on how the league's scoring differs from generic half-PPR."""
        lines = []
        for key, base in BASELINE_HALF_PPR.items():
            val = float(scoring.get(key, 0) or 0)
            if abs(val - base) > 1e-6:
                lines.append(f"{key}: {val:g} (generic half-PPR: {base:g})")
        extras = [k for k, v in scoring.items() if v and k not in BASELINE_HALF_PPR and k.startswith(("bonus", "rec_", "rush_", "pass_")) and k not in ("rec_2pt", "rush_2pt", "pass_2pt")]
        for k in sorted(extras):
            lines.append(f"{k}: {float(scoring[k]):g} (not in generic half-PPR)")
        return lines
