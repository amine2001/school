import os
import re
import math
import csv
import tempfile
from typing import List, Tuple, Optional, Dict

import openpyxl
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from flask import Flask, request
from dotenv import load_dotenv
load_dotenv()

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PORT = int(os.environ.get("PORT", "10000"))
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()  # Render sets this

SHOPS_CSV = "shops.csv"

app_flask = Flask(__name__)

# =========================
# LOAD SHOPS
# =========================
def load_shops(csv_path: str) -> List[Dict]:
    shops = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sid = (row.get("store_id") or "").strip()
            name = (row.get("store_name") or "").strip()
            lat = float((row.get("lat") or "").strip())
            lon = float((row.get("lon") or "").strip())
            if sid and name:
                shops.append({"id": sid, "name": name, "lat": lat, "lon": lon})
    if not shops:
        raise RuntimeError("shops.csv est vide ou invalide.")
    return shops

SHOPS = load_shops(SHOPS_CSV)

def get_shop_by_id(shop_id: str) -> Optional[Dict]:
    for s in SHOPS:
        if s["id"] == shop_id:
            return s
    return None

# =========================
# VRP HELPERS
# =========================
def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def build_distance_matrix(points: List[Tuple[float, float]]) -> List[List[int]]:
    n = len(points)
    mat = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                mat[i][j] = 0
            else:
                mat[i][j] = int(haversine_km(points[i], points[j]) * 1000)  # meters
    return mat

def solve_vrp_balanced(
    start: Tuple[float, float],
    orders: List[Tuple[str, float, float]],
    num_vehicles: int,
    time_limit_sec: int = 8,
):
    num_vehicles = min(num_vehicles, len(orders))
    coords = [start] + [(o[1], o[2]) for o in orders]
    dist = build_distance_matrix(coords)

    manager = pywrapcp.RoutingIndexManager(len(coords), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_idx, to_idx):
        f = manager.IndexToNode(from_idx)
        t = manager.IndexToNode(to_idx)
        return dist[f][t]

    transit = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    # ✅ encourage using more vehicles (avoid 6 commandes sur 1)
    routing.SetFixedCostOfAllVehicles(10000)

    # Capacity = max commandes par livreur (2-3 quand 11 commandes et 5 livreurs)
    demands = [0] + [1] * len(orders)

    def demand_cb(from_idx):
        node = manager.IndexToNode(from_idx)
        return demands[node]

    demand = routing.RegisterUnaryTransitCallback(demand_cb)

    max_per = math.ceil(len(orders) / num_vehicles)
    routing.AddDimensionWithVehicleCapacity(
        demand,
        0,
        [max_per] * num_vehicles,
        True,
        "Capacity",
    )

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_sec)

    sol = routing.SolveWithParameters(params)
    if not sol:
        raise ValueError("Aucune solution VRP trouvée (essaie moins de livreurs).")

    routes = []
    for v in range(num_vehicles):
        idx = routing.Start(v)
        r = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                r.append(node - 1)
            idx = sol.Value(routing.NextVar(idx))
        if r:
            routes.append(r)
    return routes

def google_maps_link(start: Tuple[float, float], route_orders: List[Tuple[str, float, float]]) -> str:
    parts = [f"{start[0]},{start[1]}"] + [f"{o[1]},{o[2]}" for o in route_orders]
    return "https://www.google.com/maps/dir/" + "/".join(parts)

def read_orders_from_xlsx_simple(path: str) -> List[Tuple[str, float, float]]:
    # expects header row that includes order_id/lat/lon (can be row 1 or later)
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    # find header row
    def norm(x): return (str(x).strip().lower().replace(" ", "").replace("_", "")) if x is not None else ""
    id_keys = {"orderid", "order", "commande", "id"}
    lat_keys = {"lat", "latitude"}
    lon_keys = {"lon", "lng", "long", "longitude"}

    header_row = None
    col_id = col_lat = col_lon = None

    for r in range(1, min(ws.max_row, 60) + 1):
        row = [norm(ws.cell(row=r, column=c).value) for c in range(1, min(ws.max_column, 30) + 1)]
        for i, h in enumerate(row, start=1):
            if h in id_keys and col_id is None:
                col_id = i
            if h in lat_keys and col_lat is None:
                col_lat = i
            if h in lon_keys and col_lon is None:
                col_lon = i
        if col_id and col_lat and col_lon:
            header_row = r
            break

    if not header_row:
        raise ValueError("Excel doit contenir les colonnes order_id, lat, lon.")

    orders = []
    for r in range(header_row + 1, ws.max_row + 1):
        oid = ws.cell(row=r, column=col_id).value
        lat = ws.cell(row=r, column=col_lat).value
        lon = ws.cell(row=r, column=col_lon).value
        if oid is None and lat is None and lon is None:
            continue
        oid = str(oid).strip() if oid is not None else ""
        if not oid:
            continue
        try:
            orders.append((oid, float(lat), float(lon)))
        except Exception:
            continue

    if not orders:
        raise ValueError("Aucune commande détectée dans l’Excel.")
    return orders

# =========================
# BOT FLOW (Buttons)
# =========================
def shops_keyboard() -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(s["name"], callback_data=f"SHOP:{s['id']}")] for s in SHOPS]
    return InlineKeyboardMarkup(buttons)

def vehicles_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(str(i), callback_data=f"VEH:{i}") for i in (1, 2, 3)]
    row2 = [InlineKeyboardButton(str(i), callback_data=f"VEH:{i}") for i in (4, 5, 6)]
    return InlineKeyboardMarkup([row1, row2])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Salut 👋\nChoisis le magasin (point de départ) :",
        reply_markup=shops_keyboard()
    )

async def on_shop_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    shop_id = q.data.split(":", 1)[1]
    shop = get_shop_by_id(shop_id)
    if not shop:
        await q.edit_message_text("❌ Magasin introuvable. Refais /start.")
        return

    context.user_data["shop_id"] = shop_id
    context.user_data["start"] = (shop["lat"], shop["lon"])

    await q.edit_message_text(
        f"✅ Magasin choisi : {shop['name']}\n\nChoisis le nombre de livreurs :",
        reply_markup=vehicles_keyboard()
    )

async def on_vehicle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    k = int(q.data.split(":", 1)[1])
    context.user_data["k"] = k
    shop = get_shop_by_id(context.user_data.get("shop_id", ""))

    await q.edit_message_text(
        f"✅ Livreurs : {k}\n"
        f"📍 Départ : {shop['name'] if shop else 'Magasin'}\n\n"
        "Maintenant envoie le fichier Excel (.xlsx) 📎\n"
        "Colonnes obligatoires : order_id, lat, lon"
    )

async def on_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "start" not in context.user_data or "k" not in context.user_data:
        await update.message.reply_text("❌ D’abord fais /start et choisis magasin + livreurs.")
        return

    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Envoie un fichier .xlsx (Excel).")
        return

    start_pt = context.user_data["start"]
    k = context.user_data["k"]

    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, doc.file_name)
        await tg_file.download_to_drive(custom_path=path)

        try:
            orders = read_orders_from_xlsx_simple(path)
            routes = solve_vrp_balanced(start_pt, orders, k)

            # build response
            out = []
            for i, r in enumerate(routes, start=1):
                route_orders = [orders[idx] for idx in r]
                out.append(f"🚚 *Livreur {i}* ({len(route_orders)} commandes)")
                out.append("Ordre : " + " → ".join([o[0] for o in route_orders]))
                out.append("🔗 " + google_maps_link(start_pt, route_orders))
                out.append("")

            await update.message.reply_text("\n".join(out).strip(), parse_mode="Markdown", disable_web_page_preview=True)

        except Exception as e:
            await update.message.reply_text(f"❌ Erreur VRP/Excel: {e}")

# =========================
# WEBHOOK (Flask)
# =========================
application = Application.builder().token(TOKEN).build()
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(on_shop_choice, pattern=r"^SHOP:"))
application.add_handler(CallbackQueryHandler(on_vehicle_choice, pattern=r"^VEH:"))
application.add_handler(MessageHandler(filters.Document.ALL, on_excel))

@app_flask.route("/", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "ok"

@app_flask.route("/", methods=["GET"])
def home():
    return "Bot is running"

if __name__ == "__main__":
    import asyncio

    if not TOKEN:
        raise SystemExit("BOT_TOKEN manquant.")
    if not PUBLIC_URL:
        raise SystemExit("RENDER_EXTERNAL_URL manquant (sur Render).")

    # init PTB app and set webhook
    asyncio.run(application.initialize())
    asyncio.run(application.bot.set_webhook(f"{PUBLIC_URL}/"))

    # start web server
    app_flask.run(host="0.0.0.0", port=PORT)
