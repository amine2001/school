# bot.py — Telegram Bot (Excel upload) + OR-Tools VRP + BALANCED (répartition 2–3 commandes/livreur)
# ✅ /vrp K start LAT,LON  -> ensuite envoyer Excel .xlsx
# ✅ Excel: colonnes order_id, lat, lon (header peut être décalé, auto-détection)
# ✅ OR-Tools VRP: optimise distance + équilibre via:
#    - capacité max par livreur (max_per)
#    - pénalité fixe si un véhicule n'est pas utilisé (SetFixedCostOfAllVehicles)
# ✅ Réponse: routes + liens Google Maps

import os
import re
import math
import tempfile
from typing import List, Tuple, Optional, Dict

import openpyxl
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.environ.get("BOT_TOKEN", "").strip()


# ----------------------------
# Parse /vrp command
# ----------------------------
def parse_vrp_command(text: str) -> Tuple[int, Optional[Tuple[float, float]]]:
    """
    /vrp K [start lat,lon]
    examples:
      /vrp 5 start 33.5726,-7.6419
      /vrp 5
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        raise ValueError("Commande vide.")

    m = re.match(r"^/vrp\s+(\d+)(.*)$", lines[0].strip(), flags=re.IGNORECASE)
    if not m:
        raise ValueError("Format: /vrp 5 start 33.57,-7.64 (ou /vrp 5 si START est dans Excel)")
    k = int(m.group(1))
    if k < 1 or k > 30:
        raise ValueError("Nombre livreurs entre 1 et 30.")

    rest = (m.group(2) or "").strip()
    start = None

    if re.search(r"\bstart\b", rest, flags=re.IGNORECASE):
        coords = re.findall(r"(-?\d+(?:\.\d+)?)", rest)
        if len(coords) >= 2:
            start = (float(coords[0]), float(coords[1]))
        else:
            raise ValueError("start invalide. Exemple: start 33.5726,-7.6419")

    return k, start


# ----------------------------
# Excel reader (robust header)
# ----------------------------
def _norm(s: str) -> str:
    return (s or "").strip().lower().replace(" ", "").replace("_", "")

def find_header_row(ws) -> Tuple[int, Dict[str, int]]:
    """
    Find a header row containing order_id + lat + lon (or variants).
    Returns: (row_index_1based, col_map={'order_id': col, 'lat': col, 'lon': col})
    """
    id_keys = {"orderid", "order", "commande", "id"}
    lat_keys = {"lat", "latitude"}
    lon_keys = {"lon", "lng", "long", "longitude"}

    for r in range(1, min(ws.max_row, 60) + 1):
        row_vals = []
        for c in range(1, min(ws.max_column, 30) + 1):
            v = ws.cell(row=r, column=c).value
            row_vals.append(_norm(str(v)) if v is not None else "")

        col_map: Dict[str, int] = {}
        for idx, h in enumerate(row_vals, start=1):
            if h in id_keys and "order_id" not in col_map:
                col_map["order_id"] = idx
            if h in lat_keys and "lat" not in col_map:
                col_map["lat"] = idx
            if h in lon_keys and "lon" not in col_map:
                col_map["lon"] = idx

        if all(k in col_map for k in ("order_id", "lat", "lon")):
            return r, col_map

    raise ValueError("Excel doit contenir les colonnes: order_id, lat, lon (ou variantes).")

def read_orders_from_xlsx(path: str) -> Tuple[Optional[Tuple[float, float]], List[Tuple[str, float, float]]]:
    """
    Reads Excel even if header isn't on row 1.
    Optional row with order_id == 'START' gives depot.
    Returns: (start_opt, orders)
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    header_row, col_map = find_header_row(ws)
    col_id = col_map["order_id"]
    col_lat = col_map["lat"]
    col_lon = col_map["lon"]

    start_opt = None
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

        if oid_str.upper() == "START":
            start_opt = (lat_f, lon_f)
        else:
            orders.append((oid_str, lat_f, lon_f))

    if not orders:
        raise ValueError("Aucune commande détectée dans l’Excel.")
    return start_opt, orders


# ----------------------------
# Distance (Haversine)
# ----------------------------
def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def build_distance_matrix(points: List[Tuple[float, float]]) -> List[List[int]]:
    """
    OR-Tools expects integer costs. We'll use meters.
    """
    n = len(points)
    mat = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                mat[i][j] = 0
            else:
                mat[i][j] = int(haversine_km(points[i], points[j]) * 1000)
    return mat


# ----------------------------
# OR-Tools VRP (Balanced)
# ----------------------------
def solve_vrp_ortools_balanced(
    start: Tuple[float, float],
    orders: List[Tuple[str, float, float]],
    num_vehicles: int,
    time_limit_sec: int = 8,
):
    """
    Depot fixed (start/end at depot).
    Balance strategy:
      - capacity max_per per vehicle (max number of orders)
      - fixed cost to discourage leaving vehicles unused
      - global span on distance to reduce one route dominating
    """
    num_vehicles = min(num_vehicles, len(orders))  # can't use more vehicles than orders

    coords = [start] + [(o[1], o[2]) for o in orders]  # 0 depot, 1..N orders
    dist = build_distance_matrix(coords)

    manager = pywrapcp.RoutingIndexManager(len(coords), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_cb(from_index, to_index):
        f = manager.IndexToNode(from_index)
        t = manager.IndexToNode(to_index)
        return dist[f][t]

    transit = routing.RegisterTransitCallback(distance_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit)

    # ✅ Encourage using more vehicles (avoid 6 vs 1)
    # Tune this value if needed:
    routing.SetFixedCostOfAllVehicles(10000)

    # Distance dimension + balance distance between routes
    routing.AddDimension(
        transit,
        0,
        400000,   # 400 km max safety
        True,
        "Distance"
    )
    distance_dim = routing.GetDimensionOrDie("Distance")
    distance_dim.SetGlobalSpanCostCoefficient(120)

    # Capacity dimension: 1 per order, 0 for depot
    demands = [0] + [1] * len(orders)

    def demand_cb(from_index):
        node = manager.IndexToNode(from_index)
        return demands[node]

    demand = routing.RegisterUnaryTransitCallback(demand_cb)

    min_per = len(orders) // num_vehicles
    max_per = math.ceil(len(orders) / num_vehicles)  # ensures 2-3 when 11 orders, 5 vehicles

    vehicle_capacities = [max_per] * num_vehicles

    routing.AddDimensionWithVehicleCapacity(
        demand,
        0,
        vehicle_capacities,
        True,
        "Capacity"
    )

    # Search params
    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.FromSeconds(time_limit_sec)

    solution = routing.SolveWithParameters(params)
    if not solution:
        raise ValueError("Aucune solution VRP trouvée. (Essaie moins de livreurs ou augmente time_limit)")

    routes = []
    for v in range(num_vehicles):
        idx = routing.Start(v)
        route_nodes = []
        while not routing.IsEnd(idx):
            node = manager.IndexToNode(idx)
            if node != 0:
                route_nodes.append(node - 1)  # to orders index
            idx = solution.Value(routing.NextVar(idx))

        if route_nodes:
            dist_m = solution.Value(distance_dim.CumulVar(routing.End(v)))
            routes.append((route_nodes, dist_m))

    # Optional sort by distance desc
    routes.sort(key=lambda x: x[1], reverse=True)
    return routes


# ----------------------------
# Google Maps link + formatting
# ----------------------------
def google_maps_link(start: Tuple[float, float], route_orders: List[Tuple[str, float, float]]) -> str:
    parts = [f"{start[0]},{start[1]}"] + [f"{o[1]},{o[2]}" for o in route_orders]
    return "https://www.google.com/maps/dir/" + "/".join(parts)

def format_routes_message(start, orders, routes):
    lines = []
    total_km = 0.0
    for i, (order_idx_list, dist_m) in enumerate(routes, start=1):
        route_orders = [orders[idx] for idx in order_idx_list]
        link = google_maps_link(start, route_orders)
        order_ids = " → ".join([o[0] for o in route_orders])
        km = dist_m / 1000.0
        total_km += km
        lines.append(f"🚚 *Livreur {i}* ({len(route_orders)} commandes) — ~{km:.1f} km")
        lines.append(f"Ordre : {order_ids}")
        lines.append(f"🔗 {link}")
        lines.append("")
    lines.append(f"📌 Total approx (somme) : ~{total_km:.1f} km")
    return "\n".join(lines).strip()


# ----------------------------
# Telegram handlers
# ----------------------------
async def vrp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        k, start = parse_vrp_command(update.message.text)
        context.user_data["pending_vrp"] = {"k": k, "start": start}
        await update.message.reply_text(
            "✅ OK. Maintenant envoie le fichier Excel (.xlsx).\n\n"
            "Format Excel: colonnes order_id, lat, lon (même si le header n'est pas en ligne 1, je le détecte).\n"
            "Tu peux aussi mettre une ligne order_id=START pour le dépôt."
        )
    except Exception as e:
        await update.message.reply_text(
            "❌ Format incorrect.\n"
            "Exemples:\n"
            "/vrp 5 start 33.5726,-7.6419\n"
            "/vrp 5   (si START est dans Excel)\n\n"
            f"Erreur: {e}"
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pending = context.user_data.get("pending_vrp")
    if not pending:
        return

    doc: Document = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Envoie un fichier .xlsx (Excel).")
        return

    k = pending["k"]
    start_cmd = pending.get("start")

    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, doc.file_name)
        await tg_file.download_to_drive(custom_path=path)

        try:
            start_xlsx, orders = read_orders_from_xlsx(path)
            start = start_cmd or start_xlsx
            if not start:
                raise ValueError("Dépôt manquant: ajoute START dans Excel ou donne start dans /vrp.")

            routes = solve_vrp_ortools_balanced(start, orders, k, time_limit_sec=8)
            msg = format_routes_message(start, orders, routes)

            context.user_data.pop("pending_vrp", None)
            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

        except Exception as e:
            await update.message.reply_text(f"❌ Erreur VRP/Excel: {e}")


def main():
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN env var. (Windows: set BOT_TOKEN=xxx)")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("vrp", vrp_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.run_polling()


if __name__ == "__main__":
    main()
