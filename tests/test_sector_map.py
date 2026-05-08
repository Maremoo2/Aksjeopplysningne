from __future__ import annotations

import unittest

from utils.sector_map import resolve_sector_info


class SectorMapTests(unittest.TestCase):
    def test_resolve_sector_info_keeps_live_sector_and_adds_fallback_tags(self) -> None:
        info = resolve_sector_info("IREN", {"sector": "Financial Services", "industry": "Capital Markets"})
        self.assertEqual(info.sector, "Financial Services")
        self.assertEqual(info.industry, "Capital Markets")
        self.assertEqual(
            info.thematic_tags,
            ("Crypto Miner", "AI Datacenter", "Power Infrastructure"),
        )


if __name__ == "__main__":
    unittest.main()
