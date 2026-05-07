from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

DO_NOT_CHASE_DAY_CHANGE_THRESHOLD = 20
DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD = -8
SPREAD_PENALTY_THRESHOLD_BPS = 30
BASIS_POINTS_MULTIPLIER = 10000


@dataclass
class WatchlistItem:
    ticker: str
    category: str = ""
    news: bool = False
    sector_strength: bool = False


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def load_watchlist(path: Path) -> list[WatchlistItem]:
    df = pd.read_csv(path)
    if "ticker" not in df.columns:
        raise ValueError(f"{path} må ha kolonnen 'ticker'")

    items: list[WatchlistItem] = []
    for _, row in df.fillna("").iterrows():
        ticker = str(row["ticker"]).strip().upper()
        if not ticker:
            continue
        items.append(
            WatchlistItem(
                ticker=ticker,
                category=str(row.get("category", "")).strip(),
                news=as_bool(row.get("news", False)),
                sector_strength=as_bool(row.get("sector_strength", False)),
            )
        )
    return items


def classify(score: int) -> str:
    if score >= 70:
        return "A-list"
    if score >= 45:
        return "B-list"
    return "C-list"


def as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_non_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def first_regular_session_open(intraday: pd.DataFrame) -> float | None:
    if intraday.empty or "Open" not in intraday.columns:
        return None
    try:
        regular = intraday.between_time("09:30", "16:00")
    except (TypeError, ValueError):
        regular = intraday
    if regular.empty:
        return None
    return as_float(regular["Open"].iloc[0])


def score_stock(item: WatchlistItem) -> dict[str, Any]:
    stock = yf.Ticker(item.ticker)
    intraday = stock.history(period="1d", interval="1m", prepost=True)
    if intraday.empty:
        intraday = stock.history(period="1d", interval="5m", prepost=True)

    if intraday.empty:
        return {"ticker": item.ticker, "category": item.category, "error": "No intraday data"}

    day_volume = int(intraday["Volume"].sum())
    last = float(intraday["Close"].iloc[-1])
    open_price = float(intraday["Open"].iloc[0])
    day_high = float(intraday["High"].max())
    day_low = float(intraday["Low"].min())

    typical = (intraday["High"] + intraday["Low"] + intraday["Close"]) / 3
    volume_sum = intraday["Volume"].sum()
    vwap = float((typical * intraday["Volume"]).sum() / volume_sum) if volume_sum else last

    info = stock.info or {}
    fast_info = stock.fast_info or {}
    previous_close = as_float(
        first_non_none(
            fast_info.get("previous_close"),
            fast_info.get("previousClose"),
            info.get("previousClose"),
            info.get("regularMarketPreviousClose"),
        )
    )
    regular_open = first_regular_session_open(intraday)
    regular_open_valid = regular_open if regular_open is not None and regular_open > 0 else None
    if previous_close is not None and previous_close > 0:
        day_change_reference = previous_close
        day_change_source = "previous_close"
    elif regular_open_valid is not None:
        day_change_reference = regular_open_valid
        day_change_source = "regular_open_fallback"
    else:
        day_change_reference = None
        day_change_source = "no_reference_fallback_zero"
    day_change_pct = (
        ((last - day_change_reference) / day_change_reference) * 100
        if day_change_reference is not None and day_change_reference > 0
        else 0.0
    )
    dist_from_high_pct = ((last - day_high) / day_high) * 100 if day_high else 0.0
    range_pos = (last - day_low) / (day_high - day_low) if day_high != day_low else 0.5

    monthly = stock.history(period="1mo", interval="1d")
    avg_volume = float(monthly["Volume"].tail(20).mean()) if not monthly.empty else None
    volume_ratio = (day_volume / avg_volume) if avg_volume and avg_volume > 0 else None

    lows = intraday["Low"].tail(8)
    lower_lows = len(lows) >= 3 and bool(lows.is_monotonic_decreasing)

    bid = as_float(first_non_none(fast_info.get("bid"), info.get("bid")))
    ask = as_float(first_non_none(fast_info.get("ask"), info.get("ask")))
    spread_inputs_valid = bool(last > 0 and bid is not None and ask is not None and bid > 0 and ask > 0)
    spread_ratio = ((ask - bid) / last) if spread_inputs_valid else None
    spread_bps = (spread_ratio * BASIS_POINTS_MULTIPLIER) if spread_ratio is not None else None
    spread_pct = (spread_ratio * 100) if spread_ratio is not None else None

    market_cap_raw = info.get("marketCap")
    market_cap = int(market_cap_raw) if market_cap_raw else None
    premarket = info.get("preMarketPrice")
    after_hours = info.get("postMarketPrice")

    score = 0
    reasons: list[str] = []

    if day_change_pct > 0:
        score += 25
        reasons.append("green")
    else:
        reasons.append("red")

    if volume_ratio is not None and volume_ratio > 2:
        score += 20
        reasons.append("volume > 2x")

    if range_pos > 0.6:
        score += 15
        reasons.append("near high")

    if last > vwap:
        score += 15
        reasons.append("above VWAP")

    if item.news:
        score += 10
        reasons.append("news/earnings catalyst")

    if item.sector_strength:
        score += 10
        reasons.append("sector strong")

    if dist_from_high_pct < -10:
        score -= 20
        reasons.append("far from high")

    if lower_lows:
        score -= 20
        reasons.append("lower lows (heuristic)")

    if spread_bps is not None and spread_bps > SPREAD_PENALTY_THRESHOLD_BPS:
        reasons.append("wide spread (info)")

    if market_cap is not None and market_cap < 500_000_000:
        score -= 15
        reasons.append("very low market cap")

    return {
        "ticker": item.ticker,
        "category": item.category,
        "last": round(last, 2),
        "open": round(open_price, 2),
        "high": round(day_high, 2),
        "low": round(day_low, 2),
        "volume": day_volume,
        "avg_volume_20d": int(avg_volume) if avg_volume is not None else None,
        "volume_ratio": round(volume_ratio, 2) if volume_ratio is not None else None,
        "day_change_pct": round(day_change_pct, 2),
        "day_change_source": day_change_source,
        "previous_close": round(previous_close, 2) if previous_close else None,
        "distance_from_high_pct": round(dist_from_high_pct, 2),
        "range_position": round(range_pos, 2),
        "vwap": round(vwap, 2),
        "premarket": round(float(premarket), 2) if premarket else None,
        "after_hours": round(float(after_hours), 2) if after_hours else None,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "spread_bps": round(spread_bps, 2) if spread_bps is not None else None,
        "market_cap": market_cap,
        "score": score,
        "classification": classify(score),
        "reasons": ", ".join(reasons),
    }


def format_markdown_report(df: pd.DataFrame) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Daily Momentum Report ({now})", ""]

    if "classification" not in df.columns:
        df = df.assign(classification="", score=0, reasons="", day_change_pct=0, day_change_source="")
    missing_cols = {
        col: 0 for col in ("day_change_pct", "distance_from_high_pct") if col not in df.columns
    }
    if missing_cols:
        df = df.assign(**missing_cols)
    if "day_change_source" not in df.columns:
        df = df.assign(day_change_source="")

    lines.append(
        "> Data note: Premarket/after-hours data may be incomplete depending on Yahoo availability."
    )
    lines.append("> Heuristic note: lower-lows penalty uses a simple recent-candles pattern.")
    lines.append("")

    labels = {
        "A-list": "tradable strength",
        "B-list": "watch/reversal only",
        "C-list": "avoid",
    }

    for bucket in ["A-list", "B-list", "C-list"]:
        lines.append(f"## {bucket}: {labels[bucket]}")
        section = df[df["classification"] == bucket]
        if section.empty:
            lines.append("- (ingen kandidater)")
            lines.append("")
            continue

        for _, row in section.iterrows():
            lines.append(
                f"- {row['ticker']}: score {int(row['score'])}. "
                f"{row.get('reasons', '')}. "
                f"Endring {row.get('day_change_pct', 0)}% "
                f"(kilde: {row.get('day_change_source', '')})."
            )
    lines.append("")

    day_change_numeric = pd.to_numeric(df["day_change_pct"], errors="coerce")
    distance_from_high_numeric = pd.to_numeric(df["distance_from_high_pct"], errors="coerce")
    do_not_chase = df[
        (day_change_numeric > DO_NOT_CHASE_DAY_CHANGE_THRESHOLD)
        & (distance_from_high_numeric <= DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD)
    ]
    lines.append("## Do-not-chase warning")
    if do_not_chase.empty:
        lines.append("- (none)")
    else:
        for _, row in do_not_chase.iterrows():
            lines.append(
                f"- {row['ticker']} ({row.get('day_change_pct', 0)}%, "
                f"{row.get('distance_from_high_pct', 0)}% from high)"
            )
    lines.append("")

    errors = df[df.get("error").notna()] if "error" in df.columns else pd.DataFrame()
    if not errors.empty:
        lines.append("## Feil")
        for _, row in errors.iterrows():
            lines.append(f"- {row['ticker']}: {row['error']}")

    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market momentum screener")
    parser.add_argument("--input", default="watchlist.csv", help="CSV med ticker,category[,news,sector_strength]")
    parser.add_argument("--outdir", default=".", help="Mappe for output-filer")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    watchlist = load_watchlist(Path(args.input))
    rows: list[dict[str, Any]] = []

    for item in watchlist:
        try:
            rows.append(score_stock(item))
        except Exception as exc:  # pragma: no cover
            rows.append({"ticker": item.ticker, "category": item.category, "error": str(exc)})

    df = pd.DataFrame(rows)
    if "score" in df.columns:
        df = df.sort_values(by="score", ascending=False, na_position="last")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = outdir / f"momentum_report_{stamp}.csv"
    md_path = outdir / f"momentum_report_{stamp}.md"

    df.to_csv(csv_path, index=False)
    report = format_markdown_report(df.fillna(""))
    md_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"Saved CSV report: {csv_path}")
    print(f"Saved Markdown report: {md_path}")


if __name__ == "__main__":
    main()
