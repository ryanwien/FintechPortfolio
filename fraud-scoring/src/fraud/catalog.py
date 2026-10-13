"""Static reference data for the simulated card network: metros, merchant categories (MCCs),
the legit spending mix, and the fraud-type vocabulary.

Nothing here draws random numbers; the simulator, feature engine, rules and reason codes all
read the same tables, so an MCC code means the same thing everywhere.
"""

from __future__ import annotations

from typing import Final

import numpy as np

# (name, lat, lon, customer population weight). Weight-0 rows are abroad: merchants exist
# there (for travellers and fraudsters) but no customers live there.
LOCATIONS: Final[list[tuple[str, float, float, float]]] = [
    ("New York", 40.71, -74.01, 10.0),
    ("Los Angeles", 34.05, -118.24, 7.0),
    ("Chicago", 41.88, -87.63, 5.0),
    ("Houston", 29.76, -95.37, 4.0),
    ("Phoenix", 33.45, -112.07, 3.0),
    ("Philadelphia", 39.95, -75.17, 3.0),
    ("San Antonio", 29.42, -98.49, 2.0),
    ("San Diego", 32.72, -117.16, 2.0),
    ("Dallas", 32.78, -96.80, 4.0),
    ("Miami", 25.76, -80.19, 3.0),
    ("Atlanta", 33.75, -84.39, 3.0),
    ("Seattle", 47.61, -122.33, 2.5),
    ("Denver", 39.74, -104.99, 2.0),
    ("Boston", 42.36, -71.06, 2.5),
    ("Minneapolis", 44.98, -93.27, 1.5),
    ("Detroit", 42.33, -83.05, 1.5),
    ("Toronto", 43.65, -79.38, 0.0),
    ("London", 51.51, -0.13, 0.0),
    ("Mexico City", 19.43, -99.13, 0.0),
    ("Madrid", 40.42, -3.70, 0.0),
]
N_LOC: Final = len(LOCATIONS)
DOMESTIC: Final = [i for i, loc in enumerate(LOCATIONS) if loc[3] > 0]
ABROAD: Final = [i for i, loc in enumerate(LOCATIONS) if loc[3] == 0]

# MCC codes referenced by name in the simulator and rules.
MCC_FUEL: Final = 5541
MCC_GROCERY: Final = 5411
MCC_DEPARTMENT_STORE: Final = 5311
MCC_ELECTRONICS: Final = 5732
MCC_JEWELRY: Final = 5944
MCC_GIFT_CARD: Final = 5947
MCC_ONLINE_RETAIL: Final = 5999
MCC_DIGITAL_GOODS: Final = 5815
MCC_AIRLINE: Final = 4511
MCC_HOTEL: Final = 7011

# mcc -> (label, mean ticket in dollars, online-only?)
MCCS: Final[dict[int, tuple[str, float, bool]]] = {
    5411: ("grocery", 55.0, False),
    5541: ("fuel", 42.0, False),
    5812: ("restaurant", 48.0, False),
    5814: ("fast_food", 13.0, False),
    5311: ("department_store", 85.0, False),
    5732: ("electronics", 190.0, False),
    5944: ("jewelry", 420.0, False),
    5947: ("gift_card", 110.0, False),
    4121: ("rideshare", 22.0, True),
    5999: ("online_retail", 64.0, True),
    5815: ("digital_goods", 9.0, True),
    4511: ("airline", 360.0, True),
    7011: ("hotel", 230.0, False),
}
MCC_CODES: Final = np.array(sorted(MCCS))
MCC_INDEX: Final = {int(m): i for i, m in enumerate(MCC_CODES)}
# Baseline legit share of each MCC in a customer's wallet.
# fmt: off
MCC_BASE_WEIGHT: Final[dict[int, float]] = {
    5411: 20, 5541: 10, 5812: 12, 5814: 14, 5311: 6, 5732: 2, 5944: 0.3,
    5947: 1, 4121: 8, 5999: 14, 5815: 9, 4511: 0.6, 7011: 0.6,
}
# fmt: on
BIG_TICKET_MCCS: Final = (
    MCC_ELECTRONICS,
    MCC_JEWELRY,
    MCC_AIRLINE,
    MCC_HOTEL,
    MCC_DEPARTMENT_STORE,
)

# Relative transaction volume by hour of day (local time is ignored: one clock for all).
# fmt: off
HOUR_WEIGHTS: Final = np.array(
    [0.4, 0.25, 0.15, 0.1, 0.1, 0.2, 0.6, 1.2, 1.8, 2.0, 2.2, 2.6,
     3.2, 3.0, 2.6, 2.5, 2.7, 3.0, 3.3, 3.0, 2.4, 1.8, 1.2, 0.7]
)
# fmt: on
HOUR_P: Final = HOUR_WEIGHTS / HOUR_WEIGHTS.sum()

FRAUD_TYPES: Final = ("card_testing", "ato", "high_value", "merchant_compromise", "friendly_fraud")
