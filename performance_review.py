from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from utils.exposure import exposure_categories_for_security

DEFAULT_JOURNAL_PATH = Path(__file__).resolve().parent / "data" / "trade_journal.csv"


def _to_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value: Any) -> bool | None:
    if value in ("", None):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y"}:
        return True
    if normalized in {"0", "false", "no", "n"}:
        return False
    return None


def _parse_date(value: str) -> datetime | None:
    if not str(value).strip():
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
    return None


def load_trade_journal(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _average_label(values: list[float], suffix: str = "%") -> str:
    if not values:
        return "n/a"
    return f"{mean(values):+.2f}{suffix}"


def _best_and_worst(grouped: dict[str, list[float]]) -> tuple[str, str]:
    ranked = [(name, mean(values)) for name, values in grouped.items() if values]
    if not ranked:
        return ("n/a", "n/a")
    ranked.sort(key=lambda item: item[1], reverse=True)
    best_name, best_value = ranked[0]
    worst_name, worst_value = ranked[-1]
    return (f"{best_name} ({best_value:+.2f}%)", f"{worst_name} ({worst_value:+.2f}%)")


def summarize_performance(rows: list[dict[str, str]]) -> str:
    closed = [row for row in rows if _to_float(row.get("result_pct")) is not None]
    results_pct = [_to_float(row.get("result_pct")) for row in closed]
    numeric_results = [value for value in results_pct if value is not None]
    wins = [value for value in numeric_results if value > 0]
    losses = [value for value in numeric_results if value < 0]

    setups: dict[str, list[float]] = defaultdict(list)
    themes: dict[str, list[float]] = defaultdict(list)
    hold_days: list[float] = []
    followed_plan = 0
    followed_plan_total = 0

    for row in closed:
        result_pct = _to_float(row.get("result_pct"))
        if result_pct is None:
            continue

        setup = str(row.get("setup", "")).strip()
        if setup:
            setups[setup].append(result_pct)

        ticker = str(row.get("ticker", "")).strip()
        categories = exposure_categories_for_security(ticker) if ticker else ()
        if categories:
            for category in categories:
                themes[category].append(result_pct)
        else:
            themes["Unknown"].append(result_pct)

        followed = _to_bool(row.get("followed_plan"))
        if followed is not None:
            followed_plan_total += 1
            if followed:
                followed_plan += 1

        entry_date = _parse_date(str(row.get("entry_date", "")) or str(row.get("date", "")))
        exit_date = _parse_date(str(row.get("exit_date", "")))
        hold_days_value = _to_float(row.get("hold_days"))
        if hold_days_value is not None:
            hold_days.append(hold_days_value)
        elif entry_date and exit_date:
            hold_days.append((exit_date - entry_date).total_seconds() / 86400)

    best_setup, worst_setup = _best_and_worst(setups)
    best_theme, worst_theme = _best_and_worst(themes)
    win_rate = (len(wins) / len(numeric_results) * 100) if numeric_results else 0.0
    plan_follow_rate = (followed_plan / followed_plan_total * 100) if followed_plan_total else 0.0
    average_hold = f"{mean(hold_days):.2f} days" if hold_days else "n/a"

    lines = [
        f"Trades reviewed: {len(rows)}",
        f"Closed trades: {len(closed)}",
        f"Win rate: {win_rate:.2f}%",
        f"Average gain: {_average_label(wins)}",
        f"Average loss: {_average_label(losses)}",
        f"Best setup type: {best_setup}",
        f"Worst setup type: {worst_setup}",
        f"Best sector/theme: {best_theme}",
        f"Worst sector/theme: {worst_theme}",
        f"Average hold time: {average_hold}",
        f"Plan followed rate: {plan_follow_rate:.2f}%",
        "",
        "You perform best on:",
        f"- {best_setup}",
        f"- {best_theme}",
        f"- trades where the plan was followed {plan_follow_rate:.2f}% of the time",
        "",
        "You perform worst on:",
        f"- {worst_setup}",
        f"- {worst_theme}",
        f"- trades where the plan was not followed {100 - plan_follow_rate:.2f}% of the time",
    ]
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize trade journal performance")
    parser.add_argument("--input", default=str(DEFAULT_JOURNAL_PATH), help="Trade journal CSV path")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    path = Path(args.input)
    rows = load_trade_journal(path)
    print(summarize_performance(rows))


if __name__ == "__main__":
    main()
