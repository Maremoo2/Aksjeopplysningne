from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf
from market_regime import build_regime_report
from strategy_engine import enrich_with_strategy
from utils.exposure import build_exposure_summary, exposure_categories_for_security
from utils.sector_map import resolve_sector_info

# Risk guardrail tuned for momentum names: flags fast moves that are already
# meaningfully off highs (likely poor R/R for fresh entries).
DO_NOT_CHASE_DAY_CHANGE_THRESHOLD = 15
DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD = -7
A1_EXTENDED_DAY_CHANGE_THRESHOLD = 20
A1_HARD_EXTENDED_DAY_CHANGE_THRESHOLD = 25
A1_STRONG_CONTINUATION_DISTANCE_THRESHOLD = -1.5
A1_STRONG_CONTINUATION_VOLUME_THRESHOLD = 3.0
SPREAD_PENALTY_THRESHOLD_BPS = 30
BASIS_POINTS_MULTIPLIER = 10000

# Yahoo Finance predefined screener IDs
YAHOO_SCREENER_IDS: dict[str, str] = {
    "yahoo-gainers": "day_gainers",
    "yahoo-most-active": "most_actives",
    "yahoo-trending": "day_trending_tickers",
    "yahoo-unusual-volume": "sec_unusual_volume",
    "yahoo-high-beta": "high_beta_stocks",
    "yahoo-losers": "day_losers",
    "yahoo-oversold": "oversold_stocks",
    "yahoo-overbought": "overbought_stocks",
    "yahoo-52-week-gainers": "52wk_gainers",
    "yahoo-all-time-high": "alltime_new_highs",
}
YAHOO_SCREENER_LABEL: dict[str, str] = {
    "yahoo-gainers": "Top Gainers",
    "yahoo-most-active": "Most Active",
    "yahoo-trending": "Trending Now",
    "yahoo-unusual-volume": "Unusual Volume",
    "yahoo-high-beta": "High Beta",
    "yahoo-losers": "Top Losers",
    "yahoo-oversold": "Oversold",
    "yahoo-overbought": "Overbought",
    "yahoo-52-week-gainers": "52-Week Gainers",
    "yahoo-all-time-high": "All-Time High",
}

# Sources included in --source yahoo-momentum (and yahoo-all for backwards compat).
# Any source that returns HTTP 404 is logged as a warning and skipped gracefully.
YAHOO_MOMENTUM_SOURCES: tuple[str, ...] = (
    "yahoo-gainers",
    "yahoo-most-active",
    "yahoo-trending",
    "yahoo-unusual-volume",
    "yahoo-high-beta",
)

# Sources included in --source yahoo-expanded (superset of yahoo-momentum).
YAHOO_EXPANDED_SOURCES: tuple[str, ...] = YAHOO_MOMENTUM_SOURCES + (
    "yahoo-losers",
    "yahoo-oversold",
    "yahoo-overbought",
    "yahoo-52-week-gainers",
    "yahoo-all-time-high",
)
YAHOO_SCREENER_API = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
    "?scrIds={scr_id}&count={count}&formatted=false"
)
YAHOO_SCREENER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; momentum-screener)",
    "Accept": "application/json",
}
OTC_EXCHANGES = {"PNK", "OTC", "PINKMKT", "GREY", "OTCMKTS"}
EXCLUDED_QUOTE_TYPES = {"MUTUALFUND", "ETF"}

DEFAULT_MIN_PRICE = 2.0
DEFAULT_MIN_MARKET_CAP = 500_000_000.0
DEFAULT_MIN_VOLUME = 1_000_000.0
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PORTFOLIO_PATH = REPO_ROOT / "config" / "portfolio.yaml"
DEFAULT_TRADE_JOURNAL_PATH = REPO_ROOT / "data" / "trade_journal.csv"
PORTFOLIO_CONCENTRATION_WARNING_THRESHOLD = 2

logger = logging.getLogger(__name__)


@dataclass
class WatchlistItem:
    ticker: str
    category: str = ""
    news: bool = False
    sector_strength: bool = False
    sources: list[str] = field(default_factory=list)


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def safe_int(value: Any, default: int = 0) -> int:
    """Convert value to int, returning *default* for '', None, NaN, or non-numeric input."""
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    if isinstance(value, str) and not value.strip():
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


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


def load_portfolio_config(path: Path = DEFAULT_PORTFOLIO_PATH) -> list[str]:
    try:
        raw_text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []

    holdings: list[str] = []
    in_holdings = False
    for raw_line in raw_text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped == "holdings:":
            in_holdings = True
            continue
        if in_holdings and stripped.startswith("- "):
            holding = stripped[2:].strip().strip("'\"")
            if holding:
                holdings.append(holding)
            continue
        if in_holdings and not raw_line.startswith((" ", "\t")):
            in_holdings = False

    return holdings


def fetch_yahoo_screener(source_key: str, limit: int) -> list[dict[str, Any]]:
    """Fetch tickers from a Yahoo Finance predefined screener.

    Returns a list of dicts with at minimum: ticker, exchange, price,
    market_cap, volume.  Raises RuntimeError on failure.
    """
    scr_id = YAHOO_SCREENER_IDS[source_key]
    url = YAHOO_SCREENER_API.format(scr_id=scr_id, count=limit)
    try:
        resp = requests.get(url, headers=YAHOO_SCREENER_HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Yahoo screener '{source_key}': {exc}") from exc
    except ValueError as exc:
        raise RuntimeError(f"Invalid JSON from Yahoo screener '{source_key}': {exc}") from exc

    try:
        quotes = data["finance"]["result"][0]["quotes"] if data.get("finance", {}).get("result") else []
    except (KeyError, IndexError, TypeError):
        quotes = []

    results: list[dict[str, Any]] = []
    for q in quotes:
        ticker = q.get("symbol", "").strip().upper()
        if not ticker:
            continue
        results.append(
            {
                "ticker": ticker,
                "exchange": (q.get("fullExchangeName") or q.get("exchange") or "").upper(),
                "price": q.get("regularMarketPrice"),
                "market_cap": q.get("marketCap"),
                "volume": q.get("regularMarketVolume"),
                "quote_type": q.get("quoteType", "").upper(),
            }
        )
    return results


def fetch_yahoo_group(source_keys: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    """Fetch from a group of Yahoo screeners, combine and deduplicate.

    Each entry gets a 'sources' list showing which screeners it appeared in.
    Sources that return HTTP 404 or any other error are logged as warnings and
    skipped gracefully.  Returns an empty list if *all* sources fail.
    """
    seen: dict[str, dict[str, Any]] = {}
    any_success = False
    errors: list[str] = []

    for source_key in source_keys:
        label = YAHOO_SCREENER_LABEL[source_key]
        try:
            entries = fetch_yahoo_screener(source_key, limit)
            any_success = True
        except RuntimeError as exc:
            logger.warning("%s unavailable, skipping", source_key)
            logger.debug("  Detail: %s", exc)
            errors.append(str(exc))
            continue

        for entry in entries:
            ticker = entry["ticker"]
            if ticker in seen:
                seen[ticker]["sources"].append(label)
                # Keep the highest market_cap / price / volume we've seen
                for field_name in ("price", "market_cap", "volume"):
                    existing = seen[ticker].get(field_name)
                    incoming = entry.get(field_name)
                    if incoming is not None and (existing is None or incoming > existing):
                        seen[ticker][field_name] = incoming
            else:
                entry = dict(entry)
                entry["sources"] = [label]
                seen[ticker] = entry

    if not any_success:
        logger.error("All Yahoo screener fetches failed. Tip: run with --source watchlist instead.")
        for err in errors:
            logger.error("  %s", err)

    return list(seen.values())


def fetch_yahoo_all(limit: int) -> list[dict[str, Any]]:
    """Fetch from the momentum source group (yahoo-momentum).

    Kept for backwards compatibility — prefer ``--source yahoo-momentum``.
    """
    return fetch_yahoo_group(YAHOO_MOMENTUM_SOURCES, limit)


def apply_filters(
    entries: list[dict[str, Any]],
    min_price: float,
    min_market_cap: float,
    min_volume: float,
    exclude_otc: bool = True,
) -> list[dict[str, Any]]:
    """Filter screener entries before the scoring pipeline."""
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        ticker = entry["ticker"]

        if exclude_otc:
            exchange = entry.get("exchange", "")
            quote_type = entry.get("quote_type", "")
            if exchange in OTC_EXCHANGES or quote_type in EXCLUDED_QUOTE_TYPES:
                logger.debug("Skipping %s: OTC/pink-sheet/non-equity (%s)", ticker, exchange)
                continue

        price = entry.get("price")
        if price is not None and price < min_price:
            logger.debug("Skipping %s: price %.2f < min_price %.2f", ticker, price, min_price)
            continue

        mcap = entry.get("market_cap")
        if mcap is not None and mcap < min_market_cap:
            logger.debug("Skipping %s: market_cap %s < min_market_cap %s", ticker, mcap, min_market_cap)
            continue

        vol = entry.get("volume")
        if vol is not None and vol < min_volume:
            logger.debug("Skipping %s: volume %s < min_volume %s", ticker, vol, min_volume)
            continue

        filtered.append(entry)
    return filtered


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


def market_cap_tier(market_cap: int | None) -> str:
    if market_cap is None:
        return "Unknown"
    if market_cap < 2_000_000_000:
        return "Small"
    if market_cap < 10_000_000_000:
        return "Mid"
    return "Large"


def float_risk_label(float_shares: int | None) -> tuple[str, str]:
    if float_shares is None:
        return "Unknown", "Unknown"
    if float_shares < 50_000_000:
        return "Low", "High"
    if float_shares < 150_000_000:
        return "Medium", "Medium"
    return "High", "Low"


def atr_percent(daily: pd.DataFrame) -> float | None:
    if daily.empty or len(daily) < 15:
        return None
    high = daily["High"]
    low = daily["Low"]
    close = daily["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]
    last_close = close.iloc[-1]
    if pd.isna(atr) or not last_close:
        return None
    return float((atr / last_close) * 100)


def earnings_warning(next_earnings: datetime | None) -> tuple[str | None, str]:
    if not next_earnings:
        return None, "None"
    today = datetime.now().date()
    delta = (next_earnings.date() - today).days
    if delta < 0:
        return next_earnings.date().isoformat(), "Passed"
    if delta <= 3:
        return next_earnings.date().isoformat(), "Elevated"
    if delta <= 7:
        return next_earnings.date().isoformat(), "Watch"
    return next_earnings.date().isoformat(), "None"


def sentiment_from_headline(headline: str) -> str:
    headline_lower = headline.lower()
    positive_terms = ("upgrade", "beat", "expansion", "contract win", "new contract", "partnership", "record")
    negative_terms = ("downgrade", "miss", "investigation", "lawsuit", "cut", "recall")
    if any(term in headline_lower for term in positive_terms):
        return "Positive"
    if any(term in headline_lower for term in negative_terms):
        return "Negative"
    return "Neutral"


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
        day_change_source = "no_reference"
    day_change_pct = (
        ((last - day_change_reference) / day_change_reference) * 100
        if day_change_reference is not None and day_change_reference > 0
        else 0.0
    )
    dist_from_high_pct = ((last - day_high) / day_high) * 100 if day_high else 0.0
    range_pos = (last - day_low) / (day_high - day_low) if day_high != day_low else 0.5

    monthly = stock.history(period="1mo", interval="1d")
    daily_3m = stock.history(period="3mo", interval="1d")
    avg_volume = float(monthly["Volume"].tail(20).mean()) if not monthly.empty else None
    volume_ratio = (day_volume / avg_volume) if avg_volume and avg_volume > 0 else None

    lows = intraday["Low"].tail(8)
    lower_lows = len(lows) >= 3 and bool(lows.is_monotonic_decreasing)

    bid = as_float(first_non_none(fast_info.get("bid"), info.get("bid")))
    ask = as_float(first_non_none(fast_info.get("ask"), info.get("ask")))
    spread_inputs_valid = last > 0 and bid is not None and ask is not None and bid > 0 and ask > 0
    spread_ratio = ((ask - bid) / last) if spread_inputs_valid else None
    spread_bps = (spread_ratio * BASIS_POINTS_MULTIPLIER) if spread_ratio is not None else None
    spread_pct = (spread_ratio * 100) if spread_ratio is not None else None

    market_cap_raw = info.get("marketCap")
    market_cap = int(market_cap_raw) if market_cap_raw else None
    cap_tier = market_cap_tier(market_cap)

    float_raw = first_non_none(info.get("floatShares"), fast_info.get("shares"))
    float_shares = int(float_raw) if float_raw else None
    float_label, volatility_risk = float_risk_label(float_shares)

    atr_pct = atr_percent(daily_3m)
    atr_volatility = "Unknown"
    if atr_pct is not None:
        atr_volatility = "High" if atr_pct >= 6 else "Medium" if atr_pct >= 3 else "Low"

    premarket = info.get("preMarketPrice")
    after_hours = info.get("postMarketPrice")
    premarket_volume = first_non_none(info.get("preMarketVolume"), fast_info.get("preMarketVolume"))
    premarket_gap_pct = (
        ((float(premarket) - previous_close) / previous_close) * 100
        if premarket and previous_close and previous_close > 0
        else None
    )

    earnings_raw = info.get("earningsDate")
    next_earnings: datetime | None = None
    if isinstance(earnings_raw, (list, tuple)) and earnings_raw:
        candidate = earnings_raw[0]
        if hasattr(candidate, "to_pydatetime"):
            next_earnings = candidate.to_pydatetime()
        elif isinstance(candidate, datetime):
            next_earnings = candidate
    elif isinstance(earnings_raw, datetime):
        next_earnings = earnings_raw
    elif isinstance(info.get("earningsTimestamp"), (int, float)):
        next_earnings = datetime.fromtimestamp(float(info["earningsTimestamp"]))
    earnings_date, earnings_risk = earnings_warning(next_earnings)

    sector_info = resolve_sector_info(item.ticker, info)
    thematic_tags = ", ".join(sector_info.thematic_tags) if sector_info.thematic_tags else "None"

    try:
        raw_news = stock.news or []
    except Exception:
        raw_news = []
    catalysts: list[str] = []
    sentiment_tags: list[str] = []
    for article in raw_news[:3]:
        title = str(article.get("title") or "").strip()
        if not title:
            continue
        sentiment = sentiment_from_headline(title)
        catalysts.append(title)
        sentiment_tags.append(sentiment)
    catalyst_summary = " | ".join(catalysts) if catalysts else ""
    headline_sentiment = (
        "Positive"
        if sentiment_tags and sentiment_tags.count("Positive") > sentiment_tags.count("Negative")
        else "Negative"
        if sentiment_tags and sentiment_tags.count("Negative") > sentiment_tags.count("Positive")
        else "Neutral"
    )

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

    if atr_pct is not None and atr_pct <= 4:
        score += 5
        reasons.append("contained ATR")
    elif atr_pct is not None and atr_pct >= 8:
        score -= 5
        reasons.append("very high ATR")

    if cap_tier == "Large":
        score += 8
        reasons.append("institutional quality")
    elif cap_tier == "Mid":
        score += 4
        reasons.append("momentum growth cap")
    elif cap_tier == "Small":
        score -= 5
        reasons.append("small-cap volatility")

    if float_label == "Low":
        score -= 6
        reasons.append("low float squeeze risk")

    if earnings_risk == "Elevated":
        score -= 8
        reasons.append("earnings proximity risk")
    elif earnings_risk == "Watch":
        score -= 4
        reasons.append("earnings watch")

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
        "market_cap_tier": cap_tier,
        "float_shares": float_shares,
        "float_label": float_label,
        "volatility_risk": volatility_risk,
        "atr_pct": round(atr_pct, 2) if atr_pct is not None else None,
        "atr_volatility": atr_volatility,
        "premarket_gap_pct": round(premarket_gap_pct, 2) if premarket_gap_pct is not None else None,
        "premarket_volume": int(premarket_volume) if premarket_volume else None,
        "earnings_date": earnings_date,
        "earnings_warning": earnings_risk,
        "sector": sector_info.sector,
        "industry": sector_info.industry,
        "thematic_tags": thematic_tags,
        "catalyst_headlines": catalyst_summary,
        "sentiment_tag": headline_sentiment,
        "insider_activity": "N/A (placeholder)",
        "score": score,
        "classification": classify(score),
        "reasons": ", ".join(reasons),
        "sources": ", ".join(item.sources) if item.sources else "",
    }


def compute_setup_bucket(df: pd.DataFrame) -> pd.Series:
    """Return a Series of setup bucket labels for each row.

    A-list rows (score >= 70) are split into:
    - A1: tradable strength (meets all quality filters)
    - A2: strong but extended / wait (score >= 70 but fails one or more A1 filters)

    B-list and C-list rows are labelled from their existing ``classification`` column.
    """
    _zeros = pd.Series(0, index=df.index, dtype=float)
    day_change_numeric = pd.to_numeric(
        df["day_change_pct"] if "day_change_pct" in df.columns else _zeros, errors="coerce"
    ).fillna(0.0)
    distance_from_high_numeric = pd.to_numeric(
        df["distance_from_high_pct"] if "distance_from_high_pct" in df.columns else _zeros, errors="coerce"
    ).fillna(0.0)
    score_numeric = pd.to_numeric(
        df["score"] if "score" in df.columns else _zeros, errors="coerce"
    ).fillna(0)

    vwap_numeric = pd.to_numeric(
        df["vwap"] if "vwap" in df.columns else _zeros, errors="coerce"
    ).fillna(0.0)
    last_numeric = pd.to_numeric(
        df["last"] if "last" in df.columns else _zeros, errors="coerce"
    ).fillna(0.0)
    above_vwap = last_numeric > vwap_numeric

    sources_series = df.get("sources") if "sources" in df.columns else pd.Series([""] * len(df), index=df.index)
    has_most_active = sources_series.astype(str).str.contains("Most Active", case=False, na=False)
    volume_ratio_numeric = pd.to_numeric(
        df["volume_ratio"] if "volume_ratio" in df.columns else _zeros, errors="coerce"
    ).fillna(0.0)

    in_do_not_chase = (day_change_numeric > DO_NOT_CHASE_DAY_CHANGE_THRESHOLD) & (
        distance_from_high_numeric <= DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD
    )
    strong_continuation = (
        above_vwap
        & (day_change_numeric <= A1_HARD_EXTENDED_DAY_CHANGE_THRESHOLD)
        & (distance_from_high_numeric >= A1_STRONG_CONTINUATION_DISTANCE_THRESHOLD)
        & (volume_ratio_numeric >= A1_STRONG_CONTINUATION_VOLUME_THRESHOLD)
        & has_most_active
    )

    a_score = score_numeric >= 70
    a1_mask = (
        a_score
        & (~in_do_not_chase)
        & (distance_from_high_numeric > -8)
        & (day_change_numeric < A1_HARD_EXTENDED_DAY_CHANGE_THRESHOLD)
        & ((day_change_numeric <= A1_EXTENDED_DAY_CHANGE_THRESHOLD) | strong_continuation)
        & above_vwap
    )
    a2_mask = a_score & (~a1_mask)

    classification = df.get("classification", pd.Series([""] * len(df), index=df.index)).astype(str)

    result = pd.Series("C-list", index=df.index)
    result[classification == "B-list"] = "B-list"
    result[a2_mask] = "A2"
    result[a1_mask] = "A1"
    return result


_CHATGPT_PROMPT = (
    "Review this trading brief. Which 3 names have the best setup, "
    "which are chase-risk, and which should be ignored? "
    "Focus on market regime, setup, VWAP, distance from high, ATR, risk, entry, stop and position size."
)


def _str_col(frame: pd.DataFrame, col: str) -> pd.Series:
    """Return ``col`` as a string Series, defaulting to '' when absent."""
    if col in frame.columns:
        return frame[col].astype(str)
    return pd.Series([""] * len(frame), index=frame.index)


def _numeric_col(frame: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in frame.columns:
        values: Any = frame[col]
    else:
        values = pd.Series([default] * len(frame), index=frame.index)
    return pd.to_numeric(values, errors="coerce").fillna(default)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and not value.strip()


def _display_text(value: Any, default: str = "n/a") -> str:
    return default if _is_missing(value) else str(value)


def _display_number(
    value: Any,
    *,
    default: str = "n/a",
    suffix: str = "",
    decimals: int | None = None,
) -> str:
    numeric = as_float(value)
    if numeric is None:
        return default
    numeric = float(numeric)
    if decimals is None:
        text = str(int(numeric)) if numeric % 1 == 0 else str(numeric)
    else:
        text = f"{numeric:.{decimals}f}"
    return f"{text}{suffix}"


def _display_signed_pct(value: Any, default: str = "n/a") -> str:
    numeric = as_float(value)
    if numeric is None:
        return default
    return f"{numeric:+.2f}%"


def _display_premarket_summary(row: pd.Series) -> str:
    gap = _display_number(row.get("premarket_gap_pct"), default="unavailable", suffix="%", decimals=2)
    volume = _display_number(row.get("premarket_volume"), default="unavailable")
    if gap == "unavailable" and volume == "unavailable":
        return "Premarket: unavailable"
    return f"Premarket gap: {gap} | Premarket volume: {volume}"


def _display_position_size(value: Any) -> str:
    numeric = as_float(value)
    if numeric is None:
        return "n/a"
    return f"{float(numeric):.2f} ({float(numeric) * 100:.0f}% of book)"


def _rank_candidates(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.copy()
    src = _str_col(frame, "sources")
    frame["_multi"] = src.apply(lambda s: 1 if "," in s.strip() else 0)
    dist_h = _numeric_col(frame, "distance_from_high_pct", -100)
    frame["_near_high"] = (dist_h > -8).astype(int)
    vwap_n = _numeric_col(frame, "vwap")
    last_n = _numeric_col(frame, "last")
    frame["_above_vwap"] = (last_n > vwap_n).astype(int)
    vol_r = _numeric_col(frame, "volume_ratio")
    frame["_vol2x"] = (vol_r > 2).astype(int)
    frame["_score"] = _numeric_col(frame, "score")
    return frame.sort_values(
        by=["_multi", "_near_high", "_above_vwap", "_vol2x", "_score"],
        ascending=[False, False, False, False, False],
    )


def _build_market_regime_fallback() -> dict[str, Any]:
    return {
        "market_regime": "Unknown",
        "momentum_odds": "Unknown",
        "sector_strength": {},
    }


def format_market_cap(value: Any) -> str:
    mcap = as_float(value)
    if mcap is None or mcap <= 0:
        return "n/a"
    if mcap >= 1_000_000_000:
        return f"{mcap / 1_000_000_000:.1f}B"
    return f"{mcap / 1_000_000:.0f}M"


def _row_exposure_categories(row: dict[str, Any] | pd.Series) -> tuple[str, ...]:
    return exposure_categories_for_security(
        str(row.get("ticker", "")),
        sector=str(row.get("sector", "")),
        industry=str(row.get("industry", "")),
        thematic_tags=str(row.get("thematic_tags", "")),
        category=str(row.get("category", "")),
    )


def _primary_sector_strength(categories: tuple[str, ...], regime_report: dict[str, Any]) -> tuple[str, str]:
    sector_strength = regime_report.get("sector_strength", {})
    mapping = (
        ("Semiconductors", "SOXX"),
        ("Crypto miners", "Crypto Miners"),
        ("Cybersecurity", "Cyber"),
        ("AI / Datacenter", "AI Software"),
    )
    for category, regime_key in mapping:
        if category in categories and regime_key in sector_strength:
            return category, str(sector_strength.get(regime_key, "Unknown"))
    return ("General", "Unknown")


def _market_proxy(categories: tuple[str, ...]) -> str:
    if "Crypto miners" in categories:
        return "BTC and crypto miners"
    if "Semiconductors" in categories:
        return "QQQ and SOXX"
    if "Cybersecurity" in categories:
        return "QQQ and cybersecurity peers"
    if "Space / Aerospace" in categories:
        return "IWM and space peers"
    return "QQQ"


def _priority_label(row: dict[str, Any] | pd.Series, priority_score: int) -> str:
    bucket = str(row.get("setup_bucket", row.get("classification", "")))
    chase_risk = str(row.get("chase_risk", ""))
    setup = str(row.get("setup", "")).lower()
    if bucket == "C-list" or chase_risk == "High":
        return "Do not chase"
    if bucket == "A2":
        return "Strong but extended"
    if bucket == "A1" and priority_score >= 55:
        return "Follow actively"
    if setup == "continuation":
        return "Watch for VWAP hold"
    if setup == "breakout":
        return "Watch for breakout"
    if setup == "pullback":
        return "Pullback only"
    return "Secondary watch"


def _priority_action_hint(row: dict[str, Any] | pd.Series) -> str:
    setup = str(row.get("setup", "")).lower()
    if str(row.get("priority_label", "")) == "Do not chase":
        return "wait for a full reset"
    if setup == "breakout":
        return "wait for VWAP hold / breakout"
    if setup == "continuation":
        return "watch for VWAP hold"
    if setup == "pullback":
        return "pullback only"
    return "only act on clean confirmation"


def _build_trigger_rules(row: dict[str, Any] | pd.Series, regime_report: dict[str, Any]) -> dict[str, str]:
    categories = _row_exposure_categories(row)
    proxy = _market_proxy(categories)
    _, sector_strength = _primary_sector_strength(categories, regime_report)
    entry_low = _display_number(row.get("preferred_entry_low"), decimals=2)
    entry_high = _display_number(row.get("preferred_entry_high"), decimals=2)
    breakout = _display_number(row.get("breakout_level"), decimals=2)
    invalidation = _display_number(row.get("invalidation_level"), decimals=2)
    earnings_warning = str(row.get("earnings_warning", ""))
    spread_bps = as_float(row.get("spread_bps"))

    buy_bits = ["price holds above VWAP"]
    if entry_low != "n/a" and entry_high != "n/a":
        buy_bits.append(f"stays constructive around {entry_low}–{entry_high}")
    if sector_strength == "Strong":
        buy_bits.append("sector leadership remains strong")
    else:
        buy_bits.append(f"{proxy} stays supportive")

    avoid_bits = ["loses VWAP"]
    if entry_low != "n/a" and entry_high != "n/a":
        avoid_bits.append(f"falls below the {entry_low}–{entry_high} entry zone")
    avoid_bits.append(f"{proxy} reverses lower")
    if spread_bps is not None and spread_bps > SPREAD_PENALTY_THRESHOLD_BPS:
        avoid_bits.append("spread stays too wide")
    if earnings_warning in {"Watch", "Elevated"}:
        avoid_bits.append(f"earnings risk is {earnings_warning.lower()}")

    return {
        "buy_trigger": " and ".join(buy_bits),
        "breakout_trigger": f"breaks above {breakout} with expanding volume and holds above VWAP",
        "pullback_trigger": f"pulls back into {entry_low}–{entry_high} and reclaims VWAP",
        "invalidation_trigger": f"loses VWAP or breaks below {invalidation}",
        "avoid_trigger": " or ".join(avoid_bits),
    }


def _priority_score(
    row: dict[str, Any] | pd.Series,
    regime_report: dict[str, Any],
    concentrated_categories: set[str],
) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
    bucket = str(row.get("setup_bucket", row.get("classification", "")))
    last = as_float(row.get("last"))
    vwap = as_float(row.get("vwap"))
    entry_low = as_float(row.get("preferred_entry_low"))
    entry_high = as_float(row.get("preferred_entry_high"))
    volume_ratio = as_float(row.get("volume_ratio")) or 0.0
    distance_from_high = as_float(row.get("distance_from_high_pct")) or 0.0
    spread_bps = as_float(row.get("spread_bps"))
    earnings_warning = str(row.get("earnings_warning", ""))
    chase_risk = str(row.get("chase_risk", ""))
    categories = _row_exposure_categories(row)
    overlap = tuple(sorted(category for category in categories if category in concentrated_categories))
    _, sector_strength = _primary_sector_strength(categories, regime_report)

    score = {
        "A1": 30,
        "A2": 22,
        "B-list": 14,
        "C-list": 0,
    }.get(bucket, 8)

    if last is not None and vwap is not None and last > vwap:
        score += 10
    if last is not None and entry_low is not None and entry_high is not None:
        if entry_low <= last <= entry_high:
            score += 12
        elif entry_high > 0 and abs(last - entry_high) / entry_high <= 0.02:
            score += 6
    score += min(int(volume_ratio * 4), 15)

    if distance_from_high >= -2:
        score += 10
    elif distance_from_high >= -5:
        score += 5
    elif distance_from_high <= -10:
        score -= 8

    if sector_strength == "Strong":
        score += 8
    elif sector_strength == "Weak":
        score -= 8

    regime = str(regime_report.get("market_regime", "Unknown"))
    if regime == "Risk-on":
        score += 8
    elif regime == "Neutral":
        score += 2
    elif regime in {"Risk-off", "Mean-reversion environment"}:
        score -= 8
    elif regime == "Panic":
        score -= 12

    if chase_risk == "High":
        score -= 12
    elif chase_risk == "Medium":
        score -= 4
    else:
        score += 4

    if spread_bps is not None:
        if spread_bps > SPREAD_PENALTY_THRESHOLD_BPS:
            score -= 8
        elif spread_bps > 15:
            score -= 4

    if earnings_warning == "Elevated":
        score -= 10
    elif earnings_warning == "Watch":
        score -= 5

    score -= len(overlap) * 6
    return (max(score, 0), categories, overlap)


def build_portfolio_warnings(df: pd.DataFrame, portfolio_holdings: list[str]) -> list[str]:
    if not portfolio_holdings:
        return []

    holdings_by_category = build_exposure_summary(portfolio_holdings)
    warnings: list[str] = []
    for category, names in sorted(
        holdings_by_category.items(),
        key=lambda item: (-len(item[1]), item[0]),
    ):
        if len(names) < PORTFOLIO_CONCENTRATION_WARNING_THRESHOLD:
            continue
        overlap_names: list[str] = []
        if "exposure_categories" in df.columns:
            overlap_mask = _str_col(df, "exposure_categories").str.contains(category, regex=False, na=False)
            overlap_names = sorted(df.loc[overlap_mask, "ticker"].astype(str).drop_duplicates().tolist())
        overlap_note = f" Current overlap: {', '.join(overlap_names)}." if overlap_names else ""
        warnings.append(
            f"{category} exposure is already concentrated through {', '.join(names)}."
            f" Avoid adding another correlated {category.lower()} name unless the setup is exceptional."
            f"{overlap_note}"
        )
    return warnings


def enrich_with_intraday_assistant(
    df: pd.DataFrame,
    regime_report: dict[str, Any],
    portfolio_holdings: list[str],
) -> pd.DataFrame:
    if df.empty:
        return df

    holdings_by_category = build_exposure_summary(portfolio_holdings)
    concentrated_categories = {
        category for category, names in holdings_by_category.items() if len(names) >= PORTFOLIO_CONCENTRATION_WARNING_THRESHOLD
    }

    extras: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        priority_score, categories, overlap = _priority_score(row, regime_report, concentrated_categories)
        trigger_rules = _build_trigger_rules(row, regime_report)
        extras.append(
            {
                "priority_score": priority_score,
                "priority_label": _priority_label(row, priority_score),
                "exposure_categories": ", ".join(categories) if categories else "None",
                "portfolio_overlap": ", ".join(overlap) if overlap else "",
                **trigger_rules,
            }
        )

    extra_df = pd.DataFrame(extras)
    existing = [column for column in extra_df.columns if column in df.columns]
    if existing:
        df = df.drop(columns=existing)
    enriched = pd.concat([df.reset_index(drop=True), extra_df], axis=1)
    return enriched.sort_values(by=["priority_score", "score"], ascending=[False, False], na_position="last")


def format_decision_summary(df: pd.DataFrame, in_do_not_chase: pd.Series) -> list[str]:
    """Build the ## Decision summary section lines."""
    lines: list[str] = ["## Decision summary", ""]

    if "setup_bucket" not in df.columns:
        df = df.copy()
        df["setup_bucket"] = compute_setup_bucket(df)

    if "error" in df.columns:
        has_error = df["error"].astype(str).str.strip() != ""
    else:
        has_error = pd.Series(False, index=df.index)

    # ── 1. Top 5 actionable candidates ──────────────────────────────────────
    lines.append("### 1. Top 5 actionable candidates")
    lines.append("")

    a1_mask = (df["setup_bucket"] == "A1") & ~has_error & ~in_do_not_chase
    b_mask = (df["setup_bucket"] == "B-list") & ~has_error & ~in_do_not_chase
    primary = df[a1_mask | b_mask].copy()

    if not primary.empty:
        primary = _rank_candidates(primary)

    top5 = primary.head(5)

    # Fall back to A2 / do-not-chase if fewer than 5
    if len(top5) < 5:
        a2_mask = (df["setup_bucket"] == "A2") & ~has_error
        a2_extra = df[a2_mask & ~df.index.isin(top5.index)]
        top5 = pd.concat([top5, a2_extra.head(5 - len(top5))])
    if len(top5) < 5:
        dnc_extra = df[in_do_not_chase & ~has_error & ~df.index.isin(top5.index)]
        top5 = pd.concat([top5, dnc_extra.head(5 - len(top5))])

    def _short_reason(reasons: str) -> str:
        tags = []
        for tag in ("above VWAP", "near high", "volume > 2x", "news/earnings catalyst", "sector strong"):
            if tag in reasons:
                tags.append(tag)
        if not tags and reasons:
            tags.append(reasons.split(",")[0].strip())
        return ", ".join(tags)

    if top5.empty:
        lines.append("- (no candidates)")
    else:
        for _, row in top5.iterrows():
            ticker = str(row.get("ticker", ""))
            src_val = str(row.get("sources", "")).strip()
            src_note = f" [{src_val}]" if src_val else ""
            bucket = str(row.get("setup_bucket", ""))
            sc = safe_int(row.get("score", 0))
            chg = pd.to_numeric(row.get("day_change_pct", 0), errors="coerce") or 0.0
            reason_text = _short_reason(str(row.get("reasons", "")))
            lines.append(
                f"- **{ticker}**{src_note} | {bucket} | score {sc} | {chg:+.2f}% | {reason_text}"
            )
    lines.append("")

    # ── 2. Best multi-source candidate ──────────────────────────────────────
    lines.append("### 2. Best multi-source candidate")
    lines.append("")

    valid = df[~has_error].copy()
    src_series = _str_col(valid, "sources")
    multi_df = valid[src_series.str.contains(",", na=False)]

    if multi_df.empty:
        lines.append("No multi-source candidate found.")
    else:
        sc_num = pd.to_numeric(multi_df.get("score", 0), errors="coerce").fillna(0)
        best = multi_df.loc[sc_num.idxmax()]
        lines.append(f"- **{best.get('ticker', '')}** [{str(best.get('sources', '')).strip()}]")
    lines.append("")

    # ── 3. Do-not-chase names ────────────────────────────────────────────────
    lines.append("### 3. Do-not-chase names")
    lines.append("")

    dnc_df = df[in_do_not_chase]
    if dnc_df.empty:
        lines.append("- (none)")
    else:
        for _, row in dnc_df.iterrows():
            ticker = str(row.get("ticker", ""))
            chg = pd.to_numeric(row.get("day_change_pct", 0), errors="coerce") or 0.0
            dist_h = pd.to_numeric(row.get("distance_from_high_pct", 0), errors="coerce") or 0.0
            lines.append(f"- **{ticker}**: {chg:+.2f}% day change, {dist_h:.2f}% from high")
    lines.append("")

    # ── 4. Avoid / weak names ────────────────────────────────────────────────
    lines.append("### 4. Avoid / weak names")
    lines.append("")

    c_mask = (df["setup_bucket"] == "C-list") & ~has_error
    c_list = df[c_mask].copy()

    if not c_list.empty:
        reasons_col = _str_col(c_list, "reasons")
        chg_num = pd.to_numeric(c_list.get("day_change_pct", 0), errors="coerce").fillna(0)
        dist_num = pd.to_numeric(c_list.get("distance_from_high_pct", 0), errors="coerce").fillna(0)
        sc_num = pd.to_numeric(c_list.get("score", 0), errors="coerce").fillna(0)
        c_list = c_list.copy()
        c_list["_red"] = (chg_num < 0).astype(int)
        c_list["_far"] = (dist_num < -10).astype(int)
        c_list["_ll"] = reasons_col.str.contains("lower lows", case=False, na=False).astype(int)
        c_list["_wscore"] = -sc_num
        c_list = c_list.sort_values(
            by=["_red", "_far", "_ll", "_wscore"], ascending=[False, False, False, False]
        )
        for _, row in c_list.head(5).iterrows():
            ticker = str(row.get("ticker", ""))
            reasons = str(row.get("reasons", ""))
            tags = []
            for tag in ("red", "far from high", "lower lows", "wide spread"):
                if tag in reasons:
                    tags.append(tag)
            if not tags and reasons:
                tags.append(reasons.split(",")[0].strip())
            lines.append(f"- **{ticker}**: {', '.join(tags)}")
    else:
        lines.append("- (none)")
    lines.append("")

    # ── 5. Paste-to-ChatGPT review prompt ────────────────────────────────────
    lines.append("### 5. Paste-to-ChatGPT review prompt")
    lines.append("")
    lines.append("```")
    lines.append(_CHATGPT_PROMPT)
    lines.append("```")
    lines.append("")

    return lines


def format_shareable_report(
    df: pd.DataFrame,
    regime_report: dict[str, Any],
    portfolio_holdings: list[str] | None = None,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Trading Brief ({now})", ""]
    portfolio_holdings = portfolio_holdings or []
    if "classification" not in df.columns:
        df = df.assign(classification="", score=0, reasons="", day_change_pct=0, day_change_source="")
    missing_cols = {col: 0 for col in ("day_change_pct", "distance_from_high_pct") if col not in df.columns}
    if missing_cols:
        df = df.assign(**missing_cols)

    if "setup_bucket" not in df.columns:
        df = df.copy()
        df["setup_bucket"] = compute_setup_bucket(df)

    if "error" in df.columns:
        has_error = df["error"].astype(str).str.strip() != ""
    else:
        has_error = pd.Series(False, index=df.index)

    day_change_numeric = _numeric_col(df, "day_change_pct")
    distance_from_high_numeric = _numeric_col(df, "distance_from_high_pct")
    in_do_not_chase = (day_change_numeric > DO_NOT_CHASE_DAY_CHANGE_THRESHOLD) & (
        distance_from_high_numeric <= DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD
    )

    sector_labels = {"SOXX": "Semiconductors"}
    strong_sectors = [
        sector_labels.get(name, name)
        for name, strength in regime_report.get("sector_strength", {}).items()
        if strength == "Strong"
    ]

    lines.extend(
        [
            "## Market regime",
            f"- Market regime: {regime_report.get('market_regime', 'Unknown')}",
            f"- Momentum odds: {regime_report.get('momentum_odds', 'Unknown')}",
            f"- Strong sectors: {', '.join(strong_sectors) if strong_sectors else 'None'}",
            "",
            "## Intraday priority",
        ]
    )

    ranked = df[~has_error].copy()
    if "priority_score" in ranked.columns:
        ranked = ranked.sort_values(by=["priority_score", "score"], ascending=[False, False], na_position="last")
    else:
        ranked = _rank_candidates(ranked)

    priority = ranked.head(5)
    if priority.empty:
        lines.append("- (none)")
    else:
        for idx, (_, row) in enumerate(priority.iterrows(), start=1):
            label = _display_text(row.get("priority_label"), "Secondary watch")
            lines.append(f"{idx}. {row.get('ticker', '')} — {label} — {_priority_action_hint(row)}")
    lines.append("")

    lines.append("## Trigger alerts")
    if priority.empty:
        lines.append("- (none)")
    else:
        for _, row in priority.iterrows():
            lines.extend(
                [
                    f"### {row.get('ticker', '')}",
                    f"- Buy trigger: {_display_text(row.get('buy_trigger'), 'n/a')}",
                    f"- Breakout trigger: {_display_text(row.get('breakout_trigger'), 'n/a')}",
                    f"- Pullback trigger: {_display_text(row.get('pullback_trigger'), 'n/a')}",
                    f"- Invalidation: {_display_text(row.get('invalidation_trigger'), 'n/a')}",
                    f"- Avoid trigger: {_display_text(row.get('avoid_trigger'), 'n/a')}",
                    "",
                ]
            )

    lines.append("## Portfolio warning")
    warnings = build_portfolio_warnings(priority, portfolio_holdings)
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- No concentrated overlap detected.")
    lines.append("")

    lines.append("## Journal reminder")
    lines.append(
        "- If a trade is taken, log ticker, entry, stop, target, result, reason, and whether the plan was followed "
        "in data/trade_journal.csv."
    )
    lines.append("- Review the journal with `python performance_review.py`.")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def format_markdown_report(df: pd.DataFrame) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Daily Momentum Report ({now})", ""]

    if "classification" not in df.columns:
        df = df.assign(classification="", score=0, reasons="", day_change_pct=0, day_change_source="")
    missing_cols = {col: 0 for col in ("day_change_pct", "distance_from_high_pct") if col not in df.columns}
    if missing_cols:
        df = df.assign(**missing_cols)
    if "day_change_source" not in df.columns:
        df = df.assign(day_change_source="")

    lines.append("> Data note: Premarket/after-hours data may be incomplete depending on Yahoo availability.")
    lines.append("> Heuristic note: lower-lows penalty uses a simple recent-candles pattern.")
    lines.append("")

    # Re-compute (or use existing) setup_bucket for markdown sections
    if "setup_bucket" not in df.columns:
        df = df.copy()
        df["setup_bucket"] = compute_setup_bucket(df)

    # Build in_do_not_chase mask for the warning section
    day_change_numeric = _numeric_col(df, "day_change_pct")
    distance_from_high_numeric = _numeric_col(df, "distance_from_high_pct")
    in_do_not_chase = (day_change_numeric > DO_NOT_CHASE_DAY_CHANGE_THRESHOLD) & (
        distance_from_high_numeric <= DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD
    )

    lines.extend(format_decision_summary(df, in_do_not_chase))

    labels = {
        "A1": "tradable strength",
        "A2": "strong but extended / wait",
        "B-list": "watch/reversal only",
        "C-list": "avoid",
    }

    # Rows with a non-empty error value belong only in the Feil section.
    if "error" in df.columns:
        has_error = df["error"].astype(str).str.strip() != ""
    else:
        has_error = pd.Series(False, index=df.index)

    sections: list[tuple[str, pd.Series]] = [
        ("A1", (df["setup_bucket"] == "A1") & ~has_error),
        ("A2", (df["setup_bucket"] == "A2") & ~has_error),
        ("B-list", (df["setup_bucket"] == "B-list") & ~has_error),
        ("C-list", (df["setup_bucket"] == "C-list") & ~has_error),
    ]

    for bucket, mask in sections:
        lines.append(f"## {bucket}: {labels[bucket]}")
        section = df[mask]
        if section.empty:
            lines.append("- (ingen kandidater)")
            lines.append("")
            continue

        for _, row in section.iterrows():
            sources_str_row = str(row.get("sources", "")).strip()
            sources_note = f" [{sources_str_row}]" if sources_str_row else ""
            ticker = str(row.get("ticker", ""))
            lines.append(f"- **{ticker}**{sources_note} | score {safe_int(row.get('score', 0))} | {row.get('setup', '')}")
            lines.append(
                f"  - Sector: {row.get('sector', 'Unknown')} / {row.get('industry', 'Unknown')} "
                f"({row.get('thematic_tags', 'None')})"
            )
            lines.append(
                f"  - Market cap: {format_market_cap(row.get('market_cap'))} ({row.get('market_cap_tier', 'Unknown')}) | "
                f"Float: {row.get('float_label', 'Unknown')} | Vol risk: {row.get('volatility_risk', 'Unknown')}"
            )
            lines.append(
                f"  - ATR: {row.get('atr_pct', 'n/a')}% ({row.get('atr_volatility', 'Unknown')}) | "
                f"Relative volume: {row.get('volume_ratio', 'n/a')}x | Distance from high: {row.get('distance_from_high_pct', 0)}%"
            )
            lines.append(
                f"  - {_display_premarket_summary(row)} | "
                f"Earnings: {_display_text(row.get('earnings_date'), 'n/a')} ({_display_text(row.get('earnings_warning'), 'None')})"
            )
            if str(row.get("catalyst_headlines", "")).strip():
                lines.append(f"  - Catalyst: {row.get('catalyst_headlines')} ({row.get('sentiment_tag', 'Neutral')})")
            lines.append(
                f"  - Action: entry {row.get('preferred_entry_low', 'n/a')}–{row.get('preferred_entry_high', 'n/a')} | "
                f"breakout >{row.get('breakout_level', 'n/a')} | stop <{row.get('stop_level', 'n/a')} | "
                f"targets {row.get('target_1', 'n/a')}/{row.get('target_2', 'n/a')}"
            )
            lines.append(
                f"  - Risk: {row.get('risk', 'n/a')} | Chase risk: {row.get('chase_risk', 'n/a')} | "
                f"Position size: {_display_position_size(row.get('position_size_pct'))} | Hold: {row.get('suggested_hold', 'n/a')}"
            )
            lines.append(
                f"  - Momentum detail: {row.get('reasons', '')}. Change {row.get('day_change_pct', 0)}% "
                f"(kilde: {row.get('day_change_source', '')})."
            )
    lines.append("")

    do_not_chase = df[in_do_not_chase]
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

    if "error" in df.columns:
        error_col = df["error"].astype(str).str.strip()
        errors = df[error_col != ""]
    else:
        errors = pd.DataFrame()
    if not errors.empty:
        lines.append("## Feil")
        for _, row in errors.iterrows():
            lines.append(f"- {row['ticker']}: {row['error']}")

    return "\n".join(lines).strip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Market momentum screener")
    parser.add_argument(
        "--input",
        default="watchlist.csv",
        help="CSV med ticker,category[,news,sector_strength] (brukes med --source watchlist)",
    )
    parser.add_argument("--outdir", default=".", help="Mappe for output-filer")

    all_sources = (
        ["watchlist"]
        + list(YAHOO_SCREENER_IDS.keys())
        + ["yahoo-momentum", "yahoo-expanded", "yahoo-all"]
    )
    parser.add_argument(
        "--source",
        default="watchlist",
        choices=all_sources,
        help=(
            "Datakilde for tickers. "
            "'watchlist' bruker --input CSV (standard). "
            "Grouped Yahoo sources: 'yahoo-momentum' (recommended), "
            "'yahoo-expanded' (broadest), 'yahoo-all' (alias for yahoo-momentum). "
            "Individual Yahoo sources: yahoo-gainers, yahoo-most-active, yahoo-trending, "
            "yahoo-unusual-volume, yahoo-high-beta, yahoo-losers, yahoo-oversold, "
            "yahoo-overbought, yahoo-52-week-gainers, yahoo-all-time-high."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=25,
        help="Maks antall tickers å hente fra Yahoo screeners (standard: 25)",
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=DEFAULT_MIN_PRICE,
        help=f"Minimum aksjekurs for Yahoo-filtrering (standard: {DEFAULT_MIN_PRICE})",
    )
    parser.add_argument(
        "--min-market-cap",
        type=float,
        default=DEFAULT_MIN_MARKET_CAP,
        help=f"Minimum markedsverdi for Yahoo-filtrering (standard: {DEFAULT_MIN_MARKET_CAP:.0f})",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=DEFAULT_MIN_VOLUME,
        help=f"Minimum volum for Yahoo-filtrering (standard: {DEFAULT_MIN_VOLUME:.0f})",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    source = args.source

    if source == "watchlist":
        watchlist = load_watchlist(Path(args.input))
    else:
        # Fetch from Yahoo screener(s)
        _GROUP_SOURCES: dict[str, tuple[str, ...]] = {
            "yahoo-all": YAHOO_MOMENTUM_SOURCES,
            "yahoo-momentum": YAHOO_MOMENTUM_SOURCES,
            "yahoo-expanded": YAHOO_EXPANDED_SOURCES,
        }
        if source in _GROUP_SOURCES:
            entries = fetch_yahoo_group(_GROUP_SOURCES[source], args.limit)
            if not entries:
                print(
                    "ERROR: Could not fetch any tickers from Yahoo screeners. "
                    "Try running with --source watchlist instead.",
                    file=sys.stderr,
                )
                sys.exit(1)
        else:
            try:
                entries = fetch_yahoo_screener(source, args.limit)
            except RuntimeError as exc:
                print(
                    f"ERROR: {exc}\n"
                    "Tip: run with --source watchlist to use your local watchlist.csv instead.",
                    file=sys.stderr,
                )
                sys.exit(1)

        entries = apply_filters(
            entries,
            min_price=args.min_price,
            min_market_cap=args.min_market_cap,
            min_volume=args.min_volume,
        )

        if not entries:
            print(
                "WARNING: All fetched tickers were filtered out by min_price/min_market_cap/"
                "min_volume/OTC rules. Try relaxing the filter arguments.",
                file=sys.stderr,
            )

        watchlist = [
            WatchlistItem(
                ticker=e["ticker"],
                sources=e.get("sources", []),
            )
            for e in entries
        ]

    rows: list[dict[str, Any]] = []
    for item in watchlist:
        try:
            rows.append(score_stock(item))
        except Exception as exc:  # pragma: no cover
            rows.append({"ticker": item.ticker, "category": item.category, "error": str(exc)})

    df = pd.DataFrame(rows)
    if "score" in df.columns:
        df = df.sort_values(by="score", ascending=False, na_position="last")

    df = enrich_with_strategy(df.fillna(""))
    df["setup_bucket"] = compute_setup_bucket(df)
    portfolio_holdings = load_portfolio_config()
    try:
        regime_report = build_regime_report()
    except Exception:
        regime_report = _build_market_regime_fallback()
    df = enrich_with_intraday_assistant(df, regime_report, portfolio_holdings)

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = outdir / f"momentum_report_{stamp}.csv"
    md_path = outdir / f"momentum_report_{stamp}.md"
    json_path = outdir / f"momentum_report_{stamp}.json"

    df.to_csv(csv_path, index=False)
    report = format_markdown_report(df)
    md_path.write_text(report, encoding="utf-8")
    json_path.write_text(
        json.dumps(df.fillna("").to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    shareable_dir = outdir / "shareable"
    shareable_dir.mkdir(parents=True, exist_ok=True)
    brief_path = shareable_dir / f"trading_brief_{stamp}.md"
    brief_path.write_text(format_shareable_report(df, regime_report, portfolio_holdings), encoding="utf-8")

    print(report)
    print(f"Saved CSV report: {csv_path}")
    print(f"Saved Markdown report: {md_path}")
    print(f"Saved JSON report: {json_path}")
    print(f"Saved shareable trading brief: {brief_path}")


if __name__ == "__main__":
    main()
