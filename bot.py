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
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse, urlencode, parse_qsl, urlunparse

import requests
from flask import Flask, request

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

# ============================
# CONFIG (Render Env Vars)
# ============================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL")        # required ex: https://bringo-bot.onrender.com/webhook

# Redash CSV endpoint (can include ?api_key=... already)
REDASH_RESULTS_CSV_URL = os.getenv(
    "REDASH_RESULTS_CSV_URL",
    "https://redash-ro-live.bringo.ro/api/queries/4441/results.csv"
)

# OPTIONAL: if you prefer key separated (do NOT put an URL here, only the key)
REDASH_API_KEY = os.getenv("REDASH_API_KEY", "").strip()

STORES_CSV_PATH = os.getenv("STORES_CSV_PATH", "stores_coords.csv")
MAX_ORDERS = int(os.getenv("MAX_ORDERS", "150"))

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


# ============================
# Telegram Conversation States
# ============================
(CHOOSE_STORE, CHOOSE_DATE, CHOOSE_SLOT, CHOOSE_DRIVERS, ORDERS_MENU, ORDER_INPUT) = range(6)

# context keys
CTX_STORES = "stores"
CTX_REDASH_CACHE = "redash_cache"
CTX_REDASH_ALL_CACHE = "redash_all_cache"  # for order lookup (all statuses/types)
CTX_STORE_NAME = "store_name"
CTX_STORE_DEPOT = "store_depot"
CTX_DATE = "date"
CTX_SLOT = "slot"
CTX_NUM_DRIVERS = "num_drivers"


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

# ============================
# Helpers: Redash fetch
# ============================
def _with_api_key(url: str) -> str:
    """Append api_key param only if REDASH_API_KEY exists and api_key not already present."""
    if not REDASH_API_KEY:
        return url
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    if "api_key" in q and q["api_key"]:
        return url
    q["api_key"] = REDASH_API_KEY
    return urlunparse(u._replace(query=urlencode(q)))


def fetch_redash_csv_text() -> str:
    url = _with_api_key(REDASH_RESULTS_CSV_URL)
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
        if status != REQUIRED_STATUS:
            continue

        # ✅ ONLY Planned Order
        if delivery_type != REQUIRED_DELIVERY_TYPE:
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

    if len(rows) > MAX_ORDERS:
        rows = rows[:MAX_ORDERS]
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
    if dtype != "express":
        # planned order / others
        return "Non applicable : End-to-End express est disponible uniquement pour les commandes EXPRESS."

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
    """Return lines for Acceptance/Preparation/Chargement/Livraison with retard logic."""
    now = datetime.datetime.utcnow()
    order_date = order.get("order_date")
    accept_dt = order.get("accept_dt")
    prep_end = order.get("prep_end_dt")
    load_dt = order.get("load_dt")
    close_dt = order.get("close_dt")

    def line(label, done_dt, ref_dt, max_min):
        if done_dt:
            return f"✅ {label}: OK ({_fmt_dt(done_dt)})"
        # not done
        if not ref_dt:
            return f"⚠️ {label}: En attente (référence manquante)"
        late = (now - ref_dt).total_seconds() > max_min * 60
        if late:
            return f"🚨 {label}: Retard (>{max_min} min)"
        return f"⏳ {label}: En cours"

    # Acceptance reference: order_date
    lines = []
    lines.append(line("Acceptation", accept_dt, order_date, ACCEPT_MAX_MIN))
    # Preparation reference: accept_dt
    lines.append(line("Préparation", prep_end, accept_dt or order_date, PREP_MAX_MIN))
    # Chargement reference: prep_end
    lines.append(line("Chargement", load_dt, prep_end or accept_dt or order_date, LOAD_MAX_MIN))
    # Livraison reference: load_dt
    lines.append(line("Livraison", close_dt, load_dt or prep_end or accept_dt or order_date, DELIV_MAX_MIN))
    return lines
def available_dates_for_store(rows: List[Dict], store_name: str) -> List[str]:
    dates = sorted({
        (r.get("day_of_delivery") or "").strip()
        for r in rows
        if (r.get("store") or "").strip() == (store_name or "").strip()
        and (r.get("day_of_delivery") or "").strip()
    })
    return dates


def filter_orders(rows: List[Dict], store_name: str, slot: str, day_of_delivery: str) -> List[Tuple[str, float, float]]:
    out = []
    for r in rows:
        if (r["store"] or "").strip() != (store_name or "").strip():
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


def solve_vrp(depot: Tuple[float, float], orders: List[Tuple[str, float, float]], num_vehicles: int) -> List[List[int]]:
    points = [depot] + [(lat, lon) for (_, lat, lon) in orders]
    dist = build_distance_matrix(points)

    manager = pywrapcp.RoutingIndexManager(len(points), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(from_index, to_index):
        i = manager.IndexToNode(from_index)
        j = manager.IndexToNode(to_index)
        return dist[i][j]

    transit_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    # ✅ Balance des commandes (N/K)
    def count_cb(from_index, to_index):
        node = manager.IndexToNode(to_index)
        return 0 if node == 0 else 1

    count_idx = routing.RegisterTransitCallback(count_cb)

    routing.AddDimension(
        count_idx,
        0,
        len(orders),
        True,
        "Count"
    )

    count_dim = routing.GetDimensionOrDie("Count")
    count_dim.SetGlobalSpanCostCoefficient(1000)

    # 🔥 HARD BALANCING N/K (chaque livreur doit avoir ~ N/K commandes)
    n = len(orders)
    k = max(1, num_vehicles)
    min_load = n // k
    max_load = math.ceil(n / k)

    for v in range(num_vehicles):
        end = routing.End(v)
        count_dim.CumulVar(end).SetRange(min_load, max_load)

    # 👉 IMPORTANT: rendre les commandes "optionnelles" avec pénalité
    penalty = 10**7
    for node in range(1, len(points)):
        routing.AddDisjunction([manager.NodeToIndex(node)], penalty)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(10)

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
        [InlineKeyboardButton("Actualisation", callback_data="REFRESH")],
    ]
    return InlineKeyboardMarkup(kb)


def chunk_buttons(buttons: List[InlineKeyboardButton], per_row: int = 2) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


async def ensure_redash_cache(context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> List[Dict]:
    if (not force) and context.user_data.get(CTX_REDASH_CACHE):
        return context.user_data[CTX_REDASH_CACHE]
    csv_text = fetch_redash_csv_text()
    rows = parse_redash_rows(csv_text)
    context.user_data[CTX_REDASH_CACHE] = rows
    return rows


async def ensure_stores(context: ContextTypes.DEFAULT_TYPE) -> List[Dict]:
    if context.user_data.get(CTX_STORES):
        return context.user_data[CTX_STORES]
    stores = load_stores_from_csv(STORES_CSV_PATH)
    context.user_data[CTX_STORES] = stores
    return stores


# ============================
# Handlers
# ============================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("✅ /start received from chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await ensure_stores(context)
    if update.message:
        await update.message.reply_text("Bot Last Mile Bringo ✅\nChoisis une action :", reply_markup=main_menu_kb())



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
            await q.edit_message_text("⚠️ Aucun magasin trouvé (stores_coords.csv).", reply_markup=main_menu_kb())
            return ConversationHandler.END

        buttons = [InlineKeyboardButton(s["store_name"], callback_data=f"STORE::{s['store_id']}") for s in stores]
        kb = chunk_buttons(buttons, per_row=2)
        kb.append([InlineKeyboardButton("Menu", callback_data="BACK_MENU")])

        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_STORE

    # --------- Order module ----------
    if q.data == "CMD_MENU":
        await q.edit_message_text("📦 Module Commande — choisis une action :", reply_markup=commande_menu_kb())
        return ORDERS_MENU

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
    btns = [InlineKeyboardButton(s, callback_data=f"SLOT::{s}") for s in SLOTS_FIXED]
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

    btns = [InlineKeyboardButton(str(i), callback_data=f"DRV::{i}") for i in range(1, 11)]
    kb = chunk_buttons(btns, per_row=5)
    kb.append([InlineKeyboardButton("Saisir un nombre", callback_data="DRV::TEXT")])
    kb.append([InlineKeyboardButton("Retour créneaux", callback_data="BACK_SLOTS")])

    store_name = context.user_data.get(CTX_STORE_NAME, "")
    d = context.user_data.get(CTX_DATE, "")
    await q.edit_message_text(
        f"✅ Magasin: {store_name}\n✅ Date: {d}\n✅ Créneau: {slot}\n\n🚴 Choisis le nombre de livreurs :",
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

            btns = [InlineKeyboardButton(s, callback_data=f"SLOT::{s}") for s in SLOTS_FIXED]
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

        if q.data.startswith("DRV::"):
            try:
                n = int(q.data.split("::", 1)[1])
            except Exception:
                await q.edit_message_text("❌ Nombre invalide.", reply_markup=main_menu_kb())
                return ConversationHandler.END

            context.user_data[CTX_NUM_DRIVERS] = n
            return await run_planning_and_send(update, context, edit_from_query=True)

    return CHOOSE_DRIVERS


async def drivers_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_drivers_text"):
        return

    try:
        n = int(update.message.text.strip())
        if n < 1 or n > 50:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Nombre invalide. Envoie un entier entre 1 et 50.")
        return

    context.user_data["awaiting_drivers_text"] = False
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

    routes_nodes = solve_vrp(depot, orders, num_drivers)

    lines = [
        f"📍 Magasin: {store_name}",
        f"📅 Date: {day_of_delivery}",
        f"🕒 Créneau: {slot}",
        f"📦 Commandes (new/planned): {len(orders)}",
        f"🚴 Livreurs: {num_drivers}",
        "",
    ]

    for v in range(num_drivers):
        stops_nodes = routes_nodes[v]
        if not stops_nodes:
            lines += [f"👤 Livreur {v + 1}: 0 commande", "—"]
            continue

        stop_orders = [orders[node - 1] for node in stops_nodes]
        ids = [o[0] for o in stop_orders]
        latlons = [(o[1], o[2]) for o in stop_orders]
        gmap = google_maps_link(depot, latlons)

        lines.append(f"👤 Livreur {v + 1}: {len(ids)} commandes")
        lines.append("🧾 " + " → ".join(ids))
        if gmap:
            lines.append("🗺️ " + gmap)
        lines.append("—")

    text = "\n".join(lines)

    if edit_from_query:
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)

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
            CallbackQueryHandler(on_menu_click, pattern="^(NEW|REFRESH|CMD_MENU)$"),
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
                CallbackQueryHandler(choose_drivers, pattern="^(DRV::|BACK_SLOTS).*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, drivers_text),
            ],
            ORDERS_MENU: [
                CallbackQueryHandler(on_cmd_menu_click, pattern="^CMD::(E2E|PAY|STATUS|BACK)$"),
            ],
            ORDER_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, order_input_text),
            ],
        },
        fallbacks=[CommandHandler("start", start_cmd)],
        allow_reentry=True,
    )

    application.add_handler(conv)
    application.add_error_handler(on_error)
    return application


# ============================================================
# ✅ Render Webhook Bridge: Flask -> PTB.process_update
# ============================================================
flask_app = Flask(__name__)

ptb_app: Optional[Application] = None
BOT_LOOP: Optional[asyncio.AbstractEventLoop] = None

READY = threading.Event()
START_LOCK = threading.Lock()
STARTED = False


@flask_app.before_request
def _ensure_bot_started():
    # ✅ IMPORTANT: Render port-scan hits "/" with GET/HEAD.
    # Do NOT block health checks by waiting for Telegram startup.
    if request.path == "/" and request.method in ("GET", "HEAD"):
        return None

    if not READY.is_set():
        try:
            # Start in background, non-blocking
            start_background_bot(wait_ready=False)
        except Exception:
            log.exception("Failed to start PTB background thread")


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

        await ptb_app.initialize()
        await ptb_app.start()

        await ptb_app.bot.set_webhook(
            url=WEBHOOK_URL,
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
            raise RuntimeError("Missing TELEGRAM_TOKEN env var")
        if not WEBHOOK_URL:
            raise RuntimeError("Missing WEBHOOK_URL env var (ex: https://xxx.onrender.com/webhook)")

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
    start_background_bot(wait_ready=True)
    print("Local: gunicorn bot:flask_app --bind 0.0.0.0:10000 --workers 1 --threads 8")
