#!/usr/bin/env python3
"""Live draft loop.

    python draft.py                # follow the real draft
    python draft.py --dry-run      # rehearse with simulated opponents
    python draft.py --dry-run --auto --max-rounds 3   # non-interactive rehearsal

Ctrl-C exits cleanly. Everything is also logged to logs/draft_<date>.log.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

from rich.console import Console
from rich.live import Live

from draft_assistant.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from draft_assistant.display import Display, local_time
from draft_assistant.draft_state import DraftState, pick_label
from draft_assistant.players import RankedPlayer, normalize_name
from draft_assistant.recommend import RecommendConfig, Recommendation, recommend
from draft_assistant.session import PickSource, SessionError, build_session
from draft_assistant.simulate import DraftSimulator
from draft_assistant.sleeper import SleeperAPIError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI flags."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--dry-run", action="store_true", help="Simulate the draft with CPU opponents")
    ap.add_argument("--auto", action="store_true", help="(dry-run) auto-accept the TAKE recommendation for your picks")
    ap.add_argument("--max-rounds", type=int, default=0, help="(dry-run) stop after this many rounds")
    ap.add_argument("--slot", type=int, default=0, help="(dry-run) draft slot to use if you are not in the draft order")
    ap.add_argument("--seed", type=int, default=None, help="(dry-run) random seed for reproducible rehearsals")
    ap.add_argument("--sim-delay", type=float, default=0.6, help="(dry-run) seconds between simulated picks")
    ap.add_argument("--poll", type=float, default=None, help="Override poll interval in seconds")
    ap.add_argument("--rankings", default=None, help="Override rankings CSV path")
    ap.add_argument("--verbosity", choices=("quiet", "normal", "debug"), default=None)
    return ap.parse_args(argv)


def build_logger(logs_path: Path, verbosity: str) -> logging.Logger:
    """File logger at logs/draft_<date>.log (no console handler; rich owns the terminal)."""
    logs_path.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("draft")
    logger.setLevel(logging.DEBUG if verbosity == "debug" else logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(logs_path / f"draft_{date.today().isoformat()}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logging.getLogger("draft.players").setLevel(logger.level)
    logging.getLogger("draft.players").handlers.clear()
    logging.getLogger("draft.players").addHandler(handler)
    return logger


class DraftRunner:
    """Polls a pick source, prints picks, and recommends when it matters."""

    def __init__(
        self,
        cfg: Config,
        state: DraftState,
        ranked: list[RankedPlayer],
        display: Display,
        logger: logging.Logger,
        source: PickSource,
        slot_names: dict[int, str],
        rec_cfg: RecommendConfig,
        poll_interval: float,
        simulator: DraftSimulator | None = None,
        auto: bool = False,
        max_rounds: int = 0,
        draft_start_ms: int | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.ranked = ranked
        self.ranked_by_id = {p.sleeper_id: p for p in ranked if p.sleeper_id}
        self.display = display
        self.log = logger
        self.source = source
        self.slot_names = slot_names
        self.rec_cfg = rec_cfg
        self.poll_interval = poll_interval
        self.simulator = simulator
        self.auto = auto
        self.max_rounds = max_rounds
        self.draft_start_ms = draft_start_ms

    # ------------------------------------------------------------- helpers
    def owner_name(self, slot: int | None) -> str:
        """Display name for a slot."""
        if slot is None:
            return "-"
        name = self.slot_names.get(slot, f"slot {slot}")
        return f"{name} (slot {slot})"

    def my_roster_ranked(self) -> list[RankedPlayer]:
        """My picks as RankedPlayer objects (stubs for unranked keepers)."""
        out: list[RankedPlayer] = []
        for p in self.state.my_roster:
            r = self.ranked_by_id.get(p.player_id)
            out.append(r if r else RankedPlayer(rank=999, name=p.name, position=p.position or "?", team=p.team))
        return out

    def build_recommendation(self) -> Recommendation:
        """Recommendation for my upcoming pick."""
        st = self.state
        available = st.available(self.ranked)
        return recommend(
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
        st = self.state
        take = rec.take.player.label if rec.take else "-"
        backup = rec.backup.player.label if rec.backup else "-"
        self.log.info(
            "RECOMMEND round %d pick #%s | TAKE %s | Backup %s | Why: %s",
            st.current_round, st.my_next_pick_no, take, backup, rec.why,
        )
        self.log.info("  top10: " + "; ".join(f"{p.rank}.{p.name} {p.position}" for p in rec.top_overall))
        self.log.info("  best by pos: " + "; ".join(f"{pos}={p.name if p else '-'}" for pos, p in rec.best_by_position.items()))
        for pos, info in rec.tiers.items():
            if info.at_risk:
                self.log.info("  tier cliff: %s tier %s has %d left, expect %d taken in %d picks", pos, info.tier, info.remaining_in_tier, info.expected_taken, info.picks_until_next)
        for sc in rec.scored[:10]:
            self.log.debug("  score %.1f %s %s", sc.score, sc.player.label, "; ".join(sc.reasons))

    def prompt_my_pick(self, rec: Recommendation, live: Live) -> RankedPlayer | None:
        """(dry-run) ask which player to take. Returns ``None`` to quit."""
        available = self.state.available(self.ranked)
        if self.auto:
            return rec.take.player if rec.take else (available[0] if available else None)
        live.stop()
        try:
            while True:
                try:
                    raw = input("Your pick  [Enter = TAKE, b = backup, ? = list, q = quit, or type a name]: ").strip()
                except EOFError:
                    return rec.take.player if rec.take else None
                if raw == "":
                    if rec.take:
                        return rec.take.player
                    continue
                low = raw.lower()
                if low == "q":
                    return None
                if low == "b" and rec.backup:
                    return rec.backup.player
                if low == "?":
                    for p in available[:30]:
                        self.display.print(f"  {p.rank:>3} {p.name:<26} {p.position:<3} {p.team or 'FA'}")
                    continue
                key = normalize_name(raw)
                hits = [p for p in available if key and key in p.norm_name]
                if len(hits) == 1:
                    return hits[0]
                if not hits:
                    self.display.warn(f"No available player matches {raw!r}")
                    continue
                self.display.print("Multiple matches:")
                for i, p in enumerate(hits[:10], start=1):
                    self.display.print(f"  {i}. {p.name} ({p.position}, {p.team or 'FA'}) rk {p.rank}")
                choice = input("  number (or Enter to cancel): ").strip()
                if choice.isdigit() and 1 <= int(choice) <= min(10, len(hits)):
                    return hits[int(choice) - 1]
        finally:
            live.start()

    def status_extra(self, status: str) -> str:
        if status == "pre_draft":
            return f"waiting for draft to start ({local_time(self.draft_start_ms)})"
        if status == "paused":
            return "draft is paused"
        return ""

    # ------------------------------------------------------------- main loop
    def run(self) -> int:
        """Poll until the draft completes. Returns an exit code."""
        st = self.state
        console = self.display.console
        last_status: str | None = None
        last_rec_key: tuple[int | None, bool] | None = None
        polls = 0
        failures = 0
        panel = self.display.status_panel(st, "starting", "-")
        with Live(panel, console=console, refresh_per_second=4, transient=True) as live:
            while True:
                polls += 1
                try:
                    draft = self.source.fetch_draft()
                    raw_picks = self.source.fetch_picks()
                except SleeperAPIError as exc:
                    failures += 1
                    self.log.warning("poll %d failed: %s", polls, exc)
                    self.display.warn(f"Sleeper unreachable ({exc}); retrying in {self.poll_interval:.0f}s")
                    live.update(self.display.status_panel(st, last_status or "unknown", "-", "API error, retrying"))
                    time.sleep(self.poll_interval)
                    continue
                failures = 0
                status = str(draft.get("status") or "unknown")
                new, removed = st.update_picks(raw_picks)
                for p in removed:
                    self.display.print_removed(p, st)
                    self.log.warning("pick removed: #%d %s", p.pick_no, p.name)
                for p in new:
                    self.display.print_pick(p, st, self.slot_names.get(p.slot, f"slot {p.slot}"), self.ranked_by_id.get(p.player_id))
                    self.log.info(
                        "PICK %s #%d slot %d %s %s %s by %s%s", pick_label(p.pick_no, st.settings), p.pick_no, p.slot,
                        p.name, p.position, p.team, self.slot_names.get(p.slot, p.picked_by), " (keeper)" if p.is_keeper else "",
                    )
                if status != last_status:
                    self.announce_status(status, last_status)
                    last_status = status
                on_clock = self.owner_name(st.on_the_clock_slot)
                self.log.info(
                    "poll %d status=%s picks=%d current=#%s on_clock=%s until_me=%s new=%d",
                    polls, status, len(st.picks), st.current_pick_no, on_clock, st.picks_until_my_turn, len(new),
                )

                if status == "complete" or st.is_complete:
                    self.finish("Draft complete!")
                    return 0
                if self.max_rounds and st.current_round > self.max_rounds:
                    self.finish(f"Stopping after round {self.max_rounds} (--max-rounds).")
                    return 0

                if status in ("drafting", "paused") and (st.is_my_turn or st.is_on_deck):
                    key = (st.current_pick_no, st.is_my_turn)
                    if key != last_rec_key:
                        last_rec_key = key
                        self.display.banner("on_clock" if st.is_my_turn else "on_deck", st)
                        rec = self.build_recommendation()
                        self.display.print(self.display.recommendation_panel(rec, st, st.my_needs()))
                        self.log_recommendation(rec)
                        if self.simulator is not None and st.is_my_turn:
                            choice = self.prompt_my_pick(rec, live)
                            if choice is None:
                                self.finish("Rehearsal stopped.")
                                return 130
                            try:
                                self.simulator.submit_pick(choice)
                            except ValueError as exc:
                                self.display.warn(str(exc))
                            continue
                live.update(self.display.status_panel(st, status, on_clock, self.status_extra(status)))
                time.sleep(self.poll_interval)

    def announce_status(self, status: str, previous: str | None) -> None:
        """Human message on status transitions."""
        self.log.info("status %s -> %s", previous, status)
        if status == "pre_draft":
            self.display.print(f"[yellow]Draft has not started yet[/yellow] (scheduled {local_time(self.draft_start_ms)}). Polling every {self.poll_interval:.0f}s...")
        elif status == "drafting":
            self.display.print("[bold green]Draft is LIVE.[/bold green]")
        elif status == "paused":
            self.display.print("[yellow]Draft is paused by the commissioner.[/yellow]")
        elif status == "complete":
            self.display.print("[bold blue]Draft status: complete.[/bold blue]")
        else:
            self.display.print(f"[dim]Draft status: {status}[/dim]")

    def finish(self, message: str) -> None:
        """Print my roster and a closing line."""
        self.display.print(f"\n[bold]{message}[/bold]")
        self.display.print(self.display.roster_table(self.state.my_roster, self.state, self.ranked_by_id, "Your roster"))
        needs = self.state.my_needs()
        self.display.print(self.display.needs_line(needs, self.state.rules))
        self.log.info("%s roster: %s", message, "; ".join(f"{p.name} {p.position}" for p in self.state.my_roster))


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    console = Console()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        console.print(f"[bold red]✖ {exc}[/bold red]")
        return 1
    verbosity = args.verbosity or cfg.verbosity
    display = Display(console, verbosity)
    logger = build_logger(cfg.logs_path, verbosity)
    poll_interval = args.poll or cfg.poll_interval
    logger.info("=== draft.py start dry_run=%s auto=%s config=%s ===", args.dry_run, args.auto, cfg.config_path)

    try:
        session = build_session(cfg, logger, dry_run=args.dry_run, slot_override=args.slot, seed=args.seed, rankings_override=args.rankings)
    except SessionError as exc:
        display.error(str(exc))
        return 2 if "slot is unknown" in str(exc) else 1
    if session.unmatched:
        display.warn(f"{session.unmatched} rankings rows are unmatched and will be ignored (run setup.py to see them).")
    slot_names = dict(session.slot_names)
    slot_names[session.my_slot] = f"{slot_names[session.my_slot]} ★"
    settings, rules = session.settings, session.rules
    display.print(
        f"[bold]{session.league.get('name')}[/bold] — {settings.teams} teams, {settings.rounds} rounds, {settings.draft_type} · "
        f"you are slot [green]{session.my_slot}[/green] ({slot_names[session.my_slot]}) · {len(session.ranked)} ranked players · "
        f"{'SUPERFLEX' if rules.is_superflex else '1-QB'} · K/DEF from round {cfg.k_def_round_threshold}"
    )
    if args.dry_run:
        poll_interval = args.sim_delay
        display.print(f"[magenta]DRY RUN:[/magenta] opponents are simulated (seed={args.seed}). Keepers/existing picks are pre-loaded. {'Auto-accepting recommendations.' if args.auto else 'You will be prompted for each of your picks.'}")

    runner = DraftRunner(
        cfg, session.state, session.ranked, display, logger, session.source, slot_names, session.rec_cfg, poll_interval,
        simulator=session.simulator, auto=args.auto, max_rounds=args.max_rounds, draft_start_ms=session.draft.get("start_time"),
    )
    try:
        return runner.run()
    except KeyboardInterrupt:
        display.print("\n[yellow]Stopped (Ctrl-C).[/yellow] Log: " + str(cfg.logs_path))
        logger.info("stopped by user")
        return 130


if __name__ == "__main__":
    sys.exit(main())
