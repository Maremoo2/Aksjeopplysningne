from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class AlpacaCredentials:
    api_key: str
    secret_key: str
    key_name: str
    secret_name: str

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key and self.secret_key)


def _env_value(env: Mapping[str, str], name: str) -> str:
    return str(env.get(name, "")).strip()


def resolve_alpaca_credentials(env: Mapping[str, str] | None = None) -> AlpacaCredentials:
    values = env if env is not None else os.environ

    preferred_api = _env_value(values, "ALPACA_API_KEY")
    preferred_secret = _env_value(values, "ALPACA_SECRET_KEY")
    alias_api = _env_value(values, "ALPACA_KEY")
    alias_secret = _env_value(values, "ALPACA_SECRET")

    api_key = preferred_api or alias_api
    secret_key = preferred_secret or alias_secret
    key_name = "ALPACA_API_KEY" if preferred_api else "ALPACA_KEY" if alias_api else ""
    secret_name = "ALPACA_SECRET_KEY" if preferred_secret else "ALPACA_SECRET" if alias_secret else ""

    return AlpacaCredentials(
        api_key=api_key,
        secret_key=secret_key,
        key_name=key_name,
        secret_name=secret_name,
    )
