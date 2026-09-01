#!/usr/bin/env python3
"""One-time setup: resolve Sleeper IDs, summarize the league, cache players,
validate rankings matching, and write config.toml.

Usage:
    python setup.py --username <sleeper_username> --league <league_id> [--rankings rankings.csv]

Exit codes: 0 ok, 1 fatal error (bad IDs, missing rankings), 2 user not in draft order.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.table import Table

from draft_assistant.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, write_config
from draft_assistant.display import Display, local_time, pos_text
from draft_assistant.draft_state import DraftSettings, RosterRules, pick_label, picks_for_slot
from draft_assistant.players import PlayerDB, RankingsError, load_overrides, match_rankings, read_rankings_csv
from draft_assistant.sleeper import SleeperAPIError, SleeperClient, pick_active_draft


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI arguments; anything omitted falls back to an existing config.toml."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username", help="Your Sleeper username")
    ap.add_argument("--league", help="Sleeper league id (from the league URL)")
    ap.add_argument("--rankings", help="Path to FantasyPros rankings CSV (default rankings.csv)")
    ap.add_argument("--season", type=int, help="Season (default 2026)")
    ap.add_argument("--poll", type=float, help="Poll interval in seconds (default 4)")
    ap.add_argument("--k-def-round", type=int, help="Do not recommend K/DEF before this round (default 12)")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="config.toml path")
    ap.add_argument("--refresh-players", action="store_true", help="Force re-download of the player dump")
    return ap.parse_args(argv)


def build_config(args: argparse.Namespace) -> Config:
    """Merge CLI args over any existing config."""
    path = Path(args.config)
    try:
        cfg = load_config(path) if path.exists() else Config(config_path=path.resolve())
    except ConfigError as exc:
        raise SystemExit(f"Existing config is unreadable: {exc}") from exc
    if args.username:
        cfg.username = args.username.strip()
    if args.league:
        cfg.league_id = args.league.strip()
    if args.rankings:
        cfg.rankings_path = Path(args.rankings)
    if args.season:
        cfg.season = args.season
    if args.poll:
        cfg.poll_interval = args.poll
    if args.k_def_round:
        cfg.k_def_round_threshold = args.k_def_round
    if not cfg.username or not cfg.league_id:
        raise SystemExit("Both --username and --league are required the first time (or put them in config.toml).")
    return cfg


def scoring_summary(scoring: dict[str, float]) -> list[tuple[str, str]]:
    """Human-readable scoring facts."""
    rec = float(scoring.get("rec", 0) or 0)
    ppr = "Full PPR" if rec >= 1 else ("Half PPR" if rec >= 0.5 else ("Standard (no PPR)" if rec == 0 else f"{rec:g} per reception"))
    te_bonus = float(scoring.get("bonus_rec_te", 0) or 0)
    rows = [
        ("Reception value", f"{rec:g}  ({ppr})"),
        ("TE premium", f"+{te_bonus:g} per TE reception" if te_bonus else "none"),
        ("Passing TD", f"{float(scoring.get('pass_td', 4) or 0):g}"),
        ("Passing yards", f"{float(scoring.get('pass_yd', 0) or 0):.3g} / yd"),
        ("Rush / Rec TD", f"{float(scoring.get('rush_td', 6) or 0):g} / {float(scoring.get('rec_td', 6) or 0):g}"),
        ("Interception", f"{float(scoring.get('pass_int', 0) or 0):g}"),
        ("Fumble lost", f"{float(scoring.get('fum_lost', 0) or 0):g}"),
    ]
    for key, label in (("bonus_rec_rb", "RB reception bonus"), ("bonus_rec_wr", "WR reception bonus"), ("pass_2pt", "2-pt pass")):
        if scoring.get(key):
            rows.append((label, f"{float(scoring[key]):g}"))
    return rows


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    console = Console()
    display = Display(console)
    cfg = build_config(args)
    client = SleeperClient()

    # --- user & league -----------------------------------------------------
    try:
        user = client.get_user(cfg.username)
        if not user:
            display.error(f"Sleeper user {cfg.username!r} not found")
            return 1
        cfg.user_id = str(user["user_id"])
        league = client.get_league(cfg.league_id)
        if not league:
            display.error(f"League {cfg.league_id} not found")
            return 1
        users = client.get_league_users(cfg.league_id)
        drafts = client.get_league_drafts(cfg.league_id)
    except SleeperAPIError as exc:
        display.error(f"Sleeper API error: {exc}")
        return 1

    draft = pick_active_draft(drafts)
    if not draft:
        display.error("This league has no drafts yet")
        return 1
    try:
        draft = client.get_draft(str(draft["draft_id"])) or draft
        existing_picks = client.get_draft_picks(str(draft["draft_id"]))
    except SleeperAPIError as exc:
        display.error(f"Sleeper API error: {exc}")
        return 1
    cfg.draft_id = str(draft["draft_id"])
    settings = DraftSettings.from_draft(draft)
    rules = RosterRules.from_positions(league.get("roster_positions") or [])
    names = {str(u["user_id"]): u.get("display_name") or u["user_id"] for u in users}

    # --- summary -------------------------------------------------------------
    console.print(f"\n[bold]League:[/bold] {league.get('name')}  [dim](id {cfg.league_id}, season {league.get('season')}, status {league.get('status')})[/dim]")
    console.print(f"[bold]You:[/bold] {user.get('display_name')}  [dim](user_id {cfg.user_id})[/dim]")

    t = Table(title="Scoring", box=box.SIMPLE_HEAD, title_style="bold", show_header=False)
    t.add_column("k", style="dim")
    t.add_column("v")
    for k, v in scoring_summary(league.get("scoring_settings") or {}):
        t.add_row(k, v)
    t.add_row("Superflex", "[green]YES[/green]" if rules.is_superflex else "no")
    console.print(t)

    t = Table(title="Roster slots", box=box.SIMPLE_HEAD, title_style="bold", show_header=False)
    t.add_column("k", style="dim")
    t.add_column("v")
    starters = "  ".join(f"{pos}×{n}" for pos, n in rules.starters.items() if n)
    flex = "  ".join(f"{name}×{n}" for name, n in rules.flex.items())
    t.add_row("Starters", starters)
    t.add_row("Flex", flex or "none")
    t.add_row("Bench", str(rules.bench))
    t.add_row("Raw", " ".join(league.get("roster_positions") or []))
    console.print(t)

    t = Table(title="Draft", box=box.SIMPLE_HEAD, title_style="bold", show_header=False)
    t.add_column("k", style="dim")
    t.add_column("v")
    t.add_row("Draft id", cfg.draft_id)
    t.add_row("Status", str(draft.get("status")))
    t.add_row("Type", f"{settings.draft_type}" + (f" (3rd-round reversal at round {settings.reversal_round})" if settings.reversal_round else ""))
    t.add_row("Teams / rounds", f"{settings.teams} / {settings.rounds}  ({settings.total_picks} picks)")
    t.add_row("Starts", local_time(draft.get("start_time")))
    t.add_row("Pick timer", f"{(draft.get('settings') or {}).get('pick_timer', '?')}s")
    console.print(t)

    order = {str(k): int(v) for k, v in (draft.get("draft_order") or {}).items()}
    if order:
        t = Table(title="Draft order", box=box.SIMPLE_HEAD, title_style="bold")
        t.add_column("SLOT", justify="right")
        t.add_column("MANAGER")
        for uid, slot in sorted(order.items(), key=lambda kv: kv[1]):
            me = uid == cfg.user_id
            t.add_row(str(slot), f"[bold green]{names.get(uid, uid)} (you)[/bold green]" if me else names.get(uid, uid))
        console.print(t)

    keepers = [p for p in existing_picks if p.get("is_keeper")]
    if existing_picks:
        console.print(f"[bold]Existing picks:[/bold] {len(existing_picks)} already recorded ({len(keepers)} keepers)")
        mine = [p for p in existing_picks if str(p.get("picked_by")) == cfg.user_id]
        for p in mine:
            m = p.get("metadata") or {}
            console.print(f"  [green]•[/green] your keeper: {m.get('first_name')} {m.get('last_name')} ({m.get('position')}, {m.get('team')}) at pick #{p['pick_no']} ({pick_label(int(p['pick_no']), settings)})")

    my_slot = order.get(cfg.user_id)
    cfg.draft_slot = my_slot
    if my_slot:
        used = {int(p["pick_no"]) for p in existing_picks}
        my_picks = picks_for_slot(my_slot, settings)
        console.print(f"\n[bold]Your draft slot:[/bold] [green]{my_slot}[/green] of {settings.teams}")
        console.print("[bold]Your picks:[/bold] " + "  ".join(
            f"[dim strike]#{n} ({pick_label(n, settings)})[/dim strike]" if n in used else f"#{n} ({pick_label(n, settings)})" for n in my_picks
        ))
        if used & set(my_picks):
            console.print("[dim](struck-through picks are already used by keepers)[/dim]")

    # --- players & rankings ---------------------------------------------------
    console.print()
    with console.status("Loading Sleeper player database..."):
        try:
            db = PlayerDB.load(client, cfg.cache_path / "players.json", force_refresh=args.refresh_players)
        except SleeperAPIError as exc:
            display.error(f"Could not download player list: {exc}")
            return 1
    console.print(f"[bold]Players:[/bold] {len(db.players)} in cache ({cfg.cache_path / 'players.json'})")

    rankings_file = cfg.rankings_file
    try:
        rankings = read_rankings_csv(rankings_file)
        overrides = load_overrides(cfg.overrides_file)
    except RankingsError as exc:
        display.error(str(exc))
        console.print(
            "Export your cheat sheet from FantasyPros (Rankings → Download CSV) and save it as "
            f"[bold]{rankings_file}[/bold], or pass --rankings <file>. A stand-in built from Sleeper "
            "popularity is available via [bold]python tools_make_sample_rankings.py[/bold] (rankings.sample.csv)."
        )
        write_config(cfg)
        return 1
    result = match_rankings(rankings, db, overrides)
    console.print(f"[bold]Rankings:[/bold] {rankings_file} — {len(rankings)} rows, columns: {', '.join(rankings[0].raw.keys())}")
    by_pos = {}
    for p in rankings:
        by_pos[p.position] = by_pos.get(p.position, 0) + 1
    console.print("[dim]Rows by position: " + "  ".join(f"{k} {v}" for k, v in sorted(by_pos.items())) + "[/dim]")
    if not any(p.bye for p in rankings):
        display.warn("Rankings file has no bye weeks; the bye-week penalty will be inactive.")
    display.print_match_report(result, len(rankings))

    # --- projections under this league's scoring -----------------------------
    from draft_assistant.projections import ProjectionSet

    scoring = {k: float(v) for k, v in (league.get("scoring_settings") or {}).items() if v is not None}
    with console.status("Loading season projections..."):
        proj = ProjectionSet.load(int(league.get("season") or cfg.season), scoring, cfg.cache_path / f"projections_{league.get('season') or cfg.season}.json", session=client.session)
    if proj is None:
        display.warn("Season projections unavailable; recommendations will use rankings only.")
    else:
        proj.compute_vor(rules, settings.teams)
        hit = proj.attach(result.matched)
        notes = proj.scoring_summary(scoring)
        console.print(f"\n[bold]Projections:[/bold] {hit}/{len(result.matched)} ranked players scored under this league's rules")
        console.print("[bold]Your scoring vs generic half-PPR:[/bold] " + ("; ".join(notes) if notes else "identical"))
        movers = sorted((p for p in result.matched if p.proj_tilt is not None and p.rank <= 150), key=lambda p: -abs(p.proj_tilt or 0))[:10]
        if movers:
            t = Table(title="Biggest movers under your scoring (top 150)", box=box.SIMPLE_HEAD, title_style="bold")
            for col in ("PLAYER", "POS", "FP RANK", "PROJ PTS", "MOVE"):
                t.add_column(col)
            for p in sorted(movers, key=lambda p: -(p.proj_tilt or 0)):
                mv = p.proj_tilt or 0
                t.add_row(p.name, pos_text(p.position), str(p.rank), f"{p.proj_pts:.0f}", f"[green]+{mv}[/green]" if mv > 0 else f"[red]{mv}[/red]")
            console.print(t)

        depth = max(200, settings.total_picks + 60)
        universe = [p for p in result.matched if p.rank <= depth and p.vor is not None and p.position not in ("K", "DEF") and rules.has_slot_for(p.position)]
        vr = {p.sleeper_id: i for i, p in enumerate(sorted(universe, key=lambda p: -(p.vor or 0)), start=1)}
        sleepers = sorted(((p.rank - vr[p.sleeper_id], p) for p in universe if p.rank >= 50 and p.rank - vr[p.sleeper_id] >= 20 and vr[p.sleeper_id] <= 150), key=lambda t: -t[0])[:12]
        if sleepers:
            t = Table(title="Sleepers — projections rate them well above their ranking", box=box.SIMPLE_HEAD, title_style="bold")
            for col in ("PLAYER", "POS", "TEAM", "YOUR RANK", "PROJ RANK", "GAP", "PROJ PTS", "BYE"):
                t.add_column(col, justify="right" if col not in ("PLAYER", "POS", "TEAM") else "left")
            for gap, p in sleepers:
                t.add_row(p.name, pos_text(p.position), p.team or "FA", str(p.rank), str(vr[p.sleeper_id]), f"[green]+{gap}[/green]", f"{p.proj_pts:.0f}", str(p.bye or "-"))
            console.print(t)
            console.print("[dim]Kickers and defenses excluded (rankings push them late on purpose), and each one has to project as a startable player.[/dim]")

    # --- write config ---------------------------------------------------------
    path = write_config(cfg)
    console.print(f"\n[bold]Config written:[/bold] {path}")

    if not order:
        display.error("The draft order has not been set yet (Sleeper sets it when the commissioner randomizes/starts the draft). Re-run setup.py once it is set. For a rehearsal now: python draft.py --dry-run --slot <n>")
        return 2
    if not my_slot:
        display.error(f"User {cfg.username} ({cfg.user_id}) is not in this draft's draft_order. Check the username/league id.")
        return 2
    console.print("[bold green]Setup complete.[/bold green] Rehearse with [bold]python draft.py --dry-run[/bold]; on draft night run [bold]python draft.py[/bold].")
    return 0


if __name__ == "__main__":
    sys.exit(main())
