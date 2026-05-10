from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import intraday_monitor
import recommendation_tracker


class RecommendationTrackerTests(unittest.TestCase):
    def test_snapshot_recommendations_logs_actionable_rows_and_writes_reports(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "AMD",
                    "priority_score": 70,
                    "score": 84,
                    "action_label": "BUY SETUP",
                    "next_action": "SET BREAKOUT ALERT",
                    "confidence_score": 8,
                    "catalyst_quality": "Strong",
                    "liquidity_guardrails": "No material liquidity/slippage guardrails triggered.",
                    "setup_bucket": "A1",
                    "setup": "breakout",
                    "preferred_entry_high": 100.0,
                    "breakout_level": 101.0,
                    "stop_level": 96.0,
                    "target_1": 104.0,
                    "target_2": 107.0,
                    "last": 100.5,
                    "sector": "Technology",
                    "industry": "Semiconductors",
                    "thematic_tags": "AI Compute",
                    "sources": "Top Gainers",
                    "why_this_stock": "A1 score 84",
                },
                {
                    "ticker": "DDOG",
                    "priority_score": 52,
                    "score": 58,
                    "action_label": "WATCH",
                    "setup_bucket": "B-list",
                    "setup": "continuation",
                    "preferred_entry_high": 99.0,
                    "breakout_level": 102.0,
                    "stop_level": 95.0,
                    "target_1": 105.0,
                    "target_2": 108.0,
                    "last": 100.0,
                    "sector": "Technology",
                    "industry": "Software",
                    "thematic_tags": "Cloud",
                    "sources": "Most Active",
                    "why_this_stock": "Watch for VWAP hold",
                },
                {
                    "ticker": "XYZ",
                    "priority_score": 10,
                    "score": 20,
                    "action_label": "AVOID",
                    "setup_bucket": "C-list",
                    "setup": "pullback",
                    "last": 5.0,
                },
            ]
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "recommendation_log.csv"
            output_dir = Path(temp_dir) / "performance"
            rows, md_path, json_path = recommendation_tracker.snapshot_recommendations(
                frame,
                market="usa",
                run_id="12345",
                output_dir=output_dir,
                log_path=log_path,
                recommendation_time=datetime(2026, 5, 8, 13, 30, tzinfo=UTC),
            )

            self.assertEqual([row["ticker"] for row in rows], ["AMD", "DDOG"])
            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())
            logged_rows = recommendation_tracker.load_recommendation_log(log_path)
            self.assertEqual(len(logged_rows), 2)
            context = json.loads(logged_rows[0]["recommendation_context"])
            self.assertEqual(context["sector"], "Technology")
            self.assertEqual(logged_rows[0]["next_action"], "SET BREAKOUT ALERT")
            self.assertEqual(logged_rows[0]["confidence_score"], "8.00")
            self.assertEqual(logged_rows[0]["status"], "PENDING_SAME_DAY")

    def test_update_recommendation_results_updates_same_day_and_one_week(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "recommendation_log.csv"
            output_dir = Path(temp_dir) / "performance"
            recommendation_tracker.save_recommendation_log(
                [
                    {
                        "run_id": "12345",
                        "date": "2026-05-08",
                        "market": "USA",
                        "ticker": "AMD",
                        "recommendation_time": "2026-05-08T13:30:00+00:00",
                        "recommendation_context": "{}",
                        "classification": "A1",
                        "action_label": "BUY SETUP",
                        "setup": "breakout",
                        "entry": "100.00",
                        "breakout": "101.00",
                        "stop": "96.00",
                        "target_1": "104.00",
                        "target_2": "107.00",
                        "recommended_price": "100.00",
                        "close_price_same_day": "",
                        "result_same_day_pct": "",
                        "close_price_1w": "",
                        "result_1w_pct": "",
                        "status": "PENDING_SAME_DAY",
                        "outcome_1w": "UNKNOWN",
                        "notes": "",
                    }
                ],
                log_path,
            )
            history = pd.DataFrame(
                {
                    "Date": pd.to_datetime(
                        ["2026-05-08", "2026-05-09", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"]
                    ),
                    "Close": [102.0, 103.0, 104.0, 103.5, 105.0, 106.0],
                }
            )
            with patch.object(recommendation_tracker.yf, "download", return_value=history):
                updated_same_day, md_path, _ = recommendation_tracker.update_recommendation_results(
                    log_path=log_path,
                    output_dir=output_dir,
                    mode="same-day",
                    as_of=datetime(2026, 5, 8, tzinfo=UTC).date(),
                )
                updated_week, _, _ = recommendation_tracker.update_recommendation_results(
                    log_path=log_path,
                    output_dir=output_dir,
                    mode="1w",
                    as_of=datetime(2026, 5, 16, tzinfo=UTC).date(),
                )

            self.assertTrue(md_path.exists())
            self.assertEqual(updated_same_day[0]["status"], "POSITIVE_CLOSE")
            self.assertEqual(updated_same_day[0]["result_same_day_pct"], "2.00")
            self.assertEqual(updated_week[0]["outcome_1w"], "WIN")
            self.assertEqual(updated_week[0]["result_1w_pct"], "6.00")

    def test_build_intraday_summary_compares_previous_focus_to_current_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "recommendation_log.csv"
            recommendation_tracker.save_recommendation_log(
                [
                    {
                        "run_id": "12345",
                        "date": "2026-05-08",
                        "market": "USA",
                        "ticker": "AMD",
                        "recommendation_time": "2026-05-08T13:30:00+00:00",
                        "recommendation_context": "{}",
                        "classification": "A1",
                        "action_label": "BUY SETUP",
                        "setup": "breakout",
                        "entry": "100.00",
                        "breakout": "101.00",
                        "stop": "96.00",
                        "target_1": "104.00",
                        "target_2": "107.00",
                        "recommended_price": "100.00",
                        "close_price_same_day": "",
                        "result_same_day_pct": "",
                        "close_price_1w": "",
                        "result_1w_pct": "",
                        "status": "PENDING_SAME_DAY",
                        "outcome_1w": "UNKNOWN",
                        "notes": "",
                    }
                ],
                log_path,
            )
            frame = pd.DataFrame(
                [
                    {
                        "ticker": "AMD",
                        "priority_score": 70,
                        "score": 84,
                        "action_label": "WAIT PULLBACK",
                        "last": 99.0,
                        "vwap": 100.0,
                        "pullback_alert": "98-99",
                        "breakout_alert": "101",
                        "risk_alert": "below 96",
                        "target_alert": "104",
                    },
                    {
                        "ticker": "NVDA",
                        "priority_score": 65,
                        "score": 82,
                        "action_label": "BUY SETUP",
                        "last": 105.0,
                        "vwap": 103.0,
                        "pullback_alert": "103-104",
                        "breakout_alert": "106",
                        "risk_alert": "below 101",
                        "target_alert": "109",
                    },
                ]
            )

            with patch.object(intraday_monitor, "datetime") as mock_datetime:
                mock_datetime.now.return_value = datetime(2026, 5, 8, 16, 0, tzinfo=UTC)
                summary = intraday_monitor.build_intraday_summary(
                    frame,
                    market="usa",
                    log_path=log_path,
                    snapshot_date="2026-05-08",
                )

            self.assertEqual(summary["previous_focus"][0]["status"], "extended, wait pullback")
            self.assertEqual(summary["new_movers"], ["NVDA"])


if __name__ == "__main__":
    unittest.main()
