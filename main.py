import os
import json
import re
import base64
import requests
from datetime import datetime, date
from typing import Optional, Tuple, Dict, Any
import asyncio

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from store_loader_v2 import load_stores

# ======================
# CONFIG
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Google Apps Script Web App
# Exemple: https://script.google.com/macros/s/XXXX/exec
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL", "").strip()
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()

# allowlist.json: {"CIN123":"Amine", ...}
ALLOWLIST = json.load(open("allowlist.json", "r", encoding="utf-8"))

# Excel: code site.xlsx (colonnes: code, site, drivers_count, responsable_name, responsable_cin)
STORES: Dict[str, Dict[str, Any]] = load_stores("code site.xlsx")

# States
S_MENU, S_STORE, S_CIN, S_DATE, S_ETAT, S_RECU, S_CONFIRM = range(7)

# Callback actions
A_UPLOAD = "UPLOAD"
A_RESTART = "RESTART"
A_CANCEL = "CANCEL"
A_TODAY = "TODAY"
A_CONFIRM = "CONFIRM"

def A_STORE(code: str) -> str:
    return f"STORE|{code}"

# ======================
# UTILS
# ======================
def safe_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    # caractères interdits pour filenames
    s = re.sub(r'[\\/:*?"<>|]', "-", s)
    return s[:80]

def today_str() -> str:
    return date.today().strftime("%Y-%m-%d")

def parse_date(s: str) -> Optional[str]:
    s = (s or "").strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return None

def guess_mime_and_ext_from_document(doc) -> Tuple[str, str]:
    mt = doc.mime_type or "application/octet-stream"
    fn = (doc.file_name or "").lower()

    ext = ""
    if "." in fn:
        ext = "." + fn.split(".")[-1]
    else:
        if mt == "application/pdf":
            ext = ".pdf"
        elif mt.startswith("image/"):
            ext = ".jpg"
        else:
            ext = ""
    return mt, ext

async def get_file_bytes(update: Update) -> Tuple[bytes, str, str]:
    """
    Retourne (bytes, mimetype, ext)
    Support PHOTO ou DOCUMENT (pdf/image).
    """
    msg = update.message

    if msg.document:
        doc = msg.document
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        mimetype, ext = guess_mime_and_ext_from_document(doc)
        return bytes(data), mimetype, ext

    if msg.photo:
        p = msg.photo[-1]
        tg_file = await p.get_file()
        data = await tg_file.download_as_bytearray()
        return bytes(data), "image/jpeg", ".jpg"

    raise ValueError("Aucun fichier détecté (envoyer une PHOTO ou un DOCUMENT).")

# ======================
# GOOGLE APPS SCRIPT UPLOAD
# ======================
def upload_to_drive_via_gas(
    date_str: str,
    store_code: str,
    driver_name: str,
    kind: str,
    filename: str,
    data: bytes,
    mime: str,
) -> Dict[str, Any]:
    """
    Envoie un fichier au Web App Apps Script pour créer le path:
    YYYY-MM-DD / StoreCode / DriverName / (ETAT_*.jpg + RECU.jpg)
    """
    if not GAS_WEBAPP_URL:
        raise RuntimeError("GAS_WEBAPP_URL manquant")
    if not UPLOAD_TOKEN:
        raise RuntimeError("UPLOAD_TOKEN manquant")

    payload = {
        "token": UPLOAD_TOKEN,
        "date": date_str,
        "store_code": str(store_code),
        "driver_name": driver_name,
        "kind": kind,
        "filename": filename,
        "mimeType": mime,
        "contentBase64": base64.b64encode(data).decode("ascii"),
    }
    r = requests.post(GAS_WEBAPP_URL, json=payload, timeout=90)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {"ok": True, "raw": r.text}

# ======================
# KEYBOARDS
# ======================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload", callback_data=A_UPLOAD)],
        [InlineKeyboardButton("🔄 Restart", callback_data=A_RESTART)],
    ])

def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Annuler", callback_data=A_CANCEL)],
    ])

def date_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Aujourd’hui", callback_data=A_TODAY)],
        [InlineKeyboardButton("❌ Annuler", callback_data=A_CANCEL)],
    ])

def stores_kb() -> InlineKeyboardMarkup:
    """
    Affiche les magasins (2 colonnes) + bouton Menu
    """
    # tri par nom
    items = sorted(STORES.items(), key=lambda kv: (kv[1].get("site") or "").lower())
    rows = []
    row = []
    for code, s in items:
        label = safe_text(s.get("site") or str(code))
        row.append(InlineKeyboardButton(label, callback_data=A_STORE(str(code))))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("Menu", callback_data=A_RESTART)])
    return InlineKeyboardMarkup(rows)

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmer & Upload", callback_data=A_CONFIRM)],
        [InlineKeyboardButton("🔄 Restart", callback_data=A_RESTART)],
    ])

# ======================
# SESSION
# ======================
def reset_session(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

def ensure_store_selected(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("store_code"))

def begin_etat_step_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    store_name = context.user_data["store_name"]
    store_code = context.user_data.get("store_folder") or context.user_data["store_code"]
    resp_name = context.user_data["responsable_name"]
    required = int(context.user_data.get("required_etat", 5))
    d = context.user_data["date"]

    return (
        f"✅ Magasin: {store_name} (Code {store_code})\n"
        f"👤 Responsable: {resp_name}\n"
        f"📅 Date: {d}\n\n"
        f"📌 ETAT requis: {required} fichiers\n\n"
        f"Envoie maintenant les fichiers ETAT (photo ou document).\n"
        f"Progress: 0/{required}"
    )

# ======================
# FLOW
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_session(context)
    await update.message.reply_text("Menu ✅", reply_markup=main_menu_kb())
    return S_MENU

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == A_UPLOAD:
        reset_session(context)
        await q.edit_message_text("🏪 Choisis le magasin :", reply_markup=stores_kb())
        return S_STORE

    if q.data == A_RESTART:
        reset_session(context)
        await q.edit_message_text("Menu ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data == A_CANCEL:
        reset_session(context)
        await q.edit_message_text("Annulé ✅", reply_markup=main_menu_kb())
        return S_MENU

    # si autre callback non géré, renvoie menu
    return S_MENU

async def store_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    data = q.data or ""
    if not data.startswith("STORE|"):
        return S_STORE

    code = data.split("|", 1)[1].strip()
    if code not in STORES:
        await q.edit_message_text("❌ Magasin invalide (pas dans Excel).", reply_markup=stores_kb())
        return S_STORE

    store = STORES[code]
    store_name = store.get("site") or code
    resp_name = (store.get("responsable_name") or "").strip()

    # si responsable_name vide -> on affiche "Responsable" générique
    if not resp_name:
        resp_name = "Responsable"

    context.user_data["store_code"] = code
    context.user_data["store_name"] = store_name
    context.user_data["store_folder"] = f"{code} - {store_name}"
    context.user_data["required_etat"] = int(store.get("drivers_count") or 5)
    context.user_data["responsable_name"] = resp_name
    context.user_data["responsable_cin"] = (store.get("responsable_cin") or "").strip().upper()

    await q.edit_message_text(
        f"👋 Bonjour {resp_name}\n"
        f"🏪 Magasin: {store_name}\n\n"
        f"Entre ton CIN (doit être autorisé).",
        reply_markup=cancel_kb()
    )
    return S_CIN

async def get_cin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not ensure_store_selected(context):
        await update.message.reply_text("Choisis d'abord le magasin.", reply_markup=stores_kb())
        return S_STORE

    cin = safe_text(update.message.text).upper()

    # 1) CIN doit exister dans allowlist
    if cin not in ALLOWLIST:
        await update.message.reply_text("❌ CIN non autorisé (allowlist).", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    # 2) Si le magasin a un responsable_cin défini dans Excel -> doit matcher
    expected = (context.user_data.get("responsable_cin") or "").strip().upper()
    if expected and cin != expected:
        await update.message.reply_text(
            "❌ CIN ne correspond pas au responsable de ce magasin.",
            reply_markup=main_menu_kb()
        )
        reset_session(context)
        return S_MENU

    context.user_data["cin"] = cin

    # message "Bonjour + nom" (nom pris d'allowlist si tu veux afficher le vrai)
    allow_name = ALLOWLIST.get(cin, "").strip()
    if allow_name:
        context.user_data["responsable_name"] = safe_text(allow_name)

    await update.message.reply_text(
        "📅 Date ?\n- Clique sur Aujourd’hui ou tape: YYYY-MM-DD",
        reply_markup=date_kb()
    )
    return S_DATE

async def date_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == A_CANCEL:
        reset_session(context)
        await q.edit_message_text("Annulé ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data == A_TODAY:
        d = today_str()
        context.user_data["date"] = d
        context.user_data["etat_files"] = []
        context.user_data["recu_file"] = None

        await q.edit_message_text(begin_etat_step_text(context), reply_markup=cancel_kb())
        return S_ETAT

    return S_DATE

async def date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("Format invalide. Exemple: 2026-02-17", reply_markup=cancel_kb())
        return S_DATE

    context.user_data["date"] = d
    context.user_data["etat_files"] = []
    context.user_data["recu_file"] = None

    await update.message.reply_text(begin_etat_step_text(context), reply_markup=cancel_kb())
    return S_ETAT

async def collect_etat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    required = int(context.user_data.get("required_etat", 5))

    try:
        data, mime, ext = await get_file_bytes(update)
    except Exception as e:
        await update.message.reply_text(f"Envoie une PHOTO ou un DOCUMENT. ({e})", reply_markup=cancel_kb())
        return S_ETAT

    context.user_data["etat_files"].append({"data": data, "mime": mime, "ext": ext})
    n = len(context.user_data["etat_files"])

    if n < required:
        await update.message.reply_text(f"✅ ETAT reçu: {n}/{required}\nContinue…", reply_markup=cancel_kb())
        return S_ETAT

    await update.message.reply_text(
        f"✅ ETAT complet: {n}/{required}\n\n"
        f"Maintenant envoie le RECU (1 fichier obligatoire).",
        reply_markup=cancel_kb()
    )
    return S_RECU

async def collect_recu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        data, mime, ext = await get_file_bytes(update)
    except Exception as e:
        await update.message.reply_text(f"Envoie une PHOTO ou un DOCUMENT. ({e})", reply_markup=cancel_kb())
        return S_RECU

    context.user_data["recu_file"] = {"data": data, "mime": mime, "ext": ext}

    d = context.user_data["date"]
    store_name = context.user_data["store_name"]
    store_code = context.user_data.get("store_folder") or context.user_data["store_code"]
    resp_name = context.user_data["responsable_name"]
    required = int(context.user_data.get("required_etat", 5))

    await update.message.reply_text(
        "✅ RECU reçu.\n\n"
        "Récap:\n"
        f"- Date: {d}\n"
        f"- Magasin: {store_name} (Code {store_code})\n"
        f"- Responsable: {resp_name}\n"
        f"- ETAT: {required} fichiers\n"
        f"- RECU: 1 fichier\n\n"
        "Confirme pour envoyer sur Drive.",
        reply_markup=confirm_kb()
    )
    return S_CONFIRM

async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == A_RESTART:
        reset_session(context)
        await q.edit_message_text("Menu ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data != A_CONFIRM:
        return S_CONFIRM

    # Safety checks
    if not context.user_data.get("date") or not context.user_data.get("store_code"):
        await q.edit_message_text("Session invalide. Restart.", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    d = context.user_data["date"]
    store_code = context.user_data.get("store_folder") or context.user_data["store_code"]
    name = context.user_data["responsable_name"]

    # Upload via Apps Script Web App -> Drive
    try:
        for i, f in enumerate(context.user_data["etat_files"], start=1):
            filename = f"ETAT-{i:02d}{f['ext'] or ''}"
            upload_to_drive_via_gas(
                date_str=d,
                store_code=store_code,
                driver_name=name,
                kind="ETAT",
                filename=filename,
                data=f["data"],
                mime=f["mime"],
            )

        rf = context.user_data["recu_file"]
        recu_name = f"RECU{rf['ext'] or ''}"
        upload_to_drive_via_gas(
            date_str=d,
            store_code=store_code,
            driver_name=name,
            kind="RECU",
            filename=recu_name,
            data=rf["data"],
            mime=rf["mime"],
        )

    except Exception as e:
        await q.edit_message_text(f"❌ Upload échoué: {e}\n\n🔄 Restart.", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    await q.edit_message_text("✅ Upload terminé.\n\n🔄 Tu peux Restart.", reply_markup=main_menu_kb())
    reset_session(context)
    return S_MENU

# ======================
# APP BUILD + RUN (Render-safe)
# ======================
def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            S_MENU: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_UPLOAD}$|^{A_RESTART}$|^{A_CANCEL}$"),
            ],
            S_STORE: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_RESTART}$|^{A_CANCEL}$|^{A_UPLOAD}$"),
                CallbackQueryHandler(store_cb, pattern=r"^STORE\|"),
            ],
            S_CIN: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_RESTART}$|^{A_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_cin),
            ],
            S_DATE: [
                CallbackQueryHandler(date_cb, pattern=f"^{A_TODAY}$|^{A_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_text),
            ],
            S_ETAT: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_RESTART}$|^{A_CANCEL}$"),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, collect_etat),
            ],
            S_RECU: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_RESTART}$|^{A_CANCEL}$"),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, collect_recu),
            ],
            S_CONFIRM: [
                CallbackQueryHandler(confirm_cb),
            ],
        },
        fallbacks=[CallbackQueryHandler(on_menu_click)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    return app

async def _main() -> None:
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    # bloque à vie (worker Render)
    await asyncio.Event().wait()

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise SystemExit("❌ Mets BOT_TOKEN dans la variable d'environnement BOT_TOKEN")
    if not GAS_WEBAPP_URL:
        raise SystemExit("❌ Mets GAS_WEBAPP_URL dans les variables d'environnement (URL du Web App)")
    if not UPLOAD_TOKEN:
        raise SystemExit("❌ Mets UPLOAD_TOKEN dans les variables d'environnement UPLOAD_TOKEN")

    asyncio.run(_main())
