from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import market_regime


class MarketRegimeArtifactTests(unittest.TestCase):
    def test_main_creates_markdown_and_json_artifacts(self) -> None:
        sample_report = {
            "timestamp": "2026-05-08T19:40:00",
            "market_regime": "Risk-on",
            "momentum_odds": "Favorable",
            "metrics_20d_vs_ma": {
                "SPY": 1.3,
                "QQQ": 2.1,
                "SOXX": 2.8,
                "BTC": 3.4,
                "VIX": 17.2,
                "AI Software": 1.9,
                "Crypto Miners": 4.1,
                "Cyber": 0.7,
            },
            "sector_strength": {
                "SOXX": "Strong",
                "AI Software": "Neutral",
                "Crypto Miners": "Strong",
                "Cyber": "Neutral",
            },
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            fake_now = datetime(2026, 5, 8, 19, 40)
            with (
                patch.object(market_regime, "parse_args", return_value=argparse.Namespace(outdir=temp_dir)),
                patch.object(market_regime, "build_regime_report", return_value=sample_report),
                patch.object(market_regime, "datetime") as mock_datetime,
            ):
                mock_datetime.now.return_value = fake_now
                market_regime.main()

            outdir = Path(temp_dir)
            md_path = outdir / "market_regime_20260508_1940.md"
            json_path = outdir / "market_regime_20260508_1940.json"

            self.assertTrue(md_path.exists())
            self.assertTrue(json_path.exists())

            markdown = md_path.read_text(encoding="utf-8")
            self.assertIn("Market regime: **Risk-on**", markdown)
            self.assertIn("Momentum odds: **Favorable**", markdown)

            data = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(data["market_regime"], "Risk-on")
            self.assertIn("metrics_20d_vs_ma", data)


if __name__ == "__main__":
    unittest.main()
