import os
import io
import csv
import math
import time
import logging
import threading
import asyncio
import atexit
import datetime
import unicodedata
from functools import wraps
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from admin_store import (
    DEFAULT_REDASH_RESULTS_CSV_URL,
    find_store_by_name,
    get_app_settings,
    get_dashboard_stats,
    get_driver_km_totals,
    get_store,
    get_store_schedule,
    replace_store_schedule,
    save_slot_km,
    init_db,
    list_stores,
    save_store,
    set_store_active,
    update_app_settings,
)
from planning import (
    DAY_NAMES_FR,
    assign_routes_fairly,
    format_schedule_text,
    parse_delivery_date,
    parse_schedule_csv,
    parse_schedule_xlsx,
    split_present_off,
)

# ============================
# CONFIG (Render Env Vars)
# ============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip()
ADMIN_USER_IDS = {
    int(chunk.strip())
    for chunk in (os.getenv("ADMIN_USER_IDS") or "").split(",")
    if chunk.strip().isdigit()
}

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").strip()
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "bringo-control-room")

# ✅ Max livreurs autorisés par magasin (cap recommandation + choix)
# (Tu peux ajuster cette table sans toucher au reste du code)
MAX_DRIVERS_BY_STORE = {
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


# ✅ Créneaux FIXES (pas de "Tous les créneaux")
SLOTS_FIXED = [
    "10:00 - 12:00",
    "12:00 - 14:00",
    "14:00 - 16:00",
    "16:00 - 18:00",
    "18:00 - 20:00",
    "20:00 - 22:00",
]

# ✅ Business rules (as requested)
REQUIRED_STATUS = "new"               # ONLY NEW (others ignored)
REQUIRED_DELIVERY_TYPE = "planned order"  # Delivery Type must be Planned Order (case-insensitive)

# ============================
# SLA thresholds (minutes) for "Commande > Statut"
# Can be overridden via env vars
# ============================
ACCEPT_MAX_MIN = int(os.getenv("ACCEPT_MAX_MIN", "10"))
PREP_MAX_MIN   = int(os.getenv("PREP_MAX_MIN",   "60"))
LOAD_MAX_MIN   = int(os.getenv("LOAD_MAX_MIN",   "20"))
DELIV_MAX_MIN  = int(os.getenv("DELIV_MAX_MIN",  "120"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bringo-bot")
init_db()
DEFAULT_ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)


def runtime_settings() -> Dict:
    return get_app_settings()


def configured_slots() -> List[str]:
    return runtime_settings().get("slots", [])


def get_admin_password_hash() -> str:
    if ADMIN_PASSWORD_HASH:
        return ADMIN_PASSWORD_HASH
    return DEFAULT_ADMIN_PASSWORD_HASH


def resolve_webhook_url() -> str:
    if WEBHOOK_URL:
        return WEBHOOK_URL
    if RENDER_EXTERNAL_URL:
        return f"{RENDER_EXTERNAL_URL.rstrip('/')}/webhook"
    return ""


def admin_ids_text() -> str:
    if not ADMIN_USER_IDS:
        return "non configuré"
    return ", ".join(str(item) for item in sorted(ADMIN_USER_IDS))


# ============================
# Telegram Conversation States
# ============================
(CHOOSE_STORE, CHOOSE_DATE, CHOOSE_SLOT, CHOOSE_DRIVERS, ORDERS_MENU, ORDER_INPUT, STORE_MENU, SETTINGS_MENU, SETTINGS_INPUT) = range(9)

# context keys
CTX_STORES = "stores"
CTX_REDASH_CACHE = "redash_cache"
CTX_REDASH_ALL_CACHE = "redash_all_cache"  # for order lookup (all statuses/types)
CTX_STORE_NAME = "store_name"
CTX_STORE_DEPOT = "store_depot"
CTX_DATE = "date"
CTX_SLOT = "slot"
CTX_NUM_DRIVERS = "num_drivers"
CTX_MAX_DRIVERS_ALLOWED = "max_drivers_allowed"
CTX_SETTINGS_ACTION = "settings_action"
CTX_SETTINGS_STORE_ID = "settings_store_id"
CTX_USE_PLANNING = "use_planning"
CTX_PENDING_KM = "pending_km"

STORE_WIZARD_FIELDS = (
    "store_id",
    "store_name",
    "lat",
    "lon",
    "max_drivers",
    "capacity",
)


# ============================
# Safe callback answer
# ============================
async def safe_answer(q):
    if not q:
        return
    try:
        await q.answer(cache_time=0)
    except BadRequest:
        # callback trop vieux / id invalide -> ignorer pour éviter crash
        pass


# ============================
# Helpers: Stores CSV
# ============================
def load_stores_from_csv(path: str) -> List[Dict]:
    stores = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            name = (r.get("store_name") or "").strip()
            lat = r.get("lat")
            lon = r.get("lon")
            if not name or lat is None or lon is None:
                continue
            try:
                stores.append({
                    "store_id": (r.get("store_id") or name).strip(),
                    "store_name": name,
                    "lat": float(lat),
                    "lon": float(lon),
                })
            except Exception:
                continue
    stores.sort(key=lambda x: x["store_name"].lower())
    return stores


# ============================
# Text cleanup (mojibake)
# ============================
def fix_mojibake(s: str) -> str:
    """Fix common UTF-8/Latin-1 mojibake (e.g., SalÃ© -> Salé)."""
    if not s:
        return ""
    try:
        b = s.encode("latin1")
        return b.decode("utf-8")
    except Exception:
        return s


def norm_store_key(s: str) -> str:
    """Normalize store names so 'Hyper Sale' == 'Hyper Salé' == 'Hyper SalÃ©'."""
    s = fix_mojibake(s or "").strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")  # remove accents
    s = " ".join(s.split())
    return s

# ============================
# Helpers: Redash fetch
# ============================
def _with_api_key(url: str, api_key: str) -> str:
    """Append api_key param only if api_key exists and api_key not already present."""
    if not api_key:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    if "api_key" in q and q["api_key"]:
        return url
    q["api_key"] = api_key
    return urlunparse(u._replace(query=urlencode(q)))


def fetch_redash_csv_text() -> str:
    settings = runtime_settings()
    url = _with_api_key(
        settings.get("redash_results_csv_url") or DEFAULT_REDASH_RESULTS_CSV_URL,
        settings.get("redash_api_key") or "",
    )
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


# ============================
# Helpers: parse CSV rows
# ============================
def parse_latlong(latlong: str) -> Optional[Tuple[float, float]]:
    if not latlong:
        return None
    s = str(latlong).strip().replace(";", ",")
    if "," not in s:
        return None
    a, b = s.split(",", 1)
    try:
        lat = float(a.strip().replace(",", "."))
        lon = float(b.strip().replace(",", "."))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon
    except Exception:
        return None


def _fmt_hhmm(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.lower() == "nan":
        return None
    # examples: "20:00:00" -> "20:00"
    if ":" in s and len(s) >= 5:
        return s[:5]
    return None


def norm_slot(s: str) -> str:
    return (s or "").lower().replace(" ", "").replace("–", "-").replace("--", "-")


def norm_text(s: str) -> str:
    return (s or "").strip().lower()


def parse_redash_rows(csv_text: str) -> List[Dict]:
    """
    Expected columns:
    - Order Number
    - Store
    - Status (we keep only NEW)
    - Delivery Type (must be Planned Order)
    - Day Of Delivery (we keep ALL dates; selection done in UI)
    - Debut De Creneau, Fin De Creneau -> build slot 'HH:MM - HH:MM'
    - LatLong (or Latitude/Longitude)
    """
    settings = runtime_settings()
    required_status = norm_text(settings.get("required_status"))
    required_delivery_type = norm_text(settings.get("required_delivery_type"))
    max_orders = int(settings.get("max_orders") or 150)

    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)

    rows = []
    for row in reader:
        order_id = (row.get("Order Number") or row.get("Order") or "").strip()
        store = fix_mojibake((row.get("Store") or "").strip())

        status = norm_text(row.get("Status"))
        delivery_type = norm_text(row.get("Delivery Type"))
        day_of_delivery = (row.get("Day Of Delivery") or "").strip()

        start = _fmt_hhmm(row.get("Debut De Creneau"))
        end = _fmt_hhmm(row.get("Fin De Creneau"))
        slot = f"{start} - {end}" if start and end else ""

        # lat/lon
        ll = parse_latlong(row.get("LatLong"))
        if not ll:
            try:
                lat = float(str(row.get("Latitude")).strip().replace(",", "."))
                lon = float(str(row.get("Longitude")).strip().replace(",", "."))
                ll = (lat, lon)
            except Exception:
                ll = None

        if not order_id or not store or not slot or not ll or not day_of_delivery:
            continue

        # ✅ ONLY NEW (others ignored)
        if required_status and status != required_status:
            continue

        # ✅ ONLY Planned Order
        if required_delivery_type and delivery_type != required_delivery_type:
            continue

        rows.append({
            "order_id": order_id,
            "store": store,
            "slot": slot,
            "status": status,
            "delivery_type": delivery_type,
            "day_of_delivery": day_of_delivery,
            "lat": ll[0],
            "lon": ll[1],
            "raw": row,
        })

    if len(rows) > max_orders:
        rows = rows[:max_orders]
    return rows




# ============================
# Order lookup: parse ALL rows (no filters on status/type/date)
# ============================
def _parse_dt(s: str):
    if not s:
        return None
    s = str(s).strip()
    if not s or s.lower() == "nan":
        return None
    # common formats in your Redash exports
    fmts = [
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%Y %H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for f in fmts:
        try:
            return datetime.datetime.strptime(s, f)
        except Exception:
            pass
    return None

def parse_redash_rows_all(csv_text: str) -> List[Dict]:
    """Keep all orders for per-order queries (payment/status/e2e)."""
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    out = []
    for row in reader:
        order_id = (row.get("Order Number") or row.get("Order") or "").strip()
        if not order_id:
            continue
        store = fix_mojibake((row.get("Store") or "").strip())
        status = norm_text(row.get("Status"))
        delivery_type = norm_text(row.get("Delivery Type"))
        day_of_delivery = (row.get("Day Of Delivery") or "").strip()

        initial_amount = row.get("Initial Amount")
        total_paid = row.get("Total Amount Paid")

        # Timestamps for steps
        order_date = _parse_dt(row.get("Order Date"))
        accept_dt = _parse_dt(row.get("Date d'acceptation prep"))
        prep_end_dt = _parse_dt(row.get("Order Preparation End"))
        load_dt = _parse_dt(row.get("Chargement date"))
        close_dt = _parse_dt(row.get("Date de cloture"))

        def _to_float(x):
            try:
                if x is None:
                    return None
                s = str(x).strip().replace(" ", "").replace(",", ".")
                if not s:
                    return None
                return float(s)
            except Exception:
                return None

        out.append({
            "order_id": order_id,
            "store": store,
            "status": status,
            "delivery_type": delivery_type,
            "day_of_delivery": day_of_delivery,
            "initial_amount": _to_float(initial_amount),
            "total_paid": _to_float(total_paid),
            "order_date": order_date,
            "accept_dt": accept_dt,
            "prep_end_dt": prep_end_dt,
            "load_dt": load_dt,
            "close_dt": close_dt,
            "etat_picker": norm_text(row.get("Etat Picker")),
            "etat_shopper": norm_text(row.get("Etat Shopper")),
            "last_mile_status": norm_text(
                row.get("Status Lastmile")
                or row.get("Statut last mile")
                or row.get("Statut Last Mile")
                or row.get("Last Mile Status")
                or row.get("Last mile status")
            ),
            "raw": row,
        })
    return out

async def ensure_redash_all_cache(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> List[Dict]:
    if (not force) and context.user_data.get(CTX_REDASH_ALL_CACHE):
        return context.user_data[CTX_REDASH_ALL_CACHE]
    csv_text = fetch_redash_csv_text()
    rows_all = parse_redash_rows_all(csv_text)
    context.user_data[CTX_REDASH_ALL_CACHE] = rows_all
    return rows_all

def find_order(rows_all: List[Dict], order_number: str) -> Optional[Dict]:
    target = (order_number or "").strip()
    if not target:
        return None
    for r in rows_all:
        if str(r.get("order_id", "")).strip() == target:
            return r
    return None

def _fmt_dt(dt):
    if not dt:
        return "-"
    return dt.strftime("%d/%m/%y %H:%M")

def _fmt_duration(td: datetime.timedelta) -> str:
    mins = int(td.total_seconds() // 60)
    h = mins // 60
    m = mins % 60
    if h <= 0:
        return f"{m} min"
    return f"{h}h {m:02d}m"

def compute_e2e(order: Dict) -> str:
    """End-to-End **EXPRESS ONLY**:
    duration = (close_dt or now) - order_date
    If not EXPRESS, returns a clear message.
    """
    now = datetime.datetime.utcnow()

    dtype = (order.get("delivery_type") or "").lower().strip()

    if dtype not in ["express", "rapide xl"]:
        # planned order / autres
        return "Non applicable : End-to-End express est disponible uniquement pour Express et Rapide XL"


    dt_start = order.get("order_date")
    if not dt_start:
        return "Impossible à calculer (Order Date manquante)."

    dt_end = order.get("close_dt") or now
    td = dt_end - dt_start

    delivered = "Livrée" if order.get("close_dt") else "En cours"
    return f"{delivered} • {_fmt_duration(td)} (de {_fmt_dt(dt_start)} à {_fmt_dt(order.get('close_dt') or now)})"

def compute_payment(order: Dict) -> str:
    init_amt = order.get("initial_amount")
    paid = order.get("total_paid")
    etat = (order.get("etat_shopper") or "")
    is_paid = False
    if isinstance(init_amt, (int, float)) and isinstance(paid, (int, float)):
        if paid >= init_amt and paid > 0:
            is_paid = True
    if etat in ("store_paid", "customer_paid", "complete"):
        is_paid = True
    if is_paid:
        return f"✅ Payé (Total payé: {paid} / Montant: {init_amt})"
    return f"❌ Non payé (Total payé: {paid} / Montant: {init_amt})"

def compute_step_status(order: Dict) -> List[str]:
    """
    Statut détaillé basé sur Redash:
      - OK si la date existe
      - En cours si la date est vide
      - Retard si "Status Lastmile" indique retard/late/delay (même si la date est vide)
    """
    lm = (order.get("last_mile_status") or "").lower()

    def is_retard():
        return ("retard" in lm) or ("late" in lm) or ("delay" in lm)

    def line(label, done_dt):
        if done_dt:
            return f"✅ {label}: OK ({_fmt_dt(done_dt)})"
        if is_retard():
            return f"🚨 {label}: RETARD"
        return f"⏳ {label}: EN COURS"

    accept_dt = order.get("accept_dt")
    prep_end = order.get("prep_end_dt")
    load_dt = order.get("load_dt")
    close_dt = order.get("close_dt")

    return [
        line("Acceptation", accept_dt),
        line("Préparation", prep_end),
        line("Chargement", load_dt),
        line("Livraison", close_dt),
    ]


# ============================
# Magasin (dashboards)
# ============================
def magasin_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Hyper", callback_data="STORE::HYPER")],
        [InlineKeyboardButton("Rapide XL", callback_data="STORE::RAPIDEXL")],
        [InlineKeyboardButton("Express", callback_data="STORE::EXPRESS")],
        [InlineKeyboardButton("Retour", callback_data="STORE::BACK")],
    ]
    return InlineKeyboardMarkup(kb)

def _is_cancelled(status: str) -> bool:
    s = (status or "").lower()
    return ("annul" in s) or ("cancel" in s)

def _is_lastmile_retard(last_mile_status: str) -> bool:
    s = (last_mile_status or "").lower()
    return ("retard" in s) or ("late" in s) or ("delay" in s)

def _class_store_is_hyper(store_name: str) -> bool:
    return (store_name or "").strip().lower().startswith("hyper")

def _class_is_express(dtype: str) -> bool:
    return (dtype or "").lower().strip() == "express"

def _class_is_rapide_xl(dtype: str) -> bool:
    return (dtype or "").lower().strip() == "rapide xl"

def _to_money(x) -> float:
    try:
        if isinstance(x, (int, float)):
            return float(x)
        return 0.0
    except Exception:
        return 0.0

def build_magasin_summary(rows_all: List[Dict], mode: str) -> str:
    """
    mode:
      HYPER      -> stores starting with 'Hyper'
      RAPIDEXL   -> delivery_type == 'rapide xl'
      EXPRESS    -> delivery_type == 'express'
    Summary (global, not per store), with emojis.
    """
    now = datetime.datetime.utcnow()
    settings = runtime_settings()
    accept_max_min = int(settings.get("accept_max_min") or 10)
    prep_max_min = int(settings.get("prep_max_min") or 60)
    load_max_min = int(settings.get("load_max_min") or 20)
    deliv_max_min = int(settings.get("deliv_max_min") or 120)

    def keep(o: Dict) -> bool:
        if mode == "HYPER":
            return _class_store_is_hyper(o.get("store"))
        if mode == "RAPIDEXL":
            return _class_is_rapide_xl(o.get("delivery_type"))
        if mode == "EXPRESS":
            return _class_is_express(o.get("delivery_type"))
        return False

    items = [o for o in rows_all if keep(o)]
    title = {"HYPER":"🏬 Hyper", "RAPIDEXL":"🚚 Rapide XL", "EXPRESS":"⚡ Express"}.get(mode, mode)

    if not items:
        return f"{title}\n\n⚠️ Aucune donnée."

    total_cmd = len(items)
    retard_cmd = sum(1 for o in items if _is_lastmile_retard(o.get("last_mile_status")))
    cancel_cmd = sum(1 for o in items if _is_cancelled(o.get("status")))

    # SLA totals (counts of orders currently late at that stage)
    accept_late = prep_late = load_late = deliv_late = 0
    ca = 0.0
    ca_perdu = 0.0

    for o in items:
        ca += _to_money(o.get("total_paid"))
        init_amt = _to_money(o.get("initial_amount"))
        paid = _to_money(o.get("total_paid"))
        ca_perdu += (init_amt - paid)

        order_date = o.get("order_date")
        accept_dt = o.get("accept_dt")
        prep_end = o.get("prep_end_dt")
        load_dt = o.get("load_dt")
        close_dt = o.get("close_dt")

        def is_late(done_dt, ref_dt, max_min):
            if done_dt:
                return False
            if not ref_dt:
                return False
            return (now - ref_dt).total_seconds() > max_min * 60

        if is_late(accept_dt, order_date, accept_max_min):
            accept_late += 1
        if is_late(prep_end, accept_dt or order_date, prep_max_min):
            prep_late += 1
        if is_late(load_dt, prep_end or accept_dt or order_date, load_max_min):
            load_late += 1
        if is_late(close_dt, load_dt or prep_end or accept_dt or order_date, deliv_max_min):
            deliv_late += 1

    lines = [
        f"{title}",
        "",
        f"📦 Nombre de commande: {total_cmd}",
        f"🚨 Nombre de commande retard: {retard_cmd}",
        f"❌ Nombre de commande annuler: {cancel_cmd}",
        "",
        f"⏱ Totale retard acceptation: {accept_late}",
        f"🍳 Totale retard preparation: {prep_late}",
        f"📦 Totale retard chargement: {load_late}",
        f"🏁 Totale retard livraison: {deliv_late}",
        "",
        f"💰 Chiffre d'affaire: {ca:.2f}",
        f"💸 Chiffre d'affire perdu: {ca_perdu:.2f}",
    ]
    return "\n".join(lines)
def available_dates_for_store(rows: List[Dict], store_name: str) -> List[str]:
    target = norm_store_key(store_name)
    dates = sorted({
        (r.get("day_of_delivery") or "").strip()
        for r in rows
        if norm_store_key(r.get("store") or "") == target
        and (r.get("day_of_delivery") or "").strip()
    })
    return dates

def filter_orders(rows: List[Dict], store_name: str, slot: str, day_of_delivery: str) -> List[Tuple[str, float, float]]:
    out = []
    target_store = norm_store_key(store_name)

    for r in rows:
        if norm_store_key(r.get("store") or "") != target_store:
            continue
        if (r.get("day_of_delivery") or "").strip() != (day_of_delivery or "").strip():
            continue
        if norm_slot(r["slot"]) != norm_slot(slot):
            continue
        out.append((r["order_id"], r["lat"], r["lon"]))
    return out





# ============================
# VRP utilities
# ============================
def haversine_m(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def build_distance_matrix(points: List[Tuple[float, float]]) -> List[List[int]]:
    n = len(points)
    mat = [[0] * n for _ in range(n)]
    for i in range(n):
        lat1, lon1 = points[i]
        for j in range(n):
            if i == j:
                continue
            lat2, lon2 = points[j]
            mat[i][j] = int(haversine_m(lat1, lon1, lat2, lon2))
    return mat


def solve_vrp(
    depot: Tuple[float, float],
    orders: List[Tuple[str, float, float]],
    num_vehicles: int,
    time_limit_seconds: Optional[int] = None,
    return_to_depot: Optional[bool] = None,
) -> List[List[int]]:
    """
    VRP tuned to:
      - NEVER return "0 orders for everyone" (huge drop penalty)
      - Use all vehicles when possible (hard constraint: each vehicle >= 1 order if K <= N)
      - Keep optimization strong (GUIDED_LOCAL_SEARCH) and reduce zig-zag by distance minimization + balancing span
    Returns: list of routes as node indices (1..N) for each vehicle.
    """
    if num_vehicles <= 0:
        return []

    if return_to_depot is None:
        return_to_depot = bool(runtime_settings().get("return_to_depot", False))

    points = [depot] + [(lat, lon) for (_, lat, lon) in orders]
    dist = build_distance_matrix(points)

    manager = pywrapcp.RoutingIndexManager(len(points), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    # ---- Distance cost ----
    def dist_cb(from_index, to_index):
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        # Open route: the way back to the store is free.
        if not return_to_depot and j == 0:
            return 0
        return dist[i][j]

    transit_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # ---- Make orders droppable with a HUGE penalty (so solver serves them) ----
    drop_penalty = 10**7
    for node in range(1, len(points)):
        routing.AddDisjunction([manager.NodeToIndex(node)], drop_penalty)

    # ---- Count / balancing ----
    n = len(orders)
    k = max(1, num_vehicles)
    min_load = n // k
    max_load = int(math.ceil(n / k))

    def demand_cb(from_index):
        node = manager.IndexToNode(from_index)
        return 0 if node == 0 else 1

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)

    routing.AddDimensionWithVehicleCapacity(
        demand_idx,
        0,
        [max_load] * num_vehicles,
        True,
        "Count"
    )
    count_dim = routing.GetDimensionOrDie("Count")

    # Encourage balance
    count_dim.SetGlobalSpanCostCoefficient(2000)

    # ---- HARD: force usage of all vehicles if possible ----
    # If K <= N, each vehicle must take >= 1 order (no more "Livreur X: 0 commande").
    if num_vehicles <= n:
        solver = routing.solver()
        for v in range(num_vehicles):
            solver.Add(count_dim.CumulVar(routing.End(v)) >= 1)

    # Stabilize decisions
    for v in range(num_vehicles):
        routing.AddVariableMinimizedByFinalizer(count_dim.CumulVar(routing.End(v)))

    # ---- Search parameters ----
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.AUTOMATIC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    if time_limit_seconds is None:
        time_limit_seconds = int(runtime_settings().get("planner_time_limit_sec") or 12)
    params.time_limit.FromSeconds(max(1, int(time_limit_seconds)))

    solution = routing.SolveWithParameters(params)
    if not solution:
        return [[] for _ in range(num_vehicles)]

    routes = []
    for v in range(num_vehicles):
        idx = routing.Start(v)
        stops = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                stops.append(node)
            idx = solution.Value(routing.NextVar(idx))
        routes.append(stops)
    return routes




def route_distance_km(
    depot: Tuple[float, float],
    stops_latlon: List[Tuple[float, float]],
    return_to_depot: bool = False,
) -> float:
    if not stops_latlon:
        return 0.0
    points = [depot] + list(stops_latlon)
    if return_to_depot:
        points.append(depot)
    total_m = sum(
        haversine_m(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )
    return total_m / 1000.0


def google_maps_link(depot: Tuple[float, float], stops_latlon: List[Tuple[float, float]]) -> str:
    if not stops_latlon:
        return ""
    origin = f"{depot[0]},{depot[1]}"
    dest = f"{stops_latlon[-1][0]},{stops_latlon[-1][1]}"
    waypoints = ""
    if len(stops_latlon) > 1:
        waypoints = "|".join([f"{lat},{lon}" for (lat, lon) in stops_latlon[:-1]])
    url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={dest}"
    if waypoints:
        url += f"&waypoints={waypoints}"
    return url


# ============================
# Telegram UI helpers
# ============================
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("Nouveau trajet", callback_data="NEW")],
        [InlineKeyboardButton("Commande", callback_data="CMD_MENU")],
        [InlineKeyboardButton("Magasin", callback_data="STORE_MENU")],
        [InlineKeyboardButton("Actualisation", callback_data="REFRESH")],
    ]
    return InlineKeyboardMarkup(kb)


def chunk_buttons(buttons: List[InlineKeyboardButton], per_row: int = 2) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


def get_store_max_drivers(store_name: str) -> int:
    """Return max allowed drivers for a store (fallback 10 if unknown)."""
    if not store_name:
        return 10
    store = find_store_by_name(store_name, active_only=False)
    if not store:
        return 10
    return int(store.get("max_drivers") or 10)


def get_store_capacity(store_name: str) -> int:
    if not store_name:
        return 0
    store = find_store_by_name(store_name, active_only=False)
    if not store:
        return 0
    return int(store.get("capacity") or 0)


def compute_allowed_driver_choices(store_name: str, orders_count: int) -> int:
    """Max drivers the user is allowed to choose for this store+slot."""
    store_cap = get_store_max_drivers(store_name)
    if orders_count is None:
        orders_count = 0
    return max(0, min(store_cap, int(orders_count)))



async def ensure_redash_cache(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> List[Dict]:
    if (not force) and context.user_data.get(CTX_REDASH_CACHE):
        return context.user_data[CTX_REDASH_CACHE]
    csv_text = fetch_redash_csv_text()
    rows = parse_redash_rows(csv_text)
    context.user_data[CTX_REDASH_CACHE] = rows
    return rows


async def ensure_stores(context: ContextTypes.DEFAULT_TYPE) -> List[Dict]:
    stores = list_stores(active_only=True)
    context.user_data[CTX_STORES] = stores
    return stores


def build_store_keyboard(stores: List[Dict]) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(store["store_name"], callback_data=f"STORE::{store['store_id']}")
        for store in stores
    ]
    keyboard = chunk_buttons(buttons, per_row=2)
    keyboard.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])
    return InlineKeyboardMarkup(keyboard)


def get_effective_user_id(update: Update) -> Optional[int]:
    user = update.effective_user
    return user.id if user else None


def is_admin_user_id(user_id: Optional[int]) -> bool:
    return bool(user_id and user_id in ADMIN_USER_IDS)


def is_admin_update(update: Update) -> bool:
    return is_admin_user_id(get_effective_user_id(update))


def set_settings_action(context: ContextTypes.DEFAULT_TYPE, action: Optional[Dict]) -> None:
    if action is None:
        context.user_data.pop(CTX_SETTINGS_ACTION, None)
        context.user_data.pop(CTX_SETTINGS_STORE_ID, None)
        return
    context.user_data[CTX_SETTINGS_ACTION] = action
    if action.get("store_id"):
        context.user_data[CTX_SETTINGS_STORE_ID] = action["store_id"]


def get_settings_action(context: ContextTypes.DEFAULT_TYPE) -> Optional[Dict]:
    action = context.user_data.get(CTX_SETTINGS_ACTION)
    return action if isinstance(action, dict) else None


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "-"
    if len(value) <= 6:
        return "*" * len(value)
    return value[:4] + "*" * (len(value) - 6) + value[-2:]


def settings_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Magasins", callback_data="SET::SEC::STORES")],
        [InlineKeyboardButton("Créneaux", callback_data="SET::SEC::SLOTS")],
        [InlineKeyboardButton("Redash", callback_data="SET::SEC::REDASH")],
        [InlineKeyboardButton("Règles", callback_data="SET::SEC::RULES")],
        [InlineKeyboardButton("SLA", callback_data="SET::SEC::SLA")],
        [InlineKeyboardButton("Planning livreurs", callback_data="SET::SEC::PLANNING")],
        [InlineKeyboardButton("Infos", callback_data="SET::SEC::INFO")],
        [InlineKeyboardButton("Fermer", callback_data="SET::CLOSE")],
    ])


def settings_stores_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Voir les magasins", callback_data="SET::STORES::LIST")],
        [InlineKeyboardButton("Modifier un magasin", callback_data="SET::STORES::EDIT")],
        [InlineKeyboardButton("Ajouter un magasin", callback_data="SET::STORES::ADD")],
        [InlineKeyboardButton("Retour", callback_data="SET::ROOT")],
    ])


def settings_store_select_kb() -> InlineKeyboardMarkup:
    stores = list_stores(active_only=False)
    rows = []
    for store in stores:
        label = f"{'🟢' if store['is_active'] else '⚪'} {store['store_name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"SET::STORE::{store['store_id']}")])
    rows.append([InlineKeyboardButton("Retour", callback_data="SET::SEC::STORES")])
    return InlineKeyboardMarkup(rows)


def settings_store_detail_kb(store: Dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Nom", callback_data=f"SET::STOREFIELD::NAME::{store['store_id']}")],
        [InlineKeyboardButton("Max livreurs", callback_data=f"SET::STOREFIELD::MAXDRIVERS::{store['store_id']}")],
        [InlineKeyboardButton("Capacité", callback_data=f"SET::STOREFIELD::CAPACITY::{store['store_id']}")],
        [InlineKeyboardButton("Coordonnées", callback_data=f"SET::STOREFIELD::COORDS::{store['store_id']}")],
        [InlineKeyboardButton("Activer / Désactiver", callback_data=f"SET::STORETOGGLE::{store['store_id']}")],
        [InlineKeyboardButton("Retour", callback_data="SET::STORES::EDIT")],
    ])


def settings_slots_kb() -> InlineKeyboardMarkup:
    slots = configured_slots()
    rows = [[InlineKeyboardButton("Ajouter un créneau", callback_data="SET::SLOTS::ADD")]]
    for idx, slot in enumerate(slots):
        rows.append([InlineKeyboardButton(f"Supprimer {slot}", callback_data=f"SET::SLOTDEL::{idx}")])
    rows.append([InlineKeyboardButton("Retour", callback_data="SET::ROOT")])
    return InlineKeyboardMarkup(rows)


def settings_redash_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Changer l'URL CSV", callback_data="SET::REDASH::URL")],
        [InlineKeyboardButton("Changer l'API key", callback_data="SET::REDASH::APIKEY")],
        [InlineKeyboardButton("Tester la connexion", callback_data="SET::REDASH::TEST")],
        [InlineKeyboardButton("Retour", callback_data="SET::ROOT")],
    ])


def settings_rules_kb() -> InlineKeyboardMarkup:
    return_to_depot = runtime_settings().get("return_to_depot", False)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Status requis", callback_data="SET::RULE::STATUS")],
        [InlineKeyboardButton("Delivery Type requis", callback_data="SET::RULE::DTYPE")],
        [InlineKeyboardButton("Max commandes", callback_data="SET::RULE::MAXORDERS")],
        [InlineKeyboardButton(
            f"Retour magasin : {'Oui' if return_to_depot else 'Non'}",
            callback_data="SET::RULETOGGLE::RETURN",
        )],
        [InlineKeyboardButton("Retour", callback_data="SET::ROOT")],
    ])


def settings_sla_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Acceptation", callback_data="SET::SLA::ACCEPT")],
        [InlineKeyboardButton("Préparation", callback_data="SET::SLA::PREP")],
        [InlineKeyboardButton("Chargement", callback_data="SET::SLA::LOAD")],
        [InlineKeyboardButton("Livraison", callback_data="SET::SLA::DELIV")],
        [InlineKeyboardButton("Retour", callback_data="SET::ROOT")],
    ])


def settings_planning_stores_kb() -> InlineKeyboardMarkup:
    rows = []
    for store in list_stores(active_only=False):
        count = len(get_store_schedule(store["store_id"]))
        label = f"{store['store_name']} ({count} livreurs)" if count else f"{store['store_name']} (vide)"
        rows.append([InlineKeyboardButton(label, callback_data=f"SET::PLANSTORE::{store['store_id']}")])
    rows.append([InlineKeyboardButton("Retour", callback_data="SET::ROOT")])
    return InlineKeyboardMarkup(rows)


def settings_planning_store_kb(store_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Importer Excel", callback_data=f"SET::PLANUP::{store_id}")],
        [InlineKeyboardButton("Retour", callback_data="SET::SEC::PLANNING")],
    ])


def format_planning_store_text(store: Dict) -> str:
    schedule = get_store_schedule(store["store_id"])
    text = format_schedule_text(store["store_name"], schedule)
    today = datetime.date.today()
    totals = get_driver_km_totals(store["store_id"], today.isoformat(), today.isoformat())
    if schedule:
        text += f"\n\n📏 Km validés aujourd'hui ({today.strftime('%d/%m/%Y')}) :"
        for name in sorted((d["name"] for d in schedule), key=lambda n: totals.get(n, 0.0)):
            text += f"\n• {name} : {totals.get(name, 0.0):.1f} km"
    return text


def format_store_settings(store: Dict) -> str:
    return "\n".join([
        f"🏬 {store['store_name']}",
        f"Code: {store['store_id']}",
        f"Coordonnées: {store['lat']}, {store['lon']}",
        f"Max livreurs: {store['max_drivers']}",
        f"Capacité: {store['capacity']}",
        f"Statut: {'Actif' if store['is_active'] else 'Inactif'}",
    ])


def format_rules_settings(settings: Dict) -> str:
    return "\n".join([
        "📋 Règles métier",
        "",
        f"Status requis: {settings['required_status'] or '-'}",
        f"Delivery Type requis: {settings['required_delivery_type'] or '-'}",
        f"Max commandes: {settings['max_orders']}",
        f"Retour magasin: {'Oui' if settings.get('return_to_depot') else 'Non'}",
    ])


def format_settings_info() -> str:
    settings = runtime_settings()
    stats = get_dashboard_stats()
    return "\n".join([
        "⚙️ Bringo Routes Optimizer V3",
        "",
        f"Admins Telegram: {admin_ids_text()}",
        f"Magasins actifs: {stats['stores_active']}/{stats['stores_total']}",
        f"Créneaux: {stats['slots_total']}",
        f"Max commandes: {settings['max_orders']}",
        f"Status requis: {settings['required_status'] or '-'}",
        f"Delivery Type requis: {settings['required_delivery_type'] or '-'}",
        f"Temps solveur: {settings['planner_time_limit_sec']}s",
        f"Retour magasin: {'Oui' if settings.get('return_to_depot') else 'Non'}",
        f"Redash API key: {mask_secret(settings['redash_api_key'])}",
    ])


def format_stores_list() -> str:
    stores = list_stores(active_only=False)
    if not stores:
        return "Aucun magasin configuré."
    lines = ["🏬 Magasins configurés", ""]
    for store in stores:
        lines.append(
            f"{'🟢' if store['is_active'] else '⚪'} {store['store_name']} | "
            f"{store['store_id']} | max={store['max_drivers']} | cap={store['capacity']}"
        )
    return "\n".join(lines)


def parse_store_payload(raw_text: str) -> Dict:
    parts = [part.strip() for part in (raw_text or "").split("|")]
    if len(parts) != 6:
        raise ValueError(
            "Format invalide. Utilise: CODE | Nom magasin | latitude | longitude | max_livreurs | capacité"
        )
    return {
        "store_id": parts[0],
        "store_name": parts[1],
        "lat": float(parts[2]),
        "lon": float(parts[3]),
        "max_drivers": int(parts[4]),
        "capacity": int(parts[5]),
        "is_active": True,
    }


def store_wizard_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Annuler l'ajout", callback_data="SET::STOREADD::CANCEL")]
    ])


def store_wizard_prompt(step: str, data: Optional[Dict] = None) -> str:
    data = data or {}
    labels = {
        "store_id": "code magasin",
        "store_name": "nom magasin",
        "lat": "latitude",
        "lon": "longitude",
        "max_drivers": "max livreurs",
        "capacity": "capacité",
    }
    step_index = STORE_WIZARD_FIELDS.index(step) + 1
    prompts = {
        "store_id": "Étape 1/6\nEnvoie le code magasin. Exemple: HBF",
        "store_name": "Étape 2/6\nEnvoie le nom exact du magasin.\nIl doit correspondre au nom utilisé dans Redash.",
        "lat": f"Étape 3/6\nEnvoie la latitude pour {data.get('store_name') or 'ce magasin'}.",
        "lon": f"Étape 4/6\nEnvoie la longitude pour {data.get('store_name') or 'ce magasin'}.",
        "max_drivers": f"Étape 5/6\nEnvoie le max livreurs pour {data.get('store_name') or 'ce magasin'}.",
        "capacity": f"Étape 6/6\nEnvoie la capacité du magasin {data.get('store_name') or ''}.",
    }
    summary = []
    for field in STORE_WIZARD_FIELDS:
        if field == step:
            break
        if field in data:
            summary.append(f"- {labels[field]}: {data[field]}")
    prefix = "Assistant ajout magasin\n\n"
    if summary:
        prefix += "Déjà saisi:\n" + "\n".join(summary) + "\n\n"
    return prefix + prompts.get(step, f"Étape {step_index}/6\nEnvoie la valeur.")


def store_wizard_confirm_text(data: Dict) -> str:
    return "\n".join([
        "Confirme l'ajout de ce magasin :",
        "",
        f"Code: {data['store_id']}",
        f"Nom: {data['store_name']}",
        f"Latitude: {data['lat']}",
        f"Longitude: {data['lon']}",
        f"Max livreurs: {data['max_drivers']}",
        f"Capacité: {data['capacity']}",
    ])


def store_wizard_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Confirmer", callback_data="SET::STOREADD::CONFIRM")],
        [InlineKeyboardButton("Annuler", callback_data="SET::STOREADD::CANCEL")],
    ])


def parse_store_wizard_value(step: str, raw_text: str, data: Dict) -> object:
    text = (raw_text or "").strip()
    if step == "store_id":
        if not text:
            raise ValueError("Le code magasin est obligatoire.")
        if get_store(text, active_only=False):
            raise ValueError("Ce code magasin existe déjà.")
        return text
    if step == "store_name":
        if not text:
            raise ValueError("Le nom magasin est obligatoire.")
        return text
    if step == "lat":
        return parse_float_field(text, "La latitude")
    if step == "lon":
        return parse_float_field(text, "La longitude")
    if step == "max_drivers":
        return parse_int_field(text, "Le max livreurs", minimum=0)
    if step == "capacity":
        return parse_int_field(text, "La capacité", minimum=0)
    raise ValueError("Étape inconnue.")


def build_route_plan(
    store: Dict,
    day_of_delivery: str,
    slot: str,
    requested_drivers: int,
    rows: Optional[List[Dict]] = None,
) -> Dict:
    if rows is None:
        rows = parse_redash_rows(fetch_redash_csv_text())

    store_name = store["store_name"]
    depot = (store["lat"], store["lon"])
    orders = filter_orders(rows, store_name, slot, day_of_delivery)
    store_cap = get_store_max_drivers(store_name)
    allowed_max = compute_allowed_driver_choices(store_name, len(orders))
    selected_drivers = max(1, int(requested_drivers or 1))
    if allowed_max <= 0:
        selected_drivers = 0
    else:
        selected_drivers = min(selected_drivers, allowed_max)

    return_to_depot = bool(runtime_settings().get("return_to_depot", False))
    routes_nodes = (
        solve_vrp(depot, orders, selected_drivers, return_to_depot=return_to_depot)
        if orders and selected_drivers > 0
        else []
    )
    capacity = int(store.get("capacity") or 0)

    routes = []
    for index in range(selected_drivers):
        stops_nodes = routes_nodes[index] if index < len(routes_nodes) else []
        stop_orders = [orders[node - 1] for node in stops_nodes]
        order_ids = [order[0] for order in stop_orders]
        latlons = [(order[1], order[2]) for order in stop_orders]
        routes.append(
            {
                "driver_number": index + 1,
                "order_count": len(order_ids),
                "order_ids": order_ids,
                "distance_km": round(route_distance_km(depot, latlons, return_to_depot), 1),
                "map_url": google_maps_link(depot, latlons) if order_ids else "",
            }
        )

    warning = ""
    if requested_drivers and allowed_max > 0 and int(requested_drivers) > selected_drivers:
        warning = f"Le nombre de livreurs demandé a été plafonné à {selected_drivers}."
    elif capacity > 0 and len(orders) > capacity:
        warning = (
            f"La capacité configurée pour {store_name} est {capacity}, "
            f"mais {len(orders)} commandes ont été trouvées pour ce créneau."
        )

    return {
        "store_id": store["store_id"],
        "store_name": store_name,
        "day_of_delivery": day_of_delivery,
        "slot": slot,
        "order_count": len(orders),
        "store_cap": store_cap,
        "capacity": capacity,
        "allowed_max": allowed_max,
        "requested_drivers": int(requested_drivers or 1),
        "num_drivers": selected_drivers if orders else 0,
        "routes": routes,
        "warning": warning,
    }


def apply_planning_to_plan(result: Dict, store: Dict, present: List[str], off: List[str], delivery_date) -> Dict:
    """Remplace 'Livreur 1, 2...' par les noms du planning, en respectant l'équité des km du mois."""
    day_iso = delivery_date.isoformat()
    # Équité sur la journée uniquement : seuls les créneaux validés du même jour comptent.
    cumul = get_driver_km_totals(
        store["store_id"], day_iso, day_iso, exclude_slot=(day_iso, result["slot"])
    )
    routes = result["routes"]
    names = assign_routes_fairly([r["distance_km"] for r in routes], present, cumul)
    for route, name in zip(routes, names):
        route["driver_name"] = name
        route["cumul_km"] = round(cumul.get(name, 0.0), 1)
    used = set(names)
    result["planning"] = {
        "weekday": DAY_NAMES_FR[delivery_date.weekday()],
        "present": present,
        "off": off,
        "reserve": [n for n in present if n not in used],
        "day_iso": day_iso,
    }
    return result


def format_route_plan_text(result: Dict) -> str:
    planning = result.get("planning")
    lines = [
        f"📍 Magasin: {result['store_name']}",
        f"📅 Date: {result['day_of_delivery']}",
        f"🕒 Créneau: {result['slot']}",
        f"📦 Commandes: {result['order_count']}",
        f"🚴 Livreurs: {result['num_drivers']}",
    ]
    if planning and planning["reserve"]:
        lines.append("🔁 Réserve (non utilisés): " + ", ".join(planning["reserve"]))
    lines.append("")

    for route in result["routes"]:
        label = route.get("driver_name") or f"Livreur {route['driver_number']}"
        if not route["order_ids"]:
            lines += [f"👤 {label}: 0 commande", "—"]
            continue

        lines.append(
            f"👤 {label}: {route['order_count']} commandes"
            f" · {route.get('distance_km', 0):.1f} km"
        )
        lines.append("🧾 " + " → ".join(route["order_ids"]))
        if route["map_url"]:
            lines.append("🗺️ " + route["map_url"])
        lines.append("—")

    return "\n".join(lines)


# ============================
# Handlers
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("/start received from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await ensure_stores(context)
    if update.message:
        extra = ""
        if is_admin_update(update):
            extra = "\nAdmin: utilise /settings pour modifier la configuration globale."
        await update.message.reply_text(
            "Bringo Routes Optimizer V3🔥\nChoisis une action :" + extra,
            reply_markup=main_menu_kb(),
        )
        return
    log.info("✅ /start received from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await ensure_stores(context)
    if update.message:
        await update.message.reply_text("Bot Last Mile Bringo ✅\nChoisis une action :", reply_markup=main_menu_kb())



async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    user = update.effective_user
    await update.message.reply_text(
        f"Ton Telegram user id: {user.id if user else '-'}\nAdmins configurés: {admin_ids_text()}"
    )


async def deny_settings_access(update: Update):
    text = (
        "⛔ Accès admin refusé.\n"
        "Configure ADMIN_USER_IDS avec ton Telegram user id puis redémarre le bot."
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_kb())
    elif update.message:
        await update.message.reply_text(text, reply_markup=main_menu_kb())


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        await deny_settings_access(update)
        return ConversationHandler.END

    set_settings_action(context, None)
    if update.message:
        await update.message.reply_text(
            "⚙️ Paramètres admin\nChoisis une section à modifier :",
            reply_markup=settings_main_kb(),
        )
    return SETTINGS_MENU


async def settings_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if not is_admin_update(update):
        await deny_settings_access(update)
        return ConversationHandler.END

    data = q.data or ""
    settings = runtime_settings()

    if data == "SET::CLOSE":
        set_settings_action(context, None)
        await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if data == "SET::ROOT":
        set_settings_action(context, None)
        await q.edit_message_text(
            "⚙️ Paramètres admin\nChoisis une section à modifier :",
            reply_markup=settings_main_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::STOREADD::CANCEL":
        set_settings_action(context, None)
        await q.edit_message_text("Ajout magasin annulé.", reply_markup=settings_stores_kb())
        return SETTINGS_MENU

    if data == "SET::STOREADD::CONFIRM":
        action = get_settings_action(context)
        if not action or action.get("type") != "ADD_STORE_WIZARD":
            await q.edit_message_text("Aucun ajout magasin en cours.", reply_markup=settings_stores_kb())
            return SETTINGS_MENU

        data_payload = action.get("data") or {}
        missing = [field for field in STORE_WIZARD_FIELDS if field not in data_payload]
        if missing:
            await q.edit_message_text(
                "Ajout incomplet. Reprends l'assistant depuis le menu magasins.",
                reply_markup=settings_stores_kb(),
            )
            set_settings_action(context, None)
            return SETTINGS_MENU

        save_store(
            store_id=data_payload["store_id"],
            store_name=data_payload["store_name"],
            lat=data_payload["lat"],
            lon=data_payload["lon"],
            max_drivers=data_payload["max_drivers"],
            capacity=data_payload["capacity"],
            is_active=True,
        )
        set_settings_action(context, None)
        await q.edit_message_text(
            "✅ Magasin ajouté.\n\n" + format_stores_list(),
            reply_markup=settings_stores_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::SEC::STORES":
        set_settings_action(context, None)
        await q.edit_message_text("🏬 Gestion des magasins", reply_markup=settings_stores_kb())
        return SETTINGS_MENU

    if data == "SET::STORES::LIST":
        await q.edit_message_text(format_stores_list(), reply_markup=settings_stores_kb())
        return SETTINGS_MENU

    if data == "SET::STORES::EDIT":
        await q.edit_message_text(
            "Choisis un magasin à modifier :",
            reply_markup=settings_store_select_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::STORES::ADD":
        set_settings_action(context, {"type": "ADD_STORE_WIZARD", "step": "store_id", "data": {}})
        await q.edit_message_text(
            store_wizard_prompt("store_id"),
            reply_markup=store_wizard_cancel_kb(),
        )
        return SETTINGS_INPUT

    if data.startswith("SET::STORE::"):
        store_id = data.split("::", 2)[2]
        store = get_store(store_id, active_only=False)
        if not store:
            await q.edit_message_text("❌ Magasin introuvable.", reply_markup=settings_store_select_kb())
            return SETTINGS_MENU
        await q.edit_message_text(format_store_settings(store), reply_markup=settings_store_detail_kb(store))
        return SETTINGS_MENU

    if data.startswith("SET::STORETOGGLE::"):
        store_id = data.split("::", 2)[2]
        store = get_store(store_id, active_only=False)
        if not store:
            await q.edit_message_text("❌ Magasin introuvable.", reply_markup=settings_store_select_kb())
            return SETTINGS_MENU
        set_store_active(store_id, not store["is_active"])
        store = get_store(store_id, active_only=False)
        await q.edit_message_text(
            "✅ Statut du magasin mis à jour.\n\n" + format_store_settings(store),
            reply_markup=settings_store_detail_kb(store),
        )
        return SETTINGS_MENU

    if data.startswith("SET::STOREFIELD::"):
        _, _, field, store_id = data.split("::", 3)
        store = get_store(store_id, active_only=False)
        if not store:
            await q.edit_message_text("❌ Magasin introuvable.", reply_markup=settings_store_select_kb())
            return SETTINGS_MENU
        prompts = {
            "NAME": "Envoie le nouveau nom du magasin. Il doit correspondre au nom utilisé dans Redash.",
            "MAXDRIVERS": f"Envoie le nouveau max livreurs pour {store['store_name']}.",
            "CAPACITY": f"Envoie la nouvelle capacité pour {store['store_name']}.",
            "COORDS": "Envoie les coordonnées au format: latitude,longitude",
        }
        set_settings_action(context, {"type": "STORE_FIELD", "field": field, "store_id": store_id})
        await q.edit_message_text(
            prompts.get(field, "Envoie la nouvelle valeur."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data=f"SET::STORE::{store_id}")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::SEC::SLOTS":
        set_settings_action(context, None)
        slots_text = "\n".join(f"• {slot}" for slot in configured_slots()) or "Aucun créneau."
        await q.edit_message_text(
            "🕒 Créneaux configurés\n\n" + slots_text,
            reply_markup=settings_slots_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::SLOTS::ADD":
        set_settings_action(context, {"type": "ADD_SLOT"})
        await q.edit_message_text(
            "Envoie le nouveau créneau au format HH:MM - HH:MM",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::SEC::SLOTS")]]),
        )
        return SETTINGS_INPUT

    if data.startswith("SET::SLOTDEL::"):
        idx = int(data.split("::", 2)[2])
        slots = configured_slots()
        if 0 <= idx < len(slots):
            new_slots = [slot for pos, slot in enumerate(slots) if pos != idx]
            if not new_slots:
                await q.edit_message_text("❌ Il faut garder au moins un créneau.", reply_markup=settings_slots_kb())
                return SETTINGS_MENU
            update_app_settings({"slots": new_slots})
            slots = new_slots
        await q.edit_message_text(
            "✅ Créneaux mis à jour.\n\n" + "\n".join(f"• {slot}" for slot in slots),
            reply_markup=settings_slots_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::SEC::REDASH":
        set_settings_action(context, None)
        await q.edit_message_text(
            "\n".join([
                "🔗 Paramètres Redash",
                "",
                f"URL: {settings['redash_results_csv_url']}",
                f"API key: {mask_secret(settings['redash_api_key'])}",
            ]),
            reply_markup=settings_redash_kb(),
        )
        return SETTINGS_MENU

    if data == "SET::REDASH::URL":
        set_settings_action(context, {"type": "REDASH_URL"})
        await q.edit_message_text(
            "Envoie la nouvelle URL CSV Redash.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::SEC::REDASH")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::REDASH::APIKEY":
        set_settings_action(context, {"type": "REDASH_API_KEY"})
        await q.edit_message_text(
            "Envoie la nouvelle API key Redash.\nEnvoie VIDE pour supprimer la clé.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::SEC::REDASH")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::REDASH::TEST":
        try:
            csv_text = fetch_redash_csv_text()
            rows = parse_redash_rows(csv_text)
            rows_all = parse_redash_rows_all(csv_text)
            await q.edit_message_text(
                "\n".join([
                    "✅ Connexion Redash OK",
                    f"Lignes filtrées: {len(rows)}",
                    f"Lignes totales: {len(rows_all)}",
                ]),
                reply_markup=settings_redash_kb(),
            )
        except Exception as exc:
            await q.edit_message_text(f"❌ Test Redash échoué:\n{exc}", reply_markup=settings_redash_kb())
        return SETTINGS_MENU

    if data == "SET::SEC::RULES":
        set_settings_action(context, None)
        await q.edit_message_text(format_rules_settings(settings), reply_markup=settings_rules_kb())
        return SETTINGS_MENU

    if data == "SET::RULETOGGLE::RETURN":
        update_app_settings({"return_to_depot": not settings.get("return_to_depot", False)})
        await q.edit_message_text(
            "✅ Règle mise à jour.\n\n" + format_rules_settings(runtime_settings()),
            reply_markup=settings_rules_kb(),
        )
        return SETTINGS_MENU

    if data.startswith("SET::RULE::"):
        field = data.split("::", 2)[2]
        prompts = {
            "STATUS": "Envoie le status requis. Exemple: new",
            "DTYPE": "Envoie le delivery type requis. Exemple: planned order",
            "MAXORDERS": "Envoie le nouveau max commandes.",
        }
        set_settings_action(context, {"type": "RULE", "field": field})
        await q.edit_message_text(
            prompts.get(field, "Envoie la nouvelle valeur."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::SEC::RULES")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::SEC::SLA":
        set_settings_action(context, None)
        await q.edit_message_text(
            "\n".join([
                "⏱ SLA",
                "",
                f"Acceptation: {settings['accept_max_min']} min",
                f"Préparation: {settings['prep_max_min']} min",
                f"Chargement: {settings['load_max_min']} min",
                f"Livraison: {settings['deliv_max_min']} min",
            ]),
            reply_markup=settings_sla_kb(),
        )
        return SETTINGS_MENU

    if data.startswith("SET::SLA::"):
        field = data.split("::", 2)[2]
        prompts = {
            "ACCEPT": "Envoie le SLA acceptation en minutes.",
            "PREP": "Envoie le SLA préparation en minutes.",
            "LOAD": "Envoie le SLA chargement en minutes.",
            "DELIV": "Envoie le SLA livraison en minutes.",
        }
        set_settings_action(context, {"type": "SLA", "field": field})
        await q.edit_message_text(
            prompts.get(field, "Envoie la nouvelle valeur."),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::SEC::SLA")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::SEC::PLANNING":
        set_settings_action(context, None)
        await q.edit_message_text(
            "📅 Planning livreurs\nChoisis un magasin :",
            reply_markup=settings_planning_stores_kb(),
        )
        return SETTINGS_MENU

    if data.startswith("SET::PLANSTORE::"):
        set_settings_action(context, None)
        store = get_store(data.split("::", 2)[2])
        if not store:
            await q.edit_message_text("Magasin introuvable.", reply_markup=settings_planning_stores_kb())
            return SETTINGS_MENU
        await q.edit_message_text(
            format_planning_store_text(store),
            reply_markup=settings_planning_store_kb(store["store_id"]),
        )
        return SETTINGS_MENU

    if data.startswith("SET::PLANUP::"):
        store = get_store(data.split("::", 2)[2])
        if not store:
            await q.edit_message_text("Magasin introuvable.", reply_markup=settings_planning_stores_kb())
            return SETTINGS_MENU
        set_settings_action(context, {"type": "PLANNING_UPLOAD", "store_id": store["store_id"]})
        await q.edit_message_text(
            f"📤 Envoie le fichier Excel (.xlsx) du planning de {store['store_name']}.\n\n"
            "Format : 1ère ligne = LUNDI … DIMANCHE, 1ère colonne = noms des livreurs, "
            "X = travaille, OFF = repos.\nL'ancien planning de ce magasin sera remplacé.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data=f"SET::PLANSTORE::{store['store_id']}")]]),
        )
        return SETTINGS_INPUT

    if data == "SET::SEC::INFO":
        set_settings_action(context, None)
        await q.edit_message_text(
            format_settings_info(),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Retour", callback_data="SET::ROOT")]]),
        )
        return SETTINGS_MENU

    await q.edit_message_text(
        "⚙️ Paramètres admin\nChoisis une section à modifier :",
        reply_markup=settings_main_kb(),
    )
    return SETTINGS_MENU


async def settings_input_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Réception du fichier Excel/CSV de planning."""
    if not is_admin_update(update):
        await deny_settings_access(update)
        return ConversationHandler.END

    action = get_settings_action(context)
    if not action or action.get("type") != "PLANNING_UPLOAD":
        await update.message.reply_text("Utilise /settings → Planning livreurs → Importer Excel.")
        return SETTINGS_INPUT

    store = get_store(action["store_id"])
    doc = update.message.document
    filename = (doc.file_name or "").lower()
    try:
        tg_file = await doc.get_file()
        content = bytes(await tg_file.download_as_bytearray())
        if filename.endswith(".csv"):
            drivers = parse_schedule_csv(content)
        elif filename.endswith((".xlsx", ".xlsm")):
            drivers = parse_schedule_xlsx(content)
        else:
            raise ValueError("Format non supporté. Envoie un fichier .xlsx (ou .csv).")
    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return SETTINGS_INPUT
    except Exception as exc:
        log.exception("Planning import failed")
        await update.message.reply_text(f"❌ Lecture du fichier impossible : {exc}")
        return SETTINGS_INPUT

    replace_store_schedule(store["store_id"], drivers)
    set_settings_action(context, None)
    await update.message.reply_text(
        "✅ Planning importé.\n\n" + format_planning_store_text(store),
        reply_markup=settings_planning_store_kb(store["store_id"]),
    )
    return SETTINGS_MENU


async def settings_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_update(update):
        await deny_settings_access(update)
        return ConversationHandler.END

    action = get_settings_action(context)
    if not action:
        await update.message.reply_text("Utilise les boutons de /settings.", reply_markup=settings_main_kb())
        return SETTINGS_MENU

    raw_text = (update.message.text or "").strip()

    try:
        if action["type"] == "ADD_STORE_WIZARD":
            step = action.get("step") or "store_id"
            if step == "confirm":
                await update.message.reply_text(
                    "Utilise les boutons Confirmer ou Annuler.",
                    reply_markup=store_wizard_confirm_kb(),
                )
                return SETTINGS_INPUT
            data_payload = dict(action.get("data") or {})
            data_payload[step] = parse_store_wizard_value(step, raw_text, data_payload)

            current_index = STORE_WIZARD_FIELDS.index(step)
            if current_index == len(STORE_WIZARD_FIELDS) - 1:
                set_settings_action(
                    context,
                    {
                        "type": "ADD_STORE_WIZARD",
                        "step": "confirm",
                        "data": data_payload,
                    },
                )
                await update.message.reply_text(
                    store_wizard_confirm_text(data_payload),
                    reply_markup=store_wizard_confirm_kb(),
                )
                return SETTINGS_INPUT

            next_step = STORE_WIZARD_FIELDS[current_index + 1]
            set_settings_action(
                context,
                {
                    "type": "ADD_STORE_WIZARD",
                    "step": next_step,
                    "data": data_payload,
                },
            )
            await update.message.reply_text(
                store_wizard_prompt(next_step, data_payload),
                reply_markup=store_wizard_cancel_kb(),
            )
            return SETTINGS_INPUT

        if action["type"] == "STORE_FIELD":
            store = get_store(action["store_id"], active_only=False)
            if not store:
                raise ValueError("Magasin introuvable.")

            updated = dict(store)
            field = action["field"]
            if field == "NAME":
                updated["store_name"] = raw_text
            elif field == "MAXDRIVERS":
                updated["max_drivers"] = parse_int_field(raw_text, "Le max livreurs", minimum=0)
            elif field == "CAPACITY":
                updated["capacity"] = parse_int_field(raw_text, "La capacité", minimum=0)
            elif field == "COORDS":
                lat_txt, lon_txt = [chunk.strip() for chunk in raw_text.split(",", 1)]
                updated["lat"] = parse_float_field(lat_txt, "La latitude")
                updated["lon"] = parse_float_field(lon_txt, "La longitude")
            else:
                raise ValueError("Action de magasin inconnue.")

            save_store(
                store_id=updated["store_id"],
                store_name=updated["store_name"],
                lat=updated["lat"],
                lon=updated["lon"],
                max_drivers=updated["max_drivers"],
                capacity=updated["capacity"],
                is_active=updated["is_active"],
                original_store_id=store["store_id"],
            )
            store = get_store(updated["store_id"], active_only=False)
            set_settings_action(context, None)
            await update.message.reply_text("✅ Magasin mis à jour.\n\n" + format_store_settings(store), reply_markup=settings_store_detail_kb(store))
            return SETTINGS_MENU

        if action["type"] == "ADD_SLOT":
            slots = configured_slots()
            if raw_text in slots:
                raise ValueError("Ce créneau existe déjà.")
            slots.append(raw_text)
            update_app_settings({"slots": slots})
            set_settings_action(context, None)
            await update.message.reply_text("✅ Créneau ajouté.", reply_markup=settings_slots_kb())
            return SETTINGS_MENU

        if action["type"] == "REDASH_URL":
            update_app_settings({"redash_results_csv_url": raw_text})
            set_settings_action(context, None)
            await update.message.reply_text("✅ URL Redash mise à jour.", reply_markup=settings_redash_kb())
            return SETTINGS_MENU

        if action["type"] == "REDASH_API_KEY":
            api_key = "" if raw_text.upper() == "VIDE" else raw_text
            update_app_settings({"redash_api_key": api_key})
            set_settings_action(context, None)
            await update.message.reply_text("✅ API key Redash mise à jour.", reply_markup=settings_redash_kb())
            return SETTINGS_MENU

        if action["type"] == "RULE":
            field = action["field"]
            if field == "STATUS":
                update_app_settings({"required_status": raw_text.lower()})
            elif field == "DTYPE":
                update_app_settings({"required_delivery_type": raw_text.lower()})
            elif field == "MAXORDERS":
                update_app_settings({"max_orders": parse_int_field(raw_text, "Le max commandes", minimum=1)})
            else:
                raise ValueError("Règle inconnue.")
            set_settings_action(context, None)
            await update.message.reply_text("✅ Règle mise à jour.", reply_markup=settings_rules_kb())
            return SETTINGS_MENU

        if action["type"] == "SLA":
            value = parse_int_field(raw_text, "Le SLA", minimum=0)
            field_map = {
                "ACCEPT": "accept_max_min",
                "PREP": "prep_max_min",
                "LOAD": "load_max_min",
                "DELIV": "deliv_max_min",
            }
            key = field_map.get(action["field"])
            if not key:
                raise ValueError("SLA inconnu.")
            update_app_settings({key: value})
            set_settings_action(context, None)
            await update.message.reply_text("✅ SLA mis à jour.", reply_markup=settings_sla_kb())
            return SETTINGS_MENU

        raise ValueError("Action non supportée.")

    except ValueError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return SETTINGS_INPUT


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    # --------- Refresh Redash cache ----------
    if q.data == "REFRESH":
        try:
            await ensure_redash_cache(context, force=True)
            # keep the main menu visible
            await q.edit_message_text("✅ Actualisation effectuée.", reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ Impossible de récupérer Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    # --------- New route planning ----------
    if q.data == "NEW":
        await ensure_stores(context)
        stores = context.user_data.get(CTX_STORES, [])
        if not stores:
            await q.edit_message_text("⚠️ Aucun magasin actif trouvé.", reply_markup=main_menu_kb())
            return ConversationHandler.END

        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=build_store_keyboard(stores))
        return CHOOSE_STORE

    # --------- Order module ----------
    if q.data == "CMD_MENU":
        await q.edit_message_text("📦 Module Commande — choisis une action :", reply_markup=commande_menu_kb())
        return ORDERS_MENU

    # --------- Magasin module ----------
    if q.data == "STORE_MENU":
        await q.edit_message_text("🏬 Magasin — choisis une catégorie :", reply_markup=magasin_menu_kb())
        return STORE_MENU

    # Unknown callback from main menu -> just show menu
    await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
    return ConversationHandler.END
def commande_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("End to End express", callback_data="CMD::E2E")],
        [InlineKeyboardButton("Paiement", callback_data="CMD::PAY")],
        [InlineKeyboardButton("Statut", callback_data="CMD::STATUS")],
        [InlineKeyboardButton("Menu", callback_data="CMD::BACK")],
    ]
    return InlineKeyboardMarkup(kb)


async def on_cmd_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if q.data == "CMD::BACK":
        await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
        return ConversationHandler.END

    # store chosen mode
    if q.data == "CMD::E2E":
        context.user_data["cmd_mode"] = "E2E"
        await q.edit_message_text("✍️ Envoie le numéro de commande (EXPRESS) pour calculer End-to-End express.")
        context.user_data["awaiting_order_number"] = True
        return ORDER_INPUT

    if q.data == "CMD::PAY":
        context.user_data["cmd_mode"] = "PAY"
        await q.edit_message_text("✍️ Envoie le numéro de commande pour vérifier le paiement.")
        context.user_data["awaiting_order_number"] = True
        return ORDER_INPUT

    if q.data == "CMD::STATUS":
        context.user_data["cmd_mode"] = "STATUS"
        await q.edit_message_text("✍️ Envoie le numéro de commande pour afficher le statut détaillé.")
        context.user_data["awaiting_order_number"] = True
        return ORDER_INPUT

    return ORDERS_MENU


async def order_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_order_number"):
        return

    order_no = (update.message.text or "").strip()
    if not order_no.isdigit():
        await update.message.reply_text("❌ Envoie un numéro de commande valide (chiffres seulement).")
        return

    try:
        rows_all = await ensure_redash_all_cache(context, force=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Impossible de récupérer Redash: {e}", reply_markup=main_menu_kb())
        context.user_data["awaiting_order_number"] = False
        return ConversationHandler.END

    # Find order
    order = None
    for o in rows_all:
        if str(o.get("order_id") or "").strip() == order_no:
            order = o
            break

    if not order:
        await update.message.reply_text("⚠️ Commande introuvable dans Redash.", reply_markup=main_menu_kb())
        context.user_data["awaiting_order_number"] = False
        return ConversationHandler.END

    mode = context.user_data.get("cmd_mode") or "STATUS"

    # Common header
    header = [
        f"📦 Commande {order.get('order_id')}",
        f"🏬 {order.get('store') or '-'}",
        f"📅 Day Of Delivery: {order.get('day_of_delivery') or '-'}",
        ""
    ]

    if mode == "E2E":
        body = [f"⏱ End-to-End express: {compute_e2e(order)}"]
    elif mode == "PAY":
        body = [f"💳 Paiement: {compute_payment(order)}"]
    else:  # STATUS
        body = ["📌 Statut détaillé:"] + compute_step_status(order)

    msg = "\n".join(header + body)

    await update.message.reply_text(msg, reply_markup=main_menu_kb(), disable_web_page_preview=True)

    context.user_data["awaiting_order_number"] = False
    return ConversationHandler.END
async def on_store_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if q.data == "STORE::BACK":
        await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if q.data == "STORE_MENU":
        await q.edit_message_text("🏬 Magasin — choisis une catégorie :", reply_markup=magasin_menu_kb())
        return STORE_MENU

    if not q.data.startswith("STORE::"):
        return STORE_MENU

    mode = q.data.split("::", 1)[1].strip().upper()
    if mode not in ("HYPER", "RAPIDEXL", "EXPRESS"):
        return STORE_MENU

    try:
        rows_all = await ensure_redash_all_cache(context, force=True)
    except Exception as e:
        await q.edit_message_text(f"❌ Impossible de récupérer Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    report = build_magasin_summary(rows_all, mode)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("Retour", callback_data="STORE_MENU")],
        [InlineKeyboardButton("Menu", callback_data="STORE::BACK")],
    ])

    await q.edit_message_text(report, reply_markup=kb, disable_web_page_preview=True)
    return STORE_MENU

    order = find_order(rows_all, order_no)
    if not order:
        await update.message.reply_text("⚠️ Commande introuvable dans Redash (vérifie le numéro).", reply_markup=main_menu_kb())
        context.user_data["awaiting_order_number"] = False
        return ConversationHandler.END

    mode = context.user_data.get("cmd_mode", "")
    header = f"📦 Commande {order_no}\n🏬 {order.get('store','-')}\n📅 Day Of Delivery: {order.get('day_of_delivery','-')}\n"

    if mode == "E2E":
        res = compute_e2e(order)
        msg = header + "\n⏱ End-to-End:\n" + res

    elif mode == "PAY":
        res = compute_payment(order)
        msg = header + "\n💳 Paiement:\n" + res

    else:  # STATUS
        lines = compute_step_status(order)
        msg = header + "\n📌 Statut détaillé:\n" + "\n".join(lines)

    await update.message.reply_text(msg, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    context.user_data["awaiting_order_number"] = False
    return ConversationHandler.END
async def choose_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if q.data == "BACK_MENU":
        await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if not q.data.startswith("STORE::"):
        return CHOOSE_STORE

    store_id = q.data.split("::", 1)[1]
    stores = await ensure_stores(context)
    store = next((s for s in stores if s["store_id"] == store_id), None)
    if not store:
        await q.edit_message_text("❌ Magasin introuvable. Réessaye.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    context.user_data[CTX_STORE_NAME] = store["store_name"]
    context.user_data[CTX_STORE_DEPOT] = (store["lat"], store["lon"])
    context.user_data.pop(CTX_DATE, None)
    context.user_data.pop(CTX_SLOT, None)

    # ✅ Proposer les dates disponibles (depuis Redash)
    try:
        rows = await ensure_redash_cache(context, force=True)
    except Exception as e:
        await q.edit_message_text(f"❌ Impossible de récupérer Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    dates = available_dates_for_store(rows, store["store_name"])
    if not dates:
        await q.edit_message_text(
            f"⚠️ Aucune date trouvée dans Redash pour {store['store_name']}.",
            reply_markup=main_menu_kb()
        )
        return ConversationHandler.END

    btns = [InlineKeyboardButton(d, callback_data=f"DATE::{d}") for d in dates]
    kb = chunk_buttons(btns, per_row=2)
    kb.append([InlineKeyboardButton("Retour magasins", callback_data="BACK_STORES")])
    kb.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])

    await q.edit_message_text(
        f"📍 Magasin: {store['store_name']}\n📅 Choisis la date :",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return CHOOSE_DATE


async def choose_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if q.data == "BACK_MENU":
        await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if q.data == "BACK_STORES":
        stores = await ensure_stores(context)
        buttons = [InlineKeyboardButton(s["store_name"], callback_data=f"STORE::{s['store_id']}") for s in stores]
        kb = chunk_buttons(buttons, per_row=2)
        kb.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])
        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_STORE

    if not q.data.startswith("DATE::"):
        return CHOOSE_DATE

    d = q.data.split("::", 1)[1].strip()
    context.user_data[CTX_DATE] = d
    context.user_data.pop(CTX_SLOT, None)

    # Proposer les créneaux
    btns = [InlineKeyboardButton(s, callback_data=f"SLOT::{s}") for s in configured_slots()]
    kb = chunk_buttons(btns, per_row=2)
    kb.append([InlineKeyboardButton("Retour dates", callback_data="BACK_DATES")])

    store_name = context.user_data.get(CTX_STORE_NAME, "")
    await q.edit_message_text(
        f"📍 Magasin: {store_name}\n📅 Date: {d}\n🕒 Choisis le créneau :",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return CHOOSE_SLOT


async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)

    if q.data == "BACK_STORES":
        stores = await ensure_stores(context)
        buttons = [InlineKeyboardButton(s["store_name"], callback_data=f"STORE::{s['store_id']}") for s in stores]
        kb = chunk_buttons(buttons, per_row=2)
        kb.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])
        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_STORE

    if q.data == "BACK_DATES":
        store_name = context.user_data.get(CTX_STORE_NAME, "")
        rows = await ensure_redash_cache(context, force=True)
        dates = available_dates_for_store(rows, store_name)

        btns = [InlineKeyboardButton(d, callback_data=f"DATE::{d}") for d in dates]
        kb = chunk_buttons(btns, per_row=2)
        kb.append([InlineKeyboardButton("Retour magasins", callback_data="BACK_STORES")])
        kb.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])

        await q.edit_message_text(
            f"📍 Magasin: {store_name}\n📅 Choisis la date :",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return CHOOSE_DATE

    if not q.data.startswith("SLOT::"):
        return CHOOSE_SLOT

    slot = q.data.split("::", 1)[1]
    context.user_data[CTX_SLOT] = slot


    store_name = context.user_data.get(CTX_STORE_NAME, "")
    d = context.user_data.get(CTX_DATE, "")

    # ✅ Count how many valid orders exist for this store/date/slot BEFORE asking drivers
    try:
        rows = await ensure_redash_cache(context, force=True)
    except Exception as e:
        await q.edit_message_text(f"❌ Impossible de récupérer Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    orders = filter_orders(rows, store_name, slot, d)
    total_orders = len(orders)

    # ✅ Cap drivers by store policy and by number of orders
    store_cap = get_store_max_drivers(store_name)
    allowed_max = compute_allowed_driver_choices(store_name, total_orders)
    context.user_data[CTX_MAX_DRIVERS_ALLOWED] = allowed_max

    if total_orders <= 0:
        kb = [
            [InlineKeyboardButton("Retour créneaux", callback_data="BACK_SLOTS")],
            [InlineKeyboardButton("Menu", callback_data="BACK_MENU")],
        ]
        await q.edit_message_text(
            f"Magasin: {store_name}\nDate: {d}\nCréneau: {slot}\n\nAucune commande valide dans ce créneau.",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return CHOOSE_SLOT

    if allowed_max <= 0:
        kb = [
            [InlineKeyboardButton("Retour créneaux", callback_data="BACK_SLOTS")],
            [InlineKeyboardButton("Menu", callback_data="BACK_MENU")],
        ]
        await q.edit_message_text(
            f"Magasin: {store_name}\nDate: {d}\nCréneau: {slot}\n\nCommandes: {total_orders}\nMax livreurs (magasin): {store_cap}\n\nImpossible de choisir des livreurs (cap=0).",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return CHOOSE_SLOT

    btns = [InlineKeyboardButton(str(i), callback_data=f"DRV::{i}") for i in range(1, allowed_max + 1)]
    kb = chunk_buttons(btns, per_row=5)
    kb.append([InlineKeyboardButton("📅 Selon planning", callback_data="DRV::PLAN")])
    kb.append([InlineKeyboardButton("Saisir un nombre", callback_data="DRV::TEXT")])
    kb.append([InlineKeyboardButton("Retour créneaux", callback_data="BACK_SLOTS")])

    await q.edit_message_text(
        f"Magasin: {store_name}\nDate: {d}\nCréneau: {slot}\n\nCommandes dans le créneau: {total_orders}\nMax livreurs magasin: {store_cap}\n\nChoisis le nombre de livreurs :",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return CHOOSE_DRIVERS


async def choose_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await safe_answer(q)

        if q.data == "BACK_SLOTS":
            store_name = context.user_data.get(CTX_STORE_NAME)
            d = context.user_data.get(CTX_DATE)
            if not store_name or not d:
                await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
                return ConversationHandler.END

            btns = [InlineKeyboardButton(s, callback_data=f"SLOT::{s}") for s in configured_slots()]
            kb = chunk_buttons(btns, per_row=2)
            kb.append([InlineKeyboardButton("Retour dates", callback_data="BACK_DATES")])

            await q.edit_message_text(
                f"📍 Magasin: {store_name}\n📅 Date: {d}\n🕒 Choisis le créneau :",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return CHOOSE_SLOT

        if q.data == "DRV::TEXT":
            await q.edit_message_text("✍️ Envoie le nombre de livreurs (ex: 5).")
            context.user_data["awaiting_drivers_text"] = True
            return CHOOSE_DRIVERS

        if q.data == "BACK_MENU":
            await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
            return ConversationHandler.END

        if q.data == "DRV::PLAN":
            return await choose_drivers_from_planning(update, context)

        if q.data.startswith("DRV::"):
            context.user_data[CTX_USE_PLANNING] = False
            try:
                n = int(q.data.split("::", 1)[1])
            except Exception:
                await q.edit_message_text("❌ Nombre invalide.", reply_markup=main_menu_kb())
                return ConversationHandler.END

            allowed_max = int(context.user_data.get(CTX_MAX_DRIVERS_ALLOWED) or 0)
            # If user jumps back/forward, recompute a safe cap
            if allowed_max <= 0:
                store_name = context.user_data.get(CTX_STORE_NAME, "")
                try:
                    rows = await ensure_redash_cache(context, force=True)
                except Exception:
                    rows = []
                orders = filter_orders(rows, store_name, context.user_data.get(CTX_SLOT, ""), context.user_data.get(CTX_DATE, ""))
                allowed_max = compute_allowed_driver_choices(store_name, len(orders))
                context.user_data[CTX_MAX_DRIVERS_ALLOWED] = allowed_max

            if allowed_max > 0 and n > allowed_max:
                await q.edit_message_text(
                    f"❌ Trop de livreurs. Max autorisé ici: {allowed_max}.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("Retour", callback_data="BACK_SLOTS")],
                        [InlineKeyboardButton("Menu", callback_data="BACK_MENU")],
                    ])
                )
                return CHOOSE_SLOT

            context.user_data[CTX_NUM_DRIVERS] = n
            return await run_planning_and_send(update, context, edit_from_query=True)

    return CHOOSE_DRIVERS


def _planning_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Retour créneaux", callback_data="BACK_SLOTS")],
        [InlineKeyboardButton("Menu", callback_data="BACK_MENU")],
    ])


async def choose_drivers_from_planning(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton 'Selon planning' : livreurs présents ce jour-là, km répartis équitablement."""
    q = update.callback_query
    store_name = context.user_data.get(CTX_STORE_NAME, "")
    day = context.user_data.get(CTX_DATE, "")
    store = find_store_by_name(store_name, active_only=False)
    delivery_date = parse_delivery_date(day)

    if not store or not delivery_date:
        await q.edit_message_text(
            f"❌ Impossible de lire le magasin ou la date ({day}).", reply_markup=_planning_back_kb()
        )
        return CHOOSE_DRIVERS

    schedule = get_store_schedule(store["store_id"])
    if not schedule:
        await q.edit_message_text(
            f"⚠️ Aucun planning importé pour {store_name}.\n"
            "Admin : /settings → Planning livreurs → Importer Excel.",
            reply_markup=_planning_back_kb(),
        )
        return CHOOSE_DRIVERS

    present, _off = split_present_off(schedule, delivery_date.weekday())
    if not present:
        await q.edit_message_text(
            f"⚠️ Personne ne travaille le {DAY_NAMES_FR[delivery_date.weekday()]} selon le planning de {store_name}.",
            reply_markup=_planning_back_kb(),
        )
        return CHOOSE_DRIVERS

    allowed_max = int(context.user_data.get(CTX_MAX_DRIVERS_ALLOWED) or 0)
    n = min(len(present), allowed_max) if allowed_max > 0 else len(present)
    context.user_data[CTX_NUM_DRIVERS] = max(1, n)
    context.user_data[CTX_USE_PLANNING] = True
    await q.edit_message_text(f"⏳ Calcul selon planning ({n} livreurs présents)...")
    return await run_planning_and_send(update, context, edit_from_query=True)


async def plan_validate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bouton 'Valider' : enregistre les km du créneau pour l'équité."""
    q = update.callback_query
    await safe_answer(q)
    pending = context.user_data.pop(CTX_PENDING_KM, None)
    if not pending:
        await q.edit_message_reply_markup(reply_markup=main_menu_kb())
        return ConversationHandler.END

    save_slot_km(pending["store_id"], pending["day"], pending["slot"], pending["entries"])
    total = sum(e["km"] for e in pending["entries"])
    text = (q.message.text or "") + f"\n\n✅ Km enregistrés ({total:.1f} km au total). Équité mise à jour."
    try:
        await q.edit_message_text(text[:4096], reply_markup=main_menu_kb(), disable_web_page_preview=True)
    except BadRequest:
        await q.message.reply_text("✅ Km enregistrés. Équité mise à jour.", reply_markup=main_menu_kb())
    return ConversationHandler.END


async def drivers_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_drivers_text"):
        return

    try:
        n = int(update.message.text.strip())
        if n < 1 or n > 50:
            raise ValueError

        allowed_max = int(context.user_data.get(CTX_MAX_DRIVERS_ALLOWED) or 0)
        if allowed_max <= 0:
            store_name = context.user_data.get(CTX_STORE_NAME, "")
            try:
                rows = await ensure_redash_cache(context, force=True)
            except Exception:
                rows = []
            orders = filter_orders(rows, store_name, context.user_data.get(CTX_SLOT, ""), context.user_data.get(CTX_DATE, ""))
            allowed_max = compute_allowed_driver_choices(store_name, len(orders))
            context.user_data[CTX_MAX_DRIVERS_ALLOWED] = allowed_max

        if allowed_max > 0 and n > allowed_max:
            await update.message.reply_text(f"❌ Trop de livreurs. Max autorisé ici: {allowed_max}.")
            return

    except Exception:
        await update.message.reply_text("❌ Nombre invalide. Envoie un entier entre 1 et 50.")
        return

    context.user_data["awaiting_drivers_text"] = False
    context.user_data[CTX_USE_PLANNING] = False
    context.user_data[CTX_NUM_DRIVERS] = n
    return await run_planning_and_send(update, context, edit_from_query=False)


async def run_planning_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_from_query: bool):
    store_name = context.user_data.get(CTX_STORE_NAME)
    depot = context.user_data.get(CTX_STORE_DEPOT)
    day_of_delivery = context.user_data.get(CTX_DATE)
    slot = context.user_data.get(CTX_SLOT)
    num_drivers = int(context.user_data.get(CTX_NUM_DRIVERS, 1))

    if not store_name or not depot or not day_of_delivery or not slot:
        msg = "❌ Données manquantes. Clique sur 🆕 Nouveau trajet."
        if edit_from_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END

    try:
        rows = await ensure_redash_cache(context, force=True)
    except Exception as e:
        msg = f"❌ Impossible de récupérer Redash: {e}"
        if edit_from_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END

    orders = filter_orders(rows, store_name, slot, day_of_delivery)

    # ✅ Safety cap: never allow more drivers than store policy or number of orders
    store_cap = get_store_max_drivers(store_name)
    allowed_max = compute_allowed_driver_choices(store_name, len(orders))
    context.user_data[CTX_MAX_DRIVERS_ALLOWED] = allowed_max
    if allowed_max > 0 and num_drivers > allowed_max:
        num_drivers = allowed_max
        context.user_data[CTX_NUM_DRIVERS] = num_drivers

    if not orders:
        msg = (
            f"⚠️ Aucune commande valide trouvée.\n"
            f"Magasin: {store_name}\n"
            f"Date: {day_of_delivery}\n"
            f"Créneau: {slot}\n"
            f"(Règles: Status=new + Delivery Type=Planned Order + Lat/Lon + Créneau + Date)"
        )
        if edit_from_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END

    store = find_store_by_name(store_name, active_only=False) or {
        "store_id": store_name,
        "store_name": store_name,
        "lat": depot[0],
        "lon": depot[1],
        "capacity": 0,
        "max_drivers": get_store_max_drivers(store_name),
    }
    result = build_route_plan(store, day_of_delivery, slot, num_drivers, rows=rows)
    reply_kb = main_menu_kb()
    context.user_data.pop(CTX_PENDING_KM, None)

    if context.user_data.get(CTX_USE_PLANNING) and store.get("id"):
        delivery_date = parse_delivery_date(day_of_delivery)
        schedule = get_store_schedule(store["store_id"])
        if delivery_date and schedule:
            present, off = split_present_off(schedule, delivery_date.weekday())
            if len(present) >= len(result["routes"]):
                apply_planning_to_plan(result, store, present, off, delivery_date)
                context.user_data[CTX_PENDING_KM] = {
                    "store_id": store["store_id"],
                    "day": delivery_date.isoformat(),
                    "slot": slot,
                    "entries": [
                        {"name": r["driver_name"], "km": r["distance_km"], "orders": r["order_count"]}
                        for r in result["routes"]
                        if r["driver_name"]
                    ],
                }
                reply_kb = InlineKeyboardMarkup(
                    [[InlineKeyboardButton("✅ Valider (enregistrer les km)", callback_data="PLAN::OK")]]
                    + list(main_menu_kb().inline_keyboard)
                )

    text = format_route_plan_text(result)
    if len(text) > 4096:
        text = text[:4000] + "\n…(message tronqué)"

    if edit_from_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_kb, disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=reply_kb, disable_web_page_preview=True)

    return ConversationHandler.END


# ============================
# Error handler
# ============================

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Avoid "No error handlers are registered" and log stacktrace
    log.exception("Unhandled exception in handler", exc_info=context.error)


# ============================
# Build Telegram Application
# ============================
def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start_cmd),
            CommandHandler("settings", settings_cmd),
            CommandHandler("whoami", whoami_cmd),
            CallbackQueryHandler(on_menu_click, pattern="^(NEW|REFRESH|CMD_MENU|STORE_MENU)$"),
            CallbackQueryHandler(plan_validate, pattern="^PLAN::OK$"),
        ],
        states={
            CHOOSE_STORE: [
                CallbackQueryHandler(choose_store, pattern="^(STORE::|BACK_MENU).*$")
            ],
            CHOOSE_DATE: [
                CallbackQueryHandler(choose_date, pattern="^(DATE::|BACK_STORES|BACK_MENU).*$")
            ],
            CHOOSE_SLOT: [
                CallbackQueryHandler(choose_slot, pattern="^(SLOT::|BACK_STORES|BACK_DATES).*$")
            ],
            CHOOSE_DRIVERS: [
                CallbackQueryHandler(choose_drivers, pattern="^(DRV::|BACK_SLOTS|BACK_MENU).*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, drivers_text),
            ],
            ORDERS_MENU: [
                CallbackQueryHandler(on_cmd_menu_click, pattern="^CMD::(E2E|PAY|STATUS|BACK)$"),
            ],
            ORDER_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_input_text),
            ],
            STORE_MENU: [
                CallbackQueryHandler(on_menu_click, pattern="^STORE_MENU$"),
                CallbackQueryHandler(on_store_menu_click, pattern="^(STORE::|STORE_MENU).*$"),
            ],
            SETTINGS_MENU: [
                CallbackQueryHandler(settings_menu_click, pattern="^SET::.*$"),
            ],
            SETTINGS_INPUT: [
                CallbackQueryHandler(settings_menu_click, pattern="^SET::.*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, settings_input_text),
                MessageHandler(filters.Document.ALL, settings_input_document),
            ],
        },
        fallbacks=[
            CommandHandler("start", start_cmd),
            CommandHandler("settings", settings_cmd),
        ],
        allow_reentry=True,
    )

    application.add_handler(conv)
    application.add_error_handler(on_error)
    return application


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get("admin_authenticated"):
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)

    return wrapped


def parse_int_field(raw_value, label: str, minimum: Optional[int] = None) -> int:
    try:
        value = int(str(raw_value).strip())
    except Exception as exc:
        raise ValueError(f"{label} doit être un entier valide.") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"{label} doit être supérieur ou égal à {minimum}.")
    return value


def parse_float_field(raw_value, label: str) -> float:
    try:
        return float(str(raw_value).strip())
    except Exception as exc:
        raise ValueError(f"{label} doit être un nombre valide.") from exc


def parse_slots_text(raw_text: str) -> List[str]:
    return [line.strip() for line in (raw_text or "").splitlines() if line.strip()]


def get_dates_for_store(store_id: str) -> List[str]:
    store = get_store(store_id, active_only=True)
    if not store:
        return []
    rows = parse_redash_rows(fetch_redash_csv_text())
    return available_dates_for_store(rows, store["store_name"])


def planner_state_defaults() -> Dict:
    return {
        "store_id": "",
        "day_of_delivery": "",
        "slot": "",
        "num_drivers": 1,
        "available_dates": [],
    }


# ============================================================
# ✅ Render Webhook Bridge: Flask -> PTB.process_update
# ============================================================
flask_app = Flask(__name__)
flask_app.secret_key = FLASK_SECRET_KEY

ptb_app: Optional[Application] = None
BOT_LOOP: Optional[asyncio.AbstractEventLoop] = None

READY = threading.Event()
START_LOCK = threading.Lock()
STARTED = False


@flask_app.before_request
def _ensure_bot_started():
    # ✅ IMPORTANT: Render port-scan hits "/" with GET/HEAD.
    # Do NOT block health checks by waiting for Telegram startup.
    if request.path.startswith("/admin"):
        return None

    if request.method != "POST" or request.path not in ("/", "/webhook"):
        return None

    if not READY.is_set():
        try:
            # Start in background, non-blocking
            start_background_bot(wait_ready=False)
        except Exception:
            log.exception("Failed to start PTB background thread")


@flask_app.get("/admin")
def admin_root():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_login"))


@flask_app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if username == ADMIN_USERNAME and check_password_hash(get_admin_password_hash(), password):
            session["admin_authenticated"] = True
            flash("Connexion réussie.", "success")
            return redirect(url_for("admin_dashboard"))

        flash("Identifiants invalides.", "error")

    return render_template("login.html")


@flask_app.post("/admin/logout")
@admin_required
def admin_logout():
    session.clear()
    flash("Session fermée.", "success")
    return redirect(url_for("admin_login"))


@flask_app.get("/admin/dashboard")
@admin_required
def admin_dashboard():
    return render_template(
        "dashboard.html",
        stats=get_dashboard_stats(),
        stores=list_stores(active_only=False),
    )


@flask_app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    settings = runtime_settings()

    if request.method == "POST":
        try:
            redash_results_csv_url = (request.form.get("redash_results_csv_url") or "").strip()
            if not redash_results_csv_url:
                raise ValueError("L'URL Redash est obligatoire.")

            values = {
                "redash_results_csv_url": redash_results_csv_url,
                "redash_api_key": (request.form.get("redash_api_key") or "").strip(),
                "max_orders": parse_int_field(request.form.get("max_orders"), "Le max commandes", minimum=1),
                "required_status": (request.form.get("required_status") or "").strip().lower(),
                "required_delivery_type": (request.form.get("required_delivery_type") or "").strip().lower(),
                "accept_max_min": parse_int_field(request.form.get("accept_max_min"), "Le SLA acceptation", minimum=0),
                "prep_max_min": parse_int_field(request.form.get("prep_max_min"), "Le SLA préparation", minimum=0),
                "load_max_min": parse_int_field(request.form.get("load_max_min"), "Le SLA chargement", minimum=0),
                "deliv_max_min": parse_int_field(request.form.get("deliv_max_min"), "Le SLA livraison", minimum=0),
                "planner_time_limit_sec": parse_int_field(
                    request.form.get("planner_time_limit_sec"),
                    "Le temps solveur",
                    minimum=1,
                ),
                "return_to_depot": bool(request.form.get("return_to_depot")),
                "slots": parse_slots_text(request.form.get("slots") or ""),
            }

            if not values["slots"]:
                raise ValueError("Ajoute au moins un créneau fixe.")

            update_app_settings(values)
            flash("Paramètres enregistrés.", "success")
            return redirect(url_for("admin_settings"))
        except ValueError as exc:
            flash(str(exc), "error")
            settings = {
                "redash_results_csv_url": (request.form.get("redash_results_csv_url") or "").strip(),
                "redash_api_key": (request.form.get("redash_api_key") or "").strip(),
                "max_orders": request.form.get("max_orders") or "",
                "required_status": (request.form.get("required_status") or "").strip().lower(),
                "required_delivery_type": (request.form.get("required_delivery_type") or "").strip().lower(),
                "accept_max_min": request.form.get("accept_max_min") or "",
                "prep_max_min": request.form.get("prep_max_min") or "",
                "load_max_min": request.form.get("load_max_min") or "",
                "deliv_max_min": request.form.get("deliv_max_min") or "",
                "planner_time_limit_sec": request.form.get("planner_time_limit_sec") or "",
                "return_to_depot": bool(request.form.get("return_to_depot")),
                "slots": parse_slots_text(request.form.get("slots") or ""),
            }

    return render_template("settings.html", settings=settings)


@flask_app.get("/admin/stores")
@admin_required
def admin_stores():
    return render_template("stores.html", stores=list_stores(active_only=False))


@flask_app.route("/admin/stores/new", methods=["GET", "POST"])
@admin_required
def admin_store_new():
    store = None

    if request.method == "POST":
        try:
            store = {
                "store_id": (request.form.get("store_id") or "").strip(),
                "store_name": (request.form.get("store_name") or "").strip(),
                "lat": request.form.get("lat") or "",
                "lon": request.form.get("lon") or "",
                "max_drivers": request.form.get("max_drivers") or 0,
                "capacity": request.form.get("capacity") or 0,
                "is_active": bool(request.form.get("is_active")),
            }
            save_store(
                store_id=store["store_id"],
                store_name=store["store_name"],
                lat=parse_float_field(store["lat"], "La latitude"),
                lon=parse_float_field(store["lon"], "La longitude"),
                max_drivers=parse_int_field(store["max_drivers"], "Le max livreurs", minimum=0),
                capacity=parse_int_field(store["capacity"], "La capacité", minimum=0),
                is_active=store["is_active"],
            )
            flash("Magasin créé.", "success")
            return redirect(url_for("admin_stores"))
        except ValueError as exc:
            flash(str(exc), "error")

    return render_template("store_form.html", store=store)


@flask_app.route("/admin/stores/<store_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_store_edit(store_id: str):
    store = get_store(store_id, active_only=False)
    if not store:
        flash("Magasin introuvable.", "error")
        return redirect(url_for("admin_stores"))

    if request.method == "POST":
        try:
            submitted = {
                "store_id": (request.form.get("store_id") or "").strip(),
                "store_name": (request.form.get("store_name") or "").strip(),
                "lat": request.form.get("lat") or "",
                "lon": request.form.get("lon") or "",
                "max_drivers": request.form.get("max_drivers") or 0,
                "capacity": request.form.get("capacity") or 0,
                "is_active": bool(request.form.get("is_active")),
            }
            save_store(
                store_id=submitted["store_id"],
                store_name=submitted["store_name"],
                lat=parse_float_field(submitted["lat"], "La latitude"),
                lon=parse_float_field(submitted["lon"], "La longitude"),
                max_drivers=parse_int_field(submitted["max_drivers"], "Le max livreurs", minimum=0),
                capacity=parse_int_field(submitted["capacity"], "La capacité", minimum=0),
                is_active=submitted["is_active"],
                original_store_id=store_id,
            )
            flash("Magasin mis à jour.", "success")
            return redirect(url_for("admin_stores"))
        except ValueError as exc:
            flash(str(exc), "error")
            store = submitted

    return render_template("store_form.html", store=store)


@flask_app.post("/admin/stores/<store_id>/toggle")
@admin_required
def admin_store_toggle(store_id: str):
    is_active = (request.form.get("is_active") or "0").strip() == "1"
    set_store_active(store_id, is_active)
    flash("Statut du magasin mis à jour.", "success")
    return redirect(url_for("admin_stores"))


@flask_app.route("/admin/planner", methods=["GET", "POST"])
@admin_required
def admin_planner():
    settings = runtime_settings()
    stores = list_stores(active_only=True)
    planner = planner_state_defaults()
    result = None

    if request.method == "POST":
        planner["store_id"] = (request.form.get("store_id") or "").strip()
        planner["day_of_delivery"] = (request.form.get("day_of_delivery") or "").strip()
        planner["slot"] = (request.form.get("slot") or "").strip()
        planner["num_drivers"] = request.form.get("num_drivers") or 1
        planner["available_dates"] = get_dates_for_store(planner["store_id"]) if planner["store_id"] else []

        store = get_store(planner["store_id"], active_only=True)
        if not store:
            flash("Choisis un magasin actif.", "error")
        elif not planner["day_of_delivery"]:
            flash("Choisis une date.", "error")
        elif not planner["slot"]:
            flash("Choisis un créneau.", "error")
        else:
            try:
                requested_drivers = parse_int_field(
                    planner["num_drivers"],
                    "Le nombre de livreurs",
                    minimum=1,
                )
                rows = parse_redash_rows(fetch_redash_csv_text())
                result = build_route_plan(
                    store,
                    planner["day_of_delivery"],
                    planner["slot"],
                    requested_drivers,
                    rows=rows,
                )
                planner["num_drivers"] = requested_drivers
                if result["order_count"] == 0:
                    flash("Aucune commande trouvée pour cette sélection.", "warning")
            except ValueError as exc:
                flash(str(exc), "error")
            except Exception as exc:
                flash(f"Impossible de récupérer les données Redash: {exc}", "error")

    return render_template(
        "planner.html",
        settings=settings,
        stores=stores,
        planner=planner,
        result=result,
    )


@flask_app.get("/admin/api/stores/<store_id>/dates")
@admin_required
def admin_store_dates_api(store_id: str):
    try:
        return jsonify({"dates": get_dates_for_store(store_id)})
    except Exception as exc:
        return jsonify({"dates": [], "error": str(exc)}), 500


@flask_app.get("/")
def health():
    return "OK", 200


@flask_app.post("/webhook")
def webhook():
    return _handle_update()


@flask_app.post("/")
def webhook_root():
    return _handle_update()


def _handle_update():
    global ptb_app, BOT_LOOP

    raw = request.data.decode("utf-8", errors="ignore")
    log.info("INCOMING RAW (first 500): %s", raw[:500])

    data = request.get_json(force=False, silent=True)
    if not data:
        try:
            import json
            data = json.loads(raw) if raw else None
        except Exception:
            data = None

    if not data:
        log.warning("⚠️ No JSON parsed (content-type=%s)", request.content_type)
        return "OK", 200

    if (not READY.is_set()) or ptb_app is None or BOT_LOOP is None:
        try:
            start_background_bot(wait_ready=False)
        except Exception:
            log.exception("Failed to start PTB from webhook")

        for _ in range(25):
            if READY.is_set() and (ptb_app is not None) and (BOT_LOOP is not None):
                break
            time.sleep(0.08)

        if (not READY.is_set()) or ptb_app is None or BOT_LOOP is None:
            log.warning("⚠️ Update reçu mais PTB pas prêt (READY=%s ptb_app=%s loop=%s)",
                        READY.is_set(), bool(ptb_app), bool(BOT_LOOP))
            return "OK", 200

    try:
        update = Update.de_json(data, ptb_app.bot)
        log.info("UPDATE TYPE: message=%s callback=%s",
                 bool(update.message), bool(update.callback_query))

        fut = asyncio.run_coroutine_threadsafe(ptb_app.process_update(update), BOT_LOOP)

        def _done(f):
            try:
                f.result()
            except Exception:
                log.exception("❌ Exception pendant process_update")

        fut.add_done_callback(_done)

    except Exception:
        log.exception("Webhook bridge error")

    return "OK", 200


def _bot_thread():
    global ptb_app, BOT_LOOP

    BOT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(BOT_LOOP)

    async def runner():
        global ptb_app
        ptb_app = build_telegram_app()
        webhook_url = resolve_webhook_url()

        await ptb_app.initialize()
        await ptb_app.start()

        await ptb_app.bot.set_webhook(
            url=webhook_url,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        READY.set()
        log.info("✅ PTB READY + webhook set: %s", WEBHOOK_URL)

    try:
        BOT_LOOP.run_until_complete(runner())
        BOT_LOOP.run_forever()
    except Exception:
        log.exception("❌ PTB thread crashed")
    finally:
        try:
            if ptb_app is not None:
                BOT_LOOP.run_until_complete(ptb_app.stop())
                BOT_LOOP.run_until_complete(ptb_app.shutdown())
        except Exception:
            pass


def start_background_bot(wait_ready: bool = False):
    global STARTED
    with START_LOCK:
        if STARTED:
            return
        STARTED = True

        if not TELEGRAM_TOKEN:
            raise RuntimeError("Missing TELEGRAM_TOKEN or BOT_TOKEN env var")
        if not resolve_webhook_url():
            raise RuntimeError("Missing WEBHOOK_URL or RENDER_EXTERNAL_URL")

        t = threading.Thread(target=_bot_thread, daemon=True)
        t.start()

        if wait_ready:
            for _ in range(60):
                if READY.is_set():
                    break
                time.sleep(0.1)


def _on_exit():
    log.info("Shutdown requested (atexit).")


atexit.register(_on_exit)

if __name__ == "__main__":
    if TELEGRAM_TOKEN and resolve_webhook_url():
        start_background_bot(wait_ready=True)
    else:
        log.warning("Telegram worker not started. Configure TELEGRAM_TOKEN/BOT_TOKEN and WEBHOOK_URL or rely on RENDER_EXTERNAL_URL.")
    print("Local: gunicorn bot:flask_app --bind 0.0.0.0:10000 --workers 1 --threads 8")




