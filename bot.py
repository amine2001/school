import os
import io
import csv
import math
import time
import logging
import threading
import asyncio
from typing import Dict, List, Tuple, Optional

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

from ortools.constraint_solver import pywrapcp, routing_enums_pb2

# ----------------------------
# CONFIG (Render Env Vars)
# ----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")  # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL")        # required ex: https://bringo-bot.onrender.com/webhook

REDASH_RESULTS_CSV_URL = os.getenv(
    "REDASH_RESULTS_CSV_URL",
    "https://redash-ro-live.bringo.ro/api/queries/4441/results.csv"
)
REDASH_API_KEY = os.getenv("REDASH_API_KEY")  # optional

STORES_CSV_PATH = os.getenv("STORES_CSV_PATH", "stores_coords.csv")
MAX_ORDERS = int(os.getenv("MAX_ORDERS", "150"))

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bringo-bot")

# ----------------------------
# Telegram Conversation States
# ----------------------------
(
    CHOOSE_STORE,
    CHOOSE_SLOT,
    CHOOSE_DRIVERS,
) = range(3)

# context keys
CTX_STORES = "stores"
CTX_REDASH_CACHE = "redash_cache"
CTX_STORE_NAME = "store_name"
CTX_STORE_DEPOT = "store_depot"
CTX_SLOT = "slot"
CTX_NUM_DRIVERS = "num_drivers"

# ----------------------------
# Helpers: Stores CSV
# ----------------------------
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

# ----------------------------
# Helpers: Redash CSV fetch + parse
# Columns expected:
# - Order Number
# - Store
# - Creneau
# - LatLong (e.g "31.63219,-8.004811")
# ----------------------------
def fetch_redash_csv_text() -> str:
    headers = {}
    if REDASH_API_KEY:
        headers["Authorization"] = f"Key {REDASH_API_KEY}"
    r = requests.get(REDASH_RESULTS_CSV_URL, headers=headers, timeout=60)
    r.raise_for_status()
    return r.text

def parse_latlong(latlong: str) -> Optional[Tuple[float, float]]:
    if not latlong:
        return None
    s = str(latlong).strip().replace(";", ",")
    if "," not in s:
        return None
    a, b = s.split(",", 1)
    try:
        lat = float(a.strip())
        lon = float(b.strip())
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return lat, lon
    except Exception:
        return None

def parse_redash_rows(csv_text: str) -> List[Dict]:
    f = io.StringIO(csv_text)
    reader = csv.DictReader(f)
    rows = []
    for row in reader:
        order_id = (row.get("Order Number") or "").strip()
        store = (row.get("Store") or "").strip()
        slot = (row.get("Creneau") or "").strip()
        latlong = row.get("LatLong")

        ll = parse_latlong(latlong)
        if not order_id or not store or not ll:
            continue

        rows.append({
            "order_id": order_id,
            "store": store,
            "slot": slot,
            "lat": ll[0],
            "lon": ll[1],
            "raw": row,
        })

    if len(rows) > MAX_ORDERS:
        rows = rows[:MAX_ORDERS]
    return rows

def get_slots_for_store(rows: List[Dict], store_name: str) -> List[str]:
    return sorted({
        (r["slot"] or "").strip()
        for r in rows
        if (r["store"] or "").strip() == (store_name or "").strip()
        and (r["slot"] or "").strip()
    })

def filter_orders(rows: List[Dict], store_name: str, slot: Optional[str]) -> List[Tuple[str, float, float]]:
    out = []
    for r in rows:
        if (r["store"] or "").strip() != (store_name or "").strip():
            continue
        if slot and slot != "__ALL__" and (r["slot"] or "").strip() != (slot or "").strip():
            continue
        if r.get("lat") is None or r.get("lon") is None:
            continue
        out.append((r["order_id"], r["lat"], r["lon"]))
    return out

# ----------------------------
# VRP utilities
# ----------------------------
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

def add_count_balance_dimension(routing, manager, num_orders):
    def count_cb(from_index, to_index):
        node = manager.IndexToNode(to_index)
        return 0 if node == 0 else 1

    count_idx = routing.RegisterTransitCallback(count_cb)
    routing.AddDimension(count_idx, 0, num_orders, True, "Count")
    count_dim = routing.GetDimensionOrDie("Count")
    count_dim.SetGlobalSpanCostCoefficient(100)

def force_min_one_stop_per_vehicle_if_possible(routing, manager, num_orders, num_vehicles):
    if num_orders < num_vehicles:
        return
    solver = routing.solver()
    order_nodes = range(1, num_orders + 1)
    for v in range(num_vehicles):
        served_by_v = [
            solver.IsEqualCstVar(routing.VehicleVar(manager.NodeToIndex(i)), v)
            for i in order_nodes
        ]
        solver.Add(solver.Sum(served_by_v) >= 1)

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

    add_count_balance_dimension(routing, manager, num_orders=len(orders))
    force_min_one_stop_per_vehicle_if_possible(routing, manager, num_orders=len(orders), num_vehicles=num_vehicles)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PARALLEL_CHEAPEST_INSERTION
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(8)

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

# ----------------------------
# Telegram UI helpers
# ----------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton("🆕 Nouveau trajet", callback_data="NEW")],
        [InlineKeyboardButton("🔄 Actualisation", callback_data="REFRESH")],
    ]
    return InlineKeyboardMarkup(kb)

def chunk_buttons(buttons: List[InlineKeyboardButton], per_row: int = 2) -> List[List[InlineKeyboardButton]]:
    return [buttons[i:i+per_row] for i in range(0, len(buttons), per_row)]

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

# ----------------------------
# Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    log.info("✅ /start received chat_id=%s", update.effective_chat.id if update.effective_chat else None)
    await ensure_stores(context)
    await update.message.reply_text(
        "Bot Last Mile Bringo ✅\nChoisis une action :",
        reply_markup=main_menu_kb()
    )

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "REFRESH":
        try:
            await ensure_redash_cache(context, force=True)
            await q.edit_message_text("✅ Actualisation effectuée.", reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ Erreur actualisation Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if q.data == "NEW":
        await ensure_stores(context)
        try:
            await ensure_redash_cache(context, force=False)
        except Exception:
            pass

        stores = context.user_data[CTX_STORES]
        buttons = [InlineKeyboardButton(s["store_name"], callback_data=f"STORE::{s['store_id']}") for s in stores]
        kb = chunk_buttons(buttons, per_row=2)
        kb.append([InlineKeyboardButton("⬅️ Menu", callback_data="BACK_MENU")])
        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_STORE

    return ConversationHandler.END

async def choose_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

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

    try:
        rows = await ensure_redash_cache(context, force=False)
        slots = get_slots_for_store(rows, store["store_name"])
    except Exception as e:
        slots = []
        log.exception(e)

    btns = [InlineKeyboardButton("📦 Tous les créneaux", callback_data="SLOT::__ALL__")]
    for s in slots[:40]:
        btns.append(InlineKeyboardButton(s, callback_data=f"SLOT::{s}"))

    kb = chunk_buttons(btns, per_row=2)
    kb.append([InlineKeyboardButton("⬅️ Retour magasins", callback_data="BACK_STORES")])

    await q.edit_message_text(
        f"🕒 Magasin: {store['store_name']}\nChoisis le créneau :",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return CHOOSE_SLOT

async def choose_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "BACK_STORES":
        stores = await ensure_stores(context)
        buttons = [InlineKeyboardButton(s["store_name"], callback_data=f"STORE::{s['store_id']}") for s in stores]
        kb = chunk_buttons(buttons, per_row=2)
        kb.append([InlineKeyboardButton("⬅️ Menu", callback_data="BACK_MENU")])
        await q.edit_message_text("🏬 Choisis le magasin :", reply_markup=InlineKeyboardMarkup(kb))
        return CHOOSE_STORE

    if not q.data.startswith("SLOT::"):
        return CHOOSE_SLOT

    slot = q.data.split("::", 1)[1]
    context.user_data[CTX_SLOT] = slot

    btns = [InlineKeyboardButton(str(i), callback_data=f"DRV::{i}") for i in range(1, 11)]
    kb = chunk_buttons(btns, per_row=5)
    kb.append([InlineKeyboardButton("✍️ Saisir un nombre", callback_data="DRV::TEXT")])
    kb.append([InlineKeyboardButton("⬅️ Retour créneaux", callback_data="BACK_SLOTS")])

    store_name = context.user_data.get(CTX_STORE_NAME, "")
    slot_label = "Tous" if slot == "__ALL__" else slot

    await q.edit_message_text(
        f"✅ Magasin: {store_name}\n✅ Créneau: {slot_label}\n\n🚴 Choisis le nombre de livreurs :",
        reply_markup=InlineKeyboardMarkup(kb)
    )
    return CHOOSE_DRIVERS

async def choose_drivers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await q.answer()

        if q.data == "BACK_SLOTS":
            store_name = context.user_data.get(CTX_STORE_NAME)
            if not store_name:
                await q.edit_message_text("Menu :", reply_markup=main_menu_kb())
                return ConversationHandler.END

            rows = await ensure_redash_cache(context, force=False)
            slots = get_slots_for_store(rows, store_name)

            btns = [InlineKeyboardButton("📦 Tous les créneaux", callback_data="SLOT::__ALL__")]
            for s in slots[:40]:
                btns.append(InlineKeyboardButton(s, callback_data=f"SLOT::{s}"))
            kb = chunk_buttons(btns, per_row=2)
            kb.append([InlineKeyboardButton("⬅️ Retour magasins", callback_data="BACK_STORES")])

            await q.edit_message_text(
                f"🕒 Magasin: {store_name}\nChoisis le créneau :",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return CHOOSE_SLOT

        if q.data == "DRV::TEXT":
            await q.edit_message_text("✍️ Envoie le nombre de livreurs (ex: 5).")
            context.user_data["awaiting_drivers_text"] = True
            return CHOOSE_DRIVERS

        if q.data.startswith("DRV::"):
            n = int(q.data.split("::", 1)[1])
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
    slot = context.user_data.get(CTX_SLOT)
    num_drivers = int(context.user_data.get(CTX_NUM_DRIVERS, 1))

    if not store_name or not depot:
        msg = "❌ Magasin manquant. Clique sur 🆕 Nouveau trajet."
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

    orders = filter_orders(rows, store_name, slot)
    if not orders:
        slot_label = "Tous" if slot == "__ALL__" else slot
        msg = (
            f"⚠️ Aucune commande valide trouvée.\n"
            f"Magasin: {store_name}\nCréneau: {slot_label}\n"
            f"(Contrôle: Store + LatLong + Order Number)"
        )
        if edit_from_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END

    routes_nodes = solve_vrp(depot, orders, num_drivers)

    lines = []
    slot_label = "Tous" if slot == "__ALL__" else slot
    lines.append(f"📍 Magasin: {store_name}")
    lines.append(f"🕒 Créneau: {slot_label}")
    lines.append(f"📦 Commandes: {len(orders)}")
    lines.append(f"🚴 Livreurs: {num_drivers}")
    lines.append("")

    for v in range(num_drivers):
        stops_nodes = routes_nodes[v]
        if not stops_nodes:
            lines.append(f"👤 Livreur {v+1}: 0 commande")
            lines.append("—")
            continue

        stop_orders = [orders[node-1] for node in stops_nodes]
        ids = [o[0] for o in stop_orders]
        latlons = [(o[1], o[2]) for o in stop_orders]
        gmap = google_maps_link(depot, latlons)

        lines.append(f"👤 Livreur {v+1}: {len(ids)} commandes")
        lines.append("🧾 " + " → ".join(ids))
        if gmap:
            lines.append("🗺️ " + gmap)
        lines.append("—")

    text = "\n".join(lines)

    if edit_from_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=main_menu_kb(), disable_web_page_preview=True
        )
    else:
        await update.message.reply_text(
            text, reply_markup=main_menu_kb(), disable_web_page_preview=True
        )

    return ConversationHandler.END

# ----------------------------
# Build Telegram Application
# ----------------------------
def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(on_menu_click, pattern="^(NEW|REFRESH)$"),
        ],
        states={
            CHOOSE_STORE: [
                CallbackQueryHandler(choose_store, pattern="^(STORE::|BACK_MENU|BACK_STORES).*$")
            ],
            CHOOSE_SLOT: [
                CallbackQueryHandler(choose_slot, pattern="^(SLOT::|BACK_STORES).*$")
            ],
            CHOOSE_DRIVERS: [
                CallbackQueryHandler(choose_drivers, pattern="^(DRV::|BACK_SLOTS).*$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, drivers_text),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        allow_reentry=True,
    )

    application.add_handler(conv)
    return application

# ============================================================
# ✅ PATCH ULTIME : traiter l’update directement (process_update)
# ============================================================
flask_app = Flask(__name__)

ptb_app: Optional[Application] = None
BOT_LOOP: Optional[asyncio.AbstractEventLoop] = None
_started = False
READY = threading.Event()

@flask_app.get("/")
def health():
    return "OK", 200

@flask_app.post("/webhook")
def webhook():
    return _handle_update()

@flask_app.post("/")
def webhook_root():
    # fallback si Telegram POST sur "/"
    return _handle_update()

def _handle_update():
    global ptb_app, BOT_LOOP

    # 🔥 log brut (debug)
    raw = request.data.decode("utf-8", errors="ignore")
    log.info("INCOMING RAW (first 500): %s", raw[:500])

    data = request.get_json(force=False, silent=True)

    # fallback si Flask ne parse pas le JSON
    if not data:
        try:
            import json as _json
            data = _json.loads(raw) if raw else None
        except Exception:
            data = None

    if not data:
        log.warning("⚠️ No JSON parsed (content-type=%s)", request.content_type)
        return "OK", 200

    # ✅ readiness barrière
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

    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var")
    if not WEBHOOK_URL:
        raise RuntimeError("Missing WEBHOOK_URL env var (ex: https://xxx.onrender.com/webhook)")

    BOT_LOOP = asyncio.new_event_loop()
    asyncio.set_event_loop(BOT_LOOP)

    async def runner():
        global ptb_app
        ptb_app = build_telegram_app()
        await ptb_app.initialize()
        await ptb_app.start()
        await ptb_app.bot.set_webhook(url=WEBHOOK_URL, drop_pending_updates=True)
        READY.set()
        log.info("✅ PTB READY + webhook set: %s", WEBHOOK_URL)

    BOT_LOOP.run_until_complete(runner())
    BOT_LOOP.run_forever()

def start_background_bot():
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_bot_thread, daemon=True).start()

# ✅ démarre automatiquement quand gunicorn import bot.py
start_background_bot()

if __name__ == "__main__":
    print("Local: python bot.py (Render uses gunicorn bot:flask_app)")
