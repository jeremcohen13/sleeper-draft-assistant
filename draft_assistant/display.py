"""Terminal UI built on ``rich``: pick feed, status bar, recommendation panel."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from .draft_state import DraftState, Pick, RosterNeeds, RosterRules, pick_label
from .players import FANTASY_POSITIONS, MatchResult, RankedPlayer
from .recommend import Recommendation

POS_STYLE: dict[str, str] = {
    "QB": "bold magenta",
    "RB": "bold green",
    "WR": "bold cyan",
    "TE": "bold yellow",
    "K": "white",
    "DEF": "bold red",
}


def pos_text(pos: str | None) -> Text:
    """Position rendered in its colour."""
    pos = pos or "?"
    return Text(pos, style=POS_STYLE.get(pos, "white"))


def fmt_adp(value: float | None) -> str:
    """``+4`` / ``-2`` / ``-``."""
    if value is None:
        return "-"
    return f"{value:+.0f}"


def fmt_opt(value: Any) -> str:
    """Blank-safe string."""
    return "-" if value is None or value == "" else str(value)


class Display:
    """All console output for draft.py and setup.py."""

    def __init__(self, console: Console | None = None, verbosity: str = "normal") -> None:
        self.console = console or Console()
        self.verbosity = verbosity

    # ------------------------------------------------------------- helpers
    def print(self, *objects: Any, **kwargs: Any) -> None:
        """Proxy to ``Console.print``."""
        self.console.print(*objects, **kwargs)

    def info(self, message: str) -> None:
        """Dim informational line (suppressed in quiet mode)."""
        if self.verbosity != "quiet":
            self.console.print(f"[dim]{message}[/dim]")

    def warn(self, message: str) -> None:
        """Yellow warning line."""
        self.console.print(f"[yellow]⚠ {message}[/yellow]")

    def error(self, message: str) -> None:
        """Red error line."""
        self.console.print(f"[bold red]✖ {message}[/bold red]")

    def debug(self, message: str) -> None:
        """Only shown when verbosity is ``debug``."""
        if self.verbosity == "debug":
            self.console.print(f"[dim cyan]{message}[/dim cyan]")

    # ------------------------------------------------------------- pick feed
    def print_pick(self, pick: Pick, state: DraftState, owner: str, ranked: RankedPlayer | None) -> None:
        """One line per pick as it lands."""
        label = pick_label(pick.pick_no, state.settings)
        mine = state.is_mine(pick)
        line = Text()
        line.append(f"{label} ", style="bold white" if not mine else "bold green")
        line.append(f"#{pick.pick_no:<3} ", style="dim")
        line.append(f"{pick.name:<24} ", style="bold" if mine else "")
        line.append_text(pos_text(pick.position))
        line.append(f" {fmt_opt(pick.team):<4}", style="dim")
        if ranked is not None:
            line.append(f" rk {ranked.rank:<3}", style="dim")
            if ranked.tier is not None:
                line.append(f" t{ranked.tier}", style="dim")
        else:
            line.append(" unranked", style="dim red")
        line.append(f"  ← {owner}", style="green" if mine else "dim")
        if pick.is_keeper:
            line.append("  (keeper)", style="dim magenta")
        self.console.print(line)

    def print_removed(self, pick: Pick, state: DraftState) -> None:
        """A pick disappeared from the feed (commissioner undo)."""
        self.warn(f"Pick {pick_label(pick.pick_no, state.settings)} ({pick.name}) was removed by the commissioner")

    # ------------------------------------------------------------- status
    def status_panel(
        self, state: DraftState, draft_status: str, on_clock_name: str, extra: str = ""
    ) -> Panel:
        """Persistent bottom status bar."""
        s = state.settings
        cur = state.current_pick_no
        parts = Text()
        status_style = {"drafting": "bold green", "pre_draft": "yellow", "paused": "yellow", "complete": "bold blue"}
        parts.append(f" {draft_status.upper()} ", style=status_style.get(draft_status, "white"))
        if cur is None:
            parts.append("  Draft complete", style="bold blue")
        else:
            parts.append(f"  Pick #{cur} ({pick_label(cur, s)})  ")
            parts.append("On the clock: ", style="dim")
            parts.append(on_clock_name, style="bold")
            until = state.picks_until_my_turn
            if until is None:
                parts.append("   You have no picks left", style="dim")
            elif until == 0:
                parts.append("   ★ YOU ARE ON THE CLOCK ★", style="bold black on green")
            elif until == 1:
                parts.append("   ON DECK — 1 pick until yours", style="bold yellow")
            else:
                nxt = state.my_next_pick_no
                parts.append(f"   {until} picks until your turn (#{nxt}, {pick_label(nxt, s)})", style="cyan")
        if extra:
            parts.append(f"   {extra}", style="dim")
        return Panel(parts, box=box.ROUNDED, border_style="blue", padding=(0, 1))

    def banner(self, kind: str, state: DraftState) -> None:
        """Big attention banner for on-the-clock / on-deck."""
        cur = state.my_next_pick_no
        label = pick_label(cur, state.settings) if cur else "?"
        if kind == "on_clock":
            self.console.print(
                Panel(
                    Text(f"YOU ARE ON THE CLOCK  —  pick #{cur} ({label})", justify="center", style="bold black"),
                    style="on green",
                    box=box.HEAVY,
                )
            )
        else:
            self.console.print(
                Panel(
                    Text(f"ON DECK  —  your pick #{cur} ({label}) is next", justify="center", style="bold black"),
                    style="on yellow",
                    box=box.HEAVY,
                )
            )

    # ------------------------------------------------------------- recommendation
    def needs_line(self, needs: RosterNeeds, rules: RosterRules) -> Text:
        """``QB 0/1  RB 1/2 ...`` starters summary."""
        t = Text("Roster: ")
        for pos in FANTASY_POSITIONS:
            n = rules.starters.get(pos, 0)
            if n == 0:
                continue
            have = min(needs.counts.get(pos, 0), n)
            style = "green" if have >= n else "yellow"
            t.append(f"{pos} {have}/{n}  ", style=style)
        for name, n in rules.flex.items():
            filled = n - needs.open_flex.get(name, 0)
            t.append(f"{name} {filled}/{n}  ", style="green" if filled >= n else "yellow")
        bench_used = sum(needs.surplus.values())
        t.append(f"BN {bench_used}/{rules.bench}", style="dim")
        return t

    def recommendation_panel(self, rec: Recommendation, state: DraftState, needs: RosterNeeds) -> Group:
        """Compose the on-the-clock / on-deck panel."""
        s = state.settings
        cur = state.my_next_pick_no
        horizon = state.picks_between_my_next_two
        header = Text()
        if cur is not None:
            header.append(f"Round {state.current_round} · your pick #{cur} ({pick_label(cur, s)}) · ", style="bold")
        header.append(f"{horizon} picks between this and your next turn", style="dim")

        top = Table(title="Top 10 available (overall rank)", box=box.SIMPLE_HEAD, title_style="bold", pad_edge=False)
        for col, justify in (("RK", "right"), ("TIER", "right"), ("PLAYER", "left"), ("POS", "left"), ("TEAM", "left"), ("BYE", "right"), ("VS ADP", "right"), ("FIT", "right")):
            top.add_column(col, justify=justify)
        fit = {sc.player.sleeper_id: sc for sc in rec.scored}
        for p in rec.top_overall:
            sc = fit.get(p.sleeper_id)
            fit_txt = "-" if sc is None else ("✖" if sc.blocked else f"{sc.score:.0f}")
            style = "bold green" if rec.take and p is rec.take.player else ("green" if rec.backup and p is rec.backup.player else "")
            top.add_row(
                str(p.rank), fmt_opt(p.tier), Text(p.name, style=style), pos_text(p.position),
                fmt_opt(p.team), fmt_opt(p.bye), fmt_adp(p.ecr_vs_adp), fit_txt,
            )

        best = Table(title="Best available by position + tier cliff", box=box.SIMPLE_HEAD, title_style="bold", pad_edge=False)
        for col, justify in (("POS", "left"), ("PLAYER", "left"), ("RK", "right"), ("TIER", "right"), ("BYE", "right"), ("LEFT IN TIER", "right"), ("EXP. TAKEN", "right"), ("WARNING", "left")):
            best.add_column(col, justify=justify)
        for pos, player in rec.best_by_position.items():
            info = rec.tiers.get(pos)
            if player is None:
                best.add_row(pos_text(pos), Text("none left", style="dim"), "-", "-", "-", "-", "-", "")
                continue
            warn = ""
            style = ""
            if info and info.at_risk:
                warn = f"tier {info.tier} likely gone before your next pick"
                style = "bold red"
            elif info and info.tier is not None and info.remaining_in_tier <= info.expected_taken + 1:
                warn = f"tier {info.tier} thinning"
                style = "yellow"
            best.add_row(
                pos_text(pos), player.name, str(player.rank), fmt_opt(player.tier), fmt_opt(player.bye),
                str(info.remaining_in_tier) if info else "-", str(info.expected_taken) if info else "-",
                Text(warn, style=style),
            )

        final = Text()
        if rec.take:
            final.append("TAKE: ", style="bold green")
            final.append(rec.take.player.name, style="bold green")
            final.append(f" ({rec.take.player.position}, {fmt_opt(rec.take.player.team)})", style="green")
        else:
            final.append("TAKE: nobody ranked is left", style="red")
        final.append("  |  ", style="dim")
        final.append("Backup: ", style="bold")
        final.append(rec.backup.player.name if rec.backup else "-", style="bold")
        if rec.backup:
            final.append(f" ({rec.backup.player.position})", style="dim")
        final.append("  |  ", style="dim")
        final.append("Why: ", style="bold")
        final.append(rec.why)

        return Group(
            Rule(style="green"),
            header,
            self.needs_line(needs, state.rules),
            top,
            best,
            Panel(final, box=box.HEAVY, border_style="green"),
        )

    # ------------------------------------------------------------- rosters
    def roster_table(self, picks: list[Pick], state: DraftState, ranked_by_id: dict[str, RankedPlayer], title: str) -> Table:
        """My drafted roster."""
        table = Table(title=title, box=box.SIMPLE_HEAD, title_style="bold")
        for col in ("PICK", "PLAYER", "POS", "TEAM", "RK", "TIER", "BYE"):
            table.add_column(col)
        for p in picks:
            r = ranked_by_id.get(p.player_id)
            table.add_row(
                pick_label(p.pick_no, state.settings) + (" K" if p.is_keeper else ""), p.name, pos_text(p.position),
                fmt_opt(p.team), fmt_opt(r.rank if r else None), fmt_opt(r.tier if r else None), fmt_opt(r.bye if r else None),
            )
        return table

    # ------------------------------------------------------------- setup
    def print_match_report(self, result: MatchResult, total_rows: int) -> None:
        """Matched/unmatched summary for setup.py."""
        n_ok = len(result.matched)
        self.console.print(
            f"\n[bold]Rankings matching:[/bold] {n_ok}/{total_rows} rows linked to Sleeper players "
            f"({len(result.unmatched)} unmatched, {len(result.notes)} notes)"
        )
        if result.notes:
            self.console.print("[bold]Notes (matched, but check these):[/bold]")
            for note in result.notes:
                self.console.print(f"  [yellow]•[/yellow] {note}")
        if result.unmatched:
            table = Table(title="UNMATCHED rows — fix in overrides.json or rankings.csv", box=box.SIMPLE_HEAD, title_style="bold red")
            table.add_column("RK", justify="right")
            table.add_column("RANKINGS NAME")
            table.add_column("POS")
            table.add_column("TEAM")
            table.add_column("REASON")
            table.add_column("CLOSEST SLEEPER MATCH")
            for u in result.unmatched:
                table.add_row(
                    str(u.player.rank), u.player.name, pos_text(u.player.position), fmt_opt(u.player.team),
                    u.reason, "\n".join(u.suggestions) or "(no close match)",
                )
            self.console.print(table)
        else:
            self.console.print("[green]All rankings rows matched.[/green]")


def local_time(ms: int | None) -> str:
    """Epoch milliseconds -> local ``YYYY-MM-DD HH:MM``."""
    if not ms:
        return "unknown"
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
