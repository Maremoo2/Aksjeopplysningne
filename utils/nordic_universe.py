from __future__ import annotations

from pathlib import Path

import pandas as pd

NORDIC_UNIVERSE_FILES: dict[str, tuple[str, ...]] = {
    "large_caps": ("nordic_large_caps.csv",),
    "momentum": ("nordic_momentum.csv",),
    "norway": ("norway.csv",),
    "sweden": ("sweden.csv",),
    "denmark": ("denmark.csv",),
    "finland": ("finland.csv",),
    "small_caps": ("nordic_small_caps.csv",),
    "all": (
        "nordic_large_caps.csv",
        "nordic_momentum.csv",
        "norway.csv",
        "sweden.csv",
        "denmark.csv",
        "finland.csv",
        "nordic_small_caps.csv",
    ),
}

REQUIRED_COLUMNS: tuple[str, ...] = (
    "ticker",
    "company",
    "country",
    "exchange",
    "theme",
    "liquidity_tier",
)

TICKER_REPLACEMENTS = {"NVO.CO": "NOVO-B.CO"}
EXCLUDED_TICKERS = {"CTRA"}


def resolve_nordic_universe_paths(universe: str, watchlists_dir: Path) -> list[Path]:
    normalized = str(universe).strip().lower() or "large_caps"
    if normalized not in NORDIC_UNIVERSE_FILES:
        raise ValueError(f"Unknown nordic universe: {universe}")
    return [watchlists_dir / name for name in NORDIC_UNIVERSE_FILES[normalized]]


def load_nordic_universe(universe: str, watchlists_dir: Path) -> pd.DataFrame:
    paths = resolve_nordic_universe_paths(universe, watchlists_dir)
    frames: list[pd.DataFrame] = []
    for path in paths:
        frame = pd.read_csv(path).fillna("")
        missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
        if missing:
            raise ValueError(f"{path} mangler kolonner: {', '.join(missing)}")
        frame = frame[list(REQUIRED_COLUMNS)].copy()
        frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
        frames.append(frame)

    if not frames:
        return pd.DataFrame(columns=REQUIRED_COLUMNS)

    merged = pd.concat(frames, ignore_index=True)
    merged["ticker"] = merged["ticker"].replace(TICKER_REPLACEMENTS)
    merged = merged[~merged["ticker"].isin(EXCLUDED_TICKERS)]
    merged = merged[merged["ticker"] != ""]
    merged = merged.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    return merged


def write_nordic_universe_csv(universe: str, output_path: Path, watchlists_dir: Path) -> Path:
    frame = load_nordic_universe(universe, watchlists_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False)
    return output_path
