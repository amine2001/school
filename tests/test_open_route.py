import os
import sys
import tempfile
import unittest

# Keep the test away from the real admin database.
os.environ.setdefault("APP_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_admin.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import haversine_m, solve_vrp  # noqa: E402

DEPOT = (33.536066, -7.642023)
NUM_DRIVERS = 6
ORDERS = [
    ("462569", 33.524662, -7.621755),
    ("462639", 33.461255, -7.644323),
    ("462383", 33.465137, -7.648903),
    ("462620", 33.508615, -7.691035),
    ("462573", 33.564141, -7.662941),
    ("462464", 33.562286, -7.653553),
    ("462606", 33.536813, -7.67494),
    ("462567", 33.541677, -7.679715),
    ("462662", 33.556346, -7.624548),
    ("462083", 33.579854, -7.629396),
    ("462578", 33.579526, -7.642635),
    ("462456", 33.506846, -7.640306),
    ("462570", 33.512577, -7.65511),
]
MAX_OPEN_DISTANCE_KM = 33.0


def open_route_km(routes) -> float:
    """Store -> stops distance, without the way back."""
    total_m = 0.0
    for stops in routes:
        prev = DEPOT
        for node in stops:
            _, lat, lon = ORDERS[node - 1]
            total_m += haversine_m(prev[0], prev[1], lat, lon)
            prev = (lat, lon)
    return total_m / 1000.0


class OpenRouteTest(unittest.TestCase):
    def test_open_route_distance(self):
        routes = solve_vrp(
            DEPOT,
            ORDERS,
            NUM_DRIVERS,
            time_limit_seconds=5,
            return_to_depot=False,
        )
        distance_km = open_route_km(routes)

        for v, stops in enumerate(routes, start=1):
            print(f"Livreur {v}: " + " -> ".join(ORDERS[n - 1][0] for n in stops))
        print(f"Distance aller totale (sans retour): {distance_km:.1f} km")

        served = sorted(node for stops in routes for node in stops)
        self.assertEqual(served, list(range(1, len(ORDERS) + 1)))
        self.assertLess(distance_km, MAX_OPEN_DISTANCE_KM)


if __name__ == "__main__":
    unittest.main(verbosity=2)
