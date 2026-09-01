"""Dry-run draft simulator.

Presents the same ``fetch_draft`` / ``fetch_picks`` interface as the live
Sleeper source, using the real league and draft settings (teams, rounds, order,
keepers) but generating CPU picks for the other teams. CPU teams pick near the
board with Gaussian noise and basic roster sanity so the rehearsal feels real.
"""

from __future__ import annotations

import copy
import random
from typing import Any

from .draft_state import DraftSettings, DraftState, RosterRules, compute_needs
from .players import PlayerDB, RankedPlayer


class DraftSimulator:
    """Fake pick source for ``draft.py --dry-run``."""

    def __init__(
        self,
        draft: dict[str, Any],
        rules: RosterRules,
        pool: list[RankedPlayer],
        db: PlayerDB,
        my_slot: int,
        my_user_id: str | None,
        slot_users: dict[int, str],
        existing_picks: list[dict[str, Any]] | None = None,
        rng: random.Random | None = None,
        cpu_picks_per_poll: int = 1,
    ) -> None:
        self.draft = copy.deepcopy(draft)
        self.settings = DraftSettings.from_draft(draft)
        self.rules = rules
        self.pool = [p for p in pool if p.sleeper_id]
        self.db = db
        self.my_slot = my_slot
        self.my_user_id = my_user_id
        self.slot_users = slot_users
        self.picks: list[dict[str, Any]] = copy.deepcopy(existing_picks or [])
        self.rng = rng or random.Random()
        self.cpu_picks_per_poll = max(1, cpu_picks_per_poll)
        s2r = draft.get("slot_to_roster_id") or {}
        self.roster_ids: dict[int, int | None] = {int(k): int(v) for k, v in s2r.items()} if s2r else {}
        self.status = "pre_draft"
        self._polls = 0

    # ------------------------------------------------------------- source API
    def fetch_draft(self) -> dict[str, Any]:
        """Draft payload; status moves pre_draft -> drafting -> complete."""
        self._polls += 1
        if self.status == "pre_draft" and self._polls >= 2:
            self.status = "drafting"
        if self._state().is_complete:
            self.status = "complete"
        d = dict(self.draft)
        d["status"] = self.status
        return d

    def fetch_picks(self) -> list[dict[str, Any]]:
        """Advance CPU picks (if it is not my turn) and return all picks."""
        if self.status == "drafting":
            for _ in range(self.cpu_picks_per_poll):
                state = self._state()
                if state.is_complete or state.is_my_turn:
                    break
                self._cpu_pick(state)
        return [dict(p) for p in self.picks]

    def submit_pick(self, player: RankedPlayer) -> dict[str, Any]:
        """Record my pick. Raises ``ValueError`` if it is not my turn or the player is gone."""
        state = self._state()
        if not state.is_my_turn:
            raise ValueError("It is not your turn")
        if player.sleeper_id in state.taken_ids:
            raise ValueError(f"{player.name} is already drafted")
        assert state.current_pick_no is not None
        raw = self._raw_pick(state.current_pick_no, self.my_slot, player, self.my_user_id)
        self.picks.append(raw)
        return raw

    # ------------------------------------------------------------- internals
    def _state(self) -> DraftState:
        state = DraftState(self.settings, self.rules, self.my_slot, self.my_user_id)
        state.update_picks(self.picks)
        return state

    def _raw_pick(self, pick_no: int, slot: int, player: RankedPlayer, user_id: str | None) -> dict[str, Any]:
        rec = self.db.get(player.sleeper_id or "") or {}
        if rec.get("position") == "DEF":
            first, last = rec.get("first_name", ""), rec.get("last_name", "")
        else:
            first = rec.get("first_name") or player.name.split(" ")[0]
            last = rec.get("last_name") or " ".join(player.name.split(" ")[1:])
        rnd = (pick_no - 1) // self.settings.teams + 1
        return {
            "pick_no": pick_no,
            "round": rnd,
            "draft_slot": slot,
            "player_id": player.sleeper_id,
            "picked_by": user_id or "",
            "roster_id": self.roster_ids.get(slot),
            "is_keeper": None,
            "metadata": {
                "first_name": first,
                "last_name": last,
                "position": rec.get("position") or player.position,
                "team": rec.get("team") or player.team or "",
                "player_id": player.sleeper_id,
            },
        }

    def _cpu_pick(self, state: DraftState) -> None:
        pick_no = state.current_pick_no
        slot = state.on_the_clock_slot
        assert pick_no is not None and slot is not None
        rnd = state.current_round
        taken = state.taken_ids
        avail = [p for p in self.pool if p.sleeper_id not in taken]
        if not avail:
            return
        roster = state.roster_for_slot(slot)
        needs = compute_needs([p.position or "" for p in roster], self.rules)
        counts = needs.counts
        picks_left = sum(1 for n in state.open_picks() if state.settings and self._slot_of(n) == slot)
        must_fill = picks_left <= needs.open_starter_total

        def allowed(p: RankedPlayer) -> bool:
            pos = p.position
            if not self.rules.has_slot_for(pos):
                return False
            if pos in ("K", "DEF"):
                if counts.get(pos, 0) >= self.rules.starters.get(pos, 0):
                    return False
                return rnd >= self.settings.rounds - 2 or must_fill
            if pos == "QB":
                limit = 3 if self.rules.is_superflex else 2
                if counts.get("QB", 0) >= limit:
                    return False
                if not self.rules.is_superflex and counts.get("QB", 0) == 1 and rnd < 8:
                    return False
            if pos == "TE":
                if counts.get("TE", 0) >= 2:
                    return False
                if counts.get("TE", 0) == 1 and rnd < 9:
                    return False
            return True

        cands = [p for p in avail if allowed(p)]
        if must_fill:
            needed = [p for p in cands if needs.open_starters.get(p.position, 0) > 0 or needs.open_flex_for(p.position) > 0]
            if needed:
                cands = needed
        if not cands:
            cands = avail
        sigma = 2.0 + 0.4 * rnd
        idx = min(len(cands) - 1, int(abs(self.rng.gauss(0.0, sigma))))
        choice = cands[idx]
        self.picks.append(self._raw_pick(pick_no, slot, choice, self.slot_users.get(slot)))

    def _slot_of(self, pick_no: int) -> int:
        from .draft_state import slot_for_pick

        return slot_for_pick(pick_no, self.settings)
