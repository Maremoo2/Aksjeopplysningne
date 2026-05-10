from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import performance_review
import recommendation_tracker
import screener
from strategy_engine import enrich_with_strategy
from utils.alpaca_credentials import resolve_alpaca_credentials

DEFAULT_OUTPUT_DIR = REPO_ROOT / "reports" / "data_quality"
ALPACA_DATA_SNAPSHOT_URL = "https://data.alpaca.markets/v2/stocks/snapshots"
NORDIC_UNIVERSE_FILES = {
    "large_caps": REPO_ROOT / "watchlists" / "nordic_large_caps.csv",
    "momentum": REPO_ROOT / "watchlists" / "nordic_momentum.csv",
    "norway": REPO_ROOT / "watchlists" / "norway.csv",
    "sweden": REPO_ROOT / "watchlists" / "sweden.csv",
    "denmark": REPO_ROOT / "watchlists" / "denmark.csv",
    "finland": REPO_ROOT / "watchlists" / "finland.csv",
    "small_caps": REPO_ROOT / "watchlists" / "nordic_small_caps.csv",
}
REQUIRED_NORDIC_COLUMNS = {"ticker", "company", "country", "exchange", "theme", "liquidity_tier"}
VALID_NORDIC_SUFFIXES = (".OL", ".ST", ".CO", ".HE")
ALPACA_SAMPLE = ("AAPL", "MSFT", "NVDA")
AVAILABLE_STATUS = {"PASS": 0, "WARN": 1, "FAIL": 2}
_ALPACA_API_KEY_ENV_VARS = ("ALPACA_API_KEY", "ALPACA_KEY")
_ALPACA_SECRET_KEY_ENV_VARS = ("ALPACA_SECRET_KEY", "ALPACA_SECRET")


def _worst_status(statuses: list[str]) -> str:
    return max(statuses or ["PASS"], key=lambda item: AVAILABLE_STATUS[item])


def _new_check(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "status": "PASS",
        "warnings": [],
        "errors": [],
        "details": {},
    }


def _optionalize_fail(check: dict[str, Any], message: str) -> dict[str, Any]:
    if check.get("status") != "FAIL":
        return check
    converted = dict(check)
    warnings = list(converted.get("warnings", []))
    errors = list(converted.get("errors", []))
    warnings.extend(errors)
    warnings.append(message)
    converted["warnings"] = warnings
    converted["errors"] = []
    converted["status"] = "WARN"
    return converted


def _warn(check: dict[str, Any], message: str) -> None:
    check["warnings"].append(message)
    if check["status"] == "PASS":
        check["status"] = "WARN"


def _fail(check: dict[str, Any], message: str) -> None:
    check["errors"].append(message)
    check["status"] = "FAIL"


def _check_yahoo_screeners(limit: int) -> dict[str, Any]:
    check = _new_check("Yahoo screener status")
    per_screener: dict[str, Any] = {}
    deduped: set[str] = set()
    success_count = 0

    for source in screener.YAHOO_EXPANDED_SOURCES:
        try:
            rows = screener.fetch_yahoo_screener(source, limit)
            tickers = sorted({str(row.get("ticker", "")).strip().upper() for row in rows if row.get("ticker")})
            per_screener[source] = {"status": "enabled", "ticker_count": len(tickers)}
            deduped.update(tickers)
            success_count += 1
        except RuntimeError:
            per_screener[source] = {"status": "disabled", "ticker_count": 0}
            _warn(check, f"{source} reported as disabled/unstable")

    if success_count == 0:
        _fail(check, "No Yahoo screeners responded")

    check["details"] = {
        "screeners": per_screener,
        "enabled_screeners": success_count,
        "deduped_ticker_count": len(deduped),
    }
    return check


def _alpaca_credential_env_var_names(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return which env-var names hold Alpaca credentials (checks key presence only, never reads values)."""
    source = env if env is not None else os.environ
    api_var = next((n for n in _ALPACA_API_KEY_ENV_VARS if n in source), "not_configured")
    secret_var = next((n for n in _ALPACA_SECRET_KEY_ENV_VARS if n in source), "not_configured")
    return {"api_key_env_var": api_var, "secret_key_env_var": secret_var}


def _check_alpaca_provider(timeout: int = 15) -> dict[str, Any]:
    check = _new_check("Alpaca provider status")
    credentials = resolve_alpaca_credentials()

    if not credentials.is_configured:
        _warn(check, "Alpaca credentials missing, using Yahoo fallback")
        check["details"]["mode"] = "fallback_to_yahoo"
        return check

    headers = {
        "APCA-API-KEY-ID": credentials.api_key,
        "APCA-API-SECRET-KEY": credentials.secret_key,
    }
    params = {"symbols": ",".join(ALPACA_SAMPLE)}
    check["details"]["endpoint"] = ALPACA_DATA_SNAPSHOT_URL
    check["details"]["tested_symbols"] = list(ALPACA_SAMPLE)

    try:
        response = requests.get(ALPACA_DATA_SNAPSHOT_URL, headers=headers, params=params, timeout=timeout)
        response.raise_for_status()
        payload_data = response.json()
    except Exception:
        _fail(check, "Alpaca data endpoint validation failed: network or HTTP error")
        return check

    snapshots = payload_data.get("snapshots", {}) if isinstance(payload_data, dict) else {}
    # Derive which sample symbols have usable data; the list elements come from ALPACA_SAMPLE (constant).
    available = [symbol for symbol in ALPACA_SAMPLE if isinstance(snapshots.get(symbol), dict) and snapshots.get(symbol)]
    if not available:
        _fail(check, "Alpaca returned no usable snapshots for sample symbols")
        check["details"]["snapshot_keys_found"] = 0
    else:
        check["details"]["snapshot_keys_found"] = len(available)
    return check


def _check_yahoo_fallback() -> dict[str, Any]:
    check = _new_check("Yahoo fallback provider status")
    try:
        data = yf.download(
            tickers=" ".join(ALPACA_SAMPLE),
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=False,
            threads=False,
        )
    except Exception as exc:
        _fail(check, f"Yahoo fallback fetch failed: {type(exc).__name__}")
        check["details"]["error_type"] = type(exc).__name__
        return check

    if data is None or data.empty:
        _fail(check, "Yahoo fallback returned no data for sample symbols")
    check["details"]["tested_symbols"] = list(ALPACA_SAMPLE)
    check["details"]["rows"] = int(len(data.index)) if data is not None else 0
    return check


def _check_nordic_universes() -> dict[str, Any]:
    check = _new_check("Nordic universe status")
    per_file: dict[str, Any] = {}

    for key, file_path in NORDIC_UNIVERSE_FILES.items():
        item = {"status": "PASS", "warnings": [], "errors": [], "row_count": 0}
        if not file_path.exists():
            item["status"] = "FAIL"
            item["errors"].append("File missing")
            _fail(check, f"{file_path.name} is missing")
            per_file[key] = item
            continue

        frame = pd.read_csv(file_path).fillna("")
        item["row_count"] = int(len(frame))
        missing_cols = sorted(REQUIRED_NORDIC_COLUMNS - set(frame.columns))
        if missing_cols:
            item["status"] = "FAIL"
            item["errors"].append(f"Missing required columns: {', '.join(missing_cols)}")
            _fail(check, f"{file_path.name} missing required columns")

        tickers = frame["ticker"].astype(str).str.strip().str.upper() if "ticker" in frame.columns else pd.Series([], dtype=str)
        if tickers.empty or not bool(tickers.replace("", pd.NA).notna().any()):
            item["status"] = "FAIL"
            item["errors"].append("No non-empty tickers")
            _fail(check, f"{file_path.name} has no valid tickers")
        else:
            invalid = sorted({ticker for ticker in tickers if ticker and not ticker.endswith(VALID_NORDIC_SUFFIXES)})
            if invalid:
                item["status"] = "WARN" if item["status"] != "FAIL" else item["status"]
                item["warnings"].append(f"Invalid-looking suffixes: {', '.join(invalid[:5])}")
                _warn(check, f"{file_path.name} has tickers with unexpected suffixes")

            if "NVO.CO" in set(tickers):
                item["status"] = "FAIL"
                item["errors"].append("Found NVO.CO; expected NOVO-B.CO replacement")
                _fail(check, f"{file_path.name} contains NVO.CO")

            has_novo_company = False
            if "company" in frame.columns:
                has_novo_company = frame["company"].astype(str).str.contains("Novo Nordisk", case=False, na=False).any()
            if has_novo_company and "NOVO-B.CO" not in set(tickers):
                item["status"] = "FAIL"
                item["errors"].append("Novo Nordisk present without NOVO-B.CO ticker")
                _fail(check, f"{file_path.name} missing NOVO-B.CO for Novo Nordisk")

        per_file[key] = item

    check["details"]["files"] = per_file
    return check


def _selected_nordic_file(selection: str) -> Path:
    if selection == "all":
        return NORDIC_UNIVERSE_FILES["large_caps"]
    return NORDIC_UNIVERSE_FILES[selection]


def _build_minimal_pipeline_frame(tickers: list[str]) -> pd.DataFrame:
    rows = []
    for index, ticker in enumerate(tickers, start=1):
        rows.append(
            {
                "ticker": ticker,
                "category": "validation",
                "last": 100.0 + index,
                "open": 99.0 + index,
                "high": 101.0 + index,
                "low": 98.0 + index,
                "volume": 1_500_000,
                "avg_volume_20d": 900_000,
                "volume_ratio": 1.8,
                "day_change_pct": 2.5,
                "day_change_source": "validation",
                "previous_close": 98.5 + index,
                "distance_from_high_pct": -1.2,
                "range_position": 0.7,
                "vwap": 100.0 + index,
                "premarket": None,
                "after_hours": None,
                "spread_pct": 0.05,
                "spread_bps": 5.0,
                "market_cap": 50_000_000_000,
                "market_cap_tier": "Large",
                "float_shares": 1_000_000_000,
                "float_label": "High",
                "volatility_risk": "Low",
                "atr_pct": 3.0,
                "atr_volatility": "Medium",
                "premarket_gap_pct": None,
                "premarket_volume": None,
                "earnings_date": "",
                "earnings_warning": "None",
                "sector": "Technology",
                "industry": "Semiconductors",
                "thematic_tags": "Semiconductor, AI Compute",
                "catalyst_headlines": "",
                "sentiment_tag": "Neutral",
                "insider_activity": "N/A (placeholder)",
                "score": 75,
                "classification": "A-list",
                "reasons": "green, above VWAP",
                "sources": "Validation",
            }
        )
    return pd.DataFrame(rows)


def _check_end_to_end_dry_run(usa_provider: str, nordic_universe: str) -> dict[str, Any]:
    check = _new_check("End-to-end dry-run status")
    steps: dict[str, Any] = {}

    usa_tickers: list[str] = []
    if usa_provider == "alpaca":
        alpaca_check = _check_alpaca_provider()
        # When the check passes, use the constant ALPACA_SAMPLE (untainted) as the sample universe.
        usa_tickers = list(ALPACA_SAMPLE) if alpaca_check["status"] != "FAIL" else []
        steps["usa_universe_discovery"] = {
            "provider": "alpaca",
            "status": alpaca_check["status"],
            "count": len(usa_tickers),
        }
        if alpaca_check["status"] == "FAIL":
            _fail(check, "USA universe discovery failed for Alpaca provider")
    if not usa_tickers:
        try:
            entries = screener.fetch_yahoo_group(screener.YAHOO_MOMENTUM_SOURCES, limit=5)
        except Exception:
            entries = []
        usa_tickers = [str(item.get("ticker", "")).strip().upper() for item in entries if item.get("ticker")]
        steps["usa_universe_discovery"] = {
            "provider": "yahoo",
            "status": "PASS" if usa_tickers else "WARN",
            "count": len(usa_tickers),
        }
        if not usa_tickers:
            _warn(check, "USA discovery returned no Yahoo tickers in dry run")

    nordic_file = _selected_nordic_file(nordic_universe)
    try:
        nordic_df = pd.read_csv(nordic_file).fillna("")
        nordic_tickers = [
            str(item).strip().upper()
            for item in nordic_df.get("ticker", pd.Series([], dtype=str)).tolist()
            if str(item).strip()
        ]
    except Exception:
        nordic_tickers = []
    steps["nordic_selected_universe"] = {
        "selection": nordic_universe,
        "file": str(nordic_file.relative_to(REPO_ROOT)),
        "status": "PASS" if nordic_tickers else "FAIL",
        "count": len(nordic_tickers),
    }
    if not nordic_tickers:
        _fail(check, "Nordic selected universe dry run has no tickers")

    global_tickers = list(dict.fromkeys(usa_tickers[:3] + nordic_tickers[:3]))
    steps["global_combined_mode"] = {
        "status": "PASS" if global_tickers else "FAIL",
        "count": len(global_tickers),
    }
    if not global_tickers:
        _fail(check, "Global combined dry run produced no tickers")

    sample_frame = _build_minimal_pipeline_frame(global_tickers[:3] or ["AAPL"])
    strategy_frame = enrich_with_strategy(sample_frame.fillna(""))
    if strategy_frame.empty or "setup" not in strategy_frame.columns:
        _fail(check, "Strategy generation compatibility failed")
        steps["strategy_generation_io"] = {"status": "FAIL"}
    else:
        steps["strategy_generation_io"] = {"status": "PASS", "rows": int(len(strategy_frame))}

    setup_frame = strategy_frame.copy()
    setup_frame["setup_bucket"] = screener.compute_setup_bucket(setup_frame)
    brief = screener.format_shareable_report(setup_frame, {"market_regime": "Unknown", "momentum_odds": "Unknown", "sector_strength": {}}, [])
    if "Trading Brief" not in brief:
        _fail(check, "Shareable brief generation compatibility failed")
        steps["shareable_brief_generation"] = {"status": "FAIL"}
    else:
        steps["shareable_brief_generation"] = {"status": "PASS"}

    recommendation_path = REPO_ROOT / "data" / "recommendation_log.csv"
    recommendation_tracker.ensure_recommendation_log(recommendation_path)
    with recommendation_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or [])
    missing_reco_cols = sorted(set(recommendation_tracker.RECOMMENDATION_FIELDS) - header)
    if missing_reco_cols:
        _fail(check, "Recommendation log schema mismatch")
        steps["recommendation_log_schema"] = {"status": "FAIL", "missing_columns": missing_reco_cols}
    else:
        steps["recommendation_log_schema"] = {"status": "PASS"}

    perf_text, perf_json = performance_review.build_performance_summary([], recommendation_tracker.load_recommendation_log(recommendation_path))
    if "Recommendation performance" not in perf_text or "recommendation_summary" not in perf_json:
        _fail(check, "Performance summary generation compatibility failed")
        steps["performance_summary_generation"] = {"status": "FAIL"}
    else:
        steps["performance_summary_generation"] = {"status": "PASS"}

    check["details"] = steps
    return check


def build_validation_payload(usa_provider: str, nordic_universe: str) -> dict[str, Any]:
    alpaca_check = _check_alpaca_provider()
    if usa_provider != "alpaca":
        alpaca_check = _optionalize_fail(
            alpaca_check,
            "Alpaca validation is optional when usa-data-provider is not alpaca.",
        )
    alpaca_credential_names = _alpaca_credential_env_var_names()
    checks = [
        _check_yahoo_screeners(limit=10),
        alpaca_check,
        _check_yahoo_fallback(),
        _check_nordic_universes(),
        _check_end_to_end_dry_run(usa_provider=usa_provider, nordic_universe=nordic_universe),
    ]
    statuses = [check["status"] for check in checks]
    warnings = [warning for check in checks for warning in check["warnings"]]
    errors = [error for check in checks for error in check["errors"]]
    overall_status = _worst_status(statuses)
    next_steps: list[str] = []
    if overall_status == "PASS":
        next_steps.append("Data connections validated successfully; proceed with screener run.")
    if warnings:
        next_steps.append("Review WARN items and stabilize optional providers/screeners where needed.")
    if errors:
        next_steps.append("Resolve FAIL items before relying on intraday screener output.")
    if not next_steps:
        next_steps.append("No action required.")

    return {
        "generated_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "overall_status": overall_status,
        "usa_provider": usa_provider,
        "alpaca_credential_names": alpaca_credential_names,
        "checks": checks,
        "warnings": warnings,
        "errors": errors,
        "next_steps": next_steps,
    }


def render_validation_markdown(payload: dict[str, Any]) -> str:
    usa_provider = payload.get("usa_provider", "unknown")
    cred_names = payload.get("alpaca_credential_names", {})
    api_key_name = cred_names.get("api_key_env_var", "not_configured")
    secret_key_name = cred_names.get("secret_key_env_var", "not_configured")

    lines = [
        "# Data connection validation",
        "",
        f"- Overall status: **{payload.get('overall_status', 'FAIL')}**",
        "",
        "## Configuration",
        f"- USA data provider: **{usa_provider}**",
        f"- Alpaca API key env var: `{api_key_name}`",
        f"- Alpaca secret key env var: `{secret_key_name}`",
        "",
        "## Component status",
    ]
    for check in payload.get("checks", []):
        lines.append(f"- {check.get('name', 'Unknown')}: **{check.get('status', 'FAIL')}**")
    lines.extend(["", "## Warnings"])
    if payload.get("warnings"):
        for warning in payload["warnings"]:
            lines.append(f"- {warning}")
    else:
        lines.append("- None")
    lines.extend(["", "## Errors"])
    if payload.get("errors"):
        for error in payload["errors"]:
            lines.append(f"- {error}")
    else:
        lines.append("- None")
    lines.extend(["", "## Next steps"])
    for step in payload.get("next_steps", []):
        lines.append(f"- {step}")
    lines.append("")
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate data connections and dry-run compatibility")
    parser.add_argument("--outdir", default=str(DEFAULT_OUTPUT_DIR), help="Output directory for validation artifacts")
    parser.add_argument(
        "--usa-data-provider",
        default="yahoo",
        choices=["yahoo", "alpaca"],
        help="Preferred USA data provider for dry-run validation",
    )
    parser.add_argument(
        "--nordic-universe",
        default="all",
        choices=["large_caps", "momentum", "norway", "sweden", "denmark", "finland", "small_caps", "all"],
        help="Nordic universe selection for dry-run validation",
    )
    parser.add_argument(
        "--allow-fail",
        action="store_true",
        help="Exit with zero status even when overall validation status is FAIL",
    )
    return parser.parse_args()


def resolve_exit_code(overall_status: str, allow_fail: bool = False) -> int:
    if overall_status == "FAIL" and not allow_fail:
        return 1
    return 0


def main() -> None:
    args = parse_args()
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    payload = build_validation_payload(args.usa_data_provider, args.nordic_universe)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M")
    md_path = outdir / f"data_connection_validation_{timestamp}.md"
    json_path = outdir / f"data_connection_validation_{timestamp}.json"

    md_path.write_text(render_validation_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Overall status: {payload['overall_status']}")
    print(f"Saved Markdown report: {md_path}")
    print(f"Saved JSON report: {json_path}")
    raise SystemExit(resolve_exit_code(payload["overall_status"], allow_fail=args.allow_fail))


if __name__ == "__main__":
    main()
