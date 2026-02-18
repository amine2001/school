import pandas as pd

DEFAULT_DRIVERS_COUNT = 5  # fallback si pas de colonne drivers_count

def load_stores(xlsx_path="code site.xlsx"):
    df = pd.read_excel(xlsx_path)

    # Normalise colonnes
    cols = {c.lower().strip(): c for c in df.columns}
    if "code" not in cols or "site" not in cols:
        raise ValueError("Excel doit contenir au minimum les colonnes: code, site")

    code_col = cols["code"]
    site_col = cols["site"]
    drivers_col = cols.get("drivers_count")  # optionnel

    stores = {}
    for _, row in df.iterrows():
        code = str(row[code_col]).strip()
        site = str(row[site_col]).strip()
        drivers = DEFAULT_DRIVERS_COUNT
        if drivers_col is not None:
            try:
                drivers = int(row[drivers_col])
            except Exception:
                drivers = DEFAULT_DRIVERS_COUNT

        # ignore lignes vides
        if code and code.lower() != "nan":
            stores[code] = {"site": site, "drivers_count": drivers}

    return stores
