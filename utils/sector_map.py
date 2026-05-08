from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SectorInfo:
    sector: str
    industry: str
    thematic_tags: tuple[str, ...]


FALLBACK_SECTOR_MAP: dict[str, SectorInfo] = {
    "AMD": SectorInfo("Technology", "Semiconductors", ("Semiconductor", "AI Compute")),
    "NVDA": SectorInfo("Technology", "Semiconductors", ("Semiconductor", "AI Compute")),
    "MU": SectorInfo("Technology", "Semiconductors", ("Memory", "AI Infrastructure")),
    "CRDO": SectorInfo("Technology", "Semiconductors", ("Datacenter", "AI Infrastructure")),
    "DDOG": SectorInfo("Technology", "Software - Infrastructure", ("Cloud", "AI Software")),
    "NET": SectorInfo("Technology", "Software - Infrastructure", ("Cloud", "Cybersecurity")),
    "FTNT": SectorInfo("Technology", "Software - Infrastructure", ("Cybersecurity",)),
    "IONQ": SectorInfo("Technology", "Computer Hardware", ("Quantum", "High Beta")),
    "IREN": SectorInfo(
        "Financial Services",
        "Capital Markets",
        ("Crypto Miner", "AI Datacenter", "Power Infrastructure"),
    ),
    "APLD": SectorInfo(
        "Technology",
        "Software - Infrastructure",
        ("AI Datacenter", "HPC / Compute Infrastructure", "Power Infrastructure"),
    ),
    "CORE": SectorInfo(
        "Technology",
        "Information Technology Services",
        ("AI Datacenter", "HPC / Compute Infrastructure", "Power Infrastructure"),
    ),
    "MARA": SectorInfo(
        "Financial Services",
        "Capital Markets",
        ("Crypto Miner", "AI Datacenter", "Power Infrastructure"),
    ),
    "HUT": SectorInfo(
        "Financial Services",
        "Capital Markets",
        ("Crypto Miner", "AI Datacenter", "HPC / Compute Infrastructure"),
    ),
    "WULF": SectorInfo(
        "Financial Services",
        "Capital Markets",
        ("Crypto Miner", "Power Infrastructure", "HPC / Compute Infrastructure"),
    ),
}


def _split_themes(theme_value: str | None) -> list[str]:
    if not theme_value:
        return []
    parts = [part.strip() for part in str(theme_value).replace("|", ",").split(",")]
    return [part for part in parts if part]


def resolve_sector_info(ticker: str, info: dict) -> SectorInfo:
    sector = str(info.get("sector") or "").strip()
    industry = str(info.get("industry") or "").strip()
    thematic_tags = _split_themes(info.get("theme"))
    fallback = FALLBACK_SECTOR_MAP.get(ticker.upper())

    if fallback:
        sector = sector or fallback.sector
        industry = industry or fallback.industry
        if not thematic_tags:
            thematic_tags = list(fallback.thematic_tags)

    return SectorInfo(
        sector=sector or "Unknown",
        industry=industry or "Unknown",
        thematic_tags=tuple(thematic_tags),
    )
