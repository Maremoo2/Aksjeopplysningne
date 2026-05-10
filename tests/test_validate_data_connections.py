from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts import validate_data_connections
from utils.alpaca_credentials import resolve_alpaca_credentials


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

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
    def test_empty_snapshots_error_includes_http_status_and_body(self, mock_get, mock_resolve) -> None:
        mock_resolve.return_value = resolve_alpaca_credentials(
            {"ALPACA_API_KEY": "key", "ALPACA_SECRET_KEY": "secret"}
        )
        mock_get.return_value = _FakeResponse({"snapshots": {}})
        result = validate_data_connections._check_alpaca_provider()
        self.assertEqual(result["status"], "FAIL")
        error_text = result["errors"][0]
        self.assertIn("HTTP 200", error_text)
        self.assertIn("snapshot", error_text)
        self.assertIn("http_status", result["details"])
        self.assertIn("snapshot_count", result["details"])

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
        with (
            patch.object(
                validate_data_connections,
                "_check_yahoo_screeners",
                return_value={"name": "Yahoo", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_yahoo_fallback",
                return_value={"name": "Yahoo fallback", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_nordic_universes",
                return_value={"name": "Nordic", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_end_to_end_dry_run",
                return_value={"name": "Dry run", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
        ):
            payload = validate_data_connections.build_validation_payload("yahoo", "all")
        markdown = validate_data_connections.render_validation_markdown(payload)
        blob = f"{markdown}\n{payload}"
        self.assertNotIn(secret_value, blob)

    @patch("scripts.validate_data_connections.resolve_alpaca_credentials")
    @patch("scripts.validate_data_connections.requests.get")
    def test_markdown_shows_provider_and_credential_alias_names_not_values(self, mock_get, mock_resolve) -> None:
        secret_value = "my-actual-secret"
        mock_resolve.return_value = resolve_alpaca_credentials(
            {"ALPACA_API_KEY": "my-actual-key", "ALPACA_SECRET_KEY": secret_value}
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
        cred_names = {"api_key_env_var": "ALPACA_API_KEY", "secret_key_env_var": "ALPACA_SECRET_KEY"}
        with (
            patch.object(
                validate_data_connections,
                "_alpaca_credential_env_var_names",
                return_value=cred_names,
            ),
            patch.object(
                validate_data_connections,
                "_check_yahoo_screeners",
                return_value={"name": "Yahoo", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_yahoo_fallback",
                return_value={"name": "Yahoo fallback", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_nordic_universes",
                return_value={"name": "Nordic", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
            patch.object(
                validate_data_connections,
                "_check_end_to_end_dry_run",
                return_value={"name": "Dry run", "status": "PASS", "warnings": [], "errors": [], "details": {}},
            ),
        ):
            payload = validate_data_connections.build_validation_payload("alpaca", "all")
        markdown = validate_data_connections.render_validation_markdown(payload)
        # Provider is shown
        self.assertIn("alpaca", markdown)
        # Credential env-var names are shown
        self.assertIn("ALPACA_API_KEY", markdown)
        self.assertIn("ALPACA_SECRET_KEY", markdown)
        # Actual secret/key values are NOT shown
        self.assertNotIn(secret_value, markdown)
        self.assertNotIn("my-actual-key", markdown)

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

    def test_resolve_exit_code_matches_expected_status_rules(self) -> None:
        self.assertEqual(validate_data_connections.resolve_exit_code("PASS"), 0)
        self.assertEqual(validate_data_connections.resolve_exit_code("WARN"), 0)
        self.assertEqual(validate_data_connections.resolve_exit_code("FAIL"), 1)
        self.assertEqual(validate_data_connections.resolve_exit_code("FAIL", allow_fail=True), 0)

    def test_non_alpaca_provider_downgrades_alpaca_failure_to_warn(self) -> None:
        failing_alpaca = {
            "name": "Alpaca provider status",
            "status": "FAIL",
            "warnings": [],
            "errors": ["Alpaca returned no usable snapshots for sample symbols (HTTP 200; body: '{}')"],
            "details": {},
        }
        pass_check = {"name": "PASS", "status": "PASS", "warnings": [], "errors": [], "details": {}}
        with (
            patch.object(validate_data_connections, "_check_yahoo_screeners", return_value=pass_check),
            patch.object(validate_data_connections, "_check_alpaca_provider", return_value=failing_alpaca),
            patch.object(validate_data_connections, "_check_yahoo_fallback", return_value=pass_check),
            patch.object(validate_data_connections, "_check_nordic_universes", return_value=pass_check),
            patch.object(validate_data_connections, "_check_end_to_end_dry_run", return_value=pass_check),
        ):
            payload = validate_data_connections.build_validation_payload("yahoo", "all")
        self.assertEqual(payload["overall_status"], "WARN")
        self.assertEqual(payload["errors"], [])

    def test_alpaca_provider_keeps_alpaca_failure_as_fail(self) -> None:
        failing_alpaca = {
            "name": "Alpaca provider status",
            "status": "FAIL",
            "warnings": [],
            "errors": ["Alpaca returned no usable snapshots for sample symbols (HTTP 200; body: '{}')"],
            "details": {},
        }
        pass_check = {"name": "PASS", "status": "PASS", "warnings": [], "errors": [], "details": {}}
        with (
            patch.object(validate_data_connections, "_check_yahoo_screeners", return_value=pass_check),
            patch.object(validate_data_connections, "_check_alpaca_provider", return_value=failing_alpaca),
            patch.object(validate_data_connections, "_check_yahoo_fallback", return_value=pass_check),
            patch.object(validate_data_connections, "_check_nordic_universes", return_value=pass_check),
            patch.object(validate_data_connections, "_check_end_to_end_dry_run", return_value=pass_check),
        ):
            payload = validate_data_connections.build_validation_payload("alpaca", "all")
        self.assertEqual(payload["overall_status"], "FAIL")
        self.assertIn("Alpaca returned no usable snapshots for sample symbols", payload["errors"][0])

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
