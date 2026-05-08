from __future__ import annotations

import unittest

import pandas as pd

from strategy_engine import classify_setup, enrich_with_strategy, generate_trade_plan


class StrategyEngineTests(unittest.TestCase):
    def test_classify_setup_handles_negative_distance_from_high_as_extended(self) -> None:
        row = {
            "day_change_pct": 16.0,
            "distance_from_high_pct": -8.5,
            "volume_ratio": 1.0,
            "atr_pct": 5.0,
            "last": 100.0,
            "vwap": 100.0,
        }
        self.assertEqual(classify_setup(row), "extended/parabolic")

    def test_classify_setup_does_not_flag_positive_distance_as_extended(self) -> None:
        row = {
            "day_change_pct": 16.0,
            "distance_from_high_pct": 8.5,
            "volume_ratio": 1.0,
            "atr_pct": 5.0,
            "last": 100.0,
            "vwap": 100.0,
        }
        self.assertNotEqual(classify_setup(row), "extended/parabolic")

    def test_generate_trade_plan_outputs_expected_fields(self) -> None:
        plan = generate_trade_plan(
            {
                "ticker": "AMD",
                "day_change_pct": 5.5,
                "distance_from_high_pct": -1.2,
                "volume_ratio": 2.4,
                "atr_pct": 4.6,
                "last": 120.0,
                "vwap": 118.0,
                "earnings_warning": "None",
            }
        )

        expected_fields = {
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
        self.assertTrue(expected_fields.issubset(plan.keys()))
        self.assertLessEqual(plan["preferred_entry_low"], plan["preferred_entry_high"])
        self.assertGreater(plan["target_2"], plan["target_1"])

    def test_enrich_with_strategy_adds_columns(self) -> None:
        frame = pd.DataFrame(
            [
                {
                    "ticker": "NVDA",
                    "day_change_pct": 6.0,
                    "distance_from_high_pct": -1.0,
                    "volume_ratio": 2.2,
                    "atr_pct": 5.0,
                    "last": 900.0,
                    "vwap": 880.0,
                    "earnings_warning": "Watch",
                }
            ]
        )
        enriched = enrich_with_strategy(frame)
        for column in ("setup", "breakout_level", "stop_level", "target_1", "target_2", "risk"):
            self.assertIn(column, enriched.columns)


if __name__ == "__main__":
    unittest.main()
