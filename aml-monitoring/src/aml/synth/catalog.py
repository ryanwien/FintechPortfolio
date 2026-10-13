"""Static reference data for the synthetic world: names, segments, mixes and KYC weights.

Pure data, no randomness. Tuple/dict iteration order is part of the generator's contract:
the seeded RNG draws from these sequences, so reordering an entry changes the world.
"""

from __future__ import annotations

from typing import Final

from ..model import COUNTRY_TIERS

TYPOLOGIES: Final = (
    "structuring",
    "round_tripping",
    "mule_network",
    "rapid_movement",
    "high_risk_layering",
)

# fmt: off
FIRST_NAMES: Final = (
    "Avery", "Jordan", "Riley", "Morgan", "Casey", "Taylor", "Jamie", "Quinn", "Reese",
    "Dana", "Elliot", "Harper", "Rowan", "Sage", "Parker", "Logan", "Emerson", "Kendall",
    "Blake", "Drew", "Hayden", "Skyler", "Cameron", "Alexis",
)
LAST_NAMES: Final = (
    "Abbott", "Barlow", "Castillo", "Dunmore", "Ellison", "Fairbanks", "Galloway",
    "Hartley", "Ingram", "Jarvis", "Kessler", "Lockhart", "Mercer", "Nakamura", "Okafor",
    "Pruitt", "Quintero", "Rasmussen", "Sorensen", "Thackeray", "Underwood", "Vasquez",
    "Whitfield", "Yardley",
)
BIZ_WORDS: Final = (
    "Harbor", "Summit", "Maple", "Atlas", "Crescent", "Pioneer", "Granite", "Bluewater",
    "Redwood", "Keystone", "Northgate", "Silverline", "Lakeside", "Ironwood", "Beacon",
    "Juniper", "Meridian", "Copperfield", "Highland", "Orchard",
)
BIZ_SUFFIX: Final[dict[str, str]] = {
    "restaurant": "Grill", "laundromat": "Laundry", "convenience_store": "Mart",
    "car_wash": "Car Wash", "salon": "Salon", "professional_services": "Partners",
    "general_contractor": "Builders", "medical_practice": "Medical Group",
    "import_export": "Trading Co", "title_escrow": "Title & Escrow",
    "property_management": "Property Mgmt", "nonprofit": "Community Fund",
    "staffing_agency": "Staffing", "consulting": "Consulting", "general_trading": "Global Trade",
    "holding_company": "Holdings",
}
DEFAULT_BIZ_SUFFIX: Final = "Group"

OCCUPATIONS: Final[dict[str, tuple[int, int]]] = {  # annual income range, dollars
    "teacher": (48_000, 72_000),
    "nurse": (62_000, 98_000),
    "software_engineer": (95_000, 190_000),
    "retail_associate": (26_000, 38_000),
    "truck_driver": (45_000, 70_000),
    "accountant": (58_000, 105_000),
    "student": (8_000, 18_000),
    "retired": (24_000, 60_000),
    "contractor": (40_000, 90_000),
    "restaurant_server": (24_000, 42_000),
    "gig_worker": (18_000, 36_000),
}
OCC_WEIGHTS: Final[dict[str, int]] = {
    "teacher": 8, "nurse": 7, "software_engineer": 6, "retail_associate": 10,
    "truck_driver": 6, "accountant": 5, "student": 9, "retired": 12, "contractor": 8,
    "restaurant_server": 7, "gig_worker": 7,
}
CASH_MEDIANS: Final[dict[str, int]] = {  # median cash deposit, dollars
    "restaurant": 3_200, "laundromat": 900, "convenience_store": 2_200,
    "car_wash": 1_400, "salon": 1_100,
}
CASH_SEGMENTS: Final = tuple(CASH_MEDIANS)
PROFESSIONAL_SEGMENTS: Final = (
    "professional_services", "general_contractor", "medical_practice", "consulting",
    "general_trading", "holding_company",
)
BUSINESS_MIX: Final[dict[str, float]] = {
    "restaurant": 0.08, "laundromat": 0.05, "convenience_store": 0.06, "car_wash": 0.03,
    "salon": 0.04, "professional_services": 0.20, "general_contractor": 0.09,
    "medical_practice": 0.07, "import_export": 0.09, "title_escrow": 0.03,
    "property_management": 0.09, "nonprofit": 0.05, "staffing_agency": 0.04,
}
# fmt: on
HIGH_SCRUTINY_SEGMENTS: Final = ("import_export", "title_escrow")
BUSINESS_SEGMENTS: Final = frozenset(BIZ_SUFFIX)

BRANCH_COUNT: Final = 20
BRANCHES: Final = tuple(f"BR{n:02d}" for n in range(1, BRANCH_COUNT + 1))
BY_TIER: Final[dict[str, tuple[str, ...]]] = {
    tier: tuple(sorted(c for c, t in COUNTRY_TIERS.items() if t == tier))
    for tier in ("low", "medium", "high")
}
ELEVATED: Final = BY_TIER["medium"] + BY_TIER["high"]

# KYC rating weights over ``KYC_LEVELS`` (low, medium, high) at onboarding.
INDIVIDUAL_KYC_WEIGHTS: Final = (0.86, 0.12, 0.02)
CASH_BUSINESS_KYC_WEIGHTS: Final = (0.25, 0.6, 0.15)
HIGH_SCRUTINY_KYC_WEIGHTS: Final = (0.1, 0.7, 0.2)
STANDARD_BUSINESS_KYC_WEIGHTS: Final = (0.7, 0.25, 0.05)
MULE_RECRUIT_KYC_WEIGHTS: Final = (0.7, 0.25, 0.05)
BAD_ACTOR_KYC_WEIGHTS: Final = (0.45, 0.4, 0.15)  # often rated low risk at onboarding
