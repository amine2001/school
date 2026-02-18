import os
import json
import re
import base64
import requests
from datetime import datetime, date
from typing import Optional, Tuple
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

from store_loader import load_stores

# ======================
# CONFIG
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "7730295164:AAEUuTUqBXMwoTAMr6oTebY3Wtt_LH9lpIQ")

# Google Apps Script Web App (Solution 1)
# Exemple: https://script.google.com/macros/s/XXXX/exec
GAS_WEBAPP_URL = os.getenv("GAS_WEBAPP_URL", "").strip()
UPLOAD_TOKEN = os.getenv("UPLOAD_TOKEN", "").strip()

# allowlist.json: {"CIN123":"Amine", ...}
ALLOWLIST = json.load(open("allowlist.json", "r", encoding="utf-8"))
STORES = load_stores("code site.xlsx")

# States
S_MENU, S_DATE, S_NAME, S_CIN, S_STORE, S_ETAT, S_RECU, S_CONFIRM = range(8)

# Callback actions
A_UPLOAD = "UPLOAD"
A_RESTART = "RESTART"
A_CANCEL = "CANCEL"
A_TODAY = "TODAY"
A_CONFIRM = "CONFIRM"


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
    kind: str,          # "ETAT" or "RECU"
    filename: str,
    data: bytes,
    mime: str
):
    """
    Upload file bytes to Google Apps Script Web App.
    Apps Script doPost lit le contenu en base64 depuis postData.contents
    """
    if not GAS_WEBAPP_URL:
        raise RuntimeError("Missing env GAS_WEBAPP_URL")
    if not UPLOAD_TOKEN:
        raise RuntimeError("Missing env UPLOAD_TOKEN")

    if kind not in ("ETAT", "RECU"):
        raise RuntimeError("Invalid kind (ETAT/RECU only)")

    params = {
        "token": UPLOAD_TOKEN,
        "date": date_str,
        "store_code": str(store_code),
        "driver_name": driver_name,
        "kind": kind,
        "filename": filename,
        "mime": mime,
    }

    b64 = base64.b64encode(data).decode("utf-8")

    r = requests.post(
        GAS_WEBAPP_URL,
        params=params,
        data=b64,
        headers={"Content-Type": "text/plain"},
        timeout=90,
    )

    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Invalid response from GAS: {r.text[:200]}")

    if not j.get("ok"):
        raise RuntimeError(f"GAS error: {j.get('error')}")

    return j


# ======================
# UI KEYBOARDS
# ======================
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 Upload", callback_data=A_UPLOAD)],
        [InlineKeyboardButton("🔄 Restart", callback_data=A_RESTART)],
    ])


def cancel_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Annuler", callback_data=A_CANCEL)],
        [InlineKeyboardButton("🔄 Restart", callback_data=A_RESTART)],
    ])


def date_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📅 Aujourd’hui ({today_str()})", callback_data=A_TODAY)],
        [InlineKeyboardButton("❌ Annuler", callback_data=A_CANCEL)],
    ])


def confirm_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirmer Upload", callback_data=A_CONFIRM)],
        [InlineKeyboardButton("🔄 Restart", callback_data=A_RESTART)],
        [InlineKeyboardButton("❌ Annuler", callback_data=A_CANCEL)],
    ])


def reset_session(context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    context.user_data["etat_files"] = []   # list of dicts: {data,mime,ext}
    context.user_data["recu_file"] = None  # dict


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
        await q.edit_message_text(
            "Date ?\n- Clique sur Aujourd’hui ou tape: YYYY-MM-DD",
            reply_markup=date_kb()
        )
        return S_DATE

    if q.data == A_RESTART:
        reset_session(context)
        await q.edit_message_text("Menu ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data == A_CANCEL:
        reset_session(context)
        await q.edit_message_text("Annulé ✅", reply_markup=main_menu_kb())
        return S_MENU

    return S_MENU


async def date_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == A_TODAY:
        context.user_data["date"] = today_str()
        await query.edit_message_text(
            f"📅 Date: {context.user_data['date']}\n\n👤 Nom complet ?",
            reply_markup=cancel_kb()
        )
        return S_NAME

    if query.data == A_CANCEL:
        reset_session(context)
        await query.edit_message_text("Annulé ✅", reply_markup=main_menu_kb())
        return S_MENU

    return S_DATE


async def date_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    d = parse_date(update.message.text)
    if not d:
        await update.message.reply_text("Format invalide. Exemple: 2026-02-17", reply_markup=cancel_kb())
        return S_DATE

    context.user_data["date"] = d
    await update.message.reply_text(f"📅 Date: {d}\n\n👤 Nom complet ?", reply_markup=cancel_kb())
    return S_NAME


async def get_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = safe_text(update.message.text)
    if not name:
        await update.message.reply_text("Nom invalide. Réessaie.", reply_markup=cancel_kb())
        return S_NAME
    context.user_data["name"] = name
    await update.message.reply_text("CIN ? (doit être dans allowlist)", reply_markup=cancel_kb())
    return S_CIN


async def get_cin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cin = safe_text(update.message.text).upper()
    if cin not in ALLOWLIST:
        await update.message.reply_text("❌ CIN non autorisé (allowlist).", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    context.user_data["cin"] = cin
    await update.message.reply_text("Code magasin ? (ex: 207)", reply_markup=cancel_kb())
    return S_STORE


async def get_store(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = safe_text(update.message.text)
    if code not in STORES:
        await update.message.reply_text("❌ Magasin invalide (pas dans Excel). Réessaie.", reply_markup=cancel_kb())
        return S_STORE

    store = STORES[code]
    context.user_data["store_code"] = code
    context.user_data["store_name"] = store["site"]
    context.user_data["required_etat"] = int(store["drivers_count"])

    context.user_data["etat_files"] = []
    context.user_data["recu_file"] = None

    await update.message.reply_text(
        f"✅ Magasin: {code} — {store['site']}\n"
        f"📌 ETAT requis: {context.user_data['required_etat']} fichiers\n\n"
        f"Envoie maintenant les fichiers ETAT (photo ou document).\n"
        f"Progress: 0/{context.user_data['required_etat']}",
        reply_markup=cancel_kb()
    )
    return S_ETAT


async def collect_etat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    required = context.user_data.get("required_etat", 5)

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
    store_code = context.user_data["store_code"]
    store_name = context.user_data["store_name"]
    name = context.user_data["name"]
    cin = context.user_data["cin"]
    required = context.user_data["required_etat"]
    etat_n = len(context.user_data["etat_files"])
    recu_ok = "✅"

    recap = (
        "📌 RÉCAP\n"
        f"- Date: {d}\n"
        f"- Magasin: {store_code} — {store_name}\n"
        f"- Nom: {name}\n"
        f"- CIN: {cin}\n"
        f"- ETAT: {etat_n}/{required}\n"
        f"- RECU: {recu_ok}\n\n"
        "Clique ✅ Confirmer Upload pour envoyer sur Drive."
    )
    await update.message.reply_text(recap, reply_markup=confirm_kb())
    return S_CONFIRM


async def confirm_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == A_CANCEL:
        reset_session(context)
        await q.edit_message_text("Annulé ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data == A_RESTART:
        reset_session(context)
        await q.edit_message_text("Menu ✅", reply_markup=main_menu_kb())
        return S_MENU

    if q.data != A_CONFIRM:
        return S_CONFIRM

    required = context.user_data.get("required_etat", 5)
    if len(context.user_data.get("etat_files", [])) != required:
        await q.edit_message_text("❌ ETAT incomplet. Restart puis recommence.", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    if not context.user_data.get("recu_file"):
        await q.edit_message_text("❌ RECU manquant. Restart puis recommence.", reply_markup=main_menu_kb())
        reset_session(context)
        return S_MENU

    d = context.user_data["date"]
    store_code = context.user_data["store_code"]
    name = context.user_data["name"]

    # Upload via Apps Script Web App -> Drive perso
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
        await q.edit_message_text(
            "❌ Upload échoué (Apps Script).\n\n"
            "Vérifie:\n"
            "1) GAS_WEBAPP_URL correct\n"
            "2) UPLOAD_TOKEN correct (même valeur que dans Code.gs)\n\n"
            f"Erreur: {e}",
            reply_markup=main_menu_kb()
        )
        reset_session(context)
        return S_MENU

    await q.edit_message_text(
        "✅ Upload terminé sur Drive.\n\nTu veux refaire ?",
        reply_markup=main_menu_kb()
    )
    reset_session(context)
    return S_MENU


def build_app() -> Application:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            S_MENU: [
                CallbackQueryHandler(on_menu_click, pattern=f"^{A_UPLOAD}$|^{A_RESTART}$|^{A_CANCEL}$"),
            ],
            S_DATE: [
                CallbackQueryHandler(date_cb, pattern=f"^{A_TODAY}$|^{A_CANCEL}$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, date_text),
            ],
            S_NAME: [
                CallbackQueryHandler(on_menu_click),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_name),
            ],
            S_CIN: [
                CallbackQueryHandler(on_menu_click),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_cin),
            ],
            S_STORE: [
                CallbackQueryHandler(on_menu_click),
                MessageHandler(filters.TEXT & ~filters.COMMAND, get_store),
            ],
            S_ETAT: [
                CallbackQueryHandler(on_menu_click),
                MessageHandler((filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND, collect_etat),
            ],
            S_RECU: [
                CallbackQueryHandler(on_menu_click),
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


if __name__ == "__main__":
    if BOT_TOKEN.startswith("PUT_"):
        raise SystemExit("❌ Mets BOT_TOKEN dans la variable d'environnement BOT_TOKEN")

    if not GAS_WEBAPP_URL:
        raise SystemExit("❌ Mets GAS_WEBAPP_URL dans les variables d'environnement (URL du Web App)")
    if not UPLOAD_TOKEN:
        raise SystemExit("❌ Mets UPLOAD_TOKEN dans les variables d'environnement (même que Code.gs)")

    

async def _main():
    app = build_app()
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    # bloque "pour toujours"
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(_main())

