from __future__ import annotations

import unittest

import performance_review


class PerformanceReviewTests(unittest.TestCase):
    def test_summarize_performance_reports_core_metrics(self) -> None:
        rows = [
            {
                "date": "2026-05-01",
                "ticker": "AMD",
                "setup": "pullback",
                "classification": "A1",
                "entry": "100",
                "exit": "106",
                "position_size": "0.05",
                "stop": "96",
                "target": "110",
                "result_pct": "6.0",
                "result_nok": "6000",
                "followed_plan": "true",
                "notes": "",
            },
            {
                "date": "2026-05-03",
                "ticker": "RKLB",
                "setup": "extended/parabolic",
                "classification": "A2",
                "entry": "20",
                "exit": "18",
                "position_size": "0.03",
                "stop": "18.5",
                "target": "24",
                "result_pct": "-10.0",
                "result_nok": "-4000",
                "followed_plan": "false",
                "notes": "",
            },
        ]

        summary = performance_review.summarize_performance(rows)

        self.assertIn("Win rate: 50.00%", summary)
        self.assertIn("Average gain: +6.00%", summary)
        self.assertIn("Average loss: -10.00%", summary)
        self.assertIn("Best setup type: pullback (+6.00%)", summary)
        self.assertIn("Worst setup type: extended/parabolic (-10.00%)", summary)
        self.assertIn("Plan followed rate: 50.00%", summary)

    def test_build_performance_summary_includes_recommendation_statistics(self) -> None:
        trade_rows = [
            {
                "date": "2026-05-01",
                "ticker": "AMD",
                "setup": "pullback",
                "classification": "A1",
                "result_pct": "6.0",
                "followed_plan": "true",
            }
        ]
        recommendation_rows = [
            {
                "market": "USA",
                "ticker": "AMD",
                "recommendation_context": '{"sector":"Technology","industry":"Semiconductors","thematic_tags":"AI Compute"}',
                "setup": "breakout",
                "classification": "A1",
                "action_label": "BUY SETUP",
                "result_same_day_pct": "2.1",
                "result_1w_pct": "4.3",
            },
            {
                "market": "NORDIC",
                "ticker": "NOKIA.HE",
                "recommendation_context": '{"sector":"Technology","industry":"Communication Equipment","thematic_tags":"Telecom"}',
                "setup": "continuation",
                "classification": "B-list",
                "action_label": "WATCH",
                "result_same_day_pct": "-1.2",
                "result_1w_pct": "-0.8",
            },
        ]

        summary_text, summary_json = performance_review.build_performance_summary(trade_rows, recommendation_rows)

        self.assertIn("Recommendation win rate same-day: 50.00%", summary_text)
        self.assertIn("Recommendation win rate after 1 week: 50.00%", summary_text)
        self.assertIn("Average same-day return: +0.45%", summary_text)
        self.assertIn("Best market: USA (+2.10%)", summary_text)
        self.assertIn("Average return by classification:", summary_text)
        self.assertIn("A1", summary_json["recommendation_summary"]["average_return_by_classification"])


if __name__ == "__main__":
    unittest.main()
