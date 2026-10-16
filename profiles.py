"""Selectivity profiles. Stricter = fewer signals. Whether stricter is actually BETTER must be measured (see edge.compare_profiles)."""
from __future__ import annotations
from dataclasses import dataclass
from .scanner import ScanConfig


@dataclass(frozen=True)
class Profile:
    name: str
    label: str
    desc: str
    scan: ScanConfig
    min_net_rr: float


PROFILES = {
    "conservative": Profile("conservative", "Conservative", "Fewest, highest-bar setups: 12+ methods, 6+ families, almost no opposition, no extended moves, net R:R 2.0+.",
                            ScanConfig(min_methods=12, min_families=6, dominance=3.0, max_opposing_families=1, require_htf_alignment=True, max_extension_atr=2.0), 2.0),
    "balanced": Profile("balanced", "Balanced", "Default: 8+ methods, 4+ families, clear dominance, net R:R 1.5+.", ScanConfig(), 1.5),
    "active": Profile("active", "Active", "More signals: 6+ methods, 3+ families, tolerates some opposition, net R:R 1.2+. Expect more noise.",
                      ScanConfig(min_methods=6, min_families=3, dominance=1.5, max_opposing_families=3, require_htf_alignment=True, max_extension_atr=4.0), 1.2),
}
DEFAULT_PROFILE = "balanced"
