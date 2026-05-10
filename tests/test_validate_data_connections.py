from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts import validate_data_connections
from utils.alpaca_credentials import resolve_alpaca_credentials


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class ValidateDataConnectionsTests(unittest.TestCase):
    def test_resolve_alpaca_credentials_prefers_new_names(self) -> None:
        credentials = resolve_alpaca_credentials(
            {
                "ALPACA_API_KEY": "preferred-key",
                "ALPACA_SECRET_KEY": "preferred-secret",
                "ALPACA_KEY": "alias-key",
                "ALPACA_SECRET": "alias-secret",
            }
        )
        self.assertEqual(credentials.api_key, "preferred-key")
        self.assertEqual(credentials.secret_key, "preferred-secret")
        self.assertEqual(credentials.key_name, "ALPACA_API_KEY")
        self.assertEqual(credentials.secret_name, "ALPACA_SECRET_KEY")

    def test_resolve_alpaca_credentials_falls_back_to_alias_names(self) -> None:
        credentials = resolve_alpaca_credentials(
            {
                "ALPACA_KEY": "alias-key",
                "ALPACA_SECRET": "alias-secret",
            }
        )
        self.assertEqual(credentials.api_key, "alias-key")
        self.assertEqual(credentials.secret_key, "alias-secret")
        self.assertEqual(credentials.key_name, "ALPACA_KEY")
        self.assertEqual(credentials.secret_name, "ALPACA_SECRET")

    @patch("scripts.validate_data_connections.resolve_alpaca_credentials")
    def test_missing_alpaca_credentials_returns_warn_with_fallback(self, mock_resolve) -> None:
        mock_resolve.return_value = resolve_alpaca_credentials({})
        result = validate_data_connections._check_alpaca_provider()
        self.assertEqual(result["status"], "WARN")
        self.assertIn("fallback", " ".join(result["warnings"]).lower())

    @patch("scripts.validate_data_connections.resolve_alpaca_credentials")
    @patch("scripts.validate_data_connections.requests.get")
    def test_validation_never_calls_order_or_trading_endpoints(self, mock_get, mock_resolve) -> None:
        mock_resolve.return_value = resolve_alpaca_credentials(
            {"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret"}
        )
        mock_get.return_value = _FakeResponse(
            {
                "snapshots": {
                    "AAPL": {"latestTrade": {"p": 1}},
                    "MSFT": {"latestTrade": {"p": 1}},
                    "NVDA": {"latestTrade": {"p": 1}},
                }
            }
        )
        result = validate_data_connections._check_alpaca_provider()
        self.assertEqual(result["status"], "PASS")
        called_url = mock_get.call_args.kwargs.get("url") or mock_get.call_args.args[0]
        self.assertIn("data.alpaca.markets", called_url)
        self.assertNotIn("trading", called_url)
        self.assertNotIn("/orders", called_url)

    @patch("scripts.validate_data_connections.resolve_alpaca_credentials")
    @patch("scripts.validate_data_connections.requests.get")
    def test_validation_artifacts_do_not_contain_secret_values(self, mock_get, mock_resolve) -> None:
        secret_value = "super-secret-value"
        mock_resolve.return_value = resolve_alpaca_credentials(
            {"ALPACA_API_KEY": "visible-key", "ALPACA_SECRET_KEY": secret_value}
        )
        mock_get.return_value = _FakeResponse(
            {
                "snapshots": {
                    "AAPL": {"latestTrade": {"p": 1}},
                    "MSFT": {"latestTrade": {"p": 1}},
                    "NVDA": {"latestTrade": {"p": 1}},
                }
            }
        )
        payload = validate_data_connections.build_validation_payload("yahoo", "all")
        markdown = validate_data_connections.render_validation_markdown(payload)
        blob = f"{markdown}\n{payload}"
        self.assertNotIn(secret_value, blob)

    def test_report_renders_pass_warn_fail(self) -> None:
        payload = {
            "overall_status": "WARN",
            "checks": [
                {"name": "One", "status": "PASS"},
                {"name": "Two", "status": "WARN"},
                {"name": "Three", "status": "FAIL"},
            ],
            "warnings": ["warn item"],
            "errors": ["error item"],
            "next_steps": ["step item"],
        }
        markdown = validate_data_connections.render_validation_markdown(payload)
        self.assertIn("**PASS**", markdown)
        self.assertIn("**WARN**", markdown)
        self.assertIn("**FAIL**", markdown)

    def test_nordic_universe_files_have_required_columns(self) -> None:
        result = validate_data_connections._check_nordic_universes()
        self.assertNotEqual(result["status"], "FAIL")
        for item in result["details"]["files"].values():
            self.assertFalse(any("Missing required columns" in msg for msg in item["errors"]))

    def test_novo_replacement_is_applied(self) -> None:
        files = list(validate_data_connections.NORDIC_UNIVERSE_FILES.values()) + [
            Path(validate_data_connections.REPO_ROOT / "watchlists" / "nordic_watchlist.csv")
        ]
        all_tickers: list[str] = []
        novo_company_has_replacement = False
        for file_path in files:
            frame = pd.read_csv(file_path).fillna("")
            tickers = frame.get("ticker", pd.Series([], dtype=str)).astype(str).str.strip().str.upper().tolist()
            all_tickers.extend(tickers)
            if "company" in frame.columns:
                if frame["company"].astype(str).str.contains("Novo Nordisk", case=False, na=False).any():
                    novo_company_has_replacement = novo_company_has_replacement or ("NOVO-B.CO" in tickers)
        self.assertNotIn("NVO.CO", all_tickers)
        self.assertTrue(novo_company_has_replacement)


if __name__ == "__main__":
    unittest.main()
