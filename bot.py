import os
import io
import csv
import math
import logging
from typing import Dict, List, Tuple, Optional

import requests
from flask import Flask, request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
WEBHOOK_URL = os.getenv("WEBHOOK_URL")        # required (https://.../webhook)

# Redash: use env vars (DO NOT hardcode api key)
REDASH_RESULTS_CSV_URL = os.getenv(
    "REDASH_RESULTS_CSV_URL",
    "https://redash-ro-live.bringo.ro/api/queries/4441/results.csv"
)
REDASH_API_KEY = os.getenv("REDASH_API_KEY")  # optional if URL already includes key OR open endpoint

STORES_CSV_PATH = os.getenv("STORES_CSV_PATH", "stores_coords.csv")

# Optional: limit max orders for performance
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
CTX_REDASH_CACHE = "redash_cache"  # parsed rows cache
CTX_STORE_NAME = "store_name"
CTX_STORE_DEPOT = "store_depot"    # (lat, lon)
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
    # sort alphabetically
    stores.sort(key=lambda x: x["store_name"].lower())
    return stores


# ----------------------------
# Helpers: Redash CSV fetch + parse
# Columns you showed:
# - Order Number
# - Store
# - Creneau
# - LatLong  (format "31.63219,-8.004811")
# ----------------------------
def fetch_redash_csv_text() -> str:
    headers = {}
    # Many Redash setups accept either ?api_key=... or Authorization header.
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
        # basic validity
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
        # normalize keys with exact column names
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

    return rows


def get_slots_for_store(rows: List[Dict], store_name: str) -> List[str]:
    slots = sorted({(r["slot"] or "").strip() for r in rows if r["store"] == store_name and (r["slot"] or "").strip()})
    # If empty, still allow "Tous"
    return slots


def filter_orders(rows: List[Dict], store_name: str, slot: Optional[str]) -> List[Tuple[str, float, float]]:
    """
    Filtre les commandes Redash selon :
    - le magasin choisi
    - le créneau choisi
    - LatLong valide
    """

    out = []

    for r in rows:

        # 🔴 MATCH EXACT avec Redash colonne "Store"
        if r["store"].strip() != store_name.strip():
            continue

        # 🔴 FILTRE CRENEAU
        if slot and slot != "__ALL__" and (r["slot"] or "").strip() != slot.strip():
            continue

        # 🔴 VERIF LAT LONG
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
    # OR-Tools expects integers; use meters
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
    routing.AddDimension(
        count_idx,
        0,
        num_orders,
        True,
        "Count"
    )
    count_dim = routing.GetDimensionOrDie("Count")
    # encourage balanced number of stops
    count_dim.SetGlobalSpanCostCoefficient(100)


def force_min_one_stop_per_vehicle_if_possible(routing, manager, num_orders, num_vehicles):
    # If orders >= vehicles, ensure each vehicle serves >= 1 order
    if num_orders < num_vehicles:
        return
    solver = routing.solver()
    order_nodes = range(1, num_orders + 1)  # because node 0 is depot
    for v in range(num_vehicles):
        served_by_v = [
            solver.IsEqualCstVar(routing.VehicleVar(manager.NodeToIndex(i)), v)
            for i in order_nodes
        ]
        solver.Add(solver.Sum(served_by_v) >= 1)


def solve_vrp(
    depot: Tuple[float, float],
    orders: List[Tuple[str, float, float]],
    num_vehicles: int
) -> List[List[int]]:
    """
    Returns routes as list of node indices (1..n) per vehicle.
    Vehicle routes may be empty.
    """
    # nodes: 0=depot, 1..n=orders
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

    # Encourage compact routes & balancing
    add_count_balance_dimension(routing, manager, num_orders=len(orders))
    # Force min 1 stop per vehicle when possible
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
                stops.append(node)  # 1..n
            idx = solution.Value(routing.NextVar(idx))
        routes.append(stops)
    return routes


def google_maps_link(depot: Tuple[float, float], stops_latlon: List[Tuple[float, float]]) -> str:
    # Google Maps Directions API (simple URL)
    # origin=depot, destination=last stop, waypoints=others
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
    rows = []
    for i in range(0, len(buttons), per_row):
        rows.append(buttons[i:i+per_row])
    return rows


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
    await ensure_stores(context)
    await update.message.reply_text(
        "Bot Last Mile Bringo ✅\nChoisis une action :",
        reply_markup=main_menu_kb()
    )


async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "REFRESH":
        # refresh redash cache
        try:
            await ensure_redash_cache(context, force=True)
            await q.edit_message_text("✅ Actualisation effectuée.", reply_markup=main_menu_kb())
        except Exception as e:
            await q.edit_message_text(f"❌ Erreur actualisation Redash: {e}", reply_markup=main_menu_kb())
        return ConversationHandler.END

    if q.data == "NEW":
        # start flow
        await ensure_stores(context)
        # also preload redash for slots
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

    # load redash and compute slots for this store
    try:
        rows = await ensure_redash_cache(context, force=False)
        slots = get_slots_for_store(rows, store["store_name"])
    except Exception as e:
        slots = []
        log.exception(e)

    # slot buttons
    btns = [InlineKeyboardButton("📦 Tous les créneaux", callback_data="SLOT::__ALL__")]
    for s in slots[:40]:  # safety
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
        # show stores again
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

    # Ask drivers
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
            # go back to slot selection
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

    # Fetch & filter
    try:
        rows = await ensure_redash_cache(context, force=True)  # force refresh for planning
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
        msg = f"⚠️ Aucune commande valide trouvée.\nMagasin: {store_name}\nCréneau: {slot_label}\n(Contrôle: Store + LatLong + Order Number)"
        if edit_from_query:
            await update.callback_query.edit_message_text(msg, reply_markup=main_menu_kb())
        else:
            await update.message.reply_text(msg, reply_markup=main_menu_kb())
        return ConversationHandler.END
    

    # Solve
    routes_nodes = solve_vrp(depot, orders, num_drivers)

    # Build output
    # orders list: index 1..n corresponds to orders[i-1]
    lines = []
    slot_label = "Tous" if slot == "__ALL__" else slot
    lines.append(f"📍 Magasin: {store_name}")
    lines.append(f"🕒 Créneau: {slot_label}")
    lines.append(f"📦 Commandes: {len(orders)}")
    lines.append(f"🚴 Livreurs: {num_drivers}")
    lines.append("")

    for v in range(num_drivers):
        stops_nodes = routes_nodes[v]  # nodes 1..n
        if not stops_nodes:
            lines.append(f"👤 Livreur {v+1}: 0 commande")
            lines.append("—")
            continue

        # map nodes to orders
        stop_orders = [orders[node-1] for node in stops_nodes]  # (order_id, lat, lon)
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
        await update.callback_query.edit_message_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)
    else:
        await update.message.reply_text(text, reply_markup=main_menu_kb(), disable_web_page_preview=True)

    return ConversationHandler.END


# ----------------------------
# Flask Webhook bridge
# ----------------------------
flask_app = Flask(__name__)
tg_app: Optional[Application] = None

@flask_app.get("/")
def health():
    return "OK", 200

@flask_app.post("/webhook")
def webhook():
    if tg_app is None:
        return "Bot not ready", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        return "No JSON", 400

    update = Update.de_json(data, tg_app.bot)
    tg_app.update_queue.put_nowait(update)
    return "OK", 200


def build_telegram_app() -> Application:
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CallbackQueryHandler(on_menu_click, pattern="^(NEW|REFRESH)$"),
        ],
        states={
            CHOOSE_STORE: [CallbackQueryHandler(choose_store, pattern="^(STORE::|BACK_MENU|BACK_STORES).*$")],
            CHOOSE_SLOT: [CallbackQueryHandler(choose_slot, pattern="^(SLOT::|BACK_STORES).*$")],
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


def main():
    global tg_app
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN env var")
    if not WEBHOOK_URL:
        raise RuntimeError("Missing WEBHOOK_URL env var (ex: https://xxx.onrender.com/webhook)")

    tg_app = build_telegram_app()

    # Set webhook once at startup
    async def _set_webhook():
        await tg_app.bot.set_webhook(url=WEBHOOK_URL)

    tg_app.run_async(_set_webhook())

    # Start PTB processing (without polling)
    tg_app.run_async(tg_app.initialize())
    tg_app.run_async(tg_app.start())

    # Flask server is started by Render (gunicorn), not here.


# Render will run with gunicorn, so main() isn't called automatically.
# For local debugging: python app.py (you can add flask run manually)
if __name__ == "__main__":
    main()
