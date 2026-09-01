"""Generate rankings.sample.csv in FantasyPros format from Sleeper's search_rank.

This is ONLY a stand-in so the tool can be tested before you export real
rankings. Sleeper's search_rank is a popularity order, not expert consensus,
and bye weeks / ADP are left blank. Replace it with your FantasyPros export.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")


def main() -> int:
    cache = Path("cache/players.json")
    if not cache.exists():
        print("cache/players.json missing; run setup.py first", file=sys.stderr)
        return 1
    players = json.loads(cache.read_text())["players"]
    rows = []
    for pid, p in players.items():
        pos = p.get("position")
        if pos not in POSITIONS or not p.get("team"):
            continue
        if pos == "DEF":
            name, rank = f"{p['first_name']} {p['last_name']}", 165 + min(int(p.get("search_rank") or 9999), 5000) // 100
        else:
            if not p.get("active") or not p.get("search_rank") or p["search_rank"] >= 9999999:
                continue
            name, rank = p["full_name"], int(p["search_rank"])
            if pos == "K":
                rank += 150
        rows.append((rank, name, p["team"], pos))
    rows.sort()
    rows = rows[:300]
    pos_counter: dict[str, int] = {}
    out = Path("rankings.sample.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(["RK", "TIERS", "PLAYER NAME", "TEAM", "POS", "BYE WEEK", "SOS SEASON", "ECR VS. ADP"])
        for i, (_, name, team, pos) in enumerate(rows, start=1):
            pos_counter[pos] = pos_counter.get(pos, 0) + 1
            tier = 1 + (i - 1) // 12 if i <= 120 else 10 + (i - 121) // 30
            fp_pos = ("DST" if pos == "DEF" else pos) + str(pos_counter[pos])
            w.writerow([i, tier, name, team, fp_pos, "", "", ""])
    print(f"wrote {out} with {len(rows)} rows (SAMPLE ONLY — replace with your FantasyPros export)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
