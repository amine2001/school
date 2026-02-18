from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

SCOPES = ['https://www.googleapis.com/auth/drive']
SERVICE_ACCOUNT_FILE = 'service_account.json'
ROOT_FOLDER_ID = '1051VEQ7CZvDjckstg2DWqTMRM9Urvp4O'


_creds = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
_service = build("drive", "v3", credentials=_creds, cache_discovery=False)

def _find_child_folder(parent_id: str, name: str):
    # Drive "q" utilise des quotes simples -> il faut échapper les ' dans le nom
    escaped_name = (name or "").replace("'", "\\'")

    q = (
        "mimeType='application/vnd.google-apps.folder' and "
        f"name='{escaped_name}' and "
        f"'{parent_id}' in parents and trashed=false"
    )

    res = _service.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def ensure_folder(parent_id: str, name: str) -> str:
    """Retourne l'id du folder enfant 'name' dans parent_id (le crée si absent)."""
    existing = _find_child_folder(parent_id, name)
    if existing:
        return existing

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = _service.files().create(body=meta, fields="id").execute()
    return folder["id"]

def ensure_path(root_id: str, parts: list[str]) -> str:
    """Crée/récupère un chemin de dossiers sous root_id."""
    cur = root_id
    for p in parts:
        cur = ensure_folder(cur, p)
    return cur

def upload_bytes(parent_id: str, filename: str, data: bytes, mimetype: str):
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=True)
    meta = {"name": filename, "parents": [parent_id]}
    _service.files().create(body=meta, media_body=media, fields="id").execute()
