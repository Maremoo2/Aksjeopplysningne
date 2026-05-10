from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from data_providers import AlpacaProvider, YahooProvider
from intraday_monitor import build_intraday_summary, write_intraday_report
from market_regime import build_regime_report
from recommendation_tracker import DEFAULT_LOG_PATH, load_recommendation_log, snapshot_recommendations
from strategy_engine import enrich_with_strategy
from utils.alpaca_credentials import resolve_alpaca_credentials
from utils.exposure import build_exposure_summary, exposure_categories_for_security
from utils.nordic_universe import EXCLUDED_TICKERS, TICKER_REPLACEMENTS, load_nordic_universe
from utils.sector_map import resolve_sector_info

# Risk guardrail tuned for momentum names: flags fast moves that are already
# meaningfully off highs (likely poor R/R for fresh entries).
DO_NOT_CHASE_DAY_CHANGE_THRESHOLD = 15
DO_NOT_CHASE_DISTANCE_FROM_HIGH_THRESHOLD = -7
A1_EXTENDED_DAY_CHANGE_THRESHOLD = 20
A1_HARD_EXTENDED_DAY_CHANGE_THRESHOLD = 25
A1_STRONG_CONTINUATION_DISTANCE_THRESHOLD = -1.5
A1_STRONG_CONTINUATION_VOLUME_THRESHOLD = 3.0
NEAR_HIGH_DISTANCE_THRESHOLD_PCT = -2.5
SPREAD_PENALTY_THRESHOLD_BPS = 30
EXTREME_SPREAD_THRESHOLD_BPS = SPREAD_PENALTY_THRESHOLD_BPS * 2
BASIS_POINTS_MULTIPLIER = 10000
LOW_VOLUME_RATIO_THRESHOLD = 1.0
LOW_DAY_VOLUME_THRESHOLD = 400_000
DIFFICULT_EXECUTION_VOLUME_THRESHOLD = 800_000
NORDIC_LIQUIDITY_SUFFIXES: tuple[str, ...] = (".OL", ".ST", ".CO", ".HE")
NEXT_ACTIONS: tuple[str, ...] = (
    "SET BREAKOUT ALERT",
    "SET PULLBACK ALERT",
    "WATCH ONLY",
    "WAIT FOR VWAP RECLAIM",
    "REMOVE FROM FOCUS",
    "DO NOT CHASE",
)
PERSONAL_THEME_CATEGORIES: tuple[str, ...] = (
    "AI / Datacenter",
    "Semiconductors",
    "Cybersecurity",
    "Crypto miners",
    "Space / Aerospace",
)
ADJACENT_THEME_KEYWORDS: tuple[str, ...] = (
    "ai software",
    "ai compute",
    "ai infrastructure",
    "power infrastructure",
    "hpc / compute infrastructure",
    "cloud",
)
CLOSED_MARKET_WARNING = "This is not a live intraday signal. Use only as a pre-market/watchlist preparation run."
MARKET_HOURS_BY_MARKET: dict[str, tuple[str, int, int, int, int]] = {
    "usa": ("America/New_York", 9, 30, 16, 0),
    "nordic": ("Europe/Oslo", 9, 0, 16, 30),
}

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
OPTIONAL_YAHOO_SOURCES: tuple[str, ...] = (
    "yahoo-trending",
    "yahoo-unusual-volume",
    "yahoo-high-beta",
    "yahoo-52-week-gainers",
    "yahoo-all-time-high",
)
DISABLED_YAHOO_SOURCES_BY_DEFAULT: tuple[str, ...] = OPTIONAL_YAHOO_SOURCES

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
STALE_YAHOO_TICKERS = {"CTRA"}

DEFAULT_MIN_PRICE = 2.0
DEFAULT_MIN_MARKET_CAP = 500_000_000.0
DEFAULT_MIN_VOLUME = 1_000_000.0
REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_PORTFOLIO_PATH = REPO_ROOT / "config" / "portfolio.yaml"
DEFAULT_DATA_SOURCES_CONFIG_PATH = REPO_ROOT / "config" / "data_sources.yaml"
DEFAULT_WATCHLISTS_DIR = REPO_ROOT / "watchlists"
DEFAULT_TRADE_JOURNAL_PATH = REPO_ROOT / "data" / "trade_journal.csv"
DEFAULT_RECOMMENDATION_LOG_PATH = DEFAULT_LOG_PATH
DEFAULT_PERFORMANCE_OUTPUT_DIR = REPO_ROOT / "reports" / "performance"
DEFAULT_DATA_QUALITY_OUTPUT_DIR = REPO_ROOT / "reports" / "data_quality"
PORTFOLIO_CONCENTRATION_WARNING_THRESHOLD = 2
DEFAULT_REPORT_VALUES: dict[str, Any] = {
    "classification": "",
    "score": 0,
    "reasons": "",
    "day_change_pct": 0,
    "day_change_source": "",
}

logger = logging.getLogger(__name__)


@dataclass
class WatchlistItem:
    ticker: str
    category: str = ""
    news: bool = False
    sector_strength: bool = False
    sources: list[str] = field(default_factory=list)


def load_data_sources_config(path: Path = DEFAULT_DATA_SOURCES_CONFIG_PATH) -> dict[str, str]:
    config: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                config[key.strip()] = value.strip().strip("'\"")
    except FileNotFoundError:
        return {}
    return config


def resolve_usa_data_provider(
    market: str,
    config_path: Path = DEFAULT_DATA_SOURCES_CONFIG_PATH,
    env: dict[str, str] | None = None,
    provider_override: str | None = None,
) -> tuple[Any, str]:
    normalized_market = str(market).strip().lower()
    yahoo_provider = YahooProvider()
    if normalized_market != "usa":
        return yahoo_provider, "yahoo"

    provider_name = (provider_override or "").strip().lower()
    if not provider_name:
        config = load_data_sources_config(config_path)
        provider_name = config.get("usa_data_provider", "yahoo").strip().lower() or "yahoo"
    if provider_name not in {"yahoo", "alpaca"}:
        logger.warning("Unknown usa_data_provider '%s', falling back to Yahoo", provider_name)
        return yahoo_provider, "yahoo"
    if provider_name == "yahoo":
        return yahoo_provider, "yahoo"

    credentials = resolve_alpaca_credentials(env)
    if not credentials.is_configured:
        logger.info("Alpaca provider requested but missing credentials; falling back to Yahoo data provider.")
        return yahoo_provider, "yahoo-fallback-missing-credentials"

    try:
        provider = AlpacaProvider(api_key=credentials.api_key, secret_key=credentials.secret_key)
    except Exception as exc:
        logger.warning("Unable to initialize Alpaca provider (%s); falling back to Yahoo.", exc)
        return yahoo_provider, "yahoo-fallback-provider-init"
    return provider, "alpaca"


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


def ensure_report_defaults(df: pd.DataFrame) -> pd.DataFrame:
    missing = {key: value for key, value in DEFAULT_REPORT_VALUES.items() if key not in df.columns}
    if not missing:
        return df
    return df.assign(**missing)


def summarize_ticker_errors(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Return deduplicated error messages per ticker from raw row dictionaries."""
    summary: dict[str, set[str]] = {}
    for row in rows:
        ticker = str(row.get("ticker", "")).strip().upper()
        error = str(row.get("error", "")).strip()
        if not ticker or not error:
            continue
        if ticker not in summary:
            summary[ticker] = set()
        summary[ticker].add(error)
    return {ticker: sorted(errors) for ticker, errors in sorted(summary.items())}


def log_unavailable_screener(source_key: str, exc: RuntimeError) -> None:
    if source_key in OPTIONAL_YAHOO_SOURCES:
        logger.debug("Optional screener %s unavailable, skipping: %s", source_key, exc)
        return
    logger.warning("%s unavailable, skipping", source_key)
    logger.debug("  Detail: %s", exc)


def _watchlist_items_from_frame(df: pd.DataFrame) -> list[WatchlistItem]:
    if "ticker" not in df.columns:
        raise ValueError("Watchlist must have a ticker column")

    items: list[WatchlistItem] = []
    seen_tickers: set[str] = set()
    for _, row in df.fillna("").iterrows():
        ticker = str(row["ticker"]).strip().upper()
        ticker = TICKER_REPLACEMENTS.get(ticker, ticker)
        if ticker in EXCLUDED_TICKERS:
            continue
        if not ticker:
            continue
        if ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)
        items.append(
            WatchlistItem(
                ticker=ticker,
                category=str(row.get("category") or row.get("theme") or "").strip(),
                news=as_bool(row.get("news", False)),
                sector_strength=as_bool(row.get("sector_strength", False)),
            )
        )
    return items


def load_watchlist(path: Path) -> list[WatchlistItem]:
    df = pd.read_csv(path)
    return _watchlist_items_from_frame(df)


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


def fetch_yahoo_group_with_health(
    source_keys: tuple[str, ...],
    limit: int,
    *,
    disabled_sources: set[str] | None = None,
    fallback_behavior: str = "Unavailable Yahoo screeners are skipped; run continues with successful sources.",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch from a group of Yahoo screeners, combine and deduplicate with health metadata."""
    seen: dict[str, dict[str, Any]] = {}
    any_success = False
    disabled = {source for source in (disabled_sources or set()) if source in source_keys}
    enabled_sources = [source for source in source_keys if source not in disabled]
    errors: list[str] = []
    successful_sources: list[str] = []
    unavailable_sources: list[str] = []
    ticker_counts: dict[str, int] = {}
    duplicate_hits = 0

    for source_key in enabled_sources:
        label = YAHOO_SCREENER_LABEL[source_key]
        try:
            entries = fetch_yahoo_screener(source_key, limit)
            any_success = True
            successful_sources.append(source_key)
            ticker_counts[source_key] = len(entries)
        except RuntimeError as exc:
            unavailable_sources.append(source_key)
            log_unavailable_screener(source_key, exc)
            errors.append(str(exc))
            continue

        for entry in entries:
            ticker = entry["ticker"]
            if ticker in seen:
                duplicate_hits += 1
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
        all_optional = bool(enabled_sources) and all(source in OPTIONAL_YAHOO_SOURCES for source in enabled_sources)
        if all_optional:
            logger.info("Optional Yahoo screeners unavailable; continuing with empty ticker set.")
        else:
            logger.warning("All enabled Yahoo screener fetches failed; continuing with empty ticker set.")
        for err in errors[:3]:
            logger.debug("  %s", err)

    health_report = {
        "enabled_screeners": enabled_sources,
        "successful_screeners": successful_sources,
        "unavailable_screeners": unavailable_sources,
        "disabled_screeners": sorted(disabled),
        "fallback_behavior": fallback_behavior,
        "tickers_returned_per_screener": ticker_counts,
        "duplicate_tickers_removed": duplicate_hits,
    }
    return list(seen.values()), health_report


def fetch_yahoo_group(source_keys: tuple[str, ...], limit: int) -> list[dict[str, Any]]:
    entries, _ = fetch_yahoo_group_with_health(source_keys, limit)
    return entries


def write_screener_health_report(report: dict[str, Any], output_dir: Path = DEFAULT_DATA_QUALITY_OUTPUT_DIR) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    md_path = output_dir / f"screener_health_{stamp}.md"
    json_path = output_dir / f"screener_health_{stamp}.json"
    summary_lines = [
        f"# Screener Health ({stamp})",
        "",
        f"- Enabled screeners: {', '.join(report.get('enabled_screeners', [])) or '(none)'}",
        f"- Successful screeners: {', '.join(report.get('successful_screeners', [])) or '(none)'}",
        f"- Unavailable screeners: {', '.join(report.get('unavailable_screeners', [])) or '(none)'}",
        f"- Disabled screeners: {', '.join(report.get('disabled_screeners', [])) or '(none)'}",
        f"- Fallback behavior: {report.get('fallback_behavior', 'n/a')}",
        f"- Duplicate tickers removed: {report.get('duplicate_tickers_removed', 0)}",
        f"- Stale tickers filtered: {', '.join(report.get('stale_tickers_filtered', [])) or '(none)'}",
        "",
        "## Ticker count per screener",
    ]
    counts = report.get("tickers_returned_per_screener", {})
    if counts:
        for screener_name, count in counts.items():
            summary_lines.append(f"- {screener_name}: {count}")
    else:
        summary_lines.append("- (none)")
    failed_tickers = report.get("failed_tickers", [])
    summary_lines.extend(["", "## Failed tickers"])
    if failed_tickers:
        for item in failed_tickers:
            ticker = str(item.get("ticker", "")).strip()
            errors = [str(err).strip() for err in item.get("errors", []) if str(err).strip()]
            if ticker and errors:
                summary_lines.append(f"- {ticker}: {' | '.join(errors)}")
    else:
        summary_lines.append("- (none)")
    md_path.write_text("\n".join(summary_lines).strip() + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


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
        if ticker in EXCLUDED_TICKERS or ticker in STALE_YAHOO_TICKERS:
            logger.debug("Skipping %s: excluded stale ticker", ticker)
            continue

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


def score_stock(
    item: WatchlistItem,
    *,
    market_data_provider: Any | None = None,
    yahoo_provider: YahooProvider | None = None,
) -> dict[str, Any]:
    yahoo_data = yahoo_provider or YahooProvider()
    provider = market_data_provider or yahoo_data
    stock = yahoo_data.ticker(item.ticker)
    intraday = provider.get_intraday_bars(item.ticker, interval="1m", prepost=True)
    if intraday.empty:
        intraday = provider.get_intraday_bars(item.ticker, interval="5m", prepost=True)
    if intraday.empty and provider is not yahoo_data:
        intraday = yahoo_data.get_intraday_bars(item.ticker, interval="1m", prepost=True)
        if intraday.empty:
            intraday = yahoo_data.get_intraday_bars(item.ticker, interval="5m", prepost=True)

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

    info = yahoo_data.get_info(item.ticker)
    fast_info = yahoo_data.get_fast_info(item.ticker)
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

    monthly = provider.get_historical_bars(item.ticker, period="1mo", interval="1d")
    daily_3m = provider.get_historical_bars(item.ticker, period="3mo", interval="1d")
    if provider is not yahoo_data:
        if monthly.empty:
            monthly = yahoo_data.get_historical_bars(item.ticker, period="1mo", interval="1d")
        if daily_3m.empty:
            daily_3m = yahoo_data.get_historical_bars(item.ticker, period="3mo", interval="1d")
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

    raw_news = yahoo_data.get_news(item.ticker)
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


def _personal_theme_fit(
    row: dict[str, Any] | pd.Series,
    exposure_categories: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    matches = tuple(category for category in PERSONAL_THEME_CATEGORIES if category in exposure_categories)
    if matches:
        return ("Good fit", matches)

    search_text = " | ".join(
        str(row.get(field, "")) for field in ("sector", "industry", "thematic_tags", "category", "reasons")
    ).lower()
    if any(keyword in search_text for keyword in ADJACENT_THEME_KEYWORDS):
        return ("Medium fit", ())

    return ("Poor fit", ())


def _historical_performance_signal(
    row: dict[str, Any] | pd.Series,
    action_label: str,
    recommendation_rows: list[dict[str, str]],
) -> tuple[int, str]:
    """Return historical edge adjustment and short label based on logged outcomes."""
    if not recommendation_rows:
        return (0, "no history")
    target_classification = str(row.get("setup_bucket", row.get("classification", ""))).strip()
    target_setup = str(row.get("setup", "")).strip()
    target_action = str(action_label).strip()
    relevant: list[float] = []
    fallback: list[float] = []
    for logged in recommendation_rows:
        value = as_float(logged.get("result_same_day_pct"))
        if value is None:
            value = as_float(logged.get("result_1w_pct"))
        if value is None:
            continue
        fallback.append(value)
        if target_classification and str(logged.get("classification", "")).strip() == target_classification:
            relevant.append(value)
            continue
        if target_setup and str(logged.get("setup", "")).strip() == target_setup:
            relevant.append(value)
            continue
        if target_action and str(logged.get("action_label", "")).strip() == target_action:
            relevant.append(value)
    sample = relevant if relevant else fallback
    if not sample:
        return (0, "no history")
    avg_return = sum(sample) / len(sample)
    if avg_return >= 1.5:
        return (2, f"history strong ({avg_return:+.2f}%)")
    if avg_return >= 0.25:
        return (1, f"history mild ({avg_return:+.2f}%)")
    if avg_return <= -1.5:
        return (-2, f"history weak ({avg_return:+.2f}%)")
    if avg_return < 0:
        return (-1, f"history soft ({avg_return:+.2f}%)")
    return (0, f"history flat ({avg_return:+.2f}%)")


def _catalyst_quality(row: dict[str, Any] | pd.Series) -> str:
    """Classify catalyst strength as Strong/Medium/Weak/Technical only/Unknown."""
    headlines = str(row.get("catalyst_headlines", "")).strip()
    sentiment = str(row.get("sentiment_tag", "")).strip()
    earnings_warning = str(row.get("earnings_warning", "")).strip()
    has_context = any(
        str(row.get(field, "")).strip()
        for field in ("sources", "thematic_tags", "sector", "industry")
    )
    if not headlines and not has_context:
        return "Unknown"
    headline_count = len([part for part in headlines.split("|") if part.strip()]) if headlines else 0
    if headline_count >= 2 and sentiment == "Positive":
        return "Strong"
    if headline_count >= 1 and sentiment in {"Positive", "Neutral"} and earnings_warning in {"None", "Watch", ""}:
        return "Medium"
    if headline_count >= 1 and sentiment == "Negative":
        return "Weak"
    if not headlines and has_context:
        return "Technical only"
    if headline_count == 1:
        return "Weak"
    return "Unknown"


def _liquidity_guardrails(
    row: dict[str, Any] | pd.Series,
    market: str,
) -> str:
    """Summarize spread/volume/liquidity execution guardrails for a candidate."""
    warnings: list[str] = []
    spread_bps = as_float(row.get("spread_bps"))
    volume_ratio = as_float(row.get("volume_ratio"))
    day_volume = as_float(row.get("volume"))
    market_cap_tier = str(row.get("market_cap_tier", "")).strip()
    position_size_pct = as_float(row.get("position_size_pct")) or 0.0
    ticker = str(row.get("ticker", "")).strip().upper()
    if spread_bps is not None and spread_bps > SPREAD_PENALTY_THRESHOLD_BPS:
        warnings.append("spread too wide")
    if volume_ratio is not None and volume_ratio < LOW_VOLUME_RATIO_THRESHOLD:
        warnings.append("relative volume too low")
    if day_volume is not None and day_volume < LOW_DAY_VOLUME_THRESHOLD:
        warnings.append("day volume too low")
    is_nordic = _is_nordic_security(ticker, market)
    if is_nordic and market_cap_tier == "Small":
        warnings.append("Nordic small-cap liquidity can be weak")
    if (
        position_size_pct >= 0.03
        and (
            (spread_bps is not None and spread_bps > 25)
            or (day_volume is not None and day_volume < DIFFICULT_EXECUTION_VOLUME_THRESHOLD)
            or str(row.get("float_label", "")) == "Low"
        )
    ):
        warnings.append("position size may be difficult to execute")
    if not warnings:
        return "No material liquidity/slippage guardrails triggered."
    # dict.fromkeys keeps insertion order while removing duplicate warning phrases.
    return "; ".join(dict.fromkeys(warnings))


def _is_nordic_security(ticker: str, market: str) -> bool:
    normalized_ticker = str(ticker).strip().upper()
    if normalized_ticker:
        return normalized_ticker.endswith(NORDIC_LIQUIDITY_SUFFIXES)
    return str(market).strip().lower() == "nordic"


def _confidence_score(
    row: dict[str, Any] | pd.Series,
    regime_report: dict[str, Any],
    action_label: str,
    recommendation_rows: list[dict[str, str]],
) -> tuple[int, str]:
    """Compute a 1-10 confidence score and compact rationale string for a ticker."""
    score = 5
    notes: list[str] = []
    bucket = str(row.get("setup_bucket", row.get("classification", "")))
    volume_ratio = as_float(row.get("volume_ratio")) or 0.0
    last = as_float(row.get("last"))
    vwap = as_float(row.get("vwap"))
    distance_from_high = as_float(row.get("distance_from_high_pct")) or 0.0
    atr_pct = as_float(row.get("atr_pct")) or 0.0
    spread_bps = as_float(row.get("spread_bps"))
    chase_risk = str(row.get("chase_risk", ""))
    day_volume = as_float(row.get("volume"))
    regime = str(regime_report.get("market_regime", "Unknown"))
    _, sector_strength = _primary_sector_strength(_row_exposure_categories(row), regime_report)

    if regime == "Risk-on":
        score += 1
        notes.append("risk-on regime")
    elif regime in {"Risk-off", "Panic"}:
        score -= 1
        notes.append("defensive regime")

    if volume_ratio >= 2:
        score += 1
        notes.append("strong relative volume")
    elif volume_ratio < 1:
        score -= 1
        notes.append("weak relative volume")

    if last is not None and vwap is not None and last > vwap:
        score += 1
    elif last is not None and vwap is not None:
        score -= 1

    if distance_from_high >= -2:
        score += 1
    elif distance_from_high <= -8:
        score -= 1

    if sector_strength == "Strong":
        score += 1
        notes.append("sector strong")
    elif sector_strength == "Weak":
        score -= 1
        notes.append("sector weak")

    if 0 < atr_pct <= 5:
        score += 1
    elif atr_pct >= 8:
        score -= 1

    if spread_bps is not None:
        if spread_bps <= 15:
            score += 1
        elif spread_bps > SPREAD_PENALTY_THRESHOLD_BPS:
            score -= 1
    if day_volume is not None and day_volume < LOW_DAY_VOLUME_THRESHOLD:
        score -= 1

    if bucket == "A1":
        score += 1
    elif bucket == "C-list":
        score -= 2
    elif bucket == "A2":
        score -= 1

    if chase_risk == "High":
        score -= 2
    elif chase_risk == "Medium":
        score -= 1
    else:
        score += 1

    history_points, history_label = _historical_performance_signal(row, action_label, recommendation_rows)
    score += history_points
    notes.append(history_label)

    clamped = max(1, min(10, score))
    return (clamped, ", ".join(notes))


def _action_label(
    row: dict[str, Any] | pd.Series,
    priority_score: int,
    regime_report: dict[str, Any],
    personal_fit_label: str,
) -> str:
    bucket = str(row.get("setup_bucket", row.get("classification", "")))
    chase_risk = str(row.get("chase_risk", ""))
    last = as_float(row.get("last"))
    vwap = as_float(row.get("vwap"))
    spread_bps = as_float(row.get("spread_bps"))
    categories = _row_exposure_categories(row)
    _, sector_strength = _primary_sector_strength(categories, regime_report)
    regime = str(regime_report.get("market_regime", "Unknown"))

    if chase_risk == "High" or str(row.get("priority_label", "")) == "Do not chase":
        return "DO NOT CHASE"
    if bucket == "C-list" or personal_fit_label == "Poor fit":
        return "AVOID"
    if last is not None and vwap is not None and last <= vwap and priority_score < 45:
        return "AVOID"
    if spread_bps is not None and spread_bps > SPREAD_PENALTY_THRESHOLD_BPS * 2:
        return "AVOID"
    if regime in {"Risk-off", "Panic"} and sector_strength == "Weak":
        return "AVOID"
    if bucket == "A2":
        return "WAIT PULLBACK"
    if (
        bucket == "A1"
        and priority_score >= 55
        and personal_fit_label != "Poor fit"
        and last is not None
        and vwap is not None
        and last > vwap
    ):
        return "BUY SETUP"
    return "WATCH"


def _best_next_action(
    row: dict[str, Any] | pd.Series,
    action_label: str,
    confidence_score: int,
) -> str:
    last = as_float(row.get("last"))
    vwap = as_float(row.get("vwap"))
    setup = str(row.get("setup", "")).strip().lower()
    chase_risk = str(row.get("chase_risk", "")).strip()
    spread_bps = as_float(row.get("spread_bps"))
    bucket = str(row.get("setup_bucket", row.get("classification", ""))).strip()
    personal_fit_label = str(row.get("personal_fit_label", "")).strip()
    day_change_pct = as_float(row.get("day_change_pct"))
    is_red_name = day_change_pct is not None and day_change_pct < 0
    near_high = as_float(row.get("distance_from_high_pct"))
    is_near_high = near_high is not None and near_high >= NEAR_HIGH_DISTANCE_THRESHOLD_PCT
    is_green = day_change_pct is not None and day_change_pct > 0
    is_above_vwap = last is not None and vwap is not None and last > vwap
    is_a1_strength = bucket == "A1" and is_green and is_above_vwap and is_near_high
    # Extended/parabolic setups are treated as chase-risk by default.
    if chase_risk == "High" or action_label == "DO NOT CHASE" or setup == "extended/parabolic":
        return "DO NOT CHASE"
    if is_a1_strength:
        if setup == "breakout":
            return "SET BREAKOUT ALERT"
        return "WATCH ONLY"
    if confidence_score <= 2:
        return "REMOVE FROM FOCUS"
    if action_label == "AVOID":
        if (
            bucket == "C-list"
            or personal_fit_label == "Poor fit"
            or setup == "reversal"
            or (
                last is not None
                and vwap is not None
                and last <= vwap
                and confidence_score <= 3
                and not is_a1_strength
            )
        ):
            return "REMOVE FROM FOCUS"
    if setup in {"pullback", "continuation"}:
        if last is not None and vwap is not None and last <= vwap:
            return "WAIT FOR VWAP RECLAIM"
        if confidence_score >= 4:
            return "SET PULLBACK ALERT"
        return "WATCH ONLY"
    is_interesting_red_reclaim = (
        is_red_name
        and last is not None
        and vwap is not None
        and last <= vwap
        and confidence_score >= 4
        and bucket != "C-list"
    )
    if is_interesting_red_reclaim:
        return "WAIT FOR VWAP RECLAIM"
    if spread_bps is not None and spread_bps > EXTREME_SPREAD_THRESHOLD_BPS:
        return "WATCH ONLY"
    if setup == "breakout" and confidence_score >= 6:
        return "SET BREAKOUT ALERT"
    return "WATCH ONLY"


def _is_red_c_list_or_reversal(row: dict[str, Any] | pd.Series) -> bool:
    day_change_pct = as_float(row.get("day_change_pct"))
    reasons = str(row.get("reasons", "")).strip().lower()
    is_red_name = day_change_pct is not None and day_change_pct < 0
    if not is_red_name:
        is_red_name = "red" in {bit.strip() for bit in reasons.split(",") if bit.strip()}
    setup = str(row.get("setup", "")).strip().lower()
    setup_bucket = str(row.get("setup_bucket", row.get("classification", ""))).strip()
    classification = str(row.get("classification", "")).strip()
    return is_red_name and (setup == "reversal" or setup_bucket == "C-list" or classification == "C-list")


def _apply_confidence_caps(
    row: dict[str, Any] | pd.Series,
    confidence_score: int,
    action_label: str,
    next_action: str,
) -> int:
    capped = confidence_score
    if next_action == "REMOVE FROM FOCUS":
        capped = min(capped, 6)
    if action_label == "DO NOT CHASE":
        capped = min(capped, 5)
    if _is_red_c_list_or_reversal(row):
        capped = min(capped, 4)
    return capped


def _alert_levels(row: dict[str, Any] | pd.Series) -> dict[str, str]:
    entry_low = _display_number(row.get("preferred_entry_low"), decimals=2)
    entry_high = _display_number(row.get("preferred_entry_high"), decimals=2)
    breakout = _display_number(row.get("breakout_level"), decimals=2)
    stop = _display_number(row.get("stop_level"), decimals=2)
    invalidation = _display_number(row.get("invalidation_level"), decimals=2)
    target_1 = _display_number(row.get("target_1"), decimals=2)
    target_2 = _display_number(row.get("target_2"), decimals=2)
    pullback_alert = f"{entry_low}–{entry_high}" if entry_low != "n/a" and entry_high != "n/a" else "n/a"
    risk_level = stop if stop != "n/a" else invalidation
    if risk_level != "n/a":
        risk_level = f"below {risk_level}"
    target_alert = target_1
    if target_1 != "n/a" and target_2 != "n/a":
        target_alert = f"{target_1} (stretch {target_2})"
    return {
        "pullback_alert": pullback_alert,
        "breakout_alert": breakout,
        "risk_alert": risk_level,
        "target_alert": target_alert,
    }


def _why_this_stock(
    row: dict[str, Any] | pd.Series,
    regime_report: dict[str, Any],
    personal_fit_label: str,
    personal_fit_matches: tuple[str, ...],
) -> str:
    bits: list[str] = []
    score = safe_int(row.get("score", 0))
    bucket = _display_text(row.get("setup_bucket"), _display_text(row.get("classification"), "n/a"))
    bits.append(f"{bucket} score {score}")

    volume_ratio = as_float(row.get("volume_ratio"))
    if volume_ratio is not None and volume_ratio > 0:
        bits.append(f"{volume_ratio:.1f}x relative volume")

    last = as_float(row.get("last"))
    vwap = as_float(row.get("vwap"))
    if last is not None and vwap is not None:
        bits.append("above VWAP" if last > vwap else "testing VWAP")

    distance_from_high = as_float(row.get("distance_from_high_pct"))
    if distance_from_high is not None:
        bits.append(f"{distance_from_high:.1f}% from high")

    category, sector_strength = _primary_sector_strength(_row_exposure_categories(row), regime_report)
    if sector_strength == "Strong":
        bits.append(f"{category.lower()} leadership is strong")
    elif sector_strength == "Weak":
        bits.append(f"{category.lower()} is lagging")

    if personal_fit_matches:
        bits.append(f"{personal_fit_label.lower()} for {', '.join(personal_fit_matches)}")
    else:
        bits.append(personal_fit_label.lower())

    return ", ".join(bits)


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
) -> tuple[int, tuple[str, ...], tuple[str, ...], str, tuple[str, ...]]:
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
    # Overlap is retained as warning metadata for the brief, not as a ranking penalty.
    _, sector_strength = _primary_sector_strength(categories, regime_report)
    personal_fit_label, personal_fit_matches = _personal_theme_fit(row, categories)

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

    if personal_fit_label == "Good fit":
        score += 10
    elif personal_fit_label == "Medium fit":
        score += 4
    else:
        score -= 6

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

    return (max(score, 0), categories, overlap, personal_fit_label, personal_fit_matches)


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
    *,
    market: str = "usa",
    recommendation_log_path: Path = DEFAULT_RECOMMENDATION_LOG_PATH,
) -> pd.DataFrame:
    if df.empty:
        return df

    holdings_by_category = build_exposure_summary(portfolio_holdings)
    concentrated_categories = {
        category for category, names in holdings_by_category.items() if len(names) >= PORTFOLIO_CONCENTRATION_WARNING_THRESHOLD
    }
    recommendation_rows = load_recommendation_log(recommendation_log_path)

    extras: list[dict[str, Any]] = []
    for row in df.to_dict(orient="records"):
        priority_score, categories, overlap, personal_fit_label, personal_fit_matches = _priority_score(
            row, regime_report, concentrated_categories
        )
        trigger_rules = _build_trigger_rules(row, regime_report)
        action_label = _action_label(row, priority_score, regime_report, personal_fit_label)
        confidence_score, confidence_notes = _confidence_score(row, regime_report, action_label, recommendation_rows)
        decision_row = {**row, "personal_fit_label": personal_fit_label}
        next_action = _best_next_action(decision_row, action_label, confidence_score)
        confidence_score = _apply_confidence_caps(decision_row, confidence_score, action_label, next_action)
        catalyst_quality = _catalyst_quality(row)
        liquidity_guardrails = _liquidity_guardrails(row, market)
        alert_levels = _alert_levels(row)
        extras.append(
            {
                "priority_score": priority_score,
                "priority_label": _priority_label(row, priority_score),
                "action_label": action_label,
                "next_action": next_action,
                "confidence_score": confidence_score,
                "confidence_notes": confidence_notes,
                "catalyst_quality": catalyst_quality,
                "liquidity_guardrails": liquidity_guardrails,
                "exposure_categories": ", ".join(categories) if categories else "None",
                "portfolio_overlap": ", ".join(overlap) if overlap else "",
                "personal_fit_label": personal_fit_label,
                "personal_fit_themes": ", ".join(personal_fit_matches) if personal_fit_matches else "",
                "why_this_stock": _why_this_stock(row, regime_report, personal_fit_label, personal_fit_matches),
                **trigger_rules,
                **alert_levels,
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


def _market_display_name(market: str) -> str:
    labels = {"usa": "USA", "nordic": "Nordic", "global": "Global"}
    return labels.get(str(market).strip().lower(), str(market).strip().title() or "Unknown")


def _run_type_display_name(run_type: str) -> str:
    return str(run_type).strip().replace("-", " ").title() or "Unknown"


def _tracking_status_message(run_type: str) -> str:
    if run_type == "open":
        return "This run will be logged as market-open recommendations."
    if run_type == "midday":
        return "This run is an intraday re-scan and does not create a new open snapshot."
    if run_type == "close":
        return "This run is intended for end-of-day result checks."
    return "This run is a manual scan and will not be auto-logged unless run at market open."


def _market_is_open_for_market(now_utc: datetime, market: str) -> tuple[bool, bool]:
    tz_name, open_hour, open_minute, close_hour, close_minute = MARKET_HOURS_BY_MARKET.get(
        str(market).strip().lower(),
        MARKET_HOURS_BY_MARKET["usa"],
    )
    normalized_now_utc = now_utc if now_utc.tzinfo is not None else now_utc.replace(tzinfo=UTC)
    now_local = normalized_now_utc.astimezone(ZoneInfo(tz_name))
    is_weekend = now_local.weekday() >= 5
    if is_weekend:
        return False, True
    minute_of_day = now_local.hour * 60 + now_local.minute
    open_minute_of_day = open_hour * 60 + open_minute
    close_minute_of_day = close_hour * 60 + close_minute
    return open_minute_of_day <= minute_of_day <= close_minute_of_day, False


def _market_session_context(market: str, now_utc: datetime | None = None) -> tuple[str, str, str | None]:
    reference_now_utc = now_utc or datetime.now(UTC)
    normalized_market = str(market).strip().lower()
    if normalized_market == "global":
        usa_open, usa_weekend = _market_is_open_for_market(reference_now_utc, "usa")
        nordic_open, nordic_weekend = _market_is_open_for_market(reference_now_utc, "nordic")
        is_open = usa_open or nordic_open
        is_weekend = usa_weekend and nordic_weekend
    else:
        is_open, is_weekend = _market_is_open_for_market(reference_now_utc, normalized_market)
    if is_open:
        return ("Open / Regular hours", "Live intraday session data", None)
    if is_weekend:
        return ("Closed / Weekend / Outside regular hours", "Latest available session data", CLOSED_MARKET_WARNING)
    return ("Closed / Outside regular hours", "Latest available session data", CLOSED_MARKET_WARNING)


def format_shareable_report(
    df: pd.DataFrame,
    regime_report: dict[str, Any],
    portfolio_holdings: list[str] | None = None,
    market: str = "usa",
    run_type: str = "manual",
    tracking_status: str | None = None,
    intraday_summary: dict[str, Any] | None = None,
    current_time_utc: datetime | None = None,
) -> str:
    reference_time_utc = current_time_utc or datetime.now(UTC)
    if reference_time_utc.tzinfo is None:
        reference_time_utc = reference_time_utc.replace(tzinfo=UTC)
    now = reference_time_utc.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    lines = [f"# Trading Brief ({now})", ""]
    portfolio_holdings = portfolio_holdings or []
    tracking_status = tracking_status or _tracking_status_message(run_type)
    market_status, data_mode, closed_market_warning = _market_session_context(market, reference_time_utc)
    df = ensure_report_defaults(df)
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
            "## Run context",
            f"- Market: {_market_display_name(market)}",
            f"- Run type: {_run_type_display_name(run_type)}",
            f"- Market status: {market_status}",
            f"- Data mode: {data_mode}",
            "",
            "## Recommendation tracking",
            f"- {tracking_status}",
            "- Same-day and 1-week results are tracked in data/recommendation_log.csv.",
            "",
            "## Market regime",
            f"- Market regime: {regime_report.get('market_regime', 'Unknown')}",
            f"- Momentum odds: {regime_report.get('momentum_odds', 'Unknown')}",
            f"- Strong sectors: {', '.join(strong_sectors) if strong_sectors else 'None'}",
            "",
            "## Top Focus Today",
        ]
    )
    if closed_market_warning:
        lines.extend([f"> {closed_market_warning}", ""])

    ranked = df[~has_error].copy()
    if "priority_score" in ranked.columns:
        ranked = ranked.sort_values(by=["priority_score", "score"], ascending=[False, False], na_position="last")
    else:
        ranked = _rank_candidates(ranked)

    focus = ranked.head(5)
    if focus.empty:
        lines.append("- (none)")
    else:
        for idx, (_, row) in enumerate(focus.iterrows(), start=1):
            last_value = as_float(row.get("last"))
            vwap_value = as_float(row.get("vwap"))
            vwap_status = "n/a"
            if last_value is not None and vwap_value is not None:
                vwap_status = "above" if last_value > vwap_value else "below"
            lines.extend(
                [
                    f"### {idx}. {row.get('ticker', '')} — {_display_text(row.get('action_label'), 'WATCH')}",
                    (
                        f"- Snapshot: {_display_text(row.get('setup_bucket'), 'n/a')} | "
                        f"priority {_display_number(row.get('priority_score'))} | "
                        f"confidence {_display_number(row.get('confidence_score'), decimals=0, suffix='/10')} | "
                        f"next action {_display_text(row.get('next_action'), 'WATCH ONLY')} | "
                        f"personal fit {_display_text(row.get('personal_fit_label'), 'n/a')} | "
                        f"rel vol {_display_number(row.get('volume_ratio'), decimals=1, suffix='x')} | "
                        f"VWAP {vwap_status} | "
                        f"distance from high {_display_number(row.get('distance_from_high_pct'), decimals=2, suffix='%')} | "
                        f"chase {_display_text(row.get('chase_risk'), 'n/a')} | "
                        f"spread {_display_number(row.get('spread_bps'), decimals=0, suffix=' bps')}"
                    ),
                    f"- Why this stock? {_display_text(row.get('why_this_stock'), 'n/a')}",
                    f"- Catalyst quality: {_display_text(row.get('catalyst_quality'), 'Unknown')}",
                    f"- Liquidity guardrails: {_display_text(row.get('liquidity_guardrails'), 'n/a')}",
                    (
                        f"- Nordnet alerts: pullback {_display_text(row.get('pullback_alert'), 'n/a')} | "
                        f"breakout {_display_text(row.get('breakout_alert'), 'n/a')} | "
                        f"risk/stop {_display_text(row.get('risk_alert'), 'n/a')} | "
                        f"target {_display_text(row.get('target_alert'), 'n/a')}"
                    ),
                    f"- Buy only if: {_display_text(row.get('buy_trigger'), 'n/a')}",
                    f"- Avoid if: {_display_text(row.get('avoid_trigger'), 'n/a')}",
                    "",
                ]
            )
    lines.append("")

    lines.append("## Portfolio warning")
    warnings = build_portfolio_warnings(focus, portfolio_holdings)
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- No concentrated overlap detected.")
    lines.append("")

    if run_type == "midday":
        lines.append("## Intraday re-scan")
        if intraday_summary:
            lines.append("Previous focus list:")
            previous_focus = intraday_summary.get("previous_focus", [])
            if previous_focus:
                for item in previous_focus:
                    lines.append(f"- {item.get('ticker', '')}: {item.get('status', '')}")
            else:
                lines.append("- No open snapshot found for this market/date.")
            lines.append("")
            lines.append("New candidates:")
            new_movers = intraday_summary.get("new_movers", [])
            if new_movers:
                for ticker in new_movers:
                    lines.append(f"- {ticker}")
            else:
                lines.append("- No new candidates yet.")
            lines.append("")
            lines.append("Updated Nordnet alert levels:")
            alerts = intraday_summary.get("updated_alerts", [])
            if alerts:
                for item in alerts:
                    lines.append(
                        f"- {item.get('ticker', '')}: pullback {item.get('pullback_alert', 'n/a') or 'n/a'} | "
                        f"breakout {item.get('breakout_alert', 'n/a') or 'n/a'} | "
                        f"risk {item.get('risk_alert', 'n/a') or 'n/a'} | "
                        f"target {item.get('target_alert', 'n/a') or 'n/a'}"
                    )
            else:
                lines.append("- No alert updates available.")
        else:
            lines.append("- No intraday comparison available yet.")
        lines.append("")

    lines.append("## Journal reminder")
    lines.append(
        "- If a trade is taken, log ticker, entry, stop, target, result, reason, and whether the plan was followed "
        "in data/trade_journal.csv."
    )
    lines.append("- Review the journal with `python performance_review.py`.")
    lines.append("- Review recommendation outcomes with `python recommendation_tracker.py --mode same-day` and `--mode 1w`.")
    lines.append("")

    return "\n".join(lines).strip() + "\n"


def format_markdown_report(df: pd.DataFrame) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# Daily Momentum Report ({now})", ""]

    df = ensure_report_defaults(df)
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
                f"  - Decision support: confidence {_display_number(row.get('confidence_score'), decimals=0, suffix='/10')} | "
                f"next action {_display_text(row.get('next_action'), 'WATCH ONLY')} | "
                f"catalyst {_display_text(row.get('catalyst_quality'), 'Unknown')}"
            )
            lines.append(
                f"  - Risk: {row.get('risk', 'n/a')} | Chase risk: {row.get('chase_risk', 'n/a')} | "
                f"Position size: {_display_position_size(row.get('position_size_pct'))} | Hold: {row.get('suggested_hold', 'n/a')}"
            )
            lines.append(f"  - Liquidity guardrails: {_display_text(row.get('liquidity_guardrails'), 'n/a')}")
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
        error_rows = df[df["error"].astype(str).str.strip() != ""]
        error_records = [
            {"ticker": ticker, "error": error}
            for ticker, error in zip(error_rows.get("ticker", pd.Series(dtype=str)), error_rows["error"], strict=False)
        ]
        errors = summarize_ticker_errors(error_records)
    else:
        errors = {}
    if errors:
        lines.append("## Feil")
        for ticker, ticker_errors in errors.items():
            lines.append(f"- {ticker}: {' | '.join(ticker_errors)}")

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
    parser.add_argument(
        "--market",
        default="usa",
        choices=["usa", "nordic", "global"],
        help="Market label for reports and workflow routing",
    )
    parser.add_argument(
        "--run-type",
        default="manual",
        choices=["open", "midday", "close", "manual"],
        help="Run type for recommendation tracking and intraday reports",
    )
    parser.add_argument(
        "--performance-outdir",
        default=str(DEFAULT_PERFORMANCE_OUTPUT_DIR),
        help="Output directory for recommendation/performance artifacts",
    )
    parser.add_argument(
        "--recommendation-log",
        default=str(DEFAULT_RECOMMENDATION_LOG_PATH),
        help="Recommendation log CSV path",
    )
    parser.add_argument(
        "--nordic-universe",
        default="large_caps",
        choices=["large_caps", "momentum", "norway", "sweden", "denmark", "finland", "small_caps", "all"],
        help="Nordic universe selector when market is nordic/global and source is watchlist",
    )
    parser.add_argument(
        "--data-sources-config",
        default=str(DEFAULT_DATA_SOURCES_CONFIG_PATH),
        help="Path to data source config file (usa_data_provider)",
    )
    parser.add_argument(
        "--usa-data-provider",
        default="auto",
        choices=["auto", "yahoo", "alpaca"],
        help="Override USA market data provider for this run (default: auto -> config value)",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    args = parse_args()
    outdir = Path(args.outdir)
    performance_outdir = Path(args.performance_outdir)
    recommendation_log_path = Path(args.recommendation_log)
    data_sources_config_path = Path(args.data_sources_config)
    outdir.mkdir(parents=True, exist_ok=True)
    performance_outdir.mkdir(parents=True, exist_ok=True)

    source = args.source
    screener_health: dict[str, Any] | None = None
    requested_usa_provider = None if args.usa_data_provider == "auto" else args.usa_data_provider
    market_data_provider, provider_resolution = resolve_usa_data_provider(
        args.market,
        data_sources_config_path,
        provider_override=requested_usa_provider,
    )
    yahoo_data_provider = YahooProvider()

    if source == "watchlist":
        if args.market in {"nordic", "global"} and Path(args.input).name == "watchlist.csv":
            watchlist = _watchlist_items_from_frame(load_nordic_universe(args.nordic_universe, DEFAULT_WATCHLISTS_DIR))
        else:
            watchlist = load_watchlist(Path(args.input))
    else:
        # Fetch from Yahoo screener(s)
        _GROUP_SOURCES: dict[str, tuple[str, ...]] = {
            "yahoo-all": YAHOO_MOMENTUM_SOURCES,
            "yahoo-momentum": YAHOO_MOMENTUM_SOURCES,
            "yahoo-expanded": YAHOO_EXPANDED_SOURCES,
        }
        if source in _GROUP_SOURCES:
            entries, screener_health = fetch_yahoo_group_with_health(
                _GROUP_SOURCES[source],
                args.limit,
                disabled_sources=set(DISABLED_YAHOO_SOURCES_BY_DEFAULT),
                fallback_behavior=(
                    "Unavailable optional Yahoo screeners are skipped silently. "
                    "If none succeed, run continues with an empty list."
                ),
            )
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

        stale_filtered = sorted(
            {
                str(entry.get("ticker", "")).strip().upper()
                for entry in entries
                if str(entry.get("ticker", "")).strip().upper() in STALE_YAHOO_TICKERS
            }
        )
        entries = apply_filters(
            entries,
            min_price=args.min_price,
            min_market_cap=args.min_market_cap,
            min_volume=args.min_volume,
        )
        if screener_health is not None:
            if stale_filtered:
                screener_health["stale_tickers_filtered"] = stale_filtered

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
            rows.append(
                score_stock(
                    item,
                    market_data_provider=market_data_provider,
                    yahoo_provider=yahoo_data_provider,
                )
            )
        except Exception as exc:  # pragma: no cover
            rows.append({"ticker": item.ticker, "category": item.category, "error": str(exc)})

    error_summary = summarize_ticker_errors(rows)
    if screener_health is not None and error_summary:
        screener_health["failed_tickers"] = [
            {"ticker": ticker, "errors": errors} for ticker, errors in error_summary.items()
        ]

    df = pd.DataFrame(rows)
    df = ensure_report_defaults(df)
    if "score" in df.columns:
        df = df.sort_values(by="score", ascending=False, na_position="last")

    df = enrich_with_strategy(df.fillna(""))
    df["setup_bucket"] = compute_setup_bucket(df)
    portfolio_holdings = load_portfolio_config()
    try:
        regime_report = build_regime_report()
    except Exception:
        regime_report = _build_market_regime_fallback()
    df = enrich_with_intraday_assistant(
        df,
        regime_report,
        portfolio_holdings,
        market=args.market,
        recommendation_log_path=recommendation_log_path,
    )

    run_type = args.run_type
    market = args.market
    run_id = os.environ.get("GITHUB_RUN_ID") or f"local-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}"
    tracking_status = _tracking_status_message(run_type)
    intraday_summary: dict[str, Any] | None = None

    if run_type == "open" and market in {"usa", "nordic"}:
        snapshot_recommendations(
            df,
            market=market,
            run_id=run_id,
            output_dir=performance_outdir,
            log_path=recommendation_log_path,
            recommendation_time=datetime.now(UTC),
            run_type=run_type,
        )
    elif run_type == "midday" and market in {"usa", "nordic"}:
        intraday_summary = build_intraday_summary(
            df,
            market=market,
            log_path=recommendation_log_path,
        )
        write_intraday_report(intraday_summary, output_dir=REPO_ROOT / "reports" / "intraday")

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
    brief_path.write_text(
        format_shareable_report(
            df,
            regime_report,
            portfolio_holdings,
            market=market,
            run_type=run_type,
            tracking_status=tracking_status,
            intraday_summary=intraday_summary,
        ),
        encoding="utf-8",
    )
    if screener_health is not None:
        screener_health["market"] = args.market
        screener_health["source"] = source
        screener_health["provider_resolution"] = provider_resolution
        if requested_usa_provider:
            screener_health["requested_usa_data_provider"] = requested_usa_provider
        health_md, health_json = write_screener_health_report(screener_health)
        print(f"Saved screener health report: {health_md}")
        print(f"Saved screener health JSON: {health_json}")

    print(report)
    print(f"Saved CSV report: {csv_path}")
    print(f"Saved Markdown report: {md_path}")
    print(f"Saved JSON report: {json_path}")
    print(f"Saved shareable trading brief: {brief_path}")


if __name__ == "__main__":
    main()
