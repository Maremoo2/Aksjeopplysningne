from __future__ import annotations

from typing import Iterable

from utils.sector_map import resolve_sector_info

EXPOSURE_CATEGORY_OVERRIDES: dict[str, tuple[str, ...]] = {
    "AMD": ("Semiconductors", "High beta growth"),
    "NVDA": ("AI / Datacenter", "Semiconductors"),
    "MU": ("AI / Datacenter", "Semiconductors"),
    "CRDO": ("AI / Datacenter", "Semiconductors"),
    "DDOG": ("High beta growth",),
    "NET": ("Cybersecurity", "High beta growth"),
    "FTNT": ("Cybersecurity",),
    "IONQ": ("High beta growth",),
    "RKLB": ("Space / Aerospace", "High beta growth"),
    "IREN": ("AI / Datacenter", "Crypto miners"),
    "APLD": ("AI / Datacenter",),
    "CORE": ("AI / Datacenter",),
    "MARA": ("Crypto miners",),
    "HUT": ("Crypto miners", "AI / Datacenter"),
    "WULF": ("Crypto miners", "AI / Datacenter"),
    "CLAV": ("Norway / Nordic small caps",),
    "GENERAL OCEANS": ("Norway / Nordic small caps",),
}

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "AI / Datacenter": (
        "ai datacenter",
        "ai compute",
        "ai infrastructure",
        "hpc / compute infrastructure",
        "power infrastructure",
        "datacenter",
        "data center",
    ),
    "Semiconductors": ("semiconductor", "semiconductors", "memory", "chip"),
    "Crypto miners": ("crypto miner", "crypto miners", "bitcoin miner"),
    "Cybersecurity": ("cybersecurity", "cyber"),
    "Space / Aerospace": ("space", "aerospace", "launch", "satellite", "orbital"),
    "Norway / Nordic small caps": ("norway", "nordic", "oslo", "bergen"),
    "High beta growth": ("high beta", "cloud", "quantum", "growth"),
}


def normalize_security_name(value: str) -> str:
    return " ".join(str(value).strip().upper().split())


def exposure_categories_for_security(
    name: str,
    *,
    sector: str = "",
    industry: str = "",
    thematic_tags: str = "",
    category: str = "",
) -> tuple[str, ...]:
    normalized_name = normalize_security_name(name)
    categories = set(EXPOSURE_CATEGORY_OVERRIDES.get(normalized_name, ()))

    fallback = resolve_sector_info(normalized_name, {}) if normalized_name and " " not in normalized_name else None
    sector_text = sector or (fallback.sector if fallback and fallback.sector != "Unknown" else "")
    industry_text = industry or (fallback.industry if fallback and fallback.industry != "Unknown" else "")
    theme_text = thematic_tags or (
        ", ".join(fallback.thematic_tags) if fallback and fallback.thematic_tags else ""
    )

    search_text = " | ".join(
        value for value in (normalized_name, sector_text, industry_text, theme_text, category) if str(value).strip()
    ).lower()

    for exposure_category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in search_text for keyword in keywords):
            categories.add(exposure_category)

    if "software - infrastructure" in industry_text.lower() and "Cybersecurity" not in categories:
        categories.add("High beta growth")

    return tuple(sorted(categories))


def build_exposure_summary(holdings: Iterable[str]) -> dict[str, list[str]]:
    summary: dict[str, list[str]] = {}
    for holding in holdings:
        normalized = " ".join(str(holding).strip().split())
        if not normalized:
            continue
        for category in exposure_categories_for_security(normalized):
            summary.setdefault(category, []).append(normalized)
    return {category: names for category, names in sorted(summary.items())}
