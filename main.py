#!/usr/bin/env python3
"""
Retail inventory tracker CLI.

  python main.py           interactive menu
  python main.py watch     start the ./invoices watcher (foreground)
  python main.py query -d 15
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date

from db import get_expiring_products, init_db
from watcher import INVOICES_DIR, start_watcher, stop_watcher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def print_expiring(days: int) -> None:
    init_db()
    rows = get_expiring_products(days=days)
    if not rows:
        print(f"No products expiring within {days} day(s).")
        return

    today = date.today()
    print(f"\nProducts expiring within {days} day(s) (as of {today.isoformat()}):\n")
    print(f"{'Product':<40} {'Expiry':<12} {'Updated':<20} Status")
    print("-" * 88)
    for row in rows:
        expiry = date.fromisoformat(row["expiry_date"])
        remaining = (expiry - today).days
        if remaining < 0:
            status = f"EXPIRED ({abs(remaining)}d ago)"
        elif remaining == 0:
            status = "EXPIRES TODAY"
        else:
            status = f"{remaining}d left"
        print(
            f"{row['product_name'][:40]:<40} "
            f"{row['expiry_date']:<12} "
            f"{str(row['last_updated'])[:19]:<20} "
            f"{status}"
        )
    print()


def _prompt_days() -> int:
    raw = input("Days until expiry to include [15]: ").strip() or "15"
    try:
        days = int(raw)
    except ValueError as exc:
        raise ValueError("Please enter a whole number of days.") from exc
    if days < 0:
        raise ValueError("Days must be 0 or greater.")
    return days


def interactive_menu() -> None:
    observer = None
    INVOICES_DIR.mkdir(parents=True, exist_ok=True)
    init_db()

    print("Retail Inventory Tracker")
    print(f"Drop invoice PDFs into: {INVOICES_DIR}")
    print("Set GEMINI_API_KEY in a .env file before watching invoices.\n")

    while True:
        print("  [1] Start folder watcher (background)")
        print("  [2] Query products expiring in X days")
        print("  [3] Stop watcher")
        print("  [4] Quit")
        choice = input("\nChoice: ").strip()

        if choice == "1":
            if observer is not None and observer.is_alive():
                print("Watcher is already running.")
                continue
            try:
                observer = start_watcher(blocking=False)
                print(f"Watching {INVOICES_DIR} — drop PDFs there to ingest them.")
            except Exception as exc:
                logger.exception("Could not start watcher")
                print(f"Failed to start watcher: {exc}")

        elif choice == "2":
            try:
                days = _prompt_days()
            except ValueError as exc:
                print(exc)
                continue
            print_expiring(days)

        elif choice == "3":
            if observer is None or not observer.is_alive():
                print("Watcher is not running.")
                continue
            stop_watcher(observer)
            observer = None
            print("Watcher stopped.")

        elif choice in {"4", "q", "quit", "exit"}:
            if observer is not None and observer.is_alive():
                stop_watcher(observer)
            print("Goodbye.")
            return

        else:
            print("Unknown option. Choose 1–4.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI invoice inventory tracker")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("watch", help="Watch ./invoices until Ctrl+C")

    query = sub.add_parser("query", help="List products expiring soon")
    query.add_argument("-d", "--days", type=int, default=15, help="Lookahead window (default: 15)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        try:
            interactive_menu()
        except KeyboardInterrupt:
            print("\nInterrupted.")
        return 0

    if args.command == "watch":
        print(f"Watching {INVOICES_DIR} (Ctrl+C to stop)")
        try:
            start_watcher(blocking=True)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    if args.command == "query":
        try:
            print_expiring(args.days)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
