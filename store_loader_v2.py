import pandas as pd

DEFAULT_DRIVERS_COUNT = 5  # fallback si pas de colonne drivers_count

def _norm_col(c: str) -> str:
    return (c or "").strip().lower()

def load_stores(xlsx_path="code site.xlsx"):
    """Retourne un dict indexé par store_code (str):
    {
      "201": {
         "site": "Hyper Salé",
         "drivers_count": 2,
         "responsable_name": "Nom Prénom",
         "responsable_cin": "AA123456"
      },
      ...
    }
    """
    df = pd.read_excel(xlsx_path)

    cols = {_norm_col(c): c for c in df.columns}
    if "code" not in cols or "site" not in cols:
        raise ValueError("Excel doit contenir au minimum les colonnes: code, site")

    code_col = cols["code"]
    site_col = cols["site"]
    drivers_col = cols.get("drivers_count")  # optionnel
    resp_name_col = cols.get("responsable_name")  # optionnel
    resp_cin_col = cols.get("responsable_cin")    # optionnel

    stores = {}
    for _, row in df.iterrows():
        code = str(row.get(code_col, "")).strip()
        site = str(row.get(site_col, "")).strip()

        # ignore lignes vides
        if not code or code.lower() == "nan":
            continue

        drivers = DEFAULT_DRIVERS_COUNT
        if drivers_col is not None:
            try:
                drivers = int(row.get(drivers_col))
            except Exception:
                drivers = DEFAULT_DRIVERS_COUNT

        resp_name = ""
        if resp_name_col is not None:
            resp_name = str(row.get(resp_name_col, "")).strip()
            if resp_name.lower() == "nan":
                resp_name = ""

        resp_cin = ""
        if resp_cin_col is not None:
            resp_cin = str(row.get(resp_cin_col, "")).strip().upper()
            if resp_cin.lower() == "nan":
                resp_cin = ""

        stores[code] = {
            "site": site,
            "drivers_count": drivers,
            "responsable_name": resp_name,
            "responsable_cin": resp_cin,
        }

    return stores
