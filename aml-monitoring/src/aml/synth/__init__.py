"""Deterministic synthetic banking world with labelled AML typologies.

Background population (individuals and businesses with KYC profiles) generates normal
activity; injectors add the five suspicious typologies, and the population deliberately
contains benign look-alikes (cash-intensive businesses, escrow agents, landlords,
non-profits, importers, payroll-heavy staffing firms, intercompany groups, people with
legitimate one-off life events) so that false positives arise naturally.

Modules:

* ``catalog``     static reference data (names, segments, mixes, KYC weights)
* ``builder``     ``WorldBuilder`` (seeded RNG, ids, posting) and ``BusinessBooks``
* ``population``  background individuals, P2P and businesses
* ``segments``    per-segment business activity profiles (most are look-alikes)
* ``lookalikes``  benign add-ons for individuals and intercompany groups
* ``typologies``  the five labelled typology injectors
* ``world``       ``generate_world``: the fixed generation order
"""

from .builder import WorldBuilder
from .catalog import TYPOLOGIES
from .lookalikes import add_family_wire, add_life_event, add_remittance, gen_intercompany_group
from .population import UnknownSegmentError, gen_business, gen_individual, gen_p2p
from .typologies import (
    inject_hr_layering,
    inject_mule_ring,
    inject_rapid_movement,
    inject_round_trip,
    inject_structuring,
)
from .world import generate_world

__all__ = [
    "TYPOLOGIES",
    "UnknownSegmentError",
    "WorldBuilder",
    "add_family_wire",
    "add_life_event",
    "add_remittance",
    "gen_business",
    "gen_individual",
    "gen_intercompany_group",
    "gen_p2p",
    "generate_world",
    "inject_hr_layering",
    "inject_mule_ring",
    "inject_rapid_movement",
    "inject_round_trip",
    "inject_structuring",
]
