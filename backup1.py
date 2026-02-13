import os
import re
import math
import tempfile
from typing import List, Tuple, Dict, Optional
import numpy as np
import openpyxl
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from telegram import Update, Document
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from dotenv import load_dotenv
load_dotenv()


TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# ----------------------------
# Utils
# ----------------------------
def haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    R = 6371.0
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))

def parse_vrp_command(text: str):
    """
    /vrp K [start lat,lon]
    examples:
      /vrp 5 start 33.5726,-7.6419
      /vrp 5
    """
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    if not lines:
        raise ValueError("Commande vide.")

    m = re.match(r"^/vrp\s+(\d+)(.*)$", lines[0])
    if not m:
        raise ValueError("Format: /vrp 5 start 33.57,-7.64  (ou /vrp 5 si START est dans Excel)")
    k = int(m.group(1))
    if k < 1 or k > 30:
        raise ValueError("Nombre livreurs entre 1 et 30.")

    rest = (m.group(2) or "").strip().lower()
    start = None

    if "start" in rest:
        coords = re.findall(r"(-?\d+\.\d+)", rest)
        if len(coords) >= 2:
            start = (float(coords[0]), float(coords[1]))
        else:
            raise ValueError("start invalide. Exemple: start 33.5726,-7.6419")

    return k, start

def read_orders_from_xlsx(path: str):
    """
    Reads Excel with headers: order_id, lat, lon (case-insensitive).
    Optional row where order_id == 'START' gives depot.
    Returns: (start_opt, orders)
      start_opt: Optional[(lat,lon)]
      orders: List[(order_id, lat, lon)]
    """
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active

    # Read header row
    header = []
    for c in ws[1]:
        header.append(str(c.value).strip().lower() if c.value is not None else "")

    def find_col(name: str):
        if name in header:
            return header.index(name) + 1
        # allow variants
        for i, h in enumerate(header):
            if h.replace(" ", "") == name.replace(" ", ""):
                return i + 1
        return None

    col_id = find_col("order_id") or find_col("id") or find_col("commande") or find_col("order")
    col_lat = find_col("lat") or find_col("latitude")
    col_lon = find_col("lon") or find_col("lng") or find_col("longitude") or find_col("long")

    if not (col_id and col_lat and col_lon):
        raise ValueError("Excel doit contenir les colonnes: order_id, lat, lon (ou variantes).")

    start_opt = None
    orders = []

    for r in range(2, ws.max_row + 1):
        oid = ws.cell(row=r, column=col_id).value
        lat = ws.cell(row=r, column=col_lat).value
        lon = ws.cell(row=r, column=col_lon).value

        if oid is None and lat is None and lon is None:
            continue

        oid_str = str(oid).strip()

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

def build_distance_matrix(points: List[Tuple[float, float]]) -> List[List[int]]:
    """
    OR-Tools uses integer costs. We'll use meters as int.
    """
    n = len(points)
    mat = [[0]*n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            if i == j:
                mat[i][j] = 0
            else:
                km = haversine_km(points[i], points[j])
                mat[i][j] = int(km * 1000)  # meters
    return mat

def solve_vrp_ortools(
    start: Tuple[float, float],
    orders: List[Tuple[str, float, float]],
    num_vehicles: int,
):
    """
    VRP with fixed depot start/end = same depot.
    Returns: list of routes, each route is list of indices into orders (not including depot),
            and total distance meters per route.
    """
    # Node 0 = depot, nodes 1..N = orders
    coords = [start] + [(o[1], o[2]) for o in orders]
    dist = build_distance_matrix(coords)

    manager = pywrapcp.RoutingIndexManager(len(coords), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def distance_callback(from_index, to_index):
        f = manager.IndexToNode(from_index)
        t = manager.IndexToNode(to_index)
        return dist[f][t]

    transit_cb = routing.RegisterTransitCallback(distance_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_cb)

    # Optional: try to balance loads a bit (minimize longest route)
    dimension_name = "Distance"
    routing.AddDimension(
        transit_cb,
        0,
        300000,  # max distance per vehicle in meters (300 km safety)
        True,
        dimension_name
    )
    distance_dimension = routing.GetDimensionOrDie(dimension_name)
    distance_dimension.SetGlobalSpanCostCoefficient(100)  # encourages balanced routes

    search_params = pywrapcp.DefaultRoutingSearchParameters()
    search_params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_params.time_limit.FromSeconds(6)  # quick but good; you can raise later

    solution = routing.SolveWithParameters(search_params)
    if not solution:
        raise ValueError("Impossible de trouver une solution VRP (essaie plus de temps ou moins de livreurs).")

    routes = []
    for v in range(num_vehicles):
        index = routing.Start(v)
        route_nodes = []
        while not routing.IsEnd(index):
            node = manager.IndexToNode(index)
            if node != 0:
                route_nodes.append(node - 1)  # convert to orders index
            index = solution.Value(routing.NextVar(index))

        # distance for vehicle
        dist_m = solution.Value(distance_dimension.CumulVar(routing.End(v)))
        routes.append((route_nodes, dist_m))

    # Remove empty routes (vehicles not used)
    routes = [(r, d) for (r, d) in routes if len(r) > 0]
    # Sort by distance desc (optional)
    routes.sort(key=lambda x: x[1], reverse=True)
    return routes

def google_maps_link(start: Tuple[float, float], route_orders: List[Tuple[str, float, float]]) -> str:
    parts = [f"{start[0]},{start[1]}"] + [f"{o[1]},{o[2]}" for o in route_orders]
    return "https://www.google.com/maps/dir/" + "/".join(parts)

def format_routes_message(start, orders, routes):
    lines = []
    for i, (order_idx_list, dist_m) in enumerate(routes, start=1):
        route_orders = [orders[idx] for idx in order_idx_list]
        link = google_maps_link(start, route_orders)
        order_ids = " → ".join([o[0] for o in route_orders])
        lines.append(f"🚚 *Livreur {i}* ({len(route_orders)} commandes) — ~{dist_m/1000:.1f} km")
        lines.append(f"Ordre : {order_ids}")
        lines.append(f"🔗 {link}")
        lines.append("")
    return "\n".join(lines).strip()

# ----------------------------
# Telegram handlers
# ----------------------------
async def vrp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        k, start = parse_vrp_command(update.message.text)
        # Store pending config; expect an Excel next
        context.user_data["pending_vrp"] = {"k": k, "start": start}
        await update.message.reply_text(
            "✅ OK. Maintenant envoie le fichier Excel (.xlsx).\n\n"
            "Rappel format Excel: colonnes order_id, lat, lon.\n"
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
        return  # ignore docs unless user started /vrp

    doc: Document = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ Envoie un fichier .xlsx (Excel).")
        return

    k = pending["k"]
    start_cmd = pending.get("start")

    # Download file
    tg_file = await doc.get_file()
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, doc.file_name)
        await tg_file.download_to_drive(custom_path=path)

        try:
            start_xlsx, orders = read_orders_from_xlsx(path)
            start = start_cmd or start_xlsx
            if not start:
                raise ValueError("Dépôt manquant: ajoute START dans Excel ou donne start dans /vrp.")

            routes = solve_vrp_ortools(start, orders, k)
            msg = format_routes_message(start, orders, routes)

            # cleanup pending
            context.user_data.pop("pending_vrp", None)

            await update.message.reply_text(msg, parse_mode="Markdown", disable_web_page_preview=True)

        except Exception as e:
            await update.message.reply_text(f"❌ Erreur VRP/Excel: {e}")

def main():
    if not TOKEN:
        raise SystemExit("Set BOT_TOKEN env var (ex: set BOT_TOKEN=xxx).")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("vrp", vrp_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.run_polling()

if __name__ == "__main__":
    main()
