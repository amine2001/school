import os
import base64
import requests

WEBAPP_URL = os.getenv("GAS_WEBAPP_URL", "").strip()
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()

def upload_to_drive_via_gas(date_str: str, store_code: str, driver_name: str,
                            kind: str, filename: str, data: bytes, mime: str):
    """
    Upload file bytes to Google Apps Script Web App.
    """
    if not WEBAPP_URL:
        raise RuntimeError("Missing env GAS_WEBAPP_URL")
    if not UPLOAD_TOKEN:
        raise RuntimeError("Missing env UPLOAD_TOKEN")

    params = {
        "token": UPLOAD_TOKEN,
        "date": date_str,
        "store_code": str(store_code),
        "driver_name": driver_name,
        "kind": kind,
        "filename": filename,
        "mime": mime,
    }

    # Apps Script reçoit base64 dans postData.contents
    b64 = base64.b64encode(data).decode("utf-8")

    r = requests.post(
        WEBAPP_URL,
        params=params,
        data=b64,
        headers={"Content-Type": "text/plain"},
        timeout=60,
    )
    # WebApp parfois renvoie 200 même en erreur -> on parse JSON
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Invalid response from GAS: {r.text[:200]}")

    if not j.get("ok"):
        raise RuntimeError(f"GAS error: {j.get('error')}")

    return j
