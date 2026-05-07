from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf


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
    if score > 70:
        return "A-list"
    if score >= 45:
        return "B-list"
    return "C-list"


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

    day_change_pct = ((last - open_price) / open_price) * 100 if open_price else 0.0
    dist_from_high_pct = ((last - day_high) / day_high) * 100 if day_high else 0.0
    range_pos = (last - day_low) / (day_high - day_low) if day_high != day_low else 0.5

    monthly = stock.history(period="1mo", interval="1d")
    avg_volume = float(monthly["Volume"].tail(20).mean()) if not monthly.empty else 0.0
    volume_ratio = (day_volume / avg_volume) if avg_volume else 0.0

    lows = intraday["Low"].tail(8)
    lower_lows = len(lows) >= 3 and bool(lows.is_monotonic_decreasing)

    info = stock.info or {}
    bid = float(info.get("bid") or 0)
    ask = float(info.get("ask") or 0)
    spread_pct = ((ask - bid) / last) * 100 if ask > 0 and bid > 0 and last > 0 else 0.0

    market_cap = int(info.get("marketCap") or 0)
    premarket = info.get("preMarketPrice")
    after_hours = info.get("postMarketPrice")

    score = 0
    reasons: list[str] = []

    if day_change_pct > 0:
        score += 25
        reasons.append("green")
    else:
        reasons.append("red")

    if volume_ratio > 2:
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
        reasons.append("lower lows")

    if spread_pct > 0.5:
        score -= 15
        reasons.append("wide spread")

    if market_cap and market_cap < 500_000_000:
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
        "avg_volume_20d": int(avg_volume),
        "volume_ratio": round(volume_ratio, 2),
        "day_change_pct": round(day_change_pct, 2),
        "distance_from_high_pct": round(dist_from_high_pct, 2),
        "range_position": round(range_pos, 2),
        "vwap": round(vwap, 2),
        "premarket": round(float(premarket), 2) if premarket else None,
        "after_hours": round(float(after_hours), 2) if after_hours else None,
        "spread_pct": round(spread_pct, 2),
        "market_cap": market_cap,
        "score": score,
        "classification": classify(score),
        "reasons": ", ".join(reasons),
    }


def format_markdown_report(df: pd.DataFrame) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Daily Momentum Report ({now})", ""]
    if "classification" not in df.columns:
        df = df.assign(classification="", score=0, reasons="", day_change_pct=0)

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
                f"Endring {row.get('day_change_pct', 0)}%."
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
