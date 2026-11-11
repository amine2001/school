"""
Tests du planning livreurs (présents / OFF + équité des km).

Lancement :
    python tests/test_planning.py
"""
import os
import sys
import tempfile

os.environ.setdefault("APP_DB_PATH", os.path.join(tempfile.mkdtemp(), "test_planning.db"))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planning import (  # noqa: E402
    assign_routes_fairly,
    parse_delivery_date,
    parse_schedule_rows,
    split_present_off,
)

ROWS = [
    (None, "lUNDI", "MARDI", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"),
    ("OTHMANE ELHACHIMI", "X", "X", "X", "X", "OFF", "X", "X"),
    ("YOUNESS ELHAYEL", "X", "X", "X", "X", "X", "OFF", "X"),
    ("OUSSAMA ZAROUAL", "OFF", "X", "X", "X", "X", "X", "X"),
    ("BELKABIR BADER", "X", "X", "X", "X", "X", "X", "OFF"),
    ("ABDERRAHMAN AMELLAL", "X", "X", "OFF", "X", "X", "X", "X"),
    ("OUSSAMA ELHAOUZI", "X", "OFF", "X", "X", "X", "X", "X"),
    ("MIMOUNI ZAKARIAE", "X", "OFF", "X", "X", "X", "X", "X"),
]


def test_parse_and_present():
    schedule = parse_schedule_rows(ROWS)
    assert len(schedule) == 7
    present, off = split_present_off(schedule, 1)  # mardi
    assert off == ["OUSSAMA ELHAOUZI", "MIMOUNI ZAKARIAE"]
    assert len(present) == 5


def test_dates():
    assert parse_delivery_date("2026-11-10").weekday() == 1
    assert parse_delivery_date("10/11/2026").weekday() == 1


def test_fairness():
    # Le plus long circuit va à celui qui a le moins de km cumulés.
    names = assign_routes_fairly([5.0, 20.0, 10.0], ["A", "B", "C", "D"], {"A": 50, "B": 10, "C": 0, "D": 30})
    assert names == ["D", "C", "B"]


def test_end_to_end():
    import admin_store
    from bot import apply_planning_to_plan, build_route_plan, format_route_plan_text

    store = admin_store.get_store("HSM")
    admin_store.replace_store_schedule("HSM", parse_schedule_rows(ROWS))
    pts = [(33.524662, -7.621755), (33.461255, -7.644323), (33.508615, -7.691035),
           (33.564141, -7.662941), (33.579854, -7.629396), (33.506846, -7.640306),
           (33.541677, -7.679715), (33.556346, -7.624548)]
    rows = [{"order_id": str(i), "store": store["store_name"], "slot": "10:00 - 12:00",
             "day_of_delivery": "2026-11-10", "lat": a, "lon": b} for i, (a, b) in enumerate(pts)]

    date = parse_delivery_date("2026-11-10")
    present, off = split_present_off(admin_store.get_store_schedule("HSM"), date.weekday())
    result = build_route_plan(store, "2026-11-10", "10:00 - 12:00", len(present), rows=rows)
    apply_planning_to_plan(result, store, present, off, date)
    print(format_route_plan_text(result))
    names = [r["driver_name"] for r in result["routes"]]
    assert sorted(names) == sorted(present)

    admin_store.save_slot_km("HSM", "2026-11-10", "10:00 - 12:00",
                             [{"name": r["driver_name"], "km": r["distance_km"], "orders": r["order_count"]} for r in result["routes"]])
    totals = admin_store.get_driver_km_totals("HSM", "2026-11-01", "2026-11-30")
    assert round(sum(totals.values()), 1) == round(sum(r["distance_km"] for r in result["routes"]), 1)
    # Revalider le même créneau ne double pas les km
    admin_store.save_slot_km("HSM", "2026-11-10", "10:00 - 12:00",
                             [{"name": r["driver_name"], "km": r["distance_km"], "orders": 1} for r in result["routes"]])
    assert admin_store.get_driver_km_totals("HSM", "2026-11-01", "2026-11-30") == totals


if __name__ == "__main__":
    test_parse_and_present()
    test_dates()
    test_fairness()
    test_end_to_end()
    print("OK")


def test_daily_equity_resets():
    """L'équité ne compte que les créneaux validés du même jour."""
    import datetime
    import admin_store
    from bot import apply_planning_to_plan

    store = admin_store.get_store("HSM")
    present = ["A", "B", "C"]
    # Jour J : A a déjà roulé 30 km sur un créneau validé
    admin_store.save_slot_km("HSM", "2026-11-11", "10:00 - 12:00", [{"name": "A", "km": 30, "orders": 3}])
    # Veille : C a roulé 100 km (ne doit PAS compter)
    admin_store.save_slot_km("HSM", "2026-11-10", "18:00 - 20:00", [{"name": "C", "km": 100, "orders": 9}])

    def plan(day, slot):
        result = {"slot": slot, "routes": [{"distance_km": 20.0}, {"distance_km": 5.0}, {"distance_km": 10.0}]}
        return apply_planning_to_plan(result, store, present, [], datetime.date.fromisoformat(day))

    names = [r["driver_name"] for r in plan("2026-11-11", "12:00 - 14:00")["routes"]]
    # A (30 km aujourd'hui) prend le plus court ; la veille de C est ignorée
    assert names[1] == "A", names
    # Lendemain sans validation : tout le monde repart à 0
    r2 = plan("2026-11-12", "10:00 - 12:00")
    assert all(r["cumul_km"] == 0 for r in r2["routes"])


if __name__ == "__main__":
    test_daily_equity_resets()
    print("OK daily")
