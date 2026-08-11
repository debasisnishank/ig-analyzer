#!/usr/bin/env python3
"""CLI for browsing stored analyses.

Usage:
    python query.py --latest            # show the most recent report
    python query.py --list              # list every stored analysis
    python query.py --days 7            # list analyses from the last N days
    python query.py --file NAME.md      # show a specific analysis file
    python query.py --search "reels"    # find reports mentioning a phrase
    python query.py --stats             # follower/engagement history
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ANALYSES_DIR = BASE_DIR / "data" / "analyses"
RAW_DIR = BASE_DIR / "data" / "raw"


def _all_analyses() -> list[Path]:
    """Every analysis markdown file, newest first."""
    if not ANALYSES_DIR.exists():
        return []
    files = sorted(ANALYSES_DIR.glob("*/analysis_*.md"), reverse=True)
    return files


def list_analyses(days: int | None = None) -> None:
    files = _all_analyses()
    if days is not None:
        cutoff = datetime.now() - timedelta(days=days)
        files = [f for f in files if _folder_date(f) and _folder_date(f) >= cutoff]

    if not files:
        print("No analyses found." + (f" (last {days} days)" if days else ""))
        return

    header = "Analyses" + (f" (last {days} days)" if days else "")
    print(f"{header}: {len(files)} found\n")
    for f in files:
        rel = f.relative_to(ANALYSES_DIR)
        size = f.stat().st_size
        print(f"  {rel}  ({size:,} bytes)")


def show_latest() -> None:
    files = _all_analyses()
    if not files:
        print("No analyses found.")
        return
    _print_file(files[0])


def show_file(name: str) -> None:
    # Accept a bare filename or a date/filename path.
    matches = [f for f in _all_analyses() if f.name == name or str(f).endswith(name)]
    if not matches:
        print(f"File not found: {name}")
        print("Use --list to see what exists.")
        return
    _print_file(matches[0])


def search(term: str) -> None:
    """Print every report line containing `term`, with its file and line no."""
    needle = term.lower()
    hits = 0
    for path in _all_analyses():
        rel = path.relative_to(ANALYSES_DIR)
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if needle in line.lower():
                hits += 1
                print(f"{rel}:{lineno}: {line.strip()}")
    if not hits:
        print(f"No matches for {term!r} in {len(_all_analyses())} report(s).")


def show_stats() -> None:
    """Follower and engagement history assembled from the raw snapshots."""
    rows = []
    for path in sorted(RAW_DIR.glob("data_*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        overall = (data.get("derived") or {}).get("overall") or {}
        rows.append(
            (
                (data.get("collected_at") or "")[:16].replace("T", " "),
                (data.get("account") or {}).get("followers_count"),
                len(data.get("media") or []),
                overall.get("avg_engagement_rate"),
            )
        )

    if not rows:
        print("No raw snapshots found. Run python main.py first.")
        return

    print(f"{'Collected (UTC)':<18} {'Followers':>10} {'Posts':>6} {'Avg ER %':>9}  Δ followers")
    print("-" * 62)
    previous = None
    for collected, followers, posts, er in rows:
        delta = ""
        if isinstance(followers, int) and isinstance(previous, int):
            change = followers - previous
            delta = f"{change:+d}"
        print(
            f"{collected:<18} {_cell(followers):>10} {posts:>6} "
            f"{_cell(er):>9}  {delta}"
        )
        if isinstance(followers, int):
            previous = followers


def _cell(value: object) -> str:
    return "—" if value is None else str(value)


def _print_file(path: Path) -> None:
    rel = path.relative_to(ANALYSES_DIR)
    print(f"# {rel}\n{'=' * 60}\n")
    print(path.read_text())


def _folder_date(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.parent.name, "%Y-%m-%d")
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Query stored Instagram analyses.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true", help="show most recent analysis")
    group.add_argument("--list", action="store_true", help="list all analyses")
    group.add_argument("--days", type=int, metavar="N", help="list last N days")
    group.add_argument("--file", type=str, metavar="NAME", help="show a specific file")
    group.add_argument("--search", type=str, metavar="TERM", help="search report text")
    group.add_argument(
        "--stats", action="store_true", help="follower/engagement history"
    )

    args = parser.parse_args()
    if args.latest:
        show_latest()
    elif args.list:
        list_analyses()
    elif args.days is not None:
        list_analyses(days=args.days)
    elif args.file:
        show_file(args.file)
    elif args.search:
        search(args.search)
    elif args.stats:
        show_stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
