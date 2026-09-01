"""Thin client for the public Sleeper API.

Every call goes through :meth:`SleeperClient._get`, which applies a timeout and
retries with exponential backoff on timeouts, connection errors, 429s and 5xx
responses. When retries are exhausted a :class:`SleeperAPIError` is raised;
callers in the draft loop catch it and keep polling instead of crashing.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import requests

BASE_URL = "https://api.sleeper.app/v1"


class SleeperAPIError(Exception):
    """A request failed after all retries, or the API returned a client error."""


class SleeperClient:
    """Small wrapper over ``requests.Session`` for the Sleeper endpoints we use."""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        max_retries: int = 3,
        backoff: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "sleeper-draft-assistant/1.0")
        self._sleep = sleep

    def _get(self, path: str, timeout: float | None = None) -> Any:
        """GET ``path`` and return decoded JSON. 404 returns ``None``."""
        url = f"{self.base_url}{path}"
        attempts = self.max_retries + 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                resp = self.session.get(url, timeout=timeout or self.timeout)
            except (requests.Timeout, requests.ConnectionError) as exc:
                last_error = exc
            else:
                if resp.status_code == 404:
                    return None
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = SleeperAPIError(f"HTTP {resp.status_code} for {path}")
                elif resp.status_code >= 400:
                    raise SleeperAPIError(f"HTTP {resp.status_code} for {path}: {resp.text[:200]}")
                else:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        last_error = exc
            if attempt < attempts - 1:
                self._sleep(self.backoff * (2**attempt))
        raise SleeperAPIError(
            f"GET {path} failed after {attempts} attempts: {last_error}"
        ) from last_error

    def get_user(self, username_or_id: str) -> dict[str, Any] | None:
        """Look up a user by username (or numeric user_id). ``None`` if not found."""
        data = self._get(f"/user/{username_or_id}")
        return data if isinstance(data, dict) else None

    def get_league(self, league_id: str) -> dict[str, Any] | None:
        """League settings, roster positions and scoring."""
        data = self._get(f"/league/{league_id}")
        return data if isinstance(data, dict) else None

    def get_league_users(self, league_id: str) -> list[dict[str, Any]]:
        """Members of the league with display names."""
        return self._get(f"/league/{league_id}/users") or []

    def get_league_rosters(self, league_id: str) -> list[dict[str, Any]]:
        """Rosters (roster_id -> owner_id) for the league."""
        return self._get(f"/league/{league_id}/rosters") or []

    def get_league_drafts(self, league_id: str) -> list[dict[str, Any]]:
        """All drafts attached to the league."""
        return self._get(f"/league/{league_id}/drafts") or []

    def get_draft(self, draft_id: str) -> dict[str, Any] | None:
        """Draft status, order, and settings."""
        data = self._get(f"/draft/{draft_id}")
        return data if isinstance(data, dict) else None

    def get_draft_picks(self, draft_id: str) -> list[dict[str, Any]]:
        """All picks made so far in the draft."""
        return self._get(f"/draft/{draft_id}/picks") or []

    def get_all_players(self) -> dict[str, dict[str, Any]]:
        """The full NFL player dump (~5MB+). Callers should cache this."""
        data = self._get("/players/nfl", timeout=max(self.timeout, 90.0))
        if not isinstance(data, dict) or not data:
            raise SleeperAPIError("players/nfl returned an empty or malformed response")
        return data

    def get_nfl_state(self) -> dict[str, Any]:
        """Current NFL season/week state."""
        return self._get("/state/nfl") or {}


def pick_active_draft(drafts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the draft to follow: ``drafting`` first, then ``paused``, then ``pre_draft``.

    Falls back to the most recently created draft if none are in progress, so a
    completed draft can still be reviewed.
    """
    if not drafts:
        return None
    priority = {"drafting": 0, "paused": 1, "pre_draft": 2, "complete": 3}
    return sorted(
        drafts,
        key=lambda d: (priority.get(str(d.get("status")), 9), -int(d.get("created") or 0)),
    )[0]
