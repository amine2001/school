import os
import csv
import re
import math
import tempfile
from typing import List, Dict, Tuple, Optional

import openpyxl
from ortools.constraint_solver import pywrapcp, routing_enums_pb2

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG (Render)
# =========================
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
PUBLIC_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip()
PORT = int(os.environ.get("PORT", "10000"))

# Ton fichier magasins (dans ton repo)
SHOPS_CSV = "shops.csv"  # si tu as stores_coords.csv -> mets "stores_coords.csv"


# =========================
# SHOPS CSV LOADER (ROBUST)
# - Accepte 2 formats:
#   A) store_id,store_name,lat,lon
#   B) Store,Lat,Lon   (ton format actuel)
# - Accepte séparateur ; ou ,
# - Ignore lignes vides/mal formées
# =========================
def load_shops(csv_path: str) -> List[Dict]:
    shops = []
    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ";" if sample.count(";") > sample.count(",") else ","

        reader = csv.DictReader(f, delimiter=delimiter)
        idx = 0

        for row in reader:
            if not row:
                continue
            idx += 1

            # Format A
            sid = (row.get("store_id") or "").strip()
            name = (row.get("store_name") or "").strip()
            lat_raw = (row.get("lat") or "").strip()
            lon_raw = (row.get("lon") or "").strip()

            # Format B (ton fichier)
            if not name:
                name = (row.get("Store") or row.get("store") or "").strip()
            if not lat_raw:
                lat_raw = (row.get("Lat") or row.get("LAT") or "").strip()
            if not lon_raw:
                lon_raw = (row.get("Lon") or row.get("LON") or "").strip()

            # Si pas store_id, on en fabrique un stable
            if not sid:
                sid = f"S{idx:02d}"

            if not (name and lat_raw and lon_raw):
                continue

            lat_raw = lat_raw.replace(",", ".")
            lon_raw = lon_raw.replace(",", ".")

            try:
                lat = float(lat_raw)
                lon = float(lon_raw)
            except ValueError:
                continue

            shops.append({"id": sid, "name": name, "lat": lat, "lon": lon})

    if not shops:
        raise RuntimeError(f"{csv_path} invalide. Vérifie colonnes + lat/lon.")
    return shops


SHOPS = load_shops(SHOPS_CSV)


def get_shop(shop_id: str) -> Optional[Dict]:
    return next((s for s in SHOPS if s["id"] == shop_id), None)


# =========================
# UI KEYBOARDS
# =========================
def shops_keyboard() -> InlineKeyboardMarkup:
    # 1 bouton par ligne
    rows = [[InlineKeyboardButton(s["name"], callback_data=f"SHOP:{s['id']}")] for s in SHOPS]
    return InlineKeyboardMarkup(rows)


def vehicles_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("1", callback_data="VEH:1"),
             InlineKeyboardButton("2", callback_data="VEH:2"),
             InlineKeyboardButton("3", callback_data="VEH:3")],
            [InlineKeyboardButton("4", callback_data="VEH:4"),
             InlineKeyboardButton("5", callback_data="VEH:5"),
             InlineKeyboardButton("6", callback_data="VEH:6")],
        ]
    )


# =========================
# EXCEL READER (ROBUST HEADER)
# - Cherche un header: order_id + lat + lon (variants acceptés)
# =========================
def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "").replace("_", "")


def read_orders_from_xlsx(path: str) -> List[Tuple[str, float, float]]:
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    id_keys = {"orderid", "order", "commande", "id"}
    lat_keys = {"lat", "latitude"}
    lon_keys = {"lon", "lng", "long", "longitude"}

    header_row = None
    col_id = col_lat = col_lon = None

    for r in range(1, min(ws.max_row, 60) + 1):
        row = []
        for c in range(1, min(ws.max_column, 30) + 1):
            row.append(_norm(str(ws.cell(row=r, column=c).value)) if ws.cell(row=r, column=c).value is not None else "")

        tmp_id = tmp_lat = tmp_lon = None
        for i, h in enumerate(row, start=1):
            if h in id_keys and tmp_id is None:
                tmp_id = i
            if h in lat_keys and tmp_lat is None:
                tmp_lat = i
            if h in lon_keys and tmp_lon is None:
                tmp_lon = i

        if tmp_id and tmp_lat and tmp_lon:
            header_row = r
            col_id, col_lat, col_lon = tmp_id, tmp_lat, tmp_lon
            break

    if not header_row:
        raise ValueError("Excel doit contenir les colonnes: order_id, lat, lon (ou variantes).")

    orders: List[Tuple[str, float, float]] = []
    for r in range(header_row + 1, ws.max_row + 1):
        oid = ws.cell(row=r, column=col_id).value
        lat = ws.cell(row=r, column=col_lat).value
        lon = ws.cell(row=r, column=col_lon).value

        if oid is None and lat is None and lon is None:
            continue

        oid_str = str(oid).strip() if oid is not None else ""
        if not oid_str:
            continue

        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except Exception:
            continue

        orders.append((oid_str, lat_f, lon_f))

    if not orders:
        raise ValueError("Aucune commande détectée dans l’Excel.")
    return orders


# =========================
# VRP HELPERS (Haversine + OR-Tools)
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
) -> List[List[int]]:
    num_vehicles = min(num_vehicles, len(orders))

    coords = [start] + [(o[1], o[2]) for o in orders]  # node0 depot
    dist = build_distance_matrix(coords)

    manager = pywrapcp.RoutingIndexManager(len(coords), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_idx, to_idx):
        f = manager.IndexToNode(from_idx)
        t = manager.IndexToNode(to_idx)
        return dist[f][t]

    transit = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    # ✅ encourage using more vehicles (évite 6 commandes sur 1)
    routing.SetFixedCostOfAllVehicles(10000)

    # ✅ capacity = max commandes par livreur
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

    routes: List[List[int]] = []
    for v in range(num_vehicles):
        idx = routing.Start(v)
        route = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                route.append(node - 1)  # order index
            idx = sol.Value(routing.NextVar(idx))
        if route:
            routes.append(route)

    return routes


def google_maps_link(start: Tuple[float, float], route_orders: List[Tuple[str, float, float]]) -> str:
    parts = [f"{start[0]},{start[1]}"] + [f"{o[1]},{o[2]}" for o in route_orders]
    return "https://www.google.com/maps/dir/" + "/".join(parts)


# =========================
# BOT FLOW
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Choisis le magasin (point de départ) :", reply_markup=shops_keyboard())


async def on_shop_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    shop_id = q.data.split(":", 1)[1]
    shop = get_shop(shop_id)
    if not shop:
        await q.edit_message_text("❌ Magasin introuvable. Fais /start.")
        return

    context.user_data["start"] = (shop["lat"], shop["lon"])
    context.user_data["shop_name"] = shop["name"]

    await q.edit_message_text(
        f"✅ Magasin choisi : {shop['name']}\n\nChoisis le nombre de livreurs :",
        reply_markup=vehicles_keyboard(),
    )


async def on_vehicle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    k = int(q.data.split(":", 1)[1])
    context.user_data["k"] = k

    shop_name = context.user_data.get("shop_name", "Magasin")
    await q.edit_message_text(
        f"✅ Livreurs : {k}\n📍 Départ : {shop_name}\n\n"
        "Maintenant envoie le fichier Excel (.xlsx) 📎\n"
        "Colonnes obligatoires : order_id, lat, lon"
    )


async def on_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "start" not in context.user_data or "k" not in context.user_data:
        await update.message.reply_text("❌ Fais /start d’abord (choisir magasin + livreurs).")
        return

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Envoie un fichier .xlsx (Excel).")
        return

    start_pt: Tuple[float, float] = context.user_data["start"]
    k: int = context.user_data["k"]

    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, doc.file_name)
        await tg_file.download_to_drive(custom_path=path)

        try:
            orders = read_orders_from_xlsx(path)
            routes = solve_vrp_balanced(start_pt, orders, k)

            out = []
            for i, r in enumerate(routes, start=1):
                route_orders = [orders[idx] for idx in r]
                out.append(f"🚚 *Livreur {i}* ({len(route_orders)} commandes)")
                out.append("Ordre : " + " → ".join([o[0] for o in route_orders]))
                out.append("🔗 " + google_maps_link(start_pt, route_orders))
                out.append("")

            await update.message.reply_text(
                "\n".join(out).strip(),
                parse_mode="Markdown",
                disable_web_page_preview=True,
            )

        except Exception as e:
            await update.message.reply_text(f"❌ Erreur VRP/Excel: {e}")


# =========================
# MAIN (Webhook stable)
# =========================
def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN manquant dans Render (Environment Variables).")
    if not PUBLIC_URL:
        raise SystemExit("RENDER_EXTERNAL_URL manquant (Render le fournit automatiquement).")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_shop_choice, pattern=r"^SHOP:"))
    app.add_handler(CallbackQueryHandler(on_vehicle_choice, pattern=r"^VEH:"))
    app.add_handler(MessageHandler(filters.Document.ALL, on_excel))

    # ✅ Webhook server intégré (pas Flask) => évite "Event loop is closed"
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="",                  # racine "/"
        webhook_url=f"{PUBLIC_URL}/", # Render external url
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
