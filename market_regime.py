from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import yfinance as yf


INDEX_TICKERS = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "SOXX": "SOXX",
    "BTC": "BTC-USD",
    "VIX": "^VIX",
    "AI Software": "IGV",
    "Crypto Miners": "WGMI",
    "Cyber": "CIBR",
}


def _pct_change(ticker: str) -> float | None:
    try:
        hist = yf.Ticker(ticker).history(period="3mo", interval="1d")
    except Exception:
        return None
    if hist.empty or len(hist) < 21:
        return None
    close = hist["Close"]
    last = float(close.iloc[-1])
    ma20 = float(close.tail(20).mean())
    if ma20 == 0:
        return None
    return ((last - ma20) / ma20) * 100


def classify_regime(metrics: dict[str, float | None]) -> str:
    vix = metrics.get("VIX")
    spy = metrics.get("SPY")
    qqq = metrics.get("QQQ")
    soxx = metrics.get("SOXX")
    btc = metrics.get("BTC")

    if vix is not None and vix > 30:
        return "Panic"
    if vix is not None and vix > 22 and ((spy or 0) < 0 or (qqq or 0) < 0):
        return "Risk-off"
    positives = sum((value or 0) > 0 for value in (spy, qqq, soxx, btc))
    if positives >= 3 and (vix is None or vix < 20):
        return "Risk-on"
    if positives <= 1:
        return "Mean-reversion environment"
    return "Neutral"


def momentum_odds(regime: str) -> str:
    if regime == "Risk-on":
        return "Favorable"
    if regime == "Neutral":
        return "Mixed"
    if regime in {"Risk-off", "Mean-reversion environment"}:
        return "Challenging"
    return "Poor"


def build_regime_report() -> dict[str, Any]:
    metrics: dict[str, float | None] = {}
    for name, ticker in INDEX_TICKERS.items():
        if name == "VIX":
            try:
                hist = yf.Ticker(ticker).history(period="5d", interval="1d")
            except Exception:
                hist = None
            metrics[name] = float(hist["Close"].iloc[-1]) if hist is not None and not hist.empty else None
        else:
            metrics[name] = _pct_change(ticker)

    regime = classify_regime(metrics)

    sector_strength = {}
    for sector in ("SOXX", "AI Software", "Crypto Miners", "Cyber"):
        value = metrics.get(sector)
        if value is None:
            sector_strength[sector] = "Unknown"
        elif value > 2:
            sector_strength[sector] = "Strong"
        elif value > -2:
            sector_strength[sector] = "Neutral"
        else:
            sector_strength[sector] = "Weak"

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "market_regime": regime,
        "momentum_odds": momentum_odds(regime),
        "metrics_20d_vs_ma": metrics,
        "sector_strength": sector_strength,
    }


def _to_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Market Regime Report ({report['timestamp']})",
        "",
        f"Market regime: **{report['market_regime']}**",
        f"Momentum odds: **{report['momentum_odds']}**",
        "",
        "## Index and volatility snapshot",
    ]
    for name, value in report["metrics_20d_vs_ma"].items():
        if value is None:
            lines.append(f"- {name}: n/a")
        elif name == "VIX":
            lines.append(f"- {name}: {value:.2f}")
        else:
            lines.append(f"- {name}: {value:+.2f}% vs 20d MA")

    lines.append("")
    lines.append("## Sector strength")
    for sector, strength in report["sector_strength"].items():
        lines.append(f"- {sector}: {strength}")

    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate market regime report")
    parser.add_argument("--outdir", default=".", help="Output directory")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    report = build_regime_report()

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    md_path = outdir / f"market_regime_{stamp}.md"
    json_path = outdir / f"market_regime_{stamp}.json"

    md_path.write_text(_to_markdown(report), encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(_to_markdown(report))
    print(f"Saved market regime Markdown report: {md_path}")
    print(f"Saved market regime JSON report: {json_path}")


if __name__ == "__main__":
    main()
