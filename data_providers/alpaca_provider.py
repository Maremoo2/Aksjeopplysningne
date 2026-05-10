from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import requests


class AlpacaProvider:
    """Alpaca market-data adapter (read-only, no trading endpoints)."""

    name = "alpaca"
    supports_batch = True

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        data_base_url: str = "https://data.alpaca.markets",
        feed: str = "iex",
        max_retries: int = 3,
        backoff_seconds: float = 1.0,
    ) -> None:
        if not api_key or not secret_key:
            raise ValueError("Alpaca API credentials are required")
        self.api_key = api_key
        self.secret_key = secret_key
        self.data_base_url = data_base_url.rstrip("/")
        self.feed = feed
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
        }

    def _request_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.data_base_url}{path}"
        delay = self.backoff_seconds
        for attempt in range(self.max_retries + 1):
            try:
                response = requests.get(url, headers=self._headers(), params=params, timeout=20)
            except requests.RequestException as exc:
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Alpaca request failed: {exc}") from exc
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code in {429, 500, 502, 503, 504} and attempt < self.max_retries:
                retry_after = response.headers.get("Retry-After")
                sleep_seconds = float(retry_after) if retry_after else delay
                time.sleep(max(sleep_seconds, 0.5))
                delay *= 2
                continue

            if response.status_code >= 400:
                raise RuntimeError(f"Alpaca data API error {response.status_code}: {response.text[:200]}")

            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(f"Invalid JSON from Alpaca: {exc}") from exc

        raise RuntimeError("Alpaca request retries exhausted")

    @staticmethod
    def _to_timeframe(interval: str) -> str:
        mapping = {
            "1m": "1Min",
            "5m": "5Min",
            "15m": "15Min",
            "1h": "1Hour",
            "1d": "1Day",
        }
        return mapping.get(interval, "1Min")

    @staticmethod
    def _window_start(period: str) -> datetime:
        now = datetime.now(UTC)
        if period.endswith("mo"):
            months = int(period[:-2]) if period[:-2].isdigit() else 1
            return now - timedelta(days=30 * months)
        if period.endswith("d"):
            days = int(period[:-1]) if period[:-1].isdigit() else 1
            return now - timedelta(days=days)
        return now - timedelta(days=7)

    @staticmethod
    def _bars_to_frame(bars: list[dict[str, Any]]) -> pd.DataFrame:
        if not bars:
            return pd.DataFrame()
        frame = pd.DataFrame(
            [
                {
                    "Open": bar.get("o"),
                    "High": bar.get("h"),
                    "Low": bar.get("l"),
                    "Close": bar.get("c"),
                    "Volume": bar.get("v"),
                    "Timestamp": bar.get("t"),
                }
                for bar in bars
            ]
        )
        if frame.empty:
            return frame
        frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["Timestamp"]).set_index("Timestamp").sort_index()
        return frame

    def get_batch_bars(
        self,
        symbols: list[str],
        *,
        timeframe: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> dict[str, pd.DataFrame]:
        if not symbols:
            return {}
        params = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": limit,
            "adjustment": "raw",
            "feed": self.feed,
        }
        payload = self._request_json("/v2/stocks/bars", params)
        bars_by_symbol = payload.get("bars", {})
        return {symbol: self._bars_to_frame(bars_by_symbol.get(symbol, [])) for symbol in symbols}

    def get_intraday_bars(self, symbol: str, interval: str = "1m", prepost: bool = True) -> pd.DataFrame:
        del prepost
        end = datetime.now(UTC)
        start = end - timedelta(days=2)
        frames = self.get_batch_bars(
            [symbol],
            timeframe=self._to_timeframe(interval),
            start=start,
            end=end,
            limit=10_000,
        )
        return frames.get(symbol, pd.DataFrame())

    def get_historical_bars(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        end = datetime.now(UTC)
        start = self._window_start(period)
        frames = self.get_batch_bars(
            [symbol],
            timeframe=self._to_timeframe(interval),
            start=start,
            end=end,
            limit=10_000,
        )
        return frames.get(symbol, pd.DataFrame())

    def get_latest_bar(self, symbol: str) -> dict[str, Any] | None:
        bars = self.get_batch_latest_bars([symbol]).get(symbol)
        return bars

    def get_batch_latest_bars(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        if not symbols:
            return {}
        params = {"symbols": ",".join(symbols), "feed": self.feed}
        payload = self._request_json("/v2/stocks/bars/latest", params)
        bars = payload.get("bars", {})
        result: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            bar = bars.get(symbol)
            if not bar:
                result[symbol] = None
                continue
            result[symbol] = {
                "timestamp": bar.get("t"),
                "open": bar.get("o"),
                "high": bar.get("h"),
                "low": bar.get("l"),
                "close": bar.get("c"),
                "volume": bar.get("v"),
            }
        return result
