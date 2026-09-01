"""Shared fixtures: a tiny fake Sleeper player dump and helpers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from draft_assistant.draft_state import DraftSettings, RosterRules  # noqa: E402
from draft_assistant.players import PlayerDB, RankedPlayer  # noqa: E402


def make_player(pid: str, first: str, last: str, pos: str, team: str | None, rank: int = 100, active: bool = True) -> dict:
    full = f"{first} {last}"
    return {
        "player_id": pid,
        "first_name": first,
        "last_name": last,
        "full_name": full,
        "position": pos,
        "fantasy_positions": [pos],
        "team": team,
        "active": active,
        "search_rank": rank,
        "search_full_name": "".join(ch for ch in full.lower() if ch.isalnum()),
    }


def make_def(team: str, city: str, nick: str) -> dict:
    return {"player_id": team, "first_name": city, "last_name": nick, "position": "DEF", "fantasy_positions": ["DEF"], "team": team, "active": True}


@pytest.fixture
def db() -> PlayerDB:
    players = {
        "1": make_player("1", "Marvin", "Harrison", "WR", "ARI", 10),
        "2": make_player("2", "Amon-Ra", "St. Brown", "WR", "DET", 5),
        "3": make_player("3", "Ja'Marr", "Chase", "WR", "CIN", 3),
        "4": make_player("4", "Kenneth", "Walker", "RB", "SEA", 20),
        "5": make_player("5", "Josh", "Allen", "QB", "BUF", 4),
        "6": make_player("6", "Josh", "Allen", "LB", "JAX", 500),  # same name, IDP -> not indexed as skill
        "7": make_player("7", "Mike", "Williams", "WR", "LAC", 90),
        "8": make_player("8", "Mike", "Williams", "WR", "NYJ", 300),
        "9": make_player("9", "Tyler", "Davis", "K", "JAX", 200),
        "10": make_player("10", "Tyler", "Davis", "TE", "GB", 400),
        "11": make_player("11", "Ka'imi", "Fairbairn", "K", "HOU", 180),
        "12": make_player("12", "José", "Ramírez", "RB", "WAS", 150),
        "13": make_player("13", "Brian", "Thomas", "WR", "JAX", 30),
        "14": make_player("14", "Odell", "Beckham", "WR", None, 470, active=False),
        "15": make_player("15", "Travis", "Etienne", "RB", "NO", 34),
        "PHI": make_def("PHI", "Philadelphia", "Eagles"),
        "JAX": make_def("JAX", "Jacksonville", "Jaguars"),
        "WAS": make_def("WAS", "Washington", "Commanders"),
        "SF": make_def("SF", "San Francisco", "49ers"),
    }
    # Sleeper's fantasy_positions for an IDP LB does not include a skill position.
    players["6"]["fantasy_positions"] = ["LB"]
    return PlayerDB(players)


@pytest.fixture
def snake12() -> DraftSettings:
    return DraftSettings(teams=12, rounds=16, draft_type="snake")


@pytest.fixture
def rules_std() -> RosterRules:
    return RosterRules.from_positions(["QB", "RB", "RB", "WR", "WR", "WR", "TE", "FLEX", "FLEX", "DEF", "BN", "BN", "BN", "BN", "BN", "BN"])


@pytest.fixture
def rules_sf() -> RosterRules:
    return RosterRules.from_positions(["QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "SUPER_FLEX", "K", "BN", "BN", "BN", "BN", "BN"])


def rp(rank: int, name: str, pos: str, team: str = "XX", tier: int | None = None, bye: int | None = None, adp: float | None = None) -> RankedPlayer:
    return RankedPlayer(rank=rank, name=name, position=pos, team=team, tier=tier, bye=bye, ecr_vs_adp=adp, sleeper_id=f"id{rank}")
