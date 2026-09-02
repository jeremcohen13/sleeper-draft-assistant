# Sleeper Live Draft Assistant

A live draft assistant for Sleeper leagues with a browser dashboard (and a
terminal mode). It follows your draft in real time, shows every pick as it
lands, and tells you who to take next based on your FantasyPros rankings, your
roster, tier cliffs, bye weeks, ADP value, season projections scored under
**your league's exact scoring**, what the rest of the room is doing, and the
players you've pinned as targets. A dry-run mode lets you rehearse the whole
draft against simulated opponents.

## Quick start (draft night)

```bash
caffeinate -i .venv/bin/python web.py
```

That opens http://localhost:8765. Then:

1. Click **🔇 sound off** once so it becomes **🔔 sound on**. It plays the
   alert as a preview: a synthesized brass fanfare. It fires **only when you
   are on the clock**, once per turn — nothing on deck, nothing for other
   people's picks. The click also unlocks browser audio, so don't skip it.
   To change the alert, edit `CLOCK_FANFARE` in
   `draft_assistant/web/index.html`; each entry is
   `[frequency, start offset, length, volume]`.
2. Keep the page visible next to the Sleeper app. The header heartbeat should
   read "live · Sleeper checked Ns ago". Amber means Sleeper is slow, red means
   the dashboard died (just rerun the command).
3. Make your picks **in Sleeper**. The dashboard is read-only for the real
   draft; it only shows advice.
4. Optional: star a few players on the board as targets before the draft.

Before that works you need a rankings file and a one-time setup (sections 1-2),
and ideally one rehearsal (section 3).

## Requirements

Python 3.11+ (tested on 3.13). Dependencies: `requests`, `rich`, `pytest`.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Every command below assumes the venv is active (`source .venv/bin/activate`)
or is prefixed with `.venv/bin/`.

## 1. Rankings file

Export your cheat sheet from FantasyPros (Rankings → your scoring format →
**Download CSV**) and save it as `rankings.csv` in this folder. The expected
columns are the FantasyPros defaults:

```
RK, TIERS, PLAYER NAME, TEAM, POS, BYE WEEK, SOS SEASON, ECR VS. ADP
```

Header matching is loose: `RANK`/`RK`, `TIER`/`TIERS`, `PLAYER`/`PLAYER NAME`,
`BYE`/`BYE WEEK` all work, position may be `WR12`/`DST3`, and if the file has an
`ADP` column instead of `ECR VS. ADP` the value delta is computed as ADP minus
rank. Positive "vs ADP" means the player usually goes *later* than ranked, i.e.
a value.

No export yet? `python tools_make_sample_rankings.py` writes
`rankings.sample.csv` from Sleeper's popularity order so you can test the tool.
It is **not** expert consensus and has no bye weeks. Don't draft off it.

## 2. One-time setup

```bash
python setup.py --username <your_sleeper_username> --league <league_id>
```

The league id is the number in the Sleeper URL
(`https://sleeper.com/leagues/<league_id>/...`). Setup:

- resolves your user id, the league, and the active draft;
- prints scoring (PPR value, TE premium, superflex), roster slots, teams,
  rounds, draft type, your slot and **every pick number you own** (keeper picks
  already used are struck through);
- downloads the Sleeper player list once into `cache/players.json`
  (refreshed only if older than 24h; `--refresh-players` forces it);
- validates `rankings.csv` against Sleeper and prints **every unmatched row
  with the closest fuzzy match** and its Sleeper id;
- writes `config.toml`.

Exit code 2 means the draft order is not set yet or your user is not in it.
Re-run setup once the commissioner has set the order (you can still rehearse
with `--dry-run --slot N`).

Other flags: `--rankings <file>`, `--poll <seconds>`, `--k-def-round <n>`.

### Fixing unmatched names

Most rows match automatically (suffixes like Jr./III, punctuation, accents,
`JAC`→`JAX`, `WSH`→`WAS`, `D/ST`/`DST`/`DEF`, duplicate names disambiguated by
position and team). For anything left over, add an entry to `overrides.json`:

```json
{
  "Marvin Harrison Jr.": "11628",
  "Hollywood Brown": "Marquise Brown",
  "Jacksonville Jaguars": "JAX"
}
```

The key is the exact name from your rankings file; the value is a Sleeper
`player_id` (shown by setup.py as `id=...`), a Sleeper full name, or a team
abbreviation for a defense. Keys starting with `_` are ignored. Re-run
`setup.py` until it reports "All rankings rows matched" (or you are happy with
what is left; unmatched rows are simply ignored during the draft).

Rows flagged as **notes** (team mismatch, ambiguous name) did match; the note
tells you which player was chosen so you can override it if it is wrong.

## 3. Rehearse (dry run)

In the browser (recommended):

```bash
python web.py --dry-run
```

Click **Draft** on the TAKE card, the **Draft** button on any Next-best card, or
the `+` on a board row to make your picks. In the terminal instead:

```bash
python draft.py --dry-run
```

Uses your real league and draft settings (teams, rounds, order, keepers already
entered) with simulated opponents who pick near the board with some randomness.
When you are on the clock, the recommendation panel appears and you are
prompted:

- `Enter` takes the recommended player, `b` the backup;
- type part of a name (`chase`) to pick anyone available; `?` lists the board;
- `q` quits.

Useful flags: `--auto` (accept every recommendation, no prompts),
`--max-rounds 3` (stop early), `--seed 42` (reproducible), `--sim-delay 0.2`
(faster), `--slot 7` (rehearse from another slot if the order is not set yet).

## 4. Draft night

Two front ends run the same logic; pick whichever you prefer (or both).

**Web dashboard (recommended):**

```bash
python web.py
```

Opens http://localhost:8765 in your browser: the board with position
filters, a big TAKE card with the reason, a "Next best" list of the four strongest alternatives for your roster, roster needs, best-by-position
with tier-cliff warnings, your lineup/bench, and the live pick feed. The page
glows green when you are on the clock and amber when you are on deck, and the
tab title changes so you notice from another tab. The server asks Sleeper
every 4 seconds and the page refreshes every 2, so a pick shows within ~5s.
Flags: `--poll 2` (faster polling), `--port` (default 8765; if it is busy the
next free port is used and printed), `--no-browser`, and for rehearsal
`--dry-run`, `--auto`, `--seed`, `--sim-delay`, `--slot`.

**Terminal:**

```bash
python draft.py
```

- Polls Sleeper every 4 seconds (`draft.poll_interval` in `config.toml` or
  `--poll`). Transient API errors are logged and retried; the loop never dies.
- Prints each new pick with its rank/tier from your rankings, and keeps a
  status bar: current pick, who is on the clock, picks until your turn.
- On deck and on the clock it shows the panel: top 10 available, best
  available per position with tier-cliff warnings, roster needs, and a final
  `TAKE: … | Backup: … | Why: …` line.
- Handles snake, linear and third-round-reversal drafts, keepers, commissioner
  undo, and status changes (`pre_draft` → `drafting` → `paused` → `complete`).
- Ctrl-C exits cleanly. Everything (every poll, pick, and recommendation) is
  in `logs/draft_<date>.log`.

### Targets (players you like)

Click ☆ next to any player on the dashboard board to pin him, or list names in
`targets.json`:

```json
{ "targets": ["Tee Higgins", "Jaylen Waddle"] }
```

The **Your targets** panel shows each one as *Take now* (his ADP is before your
next pick after this one), *Coin flip* (within 3 picks of it), *Can wait*, or
*Gone* (with who took him). Pinned players also get a small fit bonus (+6).

### Other signals in the score

- **BOOM / BUST** badges come from the UPSIDE and BUST columns of the
  FantasyPros export, each rated 1-5. Only a 5 is shown, because 4 is the
  normal rating for a good player (97 of the top 150 in a typical export) and
  therefore means nothing. `BOOM` = highest upside, +2. `BUST` = highest bust
  risk, −2. Most players carry neither badge.
- **Injury status** from Sleeper: Out/IR/PUP/Suspended −25, Doubtful −8,
  Questionable −2. Shown as a red badge. Sleeper marks many players
  Questionable in preseason, so treat that one as a heads-up, not a verdict.
- **Rookies** get an `R` badge (no score change).

### Your league's scoring (projections)

Sleeper publishes season stat projections for every player. Setup and the
dashboard multiply them by **your league's exact `scoring_settings`** (6-point
passing TDs, −1 INT, whatever you have) and also by a generic half-PPR
baseline. Ranking both by value-over-replacement at each position gives every
player a **"Yours"** number: how many spots your scoring moves him relative to
the FantasyPros ranks. That feeds the score at 0.5 points per spot (capped at
±20) and shows as a column on the board, on the TAKE card and in Next best.
`setup.py` prints the scoring differences it found and the ten biggest movers.
Projections are cached in `cache/projections_<season>.json` for 24h; if the
download fails, everything still works on rankings alone.

### Sleepers

Two filter buttons sit next to the position chips above the board:
**SLEEPERS** shows only flagged sleepers, **★ TARGETS** only players you have
starred. They combine with each other and with a position, so `WR` +
`SLEEPERS` gives you sleeper wide receivers only. The board carries the top
200 available players so these filters have something to work with.

A **Sleepers** panel under the board lists still-available players whose
projected points (under your scoring) rate them well above where your rankings
have them, and a `SLEEPER` badge marks them inline. `setup.py` prints the same
list before the draft. Three filters keep it honest:

- Kickers and defenses are excluded. Rankings push them late on purpose, so
  their gap is a drafting convention, not an edge.
- Ranked outside the early rounds, so it surfaces values rather than stars.
- The projections must see him as a startable player, which drops deep fliers
  whose gap is large but meaningless.

The gap also feeds the fit score, symmetrically: **+0.25 per rank spot**, so a
player the projections rate 40 spots above his ranking gains 10 points, and one
they rate 40 spots below loses 10. Gaps under 15 spots are ignored as noise and
the effect is clipped at ±50 spots (±12.5 points). That is a tiebreaker, not an
override: a clearly better-ranked player still wins.

The 0.25 weight was picked by simulating six complete drafts at each of
0.0, 0.15, 0.20, 0.25, 0.30 and 0.50 and comparing the resulting rosters:

| Weight | Roster per draft | Projected points |
| --- | --- | --- |
| 0.00 (off) | QB2 RB4.5 WR6.7 TE2 DEF1 | 2799 |
| 0.15 | QB1.8 RB4.5 WR6.7 TE2 DEF1 | 2819 |
| **0.25** | **QB2 RB4 WR7 TE2 DEF1** | **2857** |
| 0.30 | QB2 RB4 WR7 TE2 DEF1 | 2858 |
| 0.50 | QB1.8 RB3.5 WR7.7 TE2 DEF1 | 2842 |

Above 0.30 the roster thins out at running back without gaining points. Note
that "projected points" comes from the same projections being weighted, so it
is a directional check rather than proof; the weight is kept modest for that
reason.

### Draft trends (what the room is doing)

The tier-cliff "expected to go before your next pick" number is not just the
board. Three things add up:

- **Board:** how many players of that position sit in the next N spots by rank.
- **Run:** if a position took clearly more of the last round of picks than the
  board predicted, the excess is added, and the best remaining player at that
  position gets +3 with an "RB run: 6 of the last 12 picks" reason.
- **Needs (round 3+):** each team picking before your next turn whose biggest
  remaining hole is that position adds demand.

The dashboard's **Draft trends** strip shows the last-round position bars, a
run warning, and every team picking before you with their open starter slots.

### How the recommendation works

Score = (200 − overall rank) plus adjustments, in rank points:

| Factor | Effect |
| --- | --- |
| Fills an open dedicated starter (QB/RB/WR/TE/DEF/K) | +15, +30 more when your remaining picks ≤ open starters |
| Fills an open FLEX / SUPER_FLEX | +6 (QB in SUPER_FLEX +12) |
| Roster plan (balance) | Each position has a planned end-of-draft count derived from your lineup (your league: QB 2, RB 5, WR 6, TE 2, DEF 1). Going over the plan costs −8 per extra player (compounding); being behind pace for the round earns +4 per missing player. A soft nudge, not a quota: a clearly better-ranked player still wins. |
| Position has no slot in this league (e.g. K when the lineup has no K) | never recommended |
| K/DEF before `k_def_round_threshold` (default round 12) | blocked unless you must fill starters |
| 2nd QB in a 1-QB league | −40 before round 8, −25 after; 3rd QB blocked |
| Superflex: 3rd QB −25, 4th blocked | |
| 2nd TE | −20 (−10 if a FLEX is open); 3rd TE blocked |
| Bye-week stacking | −1.5 per rostered player on the same bye, −2 more if same position (cap −8) |
| Tier cliff | +5 if the position's current tier will likely be gone before your next pick, +3 if he is the last in his tier |
| Value vs ADP | +0.4 × delta (clipped ±10) |
| Sleeper / fade (projections vs your rankings) | ±0.25 per rank spot, ignored under 15, clipped at ±50 |
| Your league's scoring ("Yours") | +0.5 per rank spot your scoring moves him vs generic half-PPR (clipped ±20) |
| Positional run in progress | +3 for the best remaining player at that position |
| Pinned target | +6 |
| BOOM / BUST badge (FantasyPros 5-out-of-5 upside or bust rating) | +2 / −2 |
| Injury status | Out/IR/PUP/Suspended −25, Doubtful −8, Questionable −2 |

Weights live in `draft_assistant/recommend.py` (`Weights`). The reasons behind
a score are shown on the TAKE card, on each Next-best card, and as a tooltip on
any board row.

## Tests

```bash
python -m pytest -q
```

## Troubleshooting

- **A filter or column shows nothing / red banner across the top** – the page
  reloads from disk on every refresh but `web.py` loads its Python code once at
  startup, so refreshing the browser after an update is not enough. Stop the
  server with Ctrl-C and start it again. The banner appears automatically when
  the page is newer than the server.
- **`Address already in use`** – another `web.py` is still running (check the
  other terminal). Newer versions just move to the next port automatically, so
  make sure you are looking at the port printed by the server you just started.
- **Page looks frozen** – check the heartbeat under the title. If your Mac
  slept, polling stopped; run under `caffeinate -i` as in the quick start.
- **`config.toml not found`** – run `setup.py` first.
- **`user … not in this draft's draft_order`** – wrong username, or the
  commissioner has not set the order yet. Sleeper only fills `draft_order` once
  the order is randomized/set.
- **`Your draft slot is unknown`** at draft time – re-run `setup.py`; it
  re-reads the order.
- **Many unmatched rankings rows** – check the CSV is the FantasyPros export
  (not the "ADP" page) and that the `POS` column exists. Use `overrides.json`.
- **Picks not appearing** – confirm the draft id printed by setup matches the
  one in the Sleeper URL; if the commissioner recreated the draft, re-run
  setup. The status bar shows `API error, retrying` while Sleeper is
  unreachable.
- **Player list looks stale** (rookies missing, wrong teams) –
  `python setup.py --refresh-players`.
- **Bye-week penalty never fires** – your rankings file has no `BYE WEEK`
  column (setup warns about this).
- Sleeper's picks feed does not tell you who is on the clock directly; the tool
  derives it from the lowest pick number not yet made, which is also how it
  copes with keepers slotted into later rounds.

## Layout

```
draft_assistant/
  config.py       config.toml loading/writing
  sleeper.py      API client (timeouts, retry with backoff)
  players.py      player cache, name normalization, rankings matching
  draft_state.py  snake/linear order, whose pick, roster needs, tiers
  recommend.py    scoring and recommendation
  display.py      rich terminal UI
  simulate.py     dry-run opponent simulator
  session.py      shared bootstrapping + JSON snapshot (used by both front ends)
  web/index.html  the dashboard page
setup.py          one-time setup and validation
draft.py          terminal live loop / dry run
web.py            web dashboard (same logic, stdlib HTTP server)
tests/            pytest suite
```
