from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

FALLBACK_ATR_PCT = 0.03
FALLBACK_PRICE_FLOOR = 0.01
ENTRY_LOW_ATR_MULTIPLIER = 0.7
ENTRY_HIGH_ATR_MULTIPLIER = 0.3
BREAKOUT_ATR_MULTIPLIER = 0.5
STOP_ATR_MULTIPLIER = 1.2
INVALIDATION_ATR_MULTIPLIER = 1.5
TARGET1_ATR_MULTIPLIER = 1.5
TARGET2_ATR_MULTIPLIER = 2.5
EXTENDED_DAY_CHANGE_THRESHOLD = 20
HARD_EXTENDED_DAY_CHANGE_THRESHOLD = 25
STRONG_CONTINUATION_DISTANCE_THRESHOLD = -1.5
STRONG_CONTINUATION_VOLUME_THRESHOLD = 3.0


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in ("", None):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _is_strong_continuation(
    day_change: float,
    distance_from_high: float,
    volume_ratio: float,
    above_vwap: bool,
) -> bool:
    return (
        above_vwap
        and day_change <= HARD_EXTENDED_DAY_CHANGE_THRESHOLD
        and distance_from_high >= STRONG_CONTINUATION_DISTANCE_THRESHOLD
        and volume_ratio >= STRONG_CONTINUATION_VOLUME_THRESHOLD
    )


def classify_setup(row: dict[str, Any]) -> str:
    day_change = _to_float(row.get("day_change_pct"))
    distance_from_high = _to_float(row.get("distance_from_high_pct"))
    volume_ratio = _to_float(row.get("volume_ratio"))
    atr_pct = _to_float(row.get("atr_pct"))
    above_vwap = _to_float(row.get("last")) > _to_float(row.get("vwap"))
    strong_continuation = _is_strong_continuation(day_change, distance_from_high, volume_ratio, above_vwap)

    if day_change > HARD_EXTENDED_DAY_CHANGE_THRESHOLD:
        return "extended/parabolic"
    if day_change > EXTENDED_DAY_CHANGE_THRESHOLD and not strong_continuation:
        return "extended/parabolic"
    if day_change > 15 and distance_from_high < -7:
        return "extended/parabolic"
    if distance_from_high > -2 and volume_ratio >= 2:
        return "breakout"
    if above_vwap and volume_ratio >= 1.5 and day_change > 0:
        return "continuation"
    if above_vwap and day_change <= 0:
        return "pullback"
    if atr_pct > 7 and day_change < 0:
        return "reversal"
    return "pullback"


def _risk_label(
    chase_risk: str,
    atr_pct: float,
    earnings_warning: str,
    day_change: float,
    float_label: str,
) -> str:
    risk_score = 1
    if chase_risk == "High":
        risk_score += 2
    elif chase_risk == "Medium":
        risk_score += 1
    if atr_pct >= 7:
        risk_score += 2
    elif atr_pct >= 4:
        risk_score += 1
    if earnings_warning == "Elevated":
        risk_score += 2

    if risk_score >= 5:
        risk = "High"
    elif risk_score >= 3:
        risk = "Medium"
    else:
        risk = "Low"
    if risk == "Low" and (float_label == "Low" or day_change > 15 or atr_pct > 5):
        return "Medium"
    return risk


def _position_size_pct(
    risk: str,
    chase_risk: str,
    day_change: float,
    atr_pct: float,
    float_label: str,
) -> float:
    if risk == "Low":
        return 0.05
    if risk == "Medium":
        return 0.03
    if _use_minimum_high_risk_size(chase_risk, day_change, atr_pct, float_label):
        return 0.01
    return 0.02


def _use_minimum_high_risk_size(
    chase_risk: str,
    day_change: float,
    atr_pct: float,
    float_label: str,
) -> bool:
    return (
        chase_risk == "High"
        or day_change > HARD_EXTENDED_DAY_CHANGE_THRESHOLD
        or atr_pct >= 7
        or float_label == "Low"
    )


def generate_trade_plan(row: dict[str, Any]) -> dict[str, Any]:
    setup = classify_setup(row)
    last = _to_float(row.get("last"))
    atr_pct = _to_float(row.get("atr_pct"))
    day_change = _to_float(row.get("day_change_pct"))
    float_label = str(row.get("float_label", ""))
    atr_move = (
        last * (atr_pct / 100.0)
        if last > 0 and atr_pct > 0
        else max(last * FALLBACK_ATR_PCT, FALLBACK_PRICE_FLOOR)
    )
    near_high = _to_float(row.get("distance_from_high_pct")) > -2

    preferred_entry_low = round(max(last - atr_move * ENTRY_LOW_ATR_MULTIPLIER, 0), 2)
    preferred_entry_high = round(max(last - atr_move * ENTRY_HIGH_ATR_MULTIPLIER, 0), 2)
    breakout_level = round(last + atr_move * BREAKOUT_ATR_MULTIPLIER, 2)
    stop_level = round(max(last - atr_move * STOP_ATR_MULTIPLIER, 0), 2)
    target_1 = round(last + atr_move * TARGET1_ATR_MULTIPLIER, 2)
    target_2 = round(last + atr_move * TARGET2_ATR_MULTIPLIER, 2)
    invalidation = round(max(last - atr_move * INVALIDATION_ATR_MULTIPLIER, 0), 2)

    chase_risk = "Low"
    if setup == "extended/parabolic":
        chase_risk = "High"
    elif near_high and _to_float(row.get("day_change_pct")) > 6:
        chase_risk = "Medium"

    risk = _risk_label(chase_risk, atr_pct, str(row.get("earnings_warning", "")), day_change, float_label)
    position_size_pct = _position_size_pct(risk, chase_risk, day_change, atr_pct, float_label)
    hold_window = "1–2 days" if setup in {"breakout", "extended/parabolic"} else "2–5 days"

    return {
        "setup": setup,
        "preferred_entry_low": preferred_entry_low,
        "preferred_entry_high": preferred_entry_high,
        "breakout_level": breakout_level,
        "stop_level": stop_level,
        "invalidation_level": invalidation,
        "target_1": target_1,
        "target_2": target_2,
        "risk": risk,
        "chase_risk": chase_risk,
        "position_size_pct": position_size_pct,
        "suggested_hold": hold_window,
    }


def enrich_with_strategy(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    strategy_columns = {
        "setup",
        "preferred_entry_low",
        "preferred_entry_high",
        "breakout_level",
        "stop_level",
        "invalidation_level",
        "target_1",
        "target_2",
        "risk",
        "chase_risk",
        "position_size_pct",
        "suggested_hold",
    }
    existing = [column for column in df.columns if column in strategy_columns]
    if existing:
        df = df.drop(columns=existing)
    plans = [generate_trade_plan(row) for row in df.to_dict(orient="records")]
    return pd.concat([df.reset_index(drop=True), pd.DataFrame(plans)], axis=1)


def _render_markdown(df: pd.DataFrame) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Strategy Report ({now})", ""]
    if df.empty:
        lines.append("- No candidates")
        return "\n".join(lines) + "\n"

    for _, row in df.iterrows():
        lines.extend(
            [
                f"## {row.get('ticker', '')}",
                f"Setup: {row.get('setup', '')}",
                "",
                f"Preferred entry: {row.get('preferred_entry_low', '')}–{row.get('preferred_entry_high', '')}",
                f"Breakout: >{row.get('breakout_level', '')} with volume",
                f"Stop: <{row.get('stop_level', '')}",
                f"Target 1: {row.get('target_1', '')}",
                f"Target 2: {row.get('target_2', '')}",
                f"Risk: {row.get('risk', '')}",
                f"Chase risk: {row.get('chase_risk', '')}",
                f"Suggested hold: {row.get('suggested_hold', '')}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate trade strategies from momentum CSV")
    parser.add_argument("--input", required=True, help="Input momentum CSV file")
    parser.add_argument("--outdir", default=".", help="Output directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    in_path = Path(args.input)
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(in_path)
    out = enrich_with_strategy(df)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = outdir / f"strategy_report_{stamp}.csv"
    md_path = outdir / f"strategy_report_{stamp}.md"
    json_path = outdir / f"strategy_report_{stamp}.json"

    out.to_csv(csv_path, index=False)
    md_path.write_text(_render_markdown(out), encoding="utf-8")
    json_path.write_text(
        json.dumps(out.fillna("").to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"Saved strategy CSV report: {csv_path}")
    print(f"Saved strategy Markdown report: {md_path}")
    print(f"Saved strategy JSON report: {json_path}")


if __name__ == "__main__":
    main()
