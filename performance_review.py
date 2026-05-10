from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from recommendation_tracker import DEFAULT_LOG_PATH, load_recommendation_log, summarize_recommendation_metrics
from utils.exposure import exposure_categories_for_security

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_JOURNAL_PATH = REPO_ROOT / "data" / "trade_journal.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "performance"


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


def _parse_context(context_text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(context_text) if context_text else {}
        return loaded if isinstance(loaded, dict) else {}
    except json.JSONDecodeError:
        return {}


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


def _average_or_none(values: list[float]) -> float | None:
    if not values:
        return None
    return mean(values)


def _format_metric(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}%"


def _format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.2f}%"


def build_recommendation_summary(recommendation_rows: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    metrics = summarize_recommendation_metrics(recommendation_rows)
    same_day_by_market: dict[str, list[float]] = defaultdict(list)
    week_by_market: dict[str, list[float]] = defaultdict(list)
    setups: dict[str, list[float]] = defaultdict(list)
    actions: dict[str, list[float]] = defaultdict(list)
    classifications: dict[str, list[float]] = defaultdict(list)
    themes: dict[str, list[float]] = defaultdict(list)
    false_positives: Counter[str] = Counter()

    for row in recommendation_rows:
        market = str(row.get("market", "")).strip() or "Unknown"
        same_day = _to_float(row.get("result_same_day_pct"))
        week = _to_float(row.get("result_1w_pct"))
        setup = str(row.get("setup", "")).strip()
        action = str(row.get("action_label", "")).strip()
        classification = str(row.get("classification", "")).strip()
        context = _parse_context(str(row.get("recommendation_context", "")))
        theme_bits = [
            str(context.get("sector", "")).strip(),
            str(context.get("industry", "")).strip(),
            *[part.strip() for part in str(context.get("thematic_tags", "")).split(",") if part.strip()],
        ]

        if same_day is not None:
            same_day_by_market[market].append(same_day)
            if setup:
                setups[setup].append(same_day)
            if action:
                actions[action].append(same_day)
            if classification:
                classifications[classification].append(same_day)
            for theme in theme_bits or ["Unknown"]:
                themes[theme].append(same_day)
        if week is not None:
            week_by_market[market].append(week)

        if (same_day is not None and same_day < 0) or (week is not None and week < 0):
            false_positives[f"{action or 'Unknown'} / {setup or 'Unknown'}"] += 1

    best_market, worst_market = _best_and_worst(same_day_by_market)
    best_setup, worst_setup = _best_and_worst(setups)
    best_action, worst_action = _best_and_worst(actions)
    best_theme, worst_theme = _best_and_worst(themes)

    classification_returns = {
        key: _average_or_none(values) for key, values in sorted(classifications.items()) if values
    }
    action_returns = {key: _average_or_none(values) for key, values in sorted(actions.items()) if values}
    summary = {
        "recommendations_logged": metrics["recommendations_logged"],
        "same_day_win_rate": metrics["same_day_win_rate"],
        "one_week_win_rate": metrics["one_week_win_rate"],
        "average_same_day_return": metrics["average_same_day_return"],
        "average_one_week_return": metrics["average_one_week_return"],
        "best_market": best_market,
        "worst_market": worst_market,
        "best_setup_type": best_setup,
        "worst_setup_type": worst_setup,
        "best_action_label": best_action,
        "worst_action_label": worst_action,
        "best_sector_theme": best_theme,
        "worst_sector_theme": worst_theme,
        "most_common_false_positives": [
            {"pattern": label, "count": count}
            for label, count in false_positives.most_common(3)
        ],
        "average_return_by_classification": classification_returns,
        "average_return_by_action_label": action_returns,
    }

    false_positive_text = ", ".join(
        f"{item['pattern']} ({item['count']})" for item in summary["most_common_false_positives"]
    ) or "n/a"
    lines = [
        "## Recommendation performance",
        f"Recommendations logged: {summary['recommendations_logged']}",
        f"Recommendation win rate same-day: {_format_rate(summary['same_day_win_rate'])}",
        f"Recommendation win rate after 1 week: {_format_rate(summary['one_week_win_rate'])}",
        f"Average same-day return: {_format_metric(summary['average_same_day_return'])}",
        f"Average 1-week return: {_format_metric(summary['average_one_week_return'])}",
        f"Best market: {best_market}",
        f"Worst market: {worst_market}",
        f"Best setup type: {best_setup}",
        f"Worst setup type: {worst_setup}",
        f"Best action label: {best_action}",
        f"Worst action label: {worst_action}",
        f"Best sector/theme: {best_theme}",
        f"Worst sector/theme: {worst_theme}",
        f"Most common false positives: {false_positive_text}",
        "",
        "Average return by classification:",
    ]
    if classification_returns:
        for label, value in classification_returns.items():
            lines.append(f"- {label}: {_format_metric(value)}")
    else:
        lines.append("- n/a")
    lines.extend(["", "Average return by action label:"])
    if action_returns:
        for label, value in action_returns.items():
            lines.append(f"- {label}: {_format_metric(value)}")
    else:
        lines.append("- n/a")
    lines.append("")
    return "\n".join(lines), summary


def build_performance_summary(trade_rows: list[dict[str, str]], recommendation_rows: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
    trade_summary_text = summarize_performance(trade_rows).strip()
    recommendation_text, recommendation_summary = build_recommendation_summary(recommendation_rows)
    full_text = "\n\n".join([trade_summary_text, recommendation_text]).strip() + "\n"
    return (
        full_text,
        {
            "trade_summary": trade_summary_text,
            "recommendation_summary": recommendation_summary,
        },
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize trade journal and recommendation performance")
    parser.add_argument("--input", default=str(DEFAULT_JOURNAL_PATH), help="Trade journal CSV path")
    parser.add_argument("--recommendations", default=str(DEFAULT_LOG_PATH), help="Recommendation log CSV path")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory for summary artifacts")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    trade_rows = load_trade_journal(Path(args.input))
    recommendation_rows = load_recommendation_log(Path(args.recommendations))
    summary_text, summary_json = build_performance_summary(trade_rows, recommendation_rows)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    md_path = outdir / "performance_summary.md"
    json_path = outdir / "performance_summary.json"
    md_path.write_text(summary_text, encoding="utf-8")
    json_path.write_text(json.dumps(summary_json, ensure_ascii=False, indent=2), encoding="utf-8")
    print(summary_text)
    print(f"Saved performance Markdown report: {md_path}")
    print(f"Saved performance JSON report: {json_path}")


if __name__ == "__main__":
    main()
