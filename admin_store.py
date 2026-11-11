import csv
import datetime as dt
import json
import os
import sqlite3
import unicodedata
from typing import Dict, Iterable, List, Optional

DEFAULT_REDASH_RESULTS_CSV_URL = (
    "https://redash-ro-live.bringo.ro/api/queries/4441/results.csv"
)

DEFAULT_MAX_DRIVERS_BY_STORE = {
    "Hyper Borj Fes": 5,
    "Gourmet Zaers": 5,
    "Hyper Dar Bouazza": 5,
    "Hyper Sidi Maarouf": 6,
    "Hyper Temara": 5,
    "Hyper Salé": 5,
    "Hyper Targa": 5,
    "Hyper Al Mazar": 5,
    "Hyper Tanger": 5,
    "Hyper Tetouan": 5,
    "Hyper Agadir": 5,
    "Hyper Beni Mellal": 5,
}

DEFAULT_SLOTS = [
    "10:00 - 12:00",
    "12:00 - 14:00",
    "14:00 - 16:00",
    "16:00 - 18:00",
    "18:00 - 20:00",
    "20:00 - 22:00",
]

DEFAULT_SETTINGS = {
    "redash_results_csv_url": os.getenv(
        "REDASH_RESULTS_CSV_URL",
        DEFAULT_REDASH_RESULTS_CSV_URL,
    ).strip(),
    "redash_api_key": os.getenv("REDASH_API_KEY", "").strip(),
    "max_orders": os.getenv("MAX_ORDERS", "150").strip(),
    "required_status": os.getenv("REQUIRED_STATUS", "new").strip(),
    "required_delivery_type": os.getenv(
        "REQUIRED_DELIVERY_TYPE",
        "planned order",
    ).strip(),
    "accept_max_min": os.getenv("ACCEPT_MAX_MIN", "10").strip(),
    "prep_max_min": os.getenv("PREP_MAX_MIN", "60").strip(),
    "load_max_min": os.getenv("LOAD_MAX_MIN", "20").strip(),
    "deliv_max_min": os.getenv("DELIV_MAX_MIN", "120").strip(),
    "planner_time_limit_sec": os.getenv("PLANNER_TIME_LIMIT_SEC", "12").strip(),
    "return_to_depot": os.getenv("RETURN_TO_DEPOT", "0").strip(),
    "slots_json": json.dumps(DEFAULT_SLOTS, ensure_ascii=False),
}

SETTINGS_KEYS = tuple(DEFAULT_SETTINGS.keys())


def normalize_store_key(value: str) -> str:
    value = (value or "").strip().lower()
    value = unicodedata.normalize("NFD", value)
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return " ".join(value.split())


DEFAULT_MAX_DRIVERS_BY_STORE_NORMALIZED = {
    normalize_store_key(name): max_drivers
    for name, max_drivers in DEFAULT_MAX_DRIVERS_BY_STORE.items()
}


def utcnow_iso() -> str:
    return dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def get_db_path() -> str:
    return os.getenv("APP_DB_PATH", "bringo_admin.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def _ensure_store_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(stores)").fetchall()
    }
    if "capacity" not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN capacity INTEGER NOT NULL DEFAULT 0")
    if "is_active" not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
    if "created_at" not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN created_at TEXT")
    if "updated_at" not in columns:
        conn.execute("ALTER TABLE stores ADD COLUMN updated_at TEXT")


def init_db() -> None:
    db_path = get_db_path()
    _ensure_parent_dir(db_path)

    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id TEXT NOT NULL UNIQUE,
                store_name TEXT NOT NULL,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                max_drivers INTEGER NOT NULL DEFAULT 5,
                capacity INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT,
                updated_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_stores_active ON stores(is_active)"
        )
        _ensure_store_columns(conn)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS driver_schedule (
                store_id TEXT NOT NULL,
                driver_name TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                days TEXT NOT NULL,
                updated_at TEXT,
                PRIMARY KEY (store_id, driver_name)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS driver_km_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id TEXT NOT NULL,
                driver_name TEXT NOT NULL,
                day TEXT NOT NULL,
                slot TEXT NOT NULL,
                km REAL NOT NULL,
                orders INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                UNIQUE (store_id, day, slot, driver_name)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_km_store_day ON driver_km_log(store_id, day)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS planning_settings (
                store_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT
            )
            """
        )
        _seed_settings(conn)
        _seed_stores(conn)


def _seed_settings(conn: sqlite3.Connection) -> None:
    existing_keys = {
        row["key"] for row in conn.execute("SELECT key FROM app_settings").fetchall()
    }
    now = utcnow_iso()
    for key, value in DEFAULT_SETTINGS.items():
        if key in existing_keys:
            continue
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), now),
        )


def _seed_stores(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) AS c FROM stores").fetchone()["c"]
    if count > 0:
        return

    stores_csv_path = os.getenv("STORES_CSV_PATH", "stores_coords.csv")
    if not os.path.exists(stores_csv_path):
        return

    now = utcnow_iso()
    with open(stores_csv_path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            store_id = (row.get("store_id") or "").strip()
            store_name = (row.get("store_name") or "").strip()
            lat = row.get("lat")
            lon = row.get("lon")

            if not store_id or not store_name or lat is None or lon is None:
                continue

            try:
                lat_value = float(lat)
                lon_value = float(lon)
            except Exception:
                continue

            default_max_drivers = DEFAULT_MAX_DRIVERS_BY_STORE_NORMALIZED.get(
                normalize_store_key(store_name),
                5,
            )

            conn.execute(
                """
                INSERT INTO stores(
                    store_id,
                    store_name,
                    lat,
                    lon,
                    max_drivers,
                    capacity,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    store_id,
                    store_name,
                    lat_value,
                    lon_value,
                    int(default_max_drivers),
                    0,
                    now,
                    now,
                ),
            )


def _to_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except Exception:
        return fallback


def _to_bool(value, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if text in ("1", "true", "yes", "oui", "on"):
        return True
    if text in ("0", "false", "no", "non", "off"):
        return False
    return fallback


def _row_to_store(row: sqlite3.Row) -> Dict:
    return {
        "id": row["id"],
        "store_id": row["store_id"],
        "store_name": row["store_name"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "max_drivers": int(row["max_drivers"] or 0),
        "capacity": int(row["capacity"] or 0),
        "is_active": bool(row["is_active"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_stores(active_only: bool = False) -> List[Dict]:
    query = """
        SELECT id, store_id, store_name, lat, lon, max_drivers, capacity, is_active, created_at, updated_at
        FROM stores
    """
    params: List[object] = []
    if active_only:
        query += " WHERE is_active = 1"
    query += " ORDER BY LOWER(store_name)"

    with get_connection() as conn:
        rows = conn.execute(query, params).fetchall()
    return [_row_to_store(row) for row in rows]


def get_store(store_id: str, active_only: bool = False) -> Optional[Dict]:
    if not store_id:
        return None

    query = """
        SELECT id, store_id, store_name, lat, lon, max_drivers, capacity, is_active, created_at, updated_at
        FROM stores
        WHERE store_id = ?
    """
    params: List[object] = [store_id]
    if active_only:
        query += " AND is_active = 1"

    with get_connection() as conn:
        row = conn.execute(query, params).fetchone()
    return _row_to_store(row) if row else None


def find_store_by_name(store_name: str, active_only: bool = False) -> Optional[Dict]:
    target = normalize_store_key(store_name)
    if not target:
        return None

    for store in list_stores(active_only=active_only):
        if normalize_store_key(store["store_name"]) == target:
            return store
    return None


def save_store(
    store_id: str,
    store_name: str,
    lat: float,
    lon: float,
    max_drivers: int,
    capacity: int,
    is_active: bool,
    original_store_id: Optional[str] = None,
) -> None:
    now = utcnow_iso()
    target_original = (original_store_id or store_id or "").strip()
    clean_store_id = (store_id or "").strip()
    clean_store_name = (store_name or "").strip()

    if not clean_store_id:
        raise ValueError("Le code magasin est obligatoire.")
    if not clean_store_name:
        raise ValueError("Le nom du magasin est obligatoire.")

    with get_connection() as conn:
        duplicate = conn.execute(
            "SELECT id FROM stores WHERE store_id = ? AND store_id <> ?",
            (clean_store_id, target_original),
        ).fetchone()
        if duplicate:
            raise ValueError("Ce code magasin existe déjà.")

        existing = conn.execute(
            "SELECT id, created_at FROM stores WHERE store_id = ?",
            (target_original,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE stores
                SET store_id = ?, store_name = ?, lat = ?, lon = ?, max_drivers = ?, capacity = ?, is_active = ?, updated_at = ?
                WHERE store_id = ?
                """,
                (
                    clean_store_id,
                    clean_store_name,
                    float(lat),
                    float(lon),
                    int(max_drivers),
                    int(capacity),
                    1 if is_active else 0,
                    now,
                    target_original,
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO stores(
                    store_id,
                    store_name,
                    lat,
                    lon,
                    max_drivers,
                    capacity,
                    is_active,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_store_id,
                    clean_store_name,
                    float(lat),
                    float(lon),
                    int(max_drivers),
                    int(capacity),
                    1 if is_active else 0,
                    now,
                    now,
                ),
            )


def set_store_active(store_id: str, is_active: bool) -> None:
    if not store_id:
        return
    with get_connection() as conn:
        conn.execute(
            "UPDATE stores SET is_active = ?, updated_at = ? WHERE store_id = ?",
            (1 if is_active else 0, utcnow_iso(), store_id),
        )


def get_app_settings() -> Dict:
    merged = dict(DEFAULT_SETTINGS)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT key, value FROM app_settings WHERE key IN ({})".format(
                ",".join("?" for _ in SETTINGS_KEYS)
            ),
            SETTINGS_KEYS,
        ).fetchall()

    for row in rows:
        merged[row["key"]] = row["value"]

    raw_slots = merged.get("slots_json") or "[]"
    try:
        slots = [str(item).strip() for item in json.loads(raw_slots)]
    except Exception:
        slots = []
    slots = [slot for slot in slots if slot]
    if not slots:
        slots = list(DEFAULT_SLOTS)

    return {
        "redash_results_csv_url": (merged.get("redash_results_csv_url") or "").strip(),
        "redash_api_key": (merged.get("redash_api_key") or "").strip(),
        "max_orders": max(1, _to_int(merged.get("max_orders"), 150)),
        "required_status": (merged.get("required_status") or "").strip().lower(),
        "required_delivery_type": (
            merged.get("required_delivery_type") or ""
        ).strip().lower(),
        "accept_max_min": max(0, _to_int(merged.get("accept_max_min"), 10)),
        "prep_max_min": max(0, _to_int(merged.get("prep_max_min"), 60)),
        "load_max_min": max(0, _to_int(merged.get("load_max_min"), 20)),
        "deliv_max_min": max(0, _to_int(merged.get("deliv_max_min"), 120)),
        "planner_time_limit_sec": max(
            1,
            _to_int(merged.get("planner_time_limit_sec"), 12),
        ),
        "return_to_depot": _to_bool(merged.get("return_to_depot"), False),
        "slots": slots,
    }


def update_app_settings(values: Dict[str, object]) -> None:
    allowed_updates = {}
    for key in SETTINGS_KEYS:
        if key in values:
            allowed_updates[key] = values[key]

    if "slots" in values and "slots_json" not in allowed_updates:
        slots = [
            str(item).strip()
            for item in values.get("slots", [])
            if str(item).strip()
        ]
        allowed_updates["slots_json"] = json.dumps(slots, ensure_ascii=False)

    if not allowed_updates:
        return

    now = utcnow_iso()
    with get_connection() as conn:
        for key, value in allowed_updates.items():
            conn.execute(
                """
                INSERT INTO app_settings(key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, ("1" if value else "0") if isinstance(value, bool) else str(value), now),
            )


def get_dashboard_stats() -> Dict:
    settings = get_app_settings()
    stores = list_stores(active_only=False)
    active_stores = [store for store in stores if store["is_active"]]
    return {
        "stores_total": len(stores),
        "stores_active": len(active_stores),
        "slots_total": len(settings["slots"]),
        "redash_host": settings["redash_results_csv_url"],
        "max_orders": settings["max_orders"],
        "planner_time_limit_sec": settings["planner_time_limit_sec"],
    }


def stores_to_csv_rows(stores: Iterable[Dict]) -> List[Dict]:
    return [
        {
            "store_id": store["store_id"],
            "store_name": store["store_name"],
            "lat": store["lat"],
            "lon": store["lon"],
            "max_drivers": store["max_drivers"],
            "capacity": store["capacity"],
            "is_active": int(store["is_active"]),
        }
        for store in stores
    ]


# ============================
# Planning livreurs + historique km (équité)
# ============================
def replace_store_schedule(store_id: str, drivers: List[Dict]) -> None:
    """drivers: [{"name": str, "days": [bool x 7]}] (0 = lundi). Remplace tout le planning."""
    now = utcnow_iso()
    with get_connection() as conn:
        conn.execute("DELETE FROM driver_schedule WHERE store_id = ?", (store_id,))
        for position, driver in enumerate(drivers):
            days = "".join("1" if worked else "0" for worked in driver["days"])
            conn.execute(
                "INSERT INTO driver_schedule(store_id, driver_name, position, days, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (store_id, driver["name"], position, days, now),
            )


def get_store_schedule(store_id: str) -> List[Dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT driver_name, days FROM driver_schedule WHERE store_id = ? ORDER BY position",
            (store_id,),
        ).fetchall()
    return [
        {"name": row["driver_name"], "days": [ch == "1" for ch in (row["days"] + "0000000")[:7]]}
        for row in rows
    ]


def get_driver_km_totals(
    store_id: str,
    since_day: str,
    until_day: str,
    exclude_slot: Optional[tuple] = None,
) -> Dict[str, float]:
    """
    Km cumulés par livreur entre since_day et until_day inclus (ISO YYYY-MM-DD).
    exclude_slot=(day, slot) ignore un créneau déjà validé (recalcul du même créneau).
    """
    query = (
        "SELECT driver_name, day, slot, km FROM driver_km_log "
        "WHERE store_id = ? AND day >= ? AND day <= ?"
    )
    with get_connection() as conn:
        rows = conn.execute(query, (store_id, since_day, until_day)).fetchall()
    totals: Dict[str, float] = {}
    for row in rows:
        if exclude_slot and (row["day"], row["slot"]) == tuple(exclude_slot):
            continue
        totals[row["driver_name"]] = totals.get(row["driver_name"], 0.0) + float(row["km"])
    return totals


def save_slot_km(store_id: str, day: str, slot: str, entries: List[Dict]) -> None:
    """Enregistre les km d'un créneau. Revalider le même créneau remplace l'ancien enregistrement."""
    now = utcnow_iso()
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM driver_km_log WHERE store_id = ? AND day = ? AND slot = ?",
            (store_id, day, slot),
        )
        for entry in entries:
            conn.execute(
                "INSERT INTO driver_km_log(store_id, driver_name, day, slot, km, orders, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (store_id, entry["name"], day, slot, float(entry["km"]), int(entry.get("orders") or 0), now),
            )


def is_planning_enabled(store_id: str) -> bool:
    """Bouton 'Selon planning' affiché pour ce magasin ? (activé par défaut)"""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT enabled FROM planning_settings WHERE store_id = ?", (store_id,)
        ).fetchone()
    return True if row is None else bool(row["enabled"])


def set_planning_enabled(store_id: str, enabled: bool) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO planning_settings(store_id, enabled, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(store_id) DO UPDATE SET enabled = excluded.enabled, updated_at = excluded.updated_at
            """,
            (store_id, 1 if enabled else 0, utcnow_iso()),
        )
