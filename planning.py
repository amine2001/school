"""
Planning livreurs : lecture de l'emploi du temps (Excel), présents / OFF du jour,
et répartition équitable des circuits selon les km cumulés.

Format Excel attendu (une feuille par magasin, ou une seule feuille) :

    |                     | LUNDI | MARDI | MERCREDI | JEUDI | VENDREDI | SAMEDI | DIMANCHE |
    | OTHMANE ELHACHIMI   |   X   |   X   |    X     |   X   |   OFF    |   X    |    X     |
    | ...                 |

- X (ou toute valeur autre que OFF/vide) = travaille
- OFF / REPOS / CONGE / vide            = ne travaille pas
"""
import datetime as dt
import io
import unicodedata
from typing import Dict, List, Optional, Sequence, Tuple

DAY_NAMES_FR = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]

_DAY_ALIASES = {
    "lundi": 0, "lun": 0, "monday": 0, "mon": 0,
    "mardi": 1, "mar": 1, "tuesday": 1, "tue": 1,
    "mercredi": 2, "mer": 2, "wednesday": 2, "wed": 2,
    "jeudi": 3, "jeu": 3, "thursday": 3, "thu": 3,
    "vendredi": 4, "ven": 4, "friday": 4, "fri": 4,
    "samedi": 5, "sam": 5, "saturday": 5, "sat": 5,
    "dimanche": 6, "dim": 6, "sunday": 6, "sun": 6,
}

_OFF_VALUES = {"", "off", "repos", "conge", "absent", "0", "-", "non", "no"}


def _norm(value) -> str:
    text = str(value if value is not None else "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())


def clean_driver_name(value) -> str:
    return " ".join(str(value or "").split()).upper()


def parse_schedule_rows(rows: Sequence[Sequence]) -> List[Dict]:
    """
    rows : lignes brutes (première ligne contenant les jours = en-tête).
    Retourne [{"name": str, "days": [bool x 7]}] (index 0 = lundi).
    """
    header_idx = None
    day_cols: Dict[int, int] = {}
    for i, row in enumerate(rows):
        found = {}
        for col, cell in enumerate(row or []):
            day = _DAY_ALIASES.get(_norm(cell))
            if day is not None:
                found[col] = day
        if len(found) >= 5:
            header_idx, day_cols = i, found
            break

    if header_idx is None:
        raise ValueError(
            "En-tête introuvable : la première ligne doit contenir les jours "
            "(LUNDI, MARDI, ... DIMANCHE)."
        )

    name_col = min(day_cols) - 1
    if name_col < 0:
        raise ValueError("La colonne des noms doit être avant les colonnes des jours.")

    drivers: List[Dict] = []
    seen = set()
    for row in rows[header_idx + 1:]:
        row = list(row or [])
        if len(row) <= name_col:
            continue
        name = clean_driver_name(row[name_col])
        if not name or name in seen:
            continue
        days = [False] * 7
        for col, day in day_cols.items():
            cell = row[col] if col < len(row) else None
            days[day] = _norm(cell) not in _OFF_VALUES
        seen.add(name)
        drivers.append({"name": name, "days": days})

    if not drivers:
        raise ValueError("Aucun livreur trouvé sous l'en-tête des jours.")
    return drivers


def parse_schedule_xlsx(content: bytes) -> List[Dict]:
    """Lit la première feuille contenant un planning valide."""
    import openpyxl  # import local : dépendance uniquement utile pour l'import

    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
    last_error: Optional[Exception] = None
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        try:
            return parse_schedule_rows(rows)
        except ValueError as exc:
            last_error = exc
    raise last_error or ValueError("Fichier Excel vide.")


def parse_schedule_csv(content: bytes) -> List[Dict]:
    import csv

    text = content.decode("utf-8-sig", errors="replace")
    delimiter = ";" if text.count(";") > text.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    return parse_schedule_rows(rows)


_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%Y/%m/%d",
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
)


def parse_delivery_date(value: str) -> Optional[dt.date]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(text[:19]).date()
    except ValueError:
        return None


def split_present_off(schedule: List[Dict], weekday: int) -> Tuple[List[str], List[str]]:
    present = [d["name"] for d in schedule if d["days"][weekday]]
    off = [d["name"] for d in schedule if not d["days"][weekday]]
    return present, off


def assign_routes_fairly(
    route_kms: List[float],
    drivers: List[str],
    cumulative_km: Dict[str, float],
) -> List[str]:
    """
    Équité : le circuit le plus long va au livreur qui a le moins de km cumulés.
    route_kms[i] = km du circuit i. Retourne le nom du livreur pour chaque circuit.
    `drivers` doit contenir au moins len(route_kms) noms (les moins chargés sont choisis).
    """
    by_load = sorted(drivers, key=lambda n: (cumulative_km.get(n, 0.0), n))
    chosen = by_load[: len(route_kms)]
    order = sorted(range(len(route_kms)), key=lambda i: -route_kms[i])
    result = [""] * len(route_kms)
    for driver, route_idx in zip(chosen, order):
        result[route_idx] = driver
    return result


def format_schedule_text(store_name: str, schedule: List[Dict]) -> str:
    if not schedule:
        return f"📅 {store_name}\nAucun planning importé."
    lines = [f"📅 Planning {store_name} ({len(schedule)} livreurs)", ""]
    for weekday, day_name in enumerate(DAY_NAMES_FR):
        present, off = split_present_off(schedule, weekday)
        lines.append(f"{day_name} : {len(present)} présents · OFF : {', '.join(off) or '—'}")
    return "\n".join(lines)
