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


if __name__ == "__main__":
    unittest.main()
