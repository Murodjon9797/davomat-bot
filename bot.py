"""
Davomat bot — QR kod orqali davomat qiluvchi Telegram bot + Mini App.

Ishga tushirish:
    pip install -r requirements.txt
    python bot.py

Sozlamalar .env faylida (BOT_TOKEN, WEBAPP_URL, ADMIN_IDS, PORT).
"""
import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote

from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Render avtomatik RENDER_EXTERNAL_URL beradi — WEBAPP_URL bo'sh bo'lsa shuni ishlatamiz
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
# Serverda webhook rejimi (uxlab qolgan server Telegram so'rovi bilan uyg'onadi)
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
PORT = int(os.getenv("PORT", "8080"))
QR_TTL = int(os.getenv("QR_TTL", "45"))          # QR kod necha soniya amal qiladi
QR_ROTATE = int(os.getenv("QR_ROTATE", "10"))    # QR necha soniyada yangilanadi
TZ = timezone(timedelta(hours=int(os.getenv("TZ_OFFSET", "5"))))  # Toshkent: UTC+5
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "davomat.db"
WEBAPP_DIR = BASE_DIR / "webapp"

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN .env faylida ko'rsatilmagan")

SECRET = hashlib.sha256(("qr:" + BOT_TOKEN).encode()).digest()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("davomat")

# ---------------------------------------------------------------- DB
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        full_name TEXT,
        username TEXT,
        is_admin INTEGER DEFAULT 0,
        registered INTEGER DEFAULT 0,
        created_at INTEGER
    );
    CREATE TABLE IF NOT EXISTS sessions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        created_by INTEGER,
        created_at INTEGER,
        closed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS attendance(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        ts INTEGER NOT NULL,
        UNIQUE(session_id, user_id)
    );
    """
)
db.commit()


def q(sql, *args):
    return db.execute(sql, args).fetchall()


def q1(sql, *args):
    return db.execute(sql, args).fetchone()


def run(sql, *args):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def upsert_user(uid: int, full_name: str, username: str | None):
    row = q1("SELECT * FROM users WHERE id=?", uid)
    if row is None:
        has_admin = q1("SELECT 1 FROM users WHERE is_admin=1") is not None
        # ADMIN_IDS bo'sh bo'lsa — botga birinchi kirgan odam admin bo'ladi
        is_admin = 1 if (uid in ADMIN_IDS or (not ADMIN_IDS and not has_admin)) else 0
        run(
            "INSERT INTO users(id, full_name, username, is_admin, created_at) VALUES(?,?,?,?,?)",
            uid, full_name, username, is_admin, int(time.time()),
        )
    else:
        if uid in ADMIN_IDS and not row["is_admin"]:
            run("UPDATE users SET is_admin=1 WHERE id=?", uid)
        if not row["registered"]:
            run("UPDATE users SET full_name=? WHERE id=?", full_name, uid)
        run("UPDATE users SET username=? WHERE id=?", username, uid)
    return q1("SELECT * FROM users WHERE id=?", uid)


# ---------------------------------------------------------------- QR token
def make_token(session_id: int, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(SECRET, f"{session_id}:{ts}".encode(), hashlib.sha256).hexdigest()[:12]
    return f"{session_id}-{ts:x}-{sig}"


def parse_code(code: str) -> str:
    """QR ichidan tokenni ajratib oladi (t.me havola yoki to'g'ridan-to'g'ri token)."""
    code = unquote(code.strip())
    for marker in ("start=c_", "startapp=c_", "c_"):
        if marker in code:
            code = code.split(marker, 1)[1]
            break
    return code.split("&")[0].strip()


def check_in(user_id: int, raw_code: str) -> tuple[bool, str, dict | None]:
    token = parse_code(raw_code)
    try:
        sid_s, ts_s, sig = token.split("-")
        sid, ts = int(sid_s), int(ts_s, 16)
    except ValueError:
        return False, "QR kod noto'g'ri", None
    good = hmac.new(SECRET, f"{sid}:{ts}".encode(), hashlib.sha256).hexdigest()[:12]
    if not hmac.compare_digest(good, sig):
        return False, "QR kod soxta", None
    if time.time() - ts > QR_TTL:
        return False, "QR kod eskirgan. Ekrandagi yangi kodni skanerlang", None
    sess = q1("SELECT * FROM sessions WHERE id=?", sid)
    if not sess:
        return False, "Dars topilmadi", None
    if sess["closed"]:
        return False, "Bu dars uchun davomat yopilgan", None
    info = {"session_id": sid, "title": sess["title"]}
    try:
        run("INSERT INTO attendance(session_id, user_id, ts) VALUES(?,?,?)", sid, user_id, int(time.time()))
    except sqlite3.IntegrityError:
        return True, f"Siz «{sess['title']}» darsida allaqachon belgilangansiz", info
    return True, f"«{sess['title']}» — davomat qabul qilindi ✅", info


# ---------------------------------------------------------------- Telegram initData
def validate_init_data(init_data: str) -> dict | None:
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    recv_hash = pairs.pop("hash", None)
    if not recv_hash:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, recv_hash):
        return None
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        return None
    return json.loads(pairs.get("user", "{}"))


# ---------------------------------------------------------------- Bot
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
BOT_USERNAME = ""


def app_keyboard() -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="📲 Davomat ilovasini ochish", web_app=WebAppInfo(url=WEBAPP_URL))]]
    )


def tg_name(u) -> str:
    return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    user = upsert_user(m.from_user.id, tg_name(m.from_user), m.from_user.username)
    payload = command.args or ""
    if payload.startswith("c_"):
        ok, text, _ = check_in(m.from_user.id, payload)
        await m.answer(("✅ " if ok else "⚠️ ") + text, reply_markup=app_keyboard())
        if ok and not user["registered"]:
            await m.answer("Ism-familiyangizni ilovada kiritib qo'ying — hisobotda shunday ko'rinadi.")
        return
    role = "👑 Siz adminsiz: ilovada dars yaratib, QR kodni ekranga chiqarasiz." if user["is_admin"] else (
        "Darsda ekrandagi QR kodni ilova orqali skanerlang — davomat avtomatik belgilanadi."
    )
    text = f"Assalomu alaykum, {user['full_name']}!\n\n{role}"
    if not WEBAPP_URL:
        text += "\n\n⚠️ WEBAPP_URL sozlanmagan — .env faylini to'ldiring."
    await m.answer(text, reply_markup=app_keyboard())


@dp.message(Command("addadmin"))
async def cmd_addadmin(m: Message, command: CommandObject):
    me = q1("SELECT is_admin FROM users WHERE id=?", m.from_user.id)
    if not me or not me["is_admin"]:
        return await m.answer("Bu buyruq faqat adminlar uchun.")
    if not command.args or not command.args.strip().isdigit():
        return await m.answer("Foydalanish: /addadmin 123456789\n(foydalanuvchi avval botga /start bosgan bo'lishi kerak)")
    uid = int(command.args.strip())
    if run("UPDATE users SET is_admin=1 WHERE id=?", uid).rowcount == 0:
        return await m.answer("Bu foydalanuvchi hali botga kirmagan.")
    await m.answer(f"✅ {uid} admin qilindi.")


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.message(F.text)
async def any_text(m: Message):
    await m.answer("Davomat uchun ilovani oching 👇", reply_markup=app_keyboard())


async def send_csv(admin_id: int, session_id: int):
    sess = q1("SELECT * FROM sessions WHERE id=?", session_id)
    rows = q(
        """SELECT u.full_name, u.username, u.id, a.ts FROM attendance a
           JOIN users u ON u.id=a.user_id WHERE a.session_id=? ORDER BY a.ts""",
        session_id,
    )
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["№", "F.I.Sh", "Username", "Telegram ID", "Vaqt"])
    for i, r in enumerate(rows, 1):
        t = datetime.fromtimestamp(r["ts"], TZ).strftime("%d.%m.%Y %H:%M:%S")
        w.writerow([i, r["full_name"], "@" + r["username"] if r["username"] else "", r["id"], t])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — Excel uchun
    day = datetime.fromtimestamp(sess["created_at"], TZ).strftime("%Y-%m-%d")
    await bot.send_document(
        admin_id,
        BufferedInputFile(data, filename=f"davomat_{day}_{session_id}.csv"),
        caption=f"📋 {sess['title']} — {len(rows)} kishi",
    )


# ---------------------------------------------------------------- HTTP API
def fmt_ts(ts: int) -> dict:
    d = datetime.fromtimestamp(ts, TZ)
    return {"ts": ts, "date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M")}


@web.middleware
async def auth_mw(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    tg_user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not tg_user or "id" not in tg_user:
        return web.json_response({"error": "Ilovani Telegram ichidan oching"}, status=401)
    name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x)
    request["user"] = upsert_user(tg_user["id"], name or str(tg_user["id"]), tg_user.get("username"))
    return await handler(request)


def require_admin(request):
    if not request["user"]["is_admin"]:
        raise web.HTTPForbidden(text=json.dumps({"error": "Faqat admin uchun"}), content_type="application/json")


async def api_me(request):
    u = request["user"]
    hist = q(
        """SELECT s.id, s.title, a.ts FROM attendance a JOIN sessions s ON s.id=a.session_id
           WHERE a.user_id=? ORDER BY a.ts DESC LIMIT 60""",
        u["id"],
    )
    attended = q1("SELECT COUNT(*) c FROM attendance WHERE user_id=?", u["id"])["c"]
    since = u["created_at"] - 86400 * 30
    total = q1("SELECT COUNT(*) c FROM sessions WHERE created_at>=?", since)["c"]
    return web.json_response({
        "user": {"id": u["id"], "full_name": u["full_name"], "username": u["username"],
                 "is_admin": bool(u["is_admin"]), "registered": bool(u["registered"])},
        "stats": {"attended": attended, "total": max(total, attended)},
        "history": [{"session_id": r["id"], "title": r["title"], **fmt_ts(r["ts"])} for r in hist],
        "bot_username": BOT_USERNAME,
    })


async def api_register(request):
    body = await request.json()
    name = " ".join(str(body.get("full_name", "")).split())[:80]
    if len(name) < 3:
        return web.json_response({"error": "Ism-familiya juda qisqa"}, status=400)
    run("UPDATE users SET full_name=?, registered=1 WHERE id=?", name, request["user"]["id"])
    return web.json_response({"ok": True, "full_name": name})


async def api_checkin(request):
    body = await request.json()
    ok, text, info = check_in(request["user"]["id"], str(body.get("code", "")))
    return web.json_response({"ok": ok, "message": text, "session": info}, status=200 if ok else 400)


async def api_sessions(request):
    require_admin(request)
    rows = q(
        """SELECT s.*, (SELECT COUNT(*) FROM attendance a WHERE a.session_id=s.id) cnt
           FROM sessions s ORDER BY s.id DESC LIMIT 50"""
    )
    users = q1("SELECT COUNT(*) c FROM users WHERE is_admin=0")["c"]
    return web.json_response({
        "students": users,
        "sessions": [{"id": r["id"], "title": r["title"], "closed": bool(r["closed"]),
                      "count": r["cnt"], **fmt_ts(r["created_at"])} for r in rows],
    })


async def api_create_session(request):
    require_admin(request)
    body = await request.json()
    title = " ".join(str(body.get("title", "")).split())[:80] or datetime.now(TZ).strftime("Dars %d.%m %H:%M")
    cur = run("INSERT INTO sessions(title, created_by, created_at) VALUES(?,?,?)",
              title, request["user"]["id"], int(time.time()))
    return web.json_response({"id": cur.lastrowid, "title": title})


async def api_session(request):
    require_admin(request)
    sid = int(request.match_info["sid"])
    s = q1("SELECT * FROM sessions WHERE id=?", sid)
    if not s:
        raise web.HTTPNotFound()
    rows = q(
        """SELECT u.full_name, u.username, a.ts FROM attendance a JOIN users u ON u.id=a.user_id
           WHERE a.session_id=? ORDER BY a.ts DESC""",
        sid,
    )
    token = make_token(sid)
    return web.json_response({
        "id": sid, "title": s["title"], "closed": bool(s["closed"]), **fmt_ts(s["created_at"]),
        "qr": None if s["closed"] else f"https://t.me/{BOT_USERNAME}?start=c_{token}",
        "rotate": QR_ROTATE,
        "attendees": [{"full_name": r["full_name"], "username": r["username"], **fmt_ts(r["ts"])} for r in rows],
    })


async def api_close(request):
    require_admin(request)
    sid = int(request.match_info["sid"])
    body = await request.json()
    run("UPDATE sessions SET closed=? WHERE id=?", 0 if body.get("reopen") else 1, sid)
    return web.json_response({"ok": True})


async def api_export(request):
    require_admin(request)
    await send_csv(request["user"]["id"], int(request.match_info["sid"]))
    return web.json_response({"ok": True, "message": "CSV fayl botga yuborildi"})


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    app.router.add_get("/", index)
    app.router.add_get("/api/me", api_me)
    app.router.add_post("/api/register", api_register)
    app.router.add_post("/api/checkin", api_checkin)
    app.router.add_get("/api/sessions", api_sessions)
    app.router.add_post("/api/sessions", api_create_session)
    app.router.add_get("/api/sessions/{sid:\\d+}", api_session)
    app.router.add_post("/api/sessions/{sid:\\d+}/close", api_close)
    app.router.add_post("/api/sessions/{sid:\\d+}/export", api_export)
    app.router.add_static("/static", WEBAPP_DIR)
    return app


async def main():
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot: @%s", BOT_USERNAME)

    app = build_app()
    hook_path = "/tg/" + hashlib.sha256(("hook:" + BOT_TOKEN).encode()).hexdigest()[:24]
    hook_secret = hashlib.sha256(("sec:" + BOT_TOKEN).encode()).hexdigest()[:32]
    if USE_WEBHOOK:
        from aiogram.webhook.aiohttp_server import SimpleRequestHandler
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=hook_secret).register(app, path=hook_path)
        app.router.add_get("/health", lambda r: web.Response(text="ok"))

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server: http://localhost:%s  |  WEBAPP_URL=%s", PORT, WEBAPP_URL or "(yo'q)")

    if WEBAPP_URL:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Davomat", web_app=WebAppInfo(url=WEBAPP_URL)))
    try:
        if USE_WEBHOOK and WEBAPP_URL:
            await bot.set_webhook(WEBAPP_URL + hook_path, drop_pending_updates=False, secret_token=hook_secret)
            log.info("Webhook rejimi yoqildi")
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
