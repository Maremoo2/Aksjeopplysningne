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
        self.assertEqual(plan["position_size_pct"], 0.05)

    def test_classify_setup_marks_large_intraday_move_as_extended_without_strong_continuation(self) -> None:
        row = {
            "day_change_pct": 22.0,
            "distance_from_high_pct": -1.8,
            "volume_ratio": 2.4,
            "atr_pct": 4.0,
            "last": 50.0,
            "vwap": 48.0,
        }
        self.assertEqual(classify_setup(row), "extended/parabolic")

    def test_generate_trade_plan_raises_risk_floor_for_low_float_momentum_name(self) -> None:
        plan = generate_trade_plan(
            {
                "ticker": "RKLB",
                "day_change_pct": 18.5,
                "distance_from_high_pct": -1.0,
                "volume_ratio": 2.8,
                "atr_pct": 4.2,
                "last": 12.0,
                "vwap": 11.4,
                "earnings_warning": "None",
                "float_label": "Low",
            }
        )
        self.assertEqual(plan["risk"], "Medium")
        self.assertEqual(plan["position_size_pct"], 0.03)

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
