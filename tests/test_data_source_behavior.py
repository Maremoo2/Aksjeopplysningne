from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

import screener
from data_providers import AlpacaProvider, YahooProvider
from utils.nordic_universe import load_nordic_universe, resolve_nordic_universe_paths


class DataSourceBehaviorTests(unittest.TestCase):
    def test_optional_unavailable_screeners_are_reported_without_warning_noise(self) -> None:
        with (
            patch.object(screener, "fetch_yahoo_screener", side_effect=RuntimeError("404")),
            patch.object(screener.logger, "warning") as mock_warning,
        ):
            entries, health = screener.fetch_yahoo_group_with_health(("yahoo-trending",), limit=10)

        self.assertEqual(entries, [])
        self.assertEqual(health["unavailable_screeners"], ["yahoo-trending"])
        mock_warning.assert_not_called()

    def test_invalid_yahoo_screener_group_does_not_raise(self) -> None:
        def _fake_fetch(source_key: str, limit: int) -> list[dict[str, object]]:
            if source_key == "yahoo-most-active":
                return [{"ticker": "AAPL", "exchange": "NASDAQ", "price": 1, "market_cap": 1, "volume": 1}]
            raise RuntimeError("404")

        with patch.object(screener, "fetch_yahoo_screener", side_effect=_fake_fetch):
            entries, health = screener.fetch_yahoo_group_with_health(("yahoo-trending", "yahoo-most-active"), limit=10)

        self.assertEqual([entry["ticker"] for entry in entries], ["AAPL"])
        self.assertIn("yahoo-trending", health["unavailable_screeners"])
        self.assertIn("yahoo-most-active", health["successful_screeners"])

    def test_load_watchlist_replaces_nvo_with_novo_b(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            watchlist_path = Path(tmp_dir) / "watchlist.csv"
            watchlist_path.write_text("ticker\nNVO.CO\n", encoding="utf-8")
            watchlist = screener.load_watchlist(watchlist_path)

        self.assertEqual([item.ticker for item in watchlist], ["NOVO-B.CO"])

    def test_nordic_universe_selector_maps_to_expected_csv(self) -> None:
        paths = resolve_nordic_universe_paths("sweden", screener.DEFAULT_WATCHLISTS_DIR)
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].name, "sweden.csv")

    def test_all_nordic_universes_are_deduplicated(self) -> None:
        frame = load_nordic_universe("all", screener.DEFAULT_WATCHLISTS_DIR)
        tickers = frame["ticker"].tolist()
        self.assertEqual(len(tickers), len(set(tickers)))
        self.assertIn("NOVO-B.CO", tickers)
        self.assertNotIn("NVO.CO", tickers)

    def test_missing_alpaca_credentials_falls_back_to_yahoo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "data_sources.yaml"
            config_path.write_text("usa_data_provider: alpaca\n", encoding="utf-8")
            provider, resolution = screener.resolve_usa_data_provider("usa", config_path=config_path, env={})

        self.assertIsInstance(provider, YahooProvider)
        self.assertEqual(resolution, "yahoo-fallback-missing-credentials")

    def test_alpaca_provider_uses_data_endpoints_only(self) -> None:
        provider = AlpacaProvider(api_key="k", secret_key="s")
        called_paths: list[str] = []

        def _fake_request(path: str, params: dict[str, object]) -> dict[str, object]:
            del params
            called_paths.append(path)
            if path == "/v2/stocks/bars/latest":
                return {"bars": {"AAPL": {"t": "2026-05-10T10:00:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 1}}}
            return {"bars": {"AAPL": []}}

        with patch.object(provider, "_request_json", side_effect=_fake_request):
            provider.get_intraday_bars("AAPL")
            provider.get_historical_bars("AAPL", period="1mo")
            provider.get_latest_bar("AAPL")

        self.assertTrue(called_paths)
        self.assertTrue(all(path.startswith("/v2/stocks/") for path in called_paths))
        self.assertTrue(all("orders" not in path for path in called_paths))

    def test_main_does_not_exit_when_group_screeners_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            args = argparse.Namespace(
                input="watchlist.csv",
                outdir=tmp_dir,
                source="yahoo-momentum",
                limit=25,
                min_price=2.0,
                min_market_cap=500_000_000.0,
                min_volume=1_000_000.0,
                market="usa",
                run_type="manual",
                performance_outdir=tmp_dir,
                recommendation_log=str(Path(tmp_dir) / "recommendation_log.csv"),
                nordic_universe="large_caps",
                data_sources_config=str(Path(tmp_dir) / "data_sources.yaml"),
            )

            with (
                patch.object(screener, "parse_args", return_value=args),
                patch.object(
                    screener,
                    "fetch_yahoo_group_with_health",
                    return_value=([], {"enabled_screeners": [], "successful_screeners": [], "unavailable_screeners": []}),
                ),
                patch.object(screener, "apply_filters", return_value=[]),
                patch.object(screener, "enrich_with_strategy", side_effect=lambda frame: frame),
                patch.object(screener, "enrich_with_intraday_assistant", side_effect=lambda frame, *_args, **_kwargs: frame),
                patch.object(screener, "build_regime_report", return_value=screener._build_market_regime_fallback()),
                patch.object(screener, "format_markdown_report", return_value="# Empty\n"),
                patch.object(screener, "format_shareable_report", return_value="# Brief\n"),
                patch.object(
                    screener,
                    "write_screener_health_report",
                    return_value=(Path(tmp_dir) / "health.md", Path(tmp_dir) / "health.json"),
                ),
            ):
                screener.main()

            self.assertTrue(any(Path(tmp_dir).glob("momentum_report_*.csv")))


if __name__ == "__main__":
    unittest.main()
