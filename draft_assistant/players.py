"""Player cache, name normalization, and rankings-to-Sleeper matching.

The Sleeper player dump is cached to ``cache/players.json`` and refreshed only
when older than 24 hours. Rankings come from a FantasyPros CSV export whose
column names vary slightly between exports, so headers are matched loosely.
"""

from __future__ import annotations

import csv
import difflib
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .sleeper import SleeperAPIError, SleeperClient

log = logging.getLogger("draft.players")

FANTASY_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K", "DEF")
SKILL_POSITIONS: tuple[str, ...] = ("QB", "RB", "WR", "TE", "K")

NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Rankings sites use a few abbreviations that differ from Sleeper's.
TEAM_ALIASES: dict[str, str] = {
    "JAC": "JAX",
    "WSH": "WAS",
    "LA": "LAR",
    "OAK": "LV",
    "LVR": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "STL": "LAR",
    "KCC": "KC",
    "GBP": "GB",
    "NEP": "NE",
    "NWE": "NE",
    "NOS": "NO",
    "NOR": "NO",
    "SFO": "SF",
    "TBB": "TB",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
}

POSITION_ALIASES: dict[str, str] = {
    "DST": "DEF",
    "D/ST": "DEF",
    "D": "DEF",
    "DEF": "DEF",
    "PK": "K",
    "K": "K",
}

# Column-name aliases for the rankings CSV (compared after normalization).
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "rank": ("RK", "RANK", "OVERALL", "ECR", "OVERALL RANK"),
    "tier": ("TIERS", "TIER"),
    "name": ("PLAYER NAME", "PLAYER", "NAME"),
    "team": ("TEAM", "TM"),
    "position": ("POS", "POSITION"),
    "bye": ("BYE WEEK", "BYE"),
    "sos": ("SOS SEASON", "SOS", "STRENGTH OF SCHEDULE"),
    "ecr_vs_adp": ("ECR VS ADP", "VS ADP", "ECR VS. ADP"),
    "adp": ("ADP", "AVG ADP", "AVERAGE ADP"),
    "upside": ("UPSIDE",),
    "bust": ("BUST",),
}


class RankingsError(Exception):
    """The rankings CSV could not be read or has no usable columns."""


def strip_accents(text: str) -> str:
    """Return ``text`` with accents removed (``José`` -> ``Jose``)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_name(name: str) -> str:
    """Collapse a player name to a comparison key.

    Lower-cases, strips accents, removes periods/apostrophes/hyphens/spaces, and
    drops generational suffixes (Jr., Sr., II, III, IV, V) so that
    ``"Marvin Harrison Jr."`` and ``"Marvin Harrison"`` compare equal.
    """
    text = strip_accents(name or "").lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[.'’`‘\-]", "", text)
    tokens = re.sub(r"[^a-z0-9 ]", " ", text).split()
    while len(tokens) > 1 and tokens[-1] in NAME_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def normalize_team(team: str | None) -> str | None:
    """Map a team abbreviation onto Sleeper's abbreviation, or ``None`` if blank/FA."""
    if not team:
        return None
    key = re.sub(r"[^A-Z]", "", str(team).upper())
    if key in ("", "FA", "NA", "NONE", "FREE"):
        return None
    return TEAM_ALIASES.get(key, key)


def normalize_position(pos: str | None) -> str | None:
    """``"WR12"`` -> ``"WR"``, ``"DST3"`` -> ``"DEF"``, ``"pk"`` -> ``"K"``."""
    if not pos:
        return None
    key = re.sub(r"\d+$", "", str(pos).strip().upper())
    return POSITION_ALIASES.get(key, key) or None


@dataclass
class RankedPlayer:
    """One row of the rankings file, optionally linked to a Sleeper player id."""

    rank: int
    name: str
    position: str
    team: str | None = None
    tier: int | None = None
    bye: int | None = None
    sos: str | None = None
    ecr_vs_adp: float | None = None
    sleeper_id: str | None = None
    upside: int | None = None  # FantasyPros "5 out of 5" -> 5
    bust: int | None = None
    injury: str | None = None  # Sleeper injury_status (Questionable/Doubtful/Out/IR/PUP...)
    rookie: bool = False
    proj_pts: float | None = None  # projected season points under the league's scoring
    proj_tilt: int | None = None  # rank spots gained (+) or lost (-) under league scoring vs generic half-PPR
    vor: float | None = None  # value over replacement at position, league scoring
    proj_gap: int | None = None  # rank spots the projections rate him above (+) or below (-) his ranking
    raw: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.team = normalize_team(self.team)
        self.position = normalize_position(self.position) or self.position

    @property
    def norm_name(self) -> str:
        """Normalized comparison key for the name."""
        return normalize_name(self.name)

    @property
    def label(self) -> str:
        """``"Name (POS, TEAM)"`` for messages."""
        return f"{self.name} ({self.position}, {self.team or 'FA'})"


def _norm_header(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").replace(".", "").strip().upper())


def _resolve_columns(headers: Iterable[str]) -> dict[str, str]:
    """Map our logical field names onto the actual CSV header strings."""
    lookup = {_norm_header(h): h for h in headers if h}
    resolved: dict[str, str] = {}
    for field_name, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                resolved[field_name] = lookup[alias]
                break
    return resolved


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = str(value).strip().replace("+", "")
    if not text or text.lower() in ("na", "n/a", "-", "--"):
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _leading_int(value: str | None) -> int | None:
    """``"4 out of 5"`` -> 4; ``"3"`` -> 3; blank -> None."""
    if value is None:
        return None
    m = re.match(r"\s*(\d+)", str(value))
    return int(m.group(1)) if m else None


def _to_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace("+", "")
    if not text or text.lower() in ("na", "n/a", "-", "--"):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def read_rankings_csv(path: Path | str) -> list[RankedPlayer]:
    """Parse a FantasyPros-style rankings export.

    Required columns: player name and position (rank falls back to row order).
    Optional: tier, team, bye, SOS, ``ECR VS. ADP`` (or ``ADP``, from which the
    delta is computed as ADP minus rank; positive means the player usually goes
    later than they are ranked, i.e. a value).
    """
    path = Path(path)
    if not path.exists():
        raise RankingsError(f"Rankings file not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RankingsError(f"{path} is empty")
        cols = _resolve_columns(reader.fieldnames)
        if "name" not in cols or "position" not in cols:
            raise RankingsError(
                f"{path}: could not find player-name and position columns. "
                f"Headers seen: {reader.fieldnames}"
            )
        players: list[RankedPlayer] = []
        for row_no, row in enumerate(reader, start=1):
            name = (row.get(cols["name"]) or "").strip()
            position = normalize_position(row.get(cols["position"]))
            if not name or not position:
                continue
            rank = _to_int(row.get(cols["rank"])) if "rank" in cols else None
            adp = _to_float(row.get(cols["adp"])) if "adp" in cols else None
            ecr_vs_adp = _to_float(row.get(cols["ecr_vs_adp"])) if "ecr_vs_adp" in cols else None
            if ecr_vs_adp is None and adp is not None and rank is not None:
                ecr_vs_adp = adp - rank
            players.append(
                RankedPlayer(
                    rank=rank if rank is not None else row_no,
                    name=name,
                    position=position,
                    team=normalize_team(row.get(cols["team"])) if "team" in cols else None,
                    tier=_to_int(row.get(cols["tier"])) if "tier" in cols else None,
                    bye=_to_int(row.get(cols["bye"])) if "bye" in cols else None,
                    sos=(row.get(cols["sos"]) or "").strip() or None if "sos" in cols else None,
                    ecr_vs_adp=ecr_vs_adp,
                    upside=_leading_int(row.get(cols["upside"])) if "upside" in cols else None,
                    bust=_leading_int(row.get(cols["bust"])) if "bust" in cols else None,
                    raw=dict(row),
                )
            )
    if not players:
        raise RankingsError(f"{path}: no player rows found")
    players.sort(key=lambda p: p.rank)
    return players


def load_overrides(path: Path | str) -> dict[str, str]:
    """Load ``overrides.json``: ``{"Rankings Name": "<sleeper_id or Sleeper name>"}``."""
    path = Path(path)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RankingsError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RankingsError(f"{path} must be a JSON object")
    return {str(k): str(v) for k, v in data.items() if not str(k).startswith("_")}


class PlayerDB:
    """In-memory index over the Sleeper player dump."""

    def __init__(self, players: dict[str, dict[str, Any]], fetched_at: float | None = None) -> None:
        self.players = players
        self.fetched_at = fetched_at
        self._by_name: dict[str, list[str]] = {}
        self.def_by_team: dict[str, str] = {}
        self._def_by_word: dict[str, str] = {}
        self._build_index()

    # ------------------------------------------------------------------ cache
    @classmethod
    def load(
        cls,
        client: SleeperClient,
        cache_path: Path | str,
        max_age_hours: float = 24.0,
        force_refresh: bool = False,
    ) -> "PlayerDB":
        """Load from cache if fresh, otherwise download and cache.

        If the download fails but a stale cache exists, the stale cache is used
        with a warning rather than failing.
        """
        cache_path = Path(cache_path)
        cached: dict[str, Any] | None = None
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Ignoring unreadable player cache %s: %s", cache_path, exc)
                cached = None
        if cached and not force_refresh:
            age_h = (time.time() - float(cached.get("fetched_at", 0))) / 3600.0
            if age_h <= max_age_hours and cached.get("players"):
                log.info("Using cached players (%.1fh old)", age_h)
                return cls(cached["players"], float(cached["fetched_at"]))
        try:
            players = client.get_all_players()
        except SleeperAPIError as exc:
            if cached and cached.get("players"):
                log.warning("Player download failed (%s); using stale cache", exc)
                return cls(cached["players"], float(cached.get("fetched_at", 0)))
            raise
        fetched_at = time.time()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fetched_at": fetched_at, "players": players}), encoding="utf-8"
        )
        log.info("Downloaded %d players and cached to %s", len(players), cache_path)
        return cls(players, fetched_at)

    # ------------------------------------------------------------------ index
    def _build_index(self) -> None:
        for pid, p in self.players.items():
            pos = p.get("position")
            if pos == "DEF":
                team = p.get("team") or pid
                self.def_by_team[team] = pid
                for word in (p.get("first_name"), p.get("last_name")):
                    if word:
                        self._def_by_word[normalize_name(word)] = pid
                continue
            positions = set(p.get("fantasy_positions") or [])
            if pos:
                positions.add(pos)
            if not positions & set(SKILL_POSITIONS):
                continue
            key = normalize_name(self.name(pid))
            if key:
                self._by_name.setdefault(key, []).append(pid)
            alt = p.get("search_full_name")
            if alt and alt != key:
                self._by_name.setdefault(alt, []).append(pid)

    def get(self, pid: str) -> dict[str, Any] | None:
        """Raw Sleeper record."""
        return self.players.get(pid)

    def name(self, pid: str) -> str:
        """Display name (``"City Nickname"`` for defenses)."""
        p = self.players.get(pid) or {}
        if p.get("position") == "DEF":
            return f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid
        return p.get("full_name") or f"{p.get('first_name', '')} {p.get('last_name', '')}".strip() or pid

    def position(self, pid: str) -> str | None:
        """Primary position."""
        p = self.players.get(pid) or {}
        return p.get("position")

    def team(self, pid: str) -> str | None:
        """Current team abbreviation."""
        p = self.players.get(pid) or {}
        return p.get("team")

    def positions(self, pid: str) -> set[str]:
        """All fantasy-eligible positions."""
        p = self.players.get(pid) or {}
        out = set(p.get("fantasy_positions") or [])
        if p.get("position"):
            out.add(p["position"])
        return out

    def candidates(self, norm_name: str) -> list[str]:
        """Player ids whose normalized name equals ``norm_name`` (best first)."""
        pids = list(dict.fromkeys(self._by_name.get(norm_name, [])))
        return sorted(pids, key=self._sort_key)

    def _sort_key(self, pid: str) -> tuple[int, int, int]:
        p = self.players.get(pid) or {}
        return (
            0 if p.get("active") else 1,
            0 if p.get("team") else 1,
            int(p.get("search_rank") or 9_999_999),
        )

    def def_for_name(self, name: str) -> str | None:
        """Resolve ``"Philadelphia Eagles"``, ``"Eagles"``, ``"PHI D/ST"`` to a DEF id."""
        for token in re.split(r"[\s/]+", name or ""):
            team = normalize_team(token)
            if team and team in self.def_by_team and len(token) <= 3:
                return self.def_by_team[team]
        for token in re.split(r"\s+", name or ""):
            key = normalize_name(token)
            if key in self._def_by_word and key not in ("dst", "def", "d", "st"):
                return self._def_by_word[key]
        return None

    def skill_pids(self) -> list[str]:
        """All ids indexed by name (QB/RB/WR/TE/K)."""
        seen: set[str] = set()
        for pids in self._by_name.values():
            seen.update(pids)
        return list(seen)


# ---------------------------------------------------------------- matching
@dataclass
class Unmatched:
    """A rankings row that could not be linked to a Sleeper player."""

    player: RankedPlayer
    suggestions: list[str]
    reason: str


@dataclass
class MatchResult:
    """Outcome of :func:`match_rankings`."""

    matched: list[RankedPlayer]
    unmatched: list[Unmatched]
    notes: list[str]

    @property
    def by_sleeper_id(self) -> dict[str, RankedPlayer]:
        """Sleeper id -> ranked player."""
        return {p.sleeper_id: p for p in self.matched if p.sleeper_id}


def fuzzy_suggestions(player: RankedPlayer, db: PlayerDB, n: int = 3) -> list[str]:
    """Closest Sleeper names for an unmatched row, restricted to the same position."""
    if player.position == "DEF":
        return sorted(db.def_by_team)[:0] + [
            f"{db.name(pid)} [{team}]" for team, pid in sorted(db.def_by_team.items())
            if player.team and team.startswith(player.team[:1])
        ][:n]
    pool: dict[str, str] = {}
    for pid in db.skill_pids():
        if player.position in db.positions(pid):
            p = db.get(pid) or {}
            if not p.get("active"):
                continue
            pool[normalize_name(db.name(pid))] = pid
    close = difflib.get_close_matches(player.norm_name, list(pool), n=n, cutoff=0.6)
    out = []
    for key in close:
        pid = pool[key]
        out.append(f"{db.name(pid)} ({db.position(pid)}, {db.team(pid) or 'FA'}) id={pid}")
    return out


def _resolve_override(value: str, db: PlayerDB) -> str | None:
    if value in db.players:
        return value
    if value.upper() in db.def_by_team:
        return db.def_by_team[value.upper()]
    cands = db.candidates(normalize_name(value))
    return cands[0] if cands else None


def match_player(
    player: RankedPlayer, db: PlayerDB, overrides: dict[str, str] | None = None
) -> tuple[str | None, str | None]:
    """Return ``(sleeper_id, note)`` for one ranked player.

    ``note`` describes anything worth surfacing (team mismatch, ambiguity) and
    is ``None`` for a clean match. ``sleeper_id`` is ``None`` if unmatched.
    """
    overrides = overrides or {}
    override = overrides.get(player.name) or next(
        (v for k, v in overrides.items() if normalize_name(k) == player.norm_name), None
    )
    if override:
        pid = _resolve_override(override, db)
        if pid:
            return pid, None
        return None, f"override {override!r} does not resolve to a Sleeper player"

    if player.position == "DEF":
        team = normalize_team(player.team)
        if team and team in db.def_by_team:
            return db.def_by_team[team], None
        pid = db.def_for_name(player.name)
        if pid:
            return pid, None
        return None, "could not map defense to a team"

    cands = db.candidates(player.norm_name)
    if not cands:
        return None, "no Sleeper player with this name"
    pos_matches = [c for c in cands if player.position in db.positions(c)]
    if not pos_matches:
        pid = cands[0]
        return pid, f"position mismatch: rankings say {player.position}, Sleeper says {db.position(pid)}"
    if len(pos_matches) == 1:
        pid = pos_matches[0]
        if player.team and db.team(pid) and db.team(pid) != player.team:
            return pid, f"team mismatch: rankings say {player.team}, Sleeper says {db.team(pid)}"
        return pid, None
    if player.team:
        team_matches = [c for c in pos_matches if db.team(c) == player.team]
        if len(team_matches) == 1:
            return team_matches[0], None
        if len(team_matches) > 1:
            pos_matches = team_matches
    pid = pos_matches[0]
    others = ", ".join(f"{db.name(c)} {db.position(c)}/{db.team(c) or 'FA'} id={c}" for c in pos_matches[1:])
    return pid, f"ambiguous name; chose id={pid} ({db.team(pid) or 'FA'}) over {others}"


def match_rankings(
    rankings: list[RankedPlayer], db: PlayerDB, overrides: dict[str, str] | None = None
) -> MatchResult:
    """Link every ranked player to a Sleeper id.

    Each Sleeper id is used at most once; if two rows resolve to the same id
    the better-ranked row wins and the other is reported as unmatched.
    """
    matched: list[RankedPlayer] = []
    unmatched: list[Unmatched] = []
    notes: list[str] = []
    used: dict[str, RankedPlayer] = {}
    for player in sorted(rankings, key=lambda p: p.rank):
        pid, note = match_player(player, db, overrides)
        if pid is None:
            unmatched.append(Unmatched(player, fuzzy_suggestions(player, db), note or "no match"))
            continue
        if pid in used:
            unmatched.append(
                Unmatched(
                    player,
                    [f"already matched to {used[pid].label} (rank {used[pid].rank}) id={pid}"],
                    "duplicate: another row already maps to this Sleeper player",
                )
            )
            continue
        if note:
            notes.append(f"#{player.rank} {player.label}: {note}")
        player.sleeper_id = pid
        rec = db.get(pid) or {}
        player.injury = (rec.get("injury_status") or None) if rec.get("position") != "DEF" else None
        player.rookie = rec.get("years_exp") == 0
        used[pid] = player
        matched.append(player)
    return MatchResult(matched=matched, unmatched=unmatched, notes=notes)
