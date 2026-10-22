"""Top-level world generation: population, look-alikes and typologies in a fixed order.

The call order below is part of the determinism contract: every generator shares one
RNG, so reordering, adding or removing a step changes every transaction after it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from ..config import DEFAULT_CUSTOMERS, DEFAULT_SEED
from ..model import World
from .builder import WorldBuilder
from .catalog import BUSINESS_MIX
from .lookalikes import add_family_wire, add_life_event, add_remittance, gen_intercompany_group
from .population import gen_business, gen_individual, gen_p2p
from .typologies import (
    inject_hr_layering,
    inject_mule_ring,
    inject_rapid_movement,
    inject_round_trip,
    inject_structuring,
)

BUSINESS_SHARE: Final = 0.17
CUSTOMERS_PER_INTERCOMPANY_GROUP: Final = 400
INTERCOMPANY_GROUP_SIZE: Final = 3
# Cumulative thresholds on one uniform roll per individual (mutually exclusive add-ons).
REMITTANCE_ROLL: Final = 0.07
LIFE_EVENT_ROLL: Final = 0.10
FAMILY_WIRE_ROLL: Final = 0.106
# Typology instance counts per REFERENCE_POPULATION customers, in injection order.
REFERENCE_POPULATION: Final = 4000
TYPOLOGY_COUNTS: Final[tuple[tuple[Callable[[WorldBuilder], object], int], ...]] = (
    (inject_structuring, 12),
    (inject_round_trip, 5),
    (inject_mule_ring, 4),
    (inject_rapid_movement, 12),
    (inject_hr_layering, 8),
)


def generate_world(seed: int = DEFAULT_SEED, n_customers: int = DEFAULT_CUSTOMERS) -> World:
    """Build the full synthetic world; identical inputs give an identical world."""
    b = WorldBuilder(seed)
    rng = b.rng
    # Split the customer count: the business share first, then intercompany groups; individuals take
    # the remainder.
    n_biz = int(n_customers * BUSINESS_SHARE)
    n_groups = max(1, n_customers // CUSTOMERS_PER_INTERCOMPANY_GROUP)
    n_ind = n_customers - n_biz - INTERCOMPANY_GROUP_SIZE * n_groups
    individuals: list[str] = []
    for _ in range(n_ind):
        cid = gen_individual(b)
        individuals.append(cid)
        # One uniform roll per individual; the cumulative thresholds make the three add-ons mutually
        # exclusive.
        roll = rng.random()
        if roll < REMITTANCE_ROLL:
            add_remittance(b, cid)
        elif roll < LIFE_EVENT_ROLL:
            add_life_event(b, cid)
        elif roll < FAMILY_WIRE_ROLL:
            add_family_wire(b, cid)
    # Peer-to-peer ties are built only after every individual exists, since friends are drawn from
    # that population.
    gen_p2p(b, individuals)
    # The mix weights set how common each business segment is.
    segments, weights = list(BUSINESS_MIX), list(BUSINESS_MIX.values())
    for _ in range(n_biz):
        gen_business(b, rng.choices(segments, weights)[0])
    # Intercompany groups are fixed-size clusters, one per block of 400 customers.
    for _ in range(n_groups):
        gen_intercompany_group(b)

    # Scale the per-instance counts to the population, so a bigger world keeps the same typology
    # density.
    scale = n_customers / REFERENCE_POPULATION
    # Typology injectors run last; each creates its own subjects through the normal generators.
    for inject, count in TYPOLOGY_COUNTS:
        for _ in range(round(count * scale)):
            inject(b)
    return b.build()
