from __future__ import annotations

import argparse
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import screener


class ScreenerTests(unittest.TestCase):
    def test_compute_setup_bucket_moves_extended_a_list_name_to_a2(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "RKLB",
                    "score": 82,
                    "classification": "A-list",
                    "day_change_pct": 32.0,
                    "distance_from_high_pct": -1.1,
                    "volume_ratio": 4.2,
                    "last": 12.4,
                    "vwap": 11.8,
                    "sources": "Top Gainers, Most Active",
                }
            ]
        )
        result = screener.compute_setup_bucket(frame)
        self.assertEqual(result.iloc[0], "A2")

    def test_format_markdown_report_marks_missing_premarket_data_as_unavailable(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "AMD",
                    "score": 80,
                    "classification": "A-list",
                    "setup_bucket": "A1",
                    "setup": "breakout",
                    "sector": "Technology",
                    "industry": "Semiconductors",
                    "thematic_tags": "Semiconductor, AI Compute",
                    "market_cap": 1_000_000_000,
                    "market_cap_tier": "Large",
                    "float_label": "High",
                    "volatility_risk": "Low",
                    "atr_pct": 4.5,
                    "atr_volatility": "Medium",
                    "volume_ratio": 2.3,
                    "distance_from_high_pct": -1.2,
                    "premarket_gap_pct": "",
                    "premarket_volume": "",
                    "earnings_date": "",
                    "earnings_warning": "None",
                    "preferred_entry_low": 99.0,
                    "preferred_entry_high": 100.0,
                    "breakout_level": 101.0,
                    "stop_level": 96.0,
                    "target_1": 104.0,
                    "target_2": 107.0,
                    "risk": "Low",
                    "chase_risk": "Low",
                    "position_size_pct": 0.05,
                    "suggested_hold": "2–5 days",
                    "reasons": "green, above VWAP",
                    "day_change_pct": 4.2,
                    "day_change_source": "previous_close",
                }
            ]
        )

        markdown = screener.format_markdown_report(frame)
        self.assertIn("Premarket: unavailable", markdown)
        self.assertNotIn("Premarket gap: %", markdown)

    def test_main_creates_shareable_trading_brief(self) -> None:
        sample_row = {
            "ticker": "AMD",
            "category": "",
            "last": 100.0,
            "open": 95.0,
            "high": 101.0,
            "low": 94.0,
            "volume": 1000000,
            "avg_volume_20d": 500000,
            "volume_ratio": 2.0,
            "day_change_pct": 5.0,
            "day_change_source": "previous_close",
            "previous_close": 95.2,
            "distance_from_high_pct": -0.99,
            "range_position": 0.8,
            "vwap": 99.0,
            "premarket": None,
            "after_hours": None,
            "spread_pct": 0.1,
            "spread_bps": 10.0,
            "market_cap": 310_000_000_000,
            "market_cap_tier": "Large",
            "float_shares": 200_000_000,
            "float_label": "High",
            "volatility_risk": "Low",
            "atr_pct": 4.5,
            "atr_volatility": "Medium",
            "premarket_gap_pct": None,
            "premarket_volume": None,
            "earnings_date": "2026-05-15",
            "earnings_warning": "Watch",
            "sector": "Technology",
            "industry": "Semiconductors",
            "thematic_tags": "Semiconductor, AI Compute",
            "catalyst_headlines": "",
            "sentiment_tag": "Neutral",
            "insider_activity": "N/A (placeholder)",
            "score": 84,
            "classification": "A-list",
            "reasons": "green, volume > 2x, near high, above VWAP, institutional quality",
            "sources": "Top Gainers, Most Active",
        }
        sample_regime = {
            "market_regime": "Risk-on",
            "momentum_odds": "Favorable",
            "sector_strength": {
                "SOXX": "Strong",
                "AI Software": "Strong",
                "Crypto Miners": "Strong",
                "Cyber": "Neutral",
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_now = datetime(2026, 5, 8, 19, 40)
            with (
                patch.object(
                    screener,
                    "parse_args",
                    return_value=argparse.Namespace(
                        input="watchlist.csv",
                        outdir=temp_dir,
                        source="watchlist",
                        limit=25,
                        min_price=2.0,
                        min_market_cap=500_000_000.0,
                        min_volume=1_000_000.0,
                        market="usa",
                        run_type="open",
                        performance_outdir=temp_dir,
                        recommendation_log=str(Path(temp_dir) / "recommendation_log.csv"),
                    ),
                ),
                patch.object(screener, "load_watchlist", return_value=[screener.WatchlistItem(ticker="AMD")]),
                patch.object(screener, "score_stock", return_value=sample_row),
                patch.object(screener, "build_regime_report", return_value=sample_regime),
                patch.object(screener, "snapshot_recommendations"),
                patch.object(screener, "datetime") as mock_datetime,
            ):
                mock_datetime.now.return_value = fake_now
                screener.main()

            brief_path = Path(temp_dir) / "shareable" / "trading_brief_20260508_1940.md"
            self.assertTrue(brief_path.exists())
            brief = brief_path.read_text(encoding="utf-8")
            self.assertIn("Market: USA", brief)
            self.assertIn("Run type: Open", brief)
            self.assertIn("This run will be logged as market-open recommendations.", brief)
            self.assertIn("Market regime: Risk-on", brief)
            self.assertIn("Strong sectors: Semiconductors, AI Software, Crypto Miners", brief)
            self.assertIn("## Top Focus Today", brief)
            self.assertIn("BUY SETUP", brief)
            self.assertIn("confidence", brief)
            self.assertIn("next action", brief)
            self.assertIn("Catalyst quality:", brief)
            self.assertIn("Liquidity guardrails:", brief)
            self.assertIn("Nordnet alerts:", brief)
            self.assertIn("Why this stock?", brief)
            self.assertIn("## Journal reminder", brief)

    def test_format_shareable_report_includes_portfolio_warning_and_triggers(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "AMD",
                    "score": 84,
                    "classification": "A-list",
                    "setup_bucket": "A1",
                    "setup": "breakout",
                    "last": 100.0,
                    "vwap": 99.0,
                    "preferred_entry_low": 99.0,
                    "preferred_entry_high": 100.0,
                    "breakout_level": 101.0,
                    "invalidation_level": 94.0,
                    "volume_ratio": 2.4,
                    "distance_from_high_pct": -1.2,
                    "day_change_pct": 5.1,
                    "spread_bps": 10.0,
                    "earnings_warning": "Watch",
                    "sector": "Technology",
                    "industry": "Semiconductors",
                    "thematic_tags": "Semiconductor, AI Compute",
                    "priority_score": 61,
                    "priority_label": "Follow actively",
                    "action_label": "BUY SETUP",
                    "next_action": "SET BREAKOUT ALERT",
                    "confidence_score": 8,
                    "catalyst_quality": "Technical only",
                    "liquidity_guardrails": "No material liquidity/slippage guardrails triggered.",
                    "personal_fit_label": "Good fit",
                    "why_this_stock": "A1 score 84, 2.4x relative volume, above VWAP, semiconductors leadership is strong, good fit for AI / Datacenter, Semiconductors",
                    "buy_trigger": "price holds above VWAP and stays constructive around 99.00–100.00",
                    "breakout_trigger": "breaks above 101.00 with expanding volume",
                    "pullback_trigger": "pulls back into 99.00–100.00 and reclaims VWAP",
                    "invalidation_trigger": "loses VWAP or breaks below 94.00",
                    "avoid_trigger": "loses VWAP or QQQ reverses lower",
                    "pullback_alert": "99.00–100.00",
                    "breakout_alert": "101.00",
                    "risk_alert": "below 94.00",
                    "target_alert": "104.00 (stretch 107.00)",
                    "exposure_categories": "AI / Datacenter, Semiconductors",
                }
            ]
        )
        regime_report = {
            "market_regime": "Risk-on",
            "momentum_odds": "Favorable",
            "sector_strength": {"SOXX": "Strong", "AI Software": "Strong", "Crypto Miners": "Neutral"},
        }

        brief = screener.format_shareable_report(frame, regime_report, ["IREN", "APLD", "CORE", "NVDA"])

        self.assertIn("Market: USA", brief)
        self.assertIn("Run type: Manual", brief)
        self.assertIn("Same-day and 1-week results are tracked in data/recommendation_log.csv.", brief)
        self.assertIn("### 1. AMD — BUY SETUP", brief)
        self.assertIn("confidence 8/10", brief)
        self.assertIn("next action SET BREAKOUT ALERT", brief)
        self.assertIn("Catalyst quality: Technical only", brief)
        self.assertIn("personal fit Good fit", brief)
        self.assertIn("Why this stock?", brief)
        self.assertIn("Nordnet alerts: pullback 99.00–100.00 | breakout 101.00 | risk/stop below 94.00 | target 104.00 (stretch 107.00)", brief)
        self.assertIn("Buy only if:", brief)
        self.assertIn("Portfolio warning", brief)
        self.assertIn("AI / Datacenter exposure is already concentrated", brief)

    def test_portfolio_overlap_only_adds_warning_metadata(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "score": 84,
                    "classification": "A-list",
                    "setup_bucket": "A1",
                    "setup": "breakout",
                    "last": 100.0,
                    "vwap": 99.0,
                    "preferred_entry_low": 99.0,
                    "preferred_entry_high": 100.0,
                    "volume_ratio": 2.4,
                    "distance_from_high_pct": -1.2,
                    "day_change_pct": 5.1,
                    "spread_bps": 10.0,
                    "earnings_warning": "Watch",
                    "chase_risk": "Low",
                    "sector": "Technology",
                    "industry": "Semiconductors",
                    "thematic_tags": "Semiconductor, AI Compute",
                }
            ]
        )
        regime_report = {
            "market_regime": "Risk-on",
            "momentum_odds": "Favorable",
            "sector_strength": {"SOXX": "Strong", "AI Software": "Strong", "Crypto Miners": "Neutral"},
        }

        without_overlap = screener.enrich_with_intraday_assistant(frame, regime_report, [])
        with_overlap = screener.enrich_with_intraday_assistant(
            frame, regime_report, ["IREN", "APLD", "CORE", "NVDA"]
        )

        self.assertEqual(without_overlap.loc[0, "priority_score"], with_overlap.loc[0, "priority_score"])
        self.assertEqual(without_overlap.loc[0, "priority_label"], with_overlap.loc[0, "priority_label"])
        self.assertEqual(with_overlap.loc[0, "action_label"], "BUY SETUP")
        self.assertIn(with_overlap.loc[0, "next_action"], screener.NEXT_ACTIONS)
        self.assertGreaterEqual(with_overlap.loc[0, "confidence_score"], 1)
        self.assertLessEqual(with_overlap.loc[0, "confidence_score"], 10)
        self.assertTrue(with_overlap.loc[0, "catalyst_quality"])
        self.assertTrue(with_overlap.loc[0, "liquidity_guardrails"])
        self.assertEqual(with_overlap.loc[0, "personal_fit_label"], "Good fit")
        self.assertEqual(with_overlap.loc[0, "portfolio_overlap"], "AI / Datacenter")

    def test_enrich_with_intraday_assistant_assigns_medium_and_poor_personal_fit_labels(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "DDOG",
                    "score": 58,
                    "classification": "B-list",
                    "setup_bucket": "B-list",
                    "setup": "continuation",
                    "last": 100.0,
                    "vwap": 99.0,
                    "preferred_entry_low": 98.0,
                    "preferred_entry_high": 99.0,
                    "volume_ratio": 1.8,
                    "distance_from_high_pct": -3.0,
                    "day_change_pct": 3.2,
                    "spread_bps": 12.0,
                    "earnings_warning": "None",
                    "chase_risk": "Low",
                    "sector": "Technology",
                    "industry": "Software - Infrastructure",
                    "thematic_tags": "Cloud, AI Software",
                },
                {
                    "ticker": "KO",
                    "score": 32,
                    "classification": "C-list",
                    "setup_bucket": "C-list",
                    "setup": "pullback",
                    "last": 60.0,
                    "vwap": 61.0,
                    "preferred_entry_low": 59.0,
                    "preferred_entry_high": 60.0,
                    "volume_ratio": 0.9,
                    "distance_from_high_pct": -8.0,
                    "day_change_pct": -1.1,
                    "spread_bps": 8.0,
                    "earnings_warning": "None",
                    "chase_risk": "Low",
                    "sector": "Consumer Defensive",
                    "industry": "Beverages - Non-Alcoholic",
                    "thematic_tags": "Consumer Staples",
                },
            ]
        )
        regime_report = {
            "market_regime": "Risk-on",
            "momentum_odds": "Favorable",
            "sector_strength": {"SOXX": "Strong", "AI Software": "Strong", "Crypto Miners": "Neutral"},
        }

        enriched = screener.enrich_with_intraday_assistant(frame, regime_report, [])

        personal_fit_by_ticker = dict(zip(enriched["ticker"], enriched["personal_fit_label"]))
        self.assertEqual(personal_fit_by_ticker["DDOG"], "Medium fit")
        self.assertEqual(personal_fit_by_ticker["KO"], "Poor fit")


if __name__ == "__main__":
    unittest.main()
