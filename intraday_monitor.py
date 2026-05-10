from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from recommendation_tracker import DEFAULT_LOG_PATH, ACTIONABLE_LABELS, load_recommendation_log, select_recommendations

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "intraday"


def _to_float(value: Any) -> float | None:
    try:
        if value in ("", None):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _status_label(row: dict[str, Any]) -> str:
    action = str(row.get("action_label", "")).strip()
    last = _to_float(row.get("last"))
    vwap = _to_float(row.get("vwap"))
    if action == "BUY SETUP" and last is not None and vwap is not None and last > vwap:
        return "still valid"
    if action == "WAIT PULLBACK":
        return "extended, wait pullback"
    if last is not None and vwap is not None and last <= vwap:
        return "lost VWAP, downgrade to watch"
    if action in {"AVOID", "DO NOT CHASE"}:
        return "lost setup"
    return action.lower() if action else "under review"


def build_intraday_summary(
    df: pd.DataFrame,
    market: str,
    log_path: Path = DEFAULT_LOG_PATH,
    snapshot_date: str | None = None,
) -> dict[str, Any]:
    today = snapshot_date or datetime.now(UTC).strftime("%Y-%m-%d")
    log_rows = [
        row
        for row in load_recommendation_log(log_path)
        if str(row.get("market", "")).upper() == market.upper() and str(row.get("date", "")) == today
    ]
    previous_by_ticker = {row["ticker"]: row for row in log_rows}
    current_focus = select_recommendations(df).fillna("").to_dict(orient="records")
    current_by_ticker = {str(row.get("ticker", "")).strip(): row for row in current_focus}

    previous_focus: list[dict[str, str]] = []
    for ticker, old_row in previous_by_ticker.items():
        current_row = current_by_ticker.get(ticker)
        if current_row:
            status = _status_label(current_row)
        else:
            status = "not in the latest focus list"
        previous_focus.append({"ticker": ticker, "status": status, "action_label": old_row.get("action_label", "")})

    new_candidates = [
        {
            "ticker": ticker,
            "action_label": str(row.get("action_label", "")).strip(),
            "status": _status_label(row),
        }
        for ticker, row in current_by_ticker.items()
        if ticker and ticker not in previous_by_ticker and str(row.get("action_label", "")).strip() in ACTIONABLE_LABELS
    ]

    updated_alerts = [
        {
            "ticker": str(row.get("ticker", "")).strip(),
            "pullback_alert": str(row.get("pullback_alert", "")).strip(),
            "breakout_alert": str(row.get("breakout_alert", "")).strip(),
            "risk_alert": str(row.get("risk_alert", "")).strip(),
            "target_alert": str(row.get("target_alert", "")).strip(),
        }
        for row in current_focus[:5]
    ]

    return {
        "market": market.upper(),
        "date": today,
        "generated_at": datetime.now(UTC).isoformat(),
        "new_movers": [item["ticker"] for item in new_candidates],
        "previous_focus": previous_focus,
        "replacement_candidates": [item["ticker"] for item in new_candidates[:3]],
        "updated_alerts": updated_alerts,
    }


def render_intraday_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Intraday Rescan ({summary.get('generated_at', '')})",
        "",
        f"- Market: {summary.get('market', 'UNKNOWN')}",
        f"- Session date: {summary.get('date', '')}",
        "",
        "## New movers",
    ]
    new_movers = summary.get("new_movers", [])
    if not new_movers:
        lines.append("- No new movers yet.")
    else:
        for ticker in new_movers:
            lines.append(f"- {ticker}")
    lines.extend(["", "## Previous focus list"])
    previous_focus = summary.get("previous_focus", [])
    if not previous_focus:
        lines.append("- No open snapshot found for this market/date.")
    else:
        for item in previous_focus:
            lines.append(f"- {item['ticker']}: {item['status']}")
    lines.extend(["", "## Replacement focus candidates"])
    replacements = summary.get("replacement_candidates", [])
    if not replacements:
        lines.append("- No replacements needed.")
    else:
        for ticker in replacements:
            lines.append(f"- {ticker}")
    lines.extend(["", "## Updated Nordnet alert levels"])
    alerts = summary.get("updated_alerts", [])
    if not alerts:
        lines.append("- No alert updates available.")
    else:
        for item in alerts:
            lines.append(
                f"- {item['ticker']}: pullback {item['pullback_alert'] or 'n/a'} | breakout {item['breakout_alert'] or 'n/a'} | "
                f"risk {item['risk_alert'] or 'n/a'} | target {item['target_alert'] or 'n/a'}"
            )
    lines.append("")
    return "\n".join(lines)


def write_intraday_report(
    summary: dict[str, Any],
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    generated_at: datetime | None = None,
) -> tuple[Path, Path]:
    generated_at = generated_at or datetime.now(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = generated_at.strftime("%Y%m%d_%H%M")
    md_path = output_dir / f"intraday_rescan_{stamp}.md"
    json_path = output_dir / f"intraday_rescan_{stamp}.json"
    md_path.write_text(render_intraday_markdown(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Intraday re-scan placeholder monitor")
    parser.add_argument("--input", required=True, help="Momentum CSV to review")
    parser.add_argument("--market", required=True, help="Market label")
    parser.add_argument("--log-path", default=str(DEFAULT_LOG_PATH), help="Recommendation log CSV")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory")
    parser.add_argument("--date", help="Snapshot date (YYYY-MM-DD)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    df = pd.read_csv(Path(args.input))
    summary = build_intraday_summary(df, args.market, Path(args.log_path), args.date)
    md_path, json_path = write_intraday_report(summary, Path(args.outdir))
    print(render_intraday_markdown(summary))
    print(f"Saved intraday Markdown report: {md_path}")
    print(f"Saved intraday JSON report: {json_path}")


if __name__ == "__main__":
    main()
