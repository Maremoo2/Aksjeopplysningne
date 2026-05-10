from __future__ import annotations

from typing import Any

import pandas as pd
import yfinance as yf


class YahooProvider:
    """Yahoo/yfinance market data adapter used for discovery and fallback."""

    name = "yahoo"
    supports_batch = False

    def ticker(self, symbol: str) -> yf.Ticker:
        return yf.Ticker(symbol)

    def get_intraday_bars(self, symbol: str, interval: str = "1m", prepost: bool = True) -> pd.DataFrame:
        return self.ticker(symbol).history(period="1d", interval=interval, prepost=prepost)

    def get_historical_bars(self, symbol: str, period: str, interval: str = "1d") -> pd.DataFrame:
        return self.ticker(symbol).history(period=period, interval=interval)

    def get_latest_bar(self, symbol: str) -> dict[str, Any] | None:
        bars = self.get_intraday_bars(symbol, interval="1m", prepost=True)
        if bars.empty:
            bars = self.get_intraday_bars(symbol, interval="5m", prepost=True)
        if bars.empty:
            return None
        last = bars.iloc[-1]
        ts = bars.index[-1]
        return {
            "timestamp": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "open": float(last.get("Open", 0.0)),
            "high": float(last.get("High", 0.0)),
            "low": float(last.get("Low", 0.0)),
            "close": float(last.get("Close", 0.0)),
            "volume": int(last.get("Volume", 0.0)),
        }

    def get_batch_latest_bars(self, symbols: list[str]) -> dict[str, dict[str, Any] | None]:
        return {symbol: self.get_latest_bar(symbol) for symbol in symbols}

    def get_info(self, symbol: str) -> dict[str, Any]:
        return self.ticker(symbol).info or {}

    def get_fast_info(self, symbol: str) -> dict[str, Any]:
        return self.ticker(symbol).fast_info or {}

    def get_news(self, symbol: str) -> list[dict[str, Any]]:
        try:
            return self.ticker(symbol).news or []
        except Exception:
            return []
