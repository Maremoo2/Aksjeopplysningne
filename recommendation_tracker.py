from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = REPO_ROOT / "data" / "recommendation_log.csv"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "performance"
ACTIONABLE_LABELS = ("BUY SETUP", "WATCH", "WAIT PULLBACK")
RECOMMENDATION_FIELDS = [
    "run_id",
    "date",
    "market",
    "ticker",
    "recommendation_time",
    "recommendation_context",
    "classification",
    "action_label",
    "next_action",
    "confidence_score",
    "catalyst_quality",
    "liquidity_guardrails",
    "setup",
    "entry",
    "breakout",
    "stop",
    "target_1",
    "target_2",
    "recommended_price",
    "close_price_same_day",
    "result_same_day_pct",
    "close_price_1w",
    "result_1w_pct",
    "status",
    "outcome_1w",
    "notes",
]


def _to_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_number(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    return f"{number:.2f}"


def _pct_change(entry_price: Any, exit_price: Any) -> float | None:
    entry = _to_float(entry_price)
    exit_value = _to_float(exit_price)
    if entry in (None, 0) or exit_value is None:
        return None
    return (exit_value / entry - 1.0) * 100.0


def ensure_recommendation_log(path: Path = DEFAULT_LOG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECOMMENDATION_FIELDS)
        writer.writeheader()


def load_recommendation_log(path: Path = DEFAULT_LOG_PATH) -> list[dict[str, str]]:
    ensure_recommendation_log(path)
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save_recommendation_log(rows: list[dict[str, str]], path: Path = DEFAULT_LOG_PATH) -> None:
    ensure_recommendation_log(path)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECOMMENDATION_FIELDS)
        writer.writeheader()
        for row in rows:
            clean_row = {field: str(row.get(field, "")) for field in RECOMMENDATION_FIELDS}
            writer.writerow(clean_row)


def _market_label(market: str) -> str:
    return market.strip().upper() if market else "UNKNOWN"


def _title_label(value: str) -> str:
    return value.strip().replace("-", " ").title() if value else "Unknown"


def select_recommendations(df: pd.DataFrame, limit: int = 5) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    ranked = df.copy()
    if "priority_score" in ranked.columns:
        ranked = ranked.sort_values(by=["priority_score", "score"], ascending=[False, False], na_position="last")
    elif "score" in ranked.columns:
        ranked = ranked.sort_values(by="score", ascending=False, na_position="last")
    if "action_label" in ranked.columns:
        mask = ranked["action_label"].astype(str).isin(ACTIONABLE_LABELS)
        ranked = ranked[mask]
    return ranked.head(limit).reset_index(drop=True)


def _build_recommendation_context(row: dict[str, Any], market: str, run_type: str) -> str:
    context = {
        "market": _market_label(market),
        "run_type": run_type,
        "sector": str(row.get("sector", "")).strip(),
        "industry": str(row.get("industry", "")).strip(),
        "thematic_tags": str(row.get("thematic_tags", "")).strip(),
        "sources": str(row.get("sources", "")).strip(),
        "priority_score": _to_float(row.get("priority_score")) or 0.0,
        "confidence_score": _to_float(row.get("confidence_score")) or 0.0,
        "next_action": str(row.get("next_action", "")).strip(),
        "catalyst_quality": str(row.get("catalyst_quality", "")).strip(),
        "liquidity_guardrails": str(row.get("liquidity_guardrails", "")).strip(),
        "why_this_stock": str(row.get("why_this_stock", "")).strip(),
    }
    return json.dumps(context, ensure_ascii=False, sort_keys=True)


def snapshot_recommendations(
    df: pd.DataFrame,
    market: str,
    run_id: str,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    log_path: Path = DEFAULT_LOG_PATH,
    recommendation_time: datetime | None = None,
    run_type: str = "open",
) -> tuple[list[dict[str, str]], Path, Path]:
    recommendation_time = recommendation_time or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = select_recommendations(df)
    date_key = recommendation_time.strftime("%Y%m%d")

    recommendations: list[dict[str, str]] = []
    for row in selected.fillna("").to_dict(orient="records"):
        recommendations.append(
            {
                "run_id": run_id,
                "date": recommendation_time.strftime("%Y-%m-%d"),
                "market": _market_label(market),
                "ticker": str(row.get("ticker", "")).strip(),
                "recommendation_time": recommendation_time.isoformat(),
                "recommendation_context": _build_recommendation_context(row, market, run_type),
                "classification": str(row.get("setup_bucket") or row.get("classification", "")).strip(),
                "action_label": str(row.get("action_label", "")).strip(),
                "next_action": str(row.get("next_action", "")).strip(),
                "confidence_score": _format_number(row.get("confidence_score")),
                "catalyst_quality": str(row.get("catalyst_quality", "")).strip(),
                "liquidity_guardrails": str(row.get("liquidity_guardrails", "")).strip(),
                "setup": str(row.get("setup", "")).strip(),
                "entry": _format_number(row.get("preferred_entry_high") or row.get("preferred_entry_low")),
                "breakout": _format_number(row.get("breakout_level")),
                "stop": _format_number(row.get("stop_level")),
                "target_1": _format_number(row.get("target_1")),
                "target_2": _format_number(row.get("target_2")),
                "recommended_price": _format_number(row.get("last")),
                "close_price_same_day": "",
                "result_same_day_pct": "",
                "close_price_1w": "",
                "result_1w_pct": "",
                "status": "PENDING_SAME_DAY",
                "outcome_1w": "UNKNOWN",
                "notes": "",
            }
        )

    if recommendations:
        existing_rows = load_recommendation_log(log_path)
        existing_keys = {
            (row.get("run_id", ""), row.get("market", ""), row.get("ticker", ""), row.get("recommendation_time", ""))
            for row in existing_rows
        }
        for row in recommendations:
            key = (row["run_id"], row["market"], row["ticker"], row["recommendation_time"])
            if key not in existing_keys:
                existing_rows.append(row)
        save_recommendation_log(existing_rows, log_path)

    md_path = output_dir / f"recommendations_{date_key}_{market.lower()}_open.md"
    json_path = output_dir / f"recommendations_{date_key}_{market.lower()}_open.json"
    md_path.write_text(render_recommendation_snapshot_markdown(recommendations, market, recommendation_time), encoding="utf-8")
    json_path.write_text(json.dumps(recommendations, ensure_ascii=False, indent=2), encoding="utf-8")
    return recommendations, md_path, json_path


def render_recommendation_snapshot_markdown(
    recommendations: list[dict[str, str]],
    market: str,
    recommendation_time: datetime,
) -> str:
    lines = [
        f"# Recommendation Snapshot ({recommendation_time.strftime('%Y-%m-%d %H:%M UTC')})",
        "",
        f"- Market: {_market_label(market)}",
        f"- Logged at: {recommendation_time.isoformat()}",
        "",
        "## Recommended at open",
    ]
    if not recommendations:
        lines.append("- No actionable recommendations.")
    else:
        for row in recommendations:
            lines.append(
                f"- {row['ticker']} — {row['action_label']} | {row['classification']} | "
                f"next {row.get('next_action') or 'WATCH ONLY'} | "
                f"confidence {row.get('confidence_score') or 'n/a'}/10 | "
                f"setup {row['setup']} | price {row['recommended_price'] or 'n/a'}"
            )
    lines.append("")
    return "\n".join(lines)


def _fetch_trading_history(ticker: str, start_date: date, end_date: date) -> pd.DataFrame:
    history = yf.download(
        tickers=ticker,
        start=start_date.isoformat(),
        end=(end_date + timedelta(days=1)).isoformat(),
        progress=False,
        auto_adjust=False,
        group_by="column",
    )
    if history.empty:
        return history
    return history.reset_index()


def _history_close_for_day(history: pd.DataFrame, target_date: date) -> float | None:
    if history.empty:
        return None
    for _, row in history.iterrows():
        trade_date = pd.Timestamp(row["Date"]).date()
        if trade_date == target_date:
            return _to_float(row.get("Close"))
    return None


def _history_close_after_sessions(history: pd.DataFrame, start_date: date, sessions_after: int) -> float | None:
    if history.empty:
        return None
    closes: list[float] = []
    for _, row in history.iterrows():
        trade_date = pd.Timestamp(row["Date"]).date()
        if trade_date >= start_date:
            close_value = _to_float(row.get("Close"))
            if close_value is not None:
                closes.append(close_value)
    if len(closes) <= sessions_after:
        return None
    return closes[sessions_after]


def _same_day_status(row: dict[str, str], close_price: float | None) -> str:
    if close_price is None:
        return row.get("status", "PENDING_SAME_DAY")
    target = _to_float(row.get("target_1"))
    stop = _to_float(row.get("stop"))
    if target is not None and close_price >= target:
        return "TARGET_HIT"
    if stop is not None and close_price <= stop:
        return "STOP_HIT"
    result = _pct_change(row.get("recommended_price"), close_price)
    if result is None:
        return "PENDING_SAME_DAY"
    if result > 0:
        return "POSITIVE_CLOSE"
    if result < 0:
        return "NEGATIVE_CLOSE"
    return "FLAT_CLOSE"


def _one_week_outcome(result_pct: float | None) -> str:
    if result_pct is None:
        return "UNKNOWN"
    if result_pct > 0:
        return "WIN"
    if result_pct < 0:
        return "LOSS"
    return "FLAT"


def update_recommendation_results(
    log_path: Path = DEFAULT_LOG_PATH,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    mode: str = "same-day",
    as_of: date | None = None,
) -> tuple[list[dict[str, str]], Path, Path]:
    rows = load_recommendation_log(log_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    as_of = as_of or datetime.now(UTC).date()
    updated_rows: list[dict[str, str]] = []
    cache: dict[str, pd.DataFrame] = {}

    for row in rows:
        ticker = str(row.get("ticker", "")).strip()
        recommendation_date_text = str(row.get("date", "")).strip()
        if not ticker or not recommendation_date_text:
            continue
        try:
            recommendation_date = datetime.strptime(recommendation_date_text, "%Y-%m-%d").date()
        except ValueError:
            continue

        if mode == "same-day" and recommendation_date > as_of:
            continue
        if mode == "1w" and (as_of - recommendation_date).days < 7:
            continue

        if ticker not in cache:
            cache[ticker] = _fetch_trading_history(ticker, recommendation_date - timedelta(days=2), as_of + timedelta(days=10))
        history = cache[ticker]

        if mode == "same-day" and not row.get("close_price_same_day"):
            close_price = _history_close_for_day(history, recommendation_date)
            result_pct = _pct_change(row.get("recommended_price"), close_price)
            if close_price is not None:
                row["close_price_same_day"] = _format_number(close_price)
            if result_pct is not None:
                row["result_same_day_pct"] = f"{result_pct:.2f}"
            row["status"] = _same_day_status(row, close_price)
            row["notes"] = str(row.get("notes", "")).strip()
            updated_rows.append(dict(row))

        if mode == "1w" and not row.get("close_price_1w"):
            close_price_1w = _history_close_after_sessions(history, recommendation_date, sessions_after=5)
            result_pct_1w = _pct_change(row.get("recommended_price"), close_price_1w)
            if close_price_1w is not None:
                row["close_price_1w"] = _format_number(close_price_1w)
            if result_pct_1w is not None:
                row["result_1w_pct"] = f"{result_pct_1w:.2f}"
            row["outcome_1w"] = _one_week_outcome(result_pct_1w)
            updated_rows.append(dict(row))

    save_recommendation_log(rows, log_path)
    stamp = as_of.strftime("%Y%m%d")
    suffix = "recommendation_results"
    md_path = output_dir / f"{suffix}_{stamp}.md"
    json_path = output_dir / f"{suffix}_{stamp}.json"
    payload: dict[str, Any] = {"date": as_of.isoformat()}
    if json_path.exists():
        try:
            existing_payload = json.loads(json_path.read_text(encoding="utf-8"))
            if isinstance(existing_payload, dict):
                payload.update(existing_payload)
        except json.JSONDecodeError:
            pass
    payload["date"] = as_of.isoformat()
    payload[mode] = updated_rows
    sections = {
        key: value for key, value in payload.items() if key in {"same-day", "1w"} and isinstance(value, list)
    }
    md_path.write_text(render_combined_recommendation_results_markdown(sections, as_of), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return updated_rows, md_path, json_path


def render_recommendation_results_markdown(rows: list[dict[str, str]], mode: str, as_of: date) -> str:
    title = "Same-day result check" if mode == "same-day" else "One-week result check"
    lines = [f"# {title} ({as_of.isoformat()})", ""]
    if not rows:
        lines.append("- No recommendations were updated.")
        lines.append("")
        return "\n".join(lines)

    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("market", "UNKNOWN")), []).append(row)

    for market, market_rows in sorted(grouped.items()):
        lines.append(f"## {market}")
        for row in market_rows:
            if mode == "same-day":
                result_text = row.get("result_same_day_pct") or "n/a"
                lines.append(
                    f"- {row['ticker']}: {result_text}% | close {row.get('close_price_same_day') or 'n/a'} | {row.get('status') or 'UNKNOWN'}"
                )
            else:
                result_text = row.get("result_1w_pct") or "n/a"
                lines.append(
                    f"- {row['ticker']}: {result_text}% | close {row.get('close_price_1w') or 'n/a'} | {row.get('outcome_1w') or 'UNKNOWN'}"
                )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_combined_recommendation_results_markdown(sections: dict[str, list[dict[str, str]]], as_of: date) -> str:
    lines = [f"# Recommendation Results ({as_of.isoformat()})", ""]
    if not sections:
        lines.append("- No recommendations were updated.")
        lines.append("")
        return "\n".join(lines)
    for mode in ("same-day", "1w"):
        rows = sections.get(mode)
        if rows is None:
            continue
        lines.append(render_recommendation_results_markdown(rows, mode, as_of).strip())
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def summarize_recommendation_metrics(rows: list[dict[str, str]]) -> dict[str, Any]:
    same_day_returns = [_to_float(row.get("result_same_day_pct")) for row in rows]
    week_returns = [_to_float(row.get("result_1w_pct")) for row in rows]
    same_day_values = [value for value in same_day_returns if value is not None]
    week_values = [value for value in week_returns if value is not None]
    false_positive_counter: Counter[str] = Counter()

    for row in rows:
        same_day = _to_float(row.get("result_same_day_pct"))
        week = _to_float(row.get("result_1w_pct"))
        if (same_day is not None and same_day < 0) or (week is not None and week < 0):
            key = " / ".join(part for part in (row.get("action_label", ""), row.get("setup", "")) if part)
            false_positive_counter[key or "Unknown"] += 1

    def _avg(values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values)

    same_day_win_rate = (sum(1 for value in same_day_values if value > 0) / len(same_day_values) * 100) if same_day_values else None
    week_win_rate = (sum(1 for value in week_values if value > 0) / len(week_values) * 100) if week_values else None
    return {
        "recommendations_logged": len(rows),
        "same_day_win_rate": same_day_win_rate,
        "one_week_win_rate": week_win_rate,
        "average_same_day_return": _avg(same_day_values),
        "average_one_week_return": _avg(week_values),
        "most_common_false_positives": [
            {"pattern": label, "count": count} for label, count in false_positive_counter.most_common(3)
        ],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Track recommendation performance")
    parser.add_argument("--input", default=str(DEFAULT_LOG_PATH), help="Recommendation log CSV path")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    parser.add_argument("--mode", choices=["same-day", "1w"], default="same-day", help="Result check mode")
    parser.add_argument("--date", help="Override as-of date (YYYY-MM-DD)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    as_of = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else None
    updated_rows, md_path, json_path = update_recommendation_results(
        log_path=Path(args.input),
        output_dir=Path(args.outdir),
        mode=args.mode,
        as_of=as_of,
    )
    print(render_recommendation_results_markdown(updated_rows, args.mode, as_of or datetime.now(UTC).date()))
    print(f"Saved recommendation Markdown report: {md_path}")
    print(f"Saved recommendation JSON report: {json_path}")


if __name__ == "__main__":
    main()
