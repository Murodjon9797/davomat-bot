"""
O'qituvchilar davomati — Telegram bot + Mini App + Admin panel.

O'qituvchi darsga kirganda va chiqqanda texnikumdagi QR kodni skanerlaydi.
Ilova telefon joylashuvini oladi, server texnikum hududida ekanini tekshiradi.

Ishga tushirish:
    pip install -r requirements.txt
    python bot.py

Sozlamalar .env faylida yoki server muhit o'zgaruvchilarida
(BOT_TOKEN, WEBAPP_URL, ADMIN_IDS, PORT, USE_WEBHOOK, DATA_DIR, TZ_OFFSET).
Texnikum lokatsiyasi admin paneldagi «Sozlamalar» bo'limida kiritiladi.
"""
import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, unquote, quote

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
WEBAPP_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
PORT = int(os.getenv("PORT", "8080"))
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
# DATABASE_URL berilsa — PostgreSQL (Neon va h.k., ma'lumotlar saqlanib qoladi),
# aks holda lokal SQLite fayl (davomat.db).
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
PG = DATABASE_URL.startswith(("postgres://", "postgresql://"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id BIGINT PRIMARY KEY,
    full_name TEXT,
    username TEXT,
    is_admin INTEGER DEFAULT 0,
    registered INTEGER DEFAULT 0,
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS groups(
    id {serial},
    name TEXT NOT NULL,
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS lessons(
    id {serial},
    user_id BIGINT NOT NULL,
    group_id INTEGER,
    subject TEXT,
    start_ts BIGINT NOT NULL,
    end_ts BIGINT,
    start_dist INTEGER,
    end_dist INTEGER,
    start_lat {real}, start_lon {real},
    end_lat {real}, end_lon {real},
    status TEXT DEFAULT 'open',
    manual INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_lessons_user ON lessons(user_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_lessons_start ON lessons(start_ts);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""

if PG:
    import psycopg
    from psycopg.rows import dict_row

    _pg = None

    def _conn():
        global _pg
        if _pg is None or _pg.closed:
            _pg = psycopg.connect(DATABASE_URL, autocommit=True, row_factory=dict_row, connect_timeout=15)
        return _pg

    def _exec(sql, args):
        sql = sql.replace("?", "%s")
        for attempt in (1, 2):
            try:
                return _conn().execute(sql, args)
            except psycopg.OperationalError:
                # Neon bo'sh turganda ulanishni uzadi — qayta ulanamiz
                global _pg
                try:
                    _pg and _pg.close()
                except Exception:
                    pass
                _pg = None
                if attempt == 2:
                    raise

    for stmt in SCHEMA.format(serial="SERIAL PRIMARY KEY", real="DOUBLE PRECISION").split(";"):
        if stmt.strip():
            _exec(stmt, ())
    log.info("Baza: PostgreSQL")
else:
    db = sqlite3.connect(DB_PATH, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.executescript("PRAGMA journal_mode=WAL;" + SCHEMA.format(serial="INTEGER PRIMARY KEY AUTOINCREMENT", real="REAL"))
    db.commit()

    def _exec(sql, args):
        cur = db.execute(sql, args)
        db.commit()
        return cur

    log.info("Baza: SQLite (%s)", DB_PATH)

DEFAULTS = {
    "org_name": "Texnikum",
    "lat": "", "lon": "",          # texnikum markazi
    "radius": "200",               # ruxsat etilgan masofa, metr
    "geo_required": "1",           # lokatsiya tekshiruvi yoqilgan
    "max_hours": "4",              # chiqish belgilanmasa, necha soatdan keyin dars yopiladi
    "qr_version": "1",             # QR ni yangilash (eskisini bekor qilish) uchun
}


def q(sql, *args):
    return _exec(sql, args).fetchall()


def q1(sql, *args):
    return _exec(sql, args).fetchone()


def run(sql, *args):
    return _exec(sql, args)


def insert(sql, *args) -> int:
    """INSERT qiladi va yangi yozuv id sini qaytaradi."""
    if PG:
        return _exec(sql + " RETURNING id", args).fetchone()["id"]
    return _exec(sql, args).lastrowid


def setting(key: str) -> str:
    r = q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else DEFAULTS[key]


def set_setting(key: str, value) -> None:
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))


def geo_conf() -> dict:
    lat, lon = setting("lat"), setting("lon")
    return {
        "lat": float(lat) if lat else None, "lon": float(lon) if lon else None,
        "radius": int(setting("radius")), "required": setting("geo_required") == "1",
        "configured": bool(lat and lon),
    }


def upsert_user(uid: int, full_name: str, username: str | None):
    row = q1("SELECT * FROM users WHERE id=?", uid)
    if row is None:
        has_admin = q1("SELECT 1 FROM users WHERE is_admin=1") is not None
        is_admin = 1 if (uid in ADMIN_IDS or (not ADMIN_IDS and not has_admin)) else 0
        run("INSERT INTO users(id, full_name, username, is_admin, created_at) VALUES(?,?,?,?,?)",
            uid, full_name, username, is_admin, int(time.time()))
    else:
        if uid in ADMIN_IDS and not row["is_admin"]:
            run("UPDATE users SET is_admin=1 WHERE id=?", uid)
        if not row["registered"]:
            run("UPDATE users SET full_name=? WHERE id=?", full_name, uid)
        run("UPDATE users SET username=? WHERE id=?", username, uid)
    return q1("SELECT * FROM users WHERE id=?", uid)


_groups_cache: dict | None = None


def group_name(gid):
    """Guruh nomi (keshdan — har bir dars uchun bazaga alohida so'rov yubormaslik uchun)."""
    global _groups_cache
    if not gid:
        return None
    if _groups_cache is None:
        _groups_cache = {r["id"]: r["name"] for r in q("SELECT id, name FROM groups")}
    return _groups_cache.get(gid)


def groups_changed():
    global _groups_cache
    _groups_cache = None


# ---------------------------------------------------------------- vaqt yordamchilari
def now() -> int:
    return int(time.time())


def day_start(d: datetime | None = None) -> int:
    d = d or datetime.now(TZ)
    return int(d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def parse_day(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())


def fmt_dt(ts: int, f="%d.%m.%Y %H:%M") -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(f)


def fmt_ts(ts: int | None) -> dict | None:
    if not ts:
        return None
    d = datetime.fromtimestamp(ts, TZ)
    return {"ts": ts, "date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M")}


def dur_text(sec: int | None) -> str:
    if sec is None:
        return ""
    m = max(0, sec) // 60
    h, m = divmod(m, 60)
    return f"{h} soat {m} daq" if h else f"{m} daq"


# ---------------------------------------------------------------- QR va lokatsiya
def qr_token() -> str:
    v = setting("qr_version")
    sig = hmac.new(SECRET, f"teacher:{v}".encode(), hashlib.sha256).hexdigest()[:16]
    return f"t_{v}_{sig}"


def qr_link() -> str:
    return f"https://t.me/{BOT_USERNAME}?start={qr_token()}"


def check_code(raw: str) -> bool:
    code = unquote(str(raw or "").strip())
    for marker in ("start=", "startapp=", "code="):
        if marker in code:
            code = code.split(marker, 1)[1]
    code = code.split("&")[0].strip()
    return hmac.compare_digest(code, qr_token())


def distance_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_location(b: dict) -> tuple[bool, str, int | None, float | None, float | None]:
    """(ok, xabar, masofa, lat, lon)"""
    g = geo_conf()
    try:
        lat, lon = float(b.get("lat")), float(b.get("lon"))
        acc = float(b.get("acc") or 0)
    except (TypeError, ValueError):
        lat = lon = None
        acc = 0
    if lat is None:
        if g["required"] and g["configured"]:
            return False, "Joylashuv aniqlanmadi. Telefoningizda GPS ni yoqing va Telegramga ruxsat bering", None, None, None
        return True, "", None, None, None
    if not g["configured"]:
        return True, "", None, lat, lon
    dist = distance_m(lat, lon, g["lat"], g["lon"])
    # GPS xatoligini hisobga olamiz (100 m gacha)
    if g["required"] and dist - min(acc, 100) > g["radius"]:
        return False, f"Siz texnikum hududidan tashqaridasiz ({round(dist)} m uzoqda). Ruxsat: {g['radius']} m", round(dist), lat, lon
    return True, "", round(dist), lat, lon


# ---------------------------------------------------------------- darslar
def lesson_dict(r) -> dict:
    end = r["end_ts"]
    dur = (end or now()) - r["start_ts"] if r["status"] != "forgot" else None
    return {
        "id": r["id"], "user_id": r["user_id"], "group_id": r["group_id"], "group": group_name(r["group_id"]),
        "subject": r["subject"] or "", "status": r["status"], "manual": bool(r["manual"]),
        "start": fmt_ts(r["start_ts"]), "end": fmt_ts(end),
        "duration": dur, "duration_text": dur_text(dur),
        "start_dist": r["start_dist"], "end_dist": r["end_dist"],
    }


def open_lesson(uid: int):
    return q1("SELECT * FROM lessons WHERE user_id=? AND status='open' ORDER BY start_ts DESC LIMIT 1", uid)


async def close_stale():
    """Chiqish belgilanmay qolgan darslarni «unutilgan» deb yopadi va o'qituvchiga xabar beradi."""
    limit = now() - int(float(setting("max_hours")) * 3600)
    rows = q("SELECT * FROM lessons WHERE status='open' AND start_ts<?", limit)
    for r in rows:
        run("UPDATE lessons SET status='forgot' WHERE id=?", r["id"])
        try:
            await bot.send_message(
                r["user_id"],
                f"⚠️ {fmt_dt(r['start_ts'])} da boshlangan darsingiz uchun chiqish belgilanmadi.\n"
                f"Dars «chiqish belgilanmagan» deb yopildi. Kerak bo'lsa, ma'muriyatga murojaat qiling.",
            )
        except Exception:
            pass


def _month_stats(rows) -> dict:
    done = [r for r in rows if r["status"] == "done"]
    secs = sum(r["end_ts"] - r["start_ts"] for r in done)
    return {"lessons": len(rows), "done": len(done), "forgot": sum(r["status"] == "forgot" for r in rows),
            "seconds": secs, "hours_text": dur_text(secs)}


def user_month_stats(uid: int) -> dict:
    m0 = day_start(datetime.now(TZ).replace(day=1))
    return _month_stats(q("SELECT status, start_ts, end_ts FROM lessons WHERE user_id=? AND start_ts>=?", uid, m0))


def all_month_stats() -> dict:
    """Barcha o'qituvchilar uchun oylik statistika — bitta so'rov bilan."""
    m0 = day_start(datetime.now(TZ).replace(day=1))
    by_user: dict[int, list] = {}
    for r in q("SELECT user_id, status, start_ts, end_ts FROM lessons WHERE start_ts>=?", m0):
        by_user.setdefault(r["user_id"], []).append(r)
    return {uid: _month_stats(rows) for uid, rows in by_user.items()}


EMPTY_STATS = _month_stats([])


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


def app_keyboard(code: str | None = None, text="📲 Ilovani ochish") -> InlineKeyboardMarkup | None:
    if not WEBAPP_URL:
        return None
    url = WEBAPP_URL + (f"/?code={quote(code)}" if code else "/")
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url))]])


def tg_name(u) -> str:
    return " ".join(x for x in (u.first_name, u.last_name) if x) or (u.username or str(u.id))


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject):
    user = upsert_user(m.from_user.id, tg_name(m.from_user), m.from_user.username)
    payload = command.args or ""
    if payload.startswith("t_"):
        # telefon kamerasi bilan skanerlangan — joylashuvni tekshirish uchun ilovani ochamiz
        if not check_code(payload):
            return await m.answer("⚠️ Bu QR kod eskirgan. Texnikumdagi yangi QR kodni skanerlang.")
        lesson = open_lesson(m.from_user.id)
        what = "darsdan chiqishni" if lesson else "darsga kirishni"
        return await m.answer(f"📍 {what.capitalize()} tasdiqlash uchun ilovani oching — joylashuvingiz tekshiriladi 👇",
                              reply_markup=app_keyboard(payload, "📍 Tasdiqlash"))
    role = ("👑 Siz adminsiz: ilovadagi admin panelda jurnal, statistika va sozlamalar bor. "
            "Avval «Sozlamalar»da texnikum lokatsiyasini kiriting.") if user["is_admin"] else (
        "Darsga kirganda va darsdan chiqqanda texnikumdagi QR kodni ilova orqali skanerlang.")
    await m.answer(f"Assalomu alaykum, {user['full_name']}!\n\n{role}", reply_markup=app_keyboard())


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.message(F.text)
async def any_text(m: Message):
    await m.answer("Davomat uchun ilovani oching 👇", reply_markup=app_keyboard())


def csv_file(rows: list[list], name: str) -> BufferedInputFile:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(rows)
    return BufferedInputFile(("﻿" + buf.getvalue()).encode("utf-8"), filename=name)


async def broadcast(user_ids: list[int], text: str) -> tuple[int, int]:
    ok = fail = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, reply_markup=app_keyboard())
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


STATUS_TXT = {"open": "Davom etmoqda", "done": "Yakunlangan", "forgot": "Chiqish belgilanmagan"}


async def send_report(admin_id: int, d_from: str, d_to: str, user_id: int | None):
    start, end = parse_day(d_from), parse_day(d_to) + 86400
    sql = "SELECT l.*, u.full_name FROM lessons l JOIN users u ON u.id=l.user_id WHERE l.start_ts>=? AND l.start_ts<?"
    args = [start, end]
    if user_id:
        sql += " AND l.user_id=?"
        args.append(user_id)
    rows = q(sql + " ORDER BY l.start_ts", *args)
    if not rows:
        return 0
    det = [["№", "Sana", "O'qituvchi", "Guruh", "Fan", "Kirdi", "Chiqdi", "Davomiyligi (daq)", "Holat", "Masofa kirish (m)", "Masofa chiqish (m)"]]
    summ: dict[int, dict] = {}
    for i, r in enumerate(rows, 1):
        dur = (r["end_ts"] - r["start_ts"]) // 60 if r["status"] == "done" else ""
        det.append([i, fmt_dt(r["start_ts"], "%d.%m.%Y"), r["full_name"], group_name(r["group_id"]) or "", r["subject"] or "",
                    fmt_dt(r["start_ts"], "%H:%M"), fmt_dt(r["end_ts"], "%H:%M") if r["end_ts"] else "", dur,
                    STATUS_TXT.get(r["status"], r["status"]) + (" (tahrirlangan)" if r["manual"] else ""),
                    r["start_dist"] if r["start_dist"] is not None else "", r["end_dist"] if r["end_dist"] is not None else ""])
        s = summ.setdefault(r["user_id"], {"name": r["full_name"], "n": 0, "done": 0, "forgot": 0, "min": 0, "days": set()})
        s["n"] += 1
        s["days"].add(fmt_dt(r["start_ts"], "%Y-%m-%d"))
        if r["status"] == "done":
            s["done"] += 1
            s["min"] += (r["end_ts"] - r["start_ts"]) // 60
        elif r["status"] == "forgot":
            s["forgot"] += 1
    sm = [["№", "O'qituvchi", "Ish kunlari", "Darslar", "Yakunlangan", "Chiqish belgilanmagan", "Jami vaqt (soat)"]]
    for i, s in enumerate(sorted(summ.values(), key=lambda x: x["name"]), 1):
        sm.append([i, s["name"], len(s["days"]), s["n"], s["done"], s["forgot"], round(s["min"] / 60, 1)])
    title = f"{d_from}_{d_to}"
    await bot.send_document(admin_id, csv_file(sm, f"umumiy_{title}.csv"),
                            caption=f"📊 Umumiy hisobot: {d_from} — {d_to}\n👨‍🏫 O'qituvchilar: {len(summ)}   📚 Darslar: {len(rows)}")
    await bot.send_document(admin_id, csv_file(det, f"darslar_{title}.csv"), caption="📋 Barcha darslar (batafsil)")
    return len(rows)


# ---------------------------------------------------------------- HTTP API
def jerr(msg, status=400):
    return web.json_response({"error": msg}, status=status)


@web.middleware
async def auth_mw(request: web.Request, handler):
    if not request.path.startswith("/api/"):
        return await handler(request)
    tg_user = validate_init_data(request.headers.get("X-Init-Data", ""))
    if not tg_user or "id" not in tg_user:
        return jerr("Ilovani Telegram ichidan oching", 401)
    name = " ".join(x for x in (tg_user.get("first_name"), tg_user.get("last_name")) if x)
    request["user"] = upsert_user(tg_user["id"], name or str(tg_user["id"]), tg_user.get("username"))
    if request.path.startswith("/api/admin/") and not request["user"]["is_admin"]:
        return jerr("Faqat admin uchun", 403)
    await close_stale()
    try:
        return await handler(request)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        log.warning("Bad request %s: %s", request.path, e)
        return jerr("Noto'g'ri so'rov")


async def body(request) -> dict:
    if not request.can_read_body:
        return {}
    data = await request.json()
    return data if isinstance(data, dict) else {}


def clean(s, n=80) -> str:
    return " ".join(str(s or "").split())[:n]


def opt_int(v):
    return int(v) if v not in (None, "", 0, "0") else None


# ---------- o'qituvchi
async def api_me(request):
    u = request["user"]
    t0 = day_start()
    today = q("SELECT * FROM lessons WHERE user_id=? AND start_ts>=? ORDER BY start_ts DESC", u["id"], t0)
    hist = q("SELECT * FROM lessons WHERE user_id=? AND start_ts<? ORDER BY start_ts DESC LIMIT 30", u["id"], t0)
    days = sorted({fmt_dt(r["start_ts"], "%Y-%m-%d") for r in q(
        "SELECT start_ts FROM lessons WHERE user_id=? AND start_ts>=?", u["id"], t0 - 14 * 86400)})
    ol = open_lesson(u["id"])
    last = q1("SELECT group_id, subject FROM lessons WHERE user_id=? ORDER BY start_ts DESC LIMIT 1", u["id"])
    g = geo_conf()
    return web.json_response({
        "user": {"id": u["id"], "full_name": u["full_name"], "username": u["username"],
                 "is_admin": bool(u["is_admin"]), "registered": bool(u["registered"])},
        "org": setting("org_name"),
        "geo": {"configured": g["configured"], "required": g["required"], "radius": g["radius"]},
        "open": lesson_dict(ol) if ol else None,
        "last": {"group_id": last["group_id"], "subject": last["subject"]} if last else None,
        "today": [lesson_dict(r) for r in today],
        "history": [lesson_dict(r) for r in hist],
        "days": days,
        "month": user_month_stats(u["id"]),
        "groups": [{"id": g["id"], "name": g["name"]} for g in q("SELECT * FROM groups ORDER BY name")],
        "server_time": now(),
    })


async def api_register(request):
    b = await body(request)
    name = clean(b.get("full_name"))
    if len(name) < 5 or len(name.split()) < 2:
        return jerr("Familiya va ismingizni to'liq yozing")
    run("UPDATE users SET full_name=?, registered=1 WHERE id=?", name, request["user"]["id"])
    return web.json_response({"ok": True, "full_name": name})


async def api_scan(request):
    """action=start — darsga kirish, action=end — darsdan chiqish."""
    u = request["user"]
    b = await body(request)
    if not u["registered"]:
        return jerr("Avval familiya va ismingizni kiriting")
    if not check_code(b.get("code", "")):
        return jerr("QR kod noto'g'ri yoki eskirgan. Texnikumdagi QR kodni skanerlang")
    ok, msg, dist, lat, lon = check_location(b)
    if not ok:
        return jerr(msg)
    ol = open_lesson(u["id"])
    action = b.get("action")
    if action == "start":
        if ol:
            return jerr("Sizda tugallanmagan dars bor. Avval darsdan chiqishni belgilang")
        gid = opt_int(b.get("group_id"))
        lid = insert("""INSERT INTO lessons(user_id, group_id, subject, start_ts, start_dist, start_lat, start_lon)
                        VALUES(?,?,?,?,?,?,?)""", u["id"], gid, clean(b.get("subject"), 60), now(), dist, lat, lon)
        les = q1("SELECT * FROM lessons WHERE id=?", lid)
        return web.json_response({"ok": True, "message": f"Darsga kirish qayd etildi: {fmt_dt(les['start_ts'], '%H:%M')}",
                                  "lesson": lesson_dict(les)})
    if action == "end":
        if not ol:
            return jerr("Ochiq dars yo'q. Avval darsga kirishni belgilang")
        if now() - ol["start_ts"] < 60:
            return jerr("Dars endigina boshlandi. Chiqishni birozdan keyin belgilang")
        run("UPDATE lessons SET end_ts=?, end_dist=?, end_lat=?, end_lon=?, status='done' WHERE id=?",
            now(), dist, lat, lon, ol["id"])
        les = q1("SELECT * FROM lessons WHERE id=?", ol["id"])
        d = lesson_dict(les)
        return web.json_response({"ok": True, "message": f"Darsdan chiqish qayd etildi. Davomiyligi: {d['duration_text']}",
                                  "lesson": d})
    return jerr("Noto'g'ri amal")


# ---------- admin: statistika
async def api_stats(request):
    t0 = day_start()
    m0 = day_start(datetime.now(TZ).replace(day=1))
    teachers = q("SELECT * FROM users ORDER BY full_name")
    now_in = q("SELECT l.*, u.full_name FROM lessons l JOIN users u ON u.id=l.user_id WHERE l.status='open' ORDER BY l.start_ts")
    today = q("SELECT * FROM lessons WHERE start_ts>=?", t0)
    days = []
    week = q("SELECT user_id, status, start_ts, end_ts FROM lessons WHERE start_ts>=?", t0 - 6 * 86400)
    for i in range(6, -1, -1):
        d0 = t0 - i * 86400
        rows = [r for r in week if d0 <= r["start_ts"] < d0 + 86400]
        days.append({"date": fmt_dt(d0, "%Y-%m-%d"), "lessons": len(rows),
                     "teachers": len({r["user_id"] for r in rows}),
                     "hours": round(sum((r["end_ts"] - r["start_ts"]) for r in rows if r["status"] == "done") / 3600, 1)})
    ranking = []
    ms = all_month_stats()
    for t in teachers:
        s = ms.get(t["id"], EMPTY_STATS)
        if s["lessons"]:
            ranking.append({"id": t["id"], "full_name": t["full_name"], **s})
    ranking.sort(key=lambda x: -x["seconds"])
    absent_today = [{"id": t["id"], "full_name": t["full_name"]} for t in teachers
                    if t["registered"] and not t["is_admin"] and not any(r["user_id"] == t["id"] for r in today)]
    return web.json_response({
        "teachers": len(teachers),
        "unregistered": sum(not t["registered"] for t in teachers),
        "geo": geo_conf(),
        "today": {"lessons": len(today), "teachers": len({r["user_id"] for r in today}),
                  "forgot": sum(r["status"] == "forgot" for r in today),
                  "hours": round(sum((r["end_ts"] - r["start_ts"]) for r in today if r["status"] == "done") / 3600, 1)},
        "month_forgot": q1("SELECT COUNT(*) c FROM lessons WHERE status='forgot' AND start_ts>=?", m0)["c"],
        "now_in": [{**lesson_dict(r), "full_name": r["full_name"]} for r in now_in],
        "days": days,
        "ranking": ranking[:10],
        "absent_today": absent_today[:30],
        "admins": [{"id": a["id"], "full_name": a["full_name"], "username": a["username"]}
                   for a in teachers if a["is_admin"]],
    })


# ---------- admin: jurnal (darslar)
async def api_lessons(request):
    qy = request.query
    d_from = qy.get("from") or fmt_dt(now(), "%Y-%m-%d")
    d_to = qy.get("to") or d_from
    sql = """SELECT l.*, u.full_name FROM lessons l JOIN users u ON u.id=l.user_id
             WHERE l.start_ts>=? AND l.start_ts<?"""
    args = [parse_day(d_from), parse_day(d_to) + 86400]
    if qy.get("user"):
        sql += " AND l.user_id=?"
        args.append(int(qy["user"]))
    if qy.get("group"):
        sql += " AND l.group_id=?"
        args.append(int(qy["group"]))
    if qy.get("status"):
        sql += " AND l.status=?"
        args.append(qy["status"])
    rows = q(sql + " ORDER BY l.start_ts DESC LIMIT 300", *args)
    return web.json_response({"lessons": [{**lesson_dict(r), "full_name": r["full_name"]} for r in rows]})


async def api_lesson_update(request):
    lid = int(request.match_info["lid"])
    les = q1("SELECT * FROM lessons WHERE id=?", lid)
    if not les:
        return jerr("Dars topilmadi", 404)
    b = await body(request)
    if b.get("end_now"):
        if les["status"] == "done":
            return jerr("Dars allaqachon yakunlangan")
        run("UPDATE lessons SET end_ts=?, status='done', manual=1 WHERE id=?", now(), lid)
    if b.get("end_time"):   # "HH:MM" — dars kunidagi chiqish vaqti
        d = datetime.fromtimestamp(les["start_ts"], TZ)
        hh, mm = map(int, str(b["end_time"]).split(":"))
        end = int(d.replace(hour=hh, minute=mm, second=0).timestamp())
        if end <= les["start_ts"]:
            return jerr("Chiqish vaqti kirish vaqtidan keyin bo'lishi kerak")
        run("UPDATE lessons SET end_ts=?, status='done', manual=1 WHERE id=?", end, lid)
    if "subject" in b:
        run("UPDATE lessons SET subject=? WHERE id=?", clean(b["subject"], 60), lid)
    if "group_id" in b:
        run("UPDATE lessons SET group_id=? WHERE id=?", opt_int(b["group_id"]), lid)
    return web.json_response({"ok": True})


async def api_lesson_delete(request):
    run("DELETE FROM lessons WHERE id=?", int(request.match_info["lid"]))
    return web.json_response({"ok": True})


# ---------- admin: o'qituvchilar
async def api_teachers(request):
    qs = (request.query.get("q") or "").strip().lower()
    rows = q("SELECT * FROM users ORDER BY lower(full_name)")
    out = []
    ms = all_month_stats()
    in_lesson = {r["user_id"] for r in q("SELECT user_id FROM lessons WHERE status='open'")}
    for u in rows:
        if qs and qs not in (u["full_name"] or "").lower() and qs not in (u["username"] or "").lower() and qs != str(u["id"]):
            continue
        out.append({"id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
                    "registered": bool(u["registered"]), "in_lesson": u["id"] in in_lesson,
                    "month": ms.get(u["id"], EMPTY_STATS)})
    return web.json_response({"teachers": out})


async def api_teacher(request):
    uid = int(request.match_info["uid"])
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return jerr("Topilmadi", 404)
    hist = q("SELECT * FROM lessons WHERE user_id=? ORDER BY start_ts DESC LIMIT 60", uid)
    return web.json_response({
        "id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
        "registered": bool(u["registered"]), "joined": fmt_ts(u["created_at"]), "month": user_month_stats(uid),
        "history": [lesson_dict(r) for r in hist],
    })


async def api_teacher_save(request):
    uid = int(request.match_info["uid"])
    b = await body(request)
    if not q1("SELECT 1 FROM users WHERE id=?", uid):
        return jerr("Topilmadi", 404)
    if "full_name" in b:
        name = clean(b["full_name"])
        if len(name) < 3:
            return jerr("Ism-familiya juda qisqa")
        run("UPDATE users SET full_name=?, registered=1 WHERE id=?", name, uid)
    if "is_admin" in b:
        if not b["is_admin"] and uid == request["user"]["id"]:
            return jerr("O'zingizni adminlikdan olib bo'lmaydi")
        if not b["is_admin"] and uid in ADMIN_IDS:
            return jerr("Bu admin server sozlamalarida (ADMIN_IDS) belgilangan")
        run("UPDATE users SET is_admin=? WHERE id=?", 1 if b["is_admin"] else 0, uid)
    return web.json_response({"ok": True})


async def api_teacher_delete(request):
    uid = int(request.match_info["uid"])
    if uid == request["user"]["id"]:
        return jerr("O'zingizni o'chira olmaysiz")
    run("DELETE FROM lessons WHERE user_id=?", uid)
    run("DELETE FROM users WHERE id=?", uid)
    return web.json_response({"ok": True})


# ---------- admin: guruhlar
async def api_groups(request):
    rows = q("""SELECT g.*, (SELECT COUNT(*) FROM lessons l WHERE l.group_id=g.id) n FROM groups g ORDER BY g.name""")
    return web.json_response({"groups": [{"id": g["id"], "name": g["name"], "lessons": g["n"]} for g in rows]})


async def api_group_save(request):
    b = await body(request)
    name = clean(b.get("name"), 40)
    if not name:
        return jerr("Guruh nomini kiriting")
    gid = opt_int(b.get("id"))
    if any(r["id"] != gid for r in q("SELECT id FROM groups WHERE lower(name)=lower(?)", name)):
        return jerr("Bunday guruh bor")
    if gid:
        run("UPDATE groups SET name=? WHERE id=?", name, gid)
    else:
        gid = insert("INSERT INTO groups(name, created_at) VALUES(?,?)", name, now())
    groups_changed()
    return web.json_response({"ok": True, "id": gid, "name": name})


async def api_group_delete(request):
    gid = int(request.match_info["gid"])
    run("UPDATE lessons SET group_id=NULL WHERE group_id=?", gid)
    run("DELETE FROM groups WHERE id=?", gid)
    groups_changed()
    return web.json_response({"ok": True})


# ---------- admin: sozlamalar va QR
async def api_settings(request):
    g = geo_conf()
    return web.json_response({**g, "org_name": setting("org_name"), "max_hours": float(setting("max_hours")),
                              "qr": qr_link(), "qr_version": int(setting("qr_version"))})


async def api_settings_save(request):
    b = await body(request)
    if "lat" in b or "lon" in b:
        if b.get("lat") in (None, "") and b.get("lon") in (None, ""):
            set_setting("lat", "")
            set_setting("lon", "")
        else:
            lat, lon = float(b["lat"]), float(b["lon"])
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                return jerr("Koordinatalar noto'g'ri")
            set_setting("lat", round(lat, 7))
            set_setting("lon", round(lon, 7))
    if "radius" in b:
        r = int(b["radius"])
        if not 20 <= r <= 5000:
            return jerr("Radius 20 dan 5000 metrgacha bo'lishi kerak")
        set_setting("radius", r)
    if "geo_required" in b:
        set_setting("geo_required", "1" if b["geo_required"] else "0")
    if "max_hours" in b:
        h = float(b["max_hours"])
        if not 1 <= h <= 12:
            return jerr("1 dan 12 soatgacha bo'lishi kerak")
        set_setting("max_hours", h)
    if "org_name" in b and clean(b["org_name"], 60):
        set_setting("org_name", clean(b["org_name"], 60))
    return await api_settings(request)


async def api_qr_rotate(request):
    set_setting("qr_version", int(setting("qr_version")) + 1)
    return await api_settings(request)


async def api_qr_send(request):
    b = await body(request)
    data = str(b.get("image", ""))
    if not data.startswith("data:image/png;base64,"):
        return jerr("Rasm noto'g'ri")
    png = base64.b64decode(data.split(",", 1)[1])
    if len(png) > 4_000_000:
        return jerr("Rasm juda katta")
    await bot.send_document(request["user"]["id"], BufferedInputFile(png, filename="davomat_qr.png"),
                            caption="🖨 Chop etib, o'qituvchilar xonasi yoki kirish joyiga osib qo'ying.\n"
                                    "O'qituvchilar darsga kirganda va chiqqanda shu kodni skanerlaydi.")
    return web.json_response({"ok": True, "message": "QR rasm botga yuborildi 📩"})


# ---------- admin: hisobot va xabar
async def api_report(request):
    b = await body(request)
    d_from, d_to = sorted([str(b["from"]), str(b["to"])])
    parse_day(d_from), parse_day(d_to)
    n = await send_report(request["user"]["id"], d_from, d_to, opt_int(b.get("user_id")))
    if not n:
        return jerr("Bu oraliqda darslar yo'q")
    return web.json_response({"ok": True, "message": f"Hisobot ({n} ta dars) botga yuborildi 📩"})


async def api_broadcast(request):
    b = await body(request)
    text = str(b.get("text", "")).strip()[:3500]
    if len(text) < 2:
        return jerr("Xabar matnini yozing")
    target = b.get("target") or "all"
    if target == "all":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE id<>?", request["user"]["id"])]
    elif target == "unregistered":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE registered=0")]
    elif target == "absent_today":
        t0 = day_start()
        ids = [r["id"] for r in q("SELECT id FROM users WHERE is_admin=0 AND id NOT IN (SELECT user_id FROM lessons WHERE start_ts>=?)", t0)]
    else:
        return jerr("Noto'g'ri qabul qiluvchi")
    if not ids:
        return jerr("Qabul qiluvchilar yo'q")
    ok, fail = await broadcast(ids, "📢 " + text)
    return web.json_response({"ok": True, "message": f"{ok} kishiga yuborildi" + (f", {fail} tasiga yetmadi" if fail else "")})


async def api_screen(request):
    """Ekran rejimi (Telegram Desktop, katta monitor) uchun: QR, hozir darsdagilar, bugungi so'nggi harakatlar."""
    t0 = day_start()
    now_in = q("SELECT l.*, u.full_name FROM lessons l JOIN users u ON u.id=l.user_id WHERE l.status='open' ORDER BY l.start_ts DESC")
    rows = q("SELECT l.*, u.full_name FROM lessons l JOIN users u ON u.id=l.user_id WHERE l.start_ts>=? OR l.end_ts>=?", t0, t0)
    events = []
    for r in rows:
        if r["start_ts"] >= t0:
            events.append({"type": "in", "ts": r["start_ts"], "full_name": r["full_name"], "title": " · ".join(
                x for x in (group_name(r["group_id"]), r["subject"]) if x)})
        if r["end_ts"] and r["end_ts"] >= t0 and r["status"] == "done":
            events.append({"type": "out", "ts": r["end_ts"], "full_name": r["full_name"], "title": dur_text(r["end_ts"] - r["start_ts"])})
    events.sort(key=lambda e: -e["ts"])
    for e in events:
        e["time"] = fmt_dt(e["ts"], "%H:%M")
    return web.json_response({
        "org": setting("org_name"), "qr": qr_link(), "server_time": now(), "geo": geo_conf()["configured"],
        "now_in": [{**lesson_dict(r), "full_name": r["full_name"]} for r in now_in],
        "today": {"lessons": len([r for r in rows if r["start_ts"] >= t0]), "teachers": len({r["user_id"] for r in rows if r["start_ts"] >= t0})},
        "events": events[:12],
    })


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=6 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", lambda req: web.Response(text="ok"))
    r.add_get("/api/me", api_me)
    r.add_post("/api/register", api_register)
    r.add_get("/api/admin/screen", api_screen)
    r.add_post("/api/scan", api_scan)
    r.add_get("/api/admin/stats", api_stats)
    r.add_get("/api/admin/lessons", api_lessons)
    r.add_post("/api/admin/lessons/{lid:\\d+}", api_lesson_update)
    r.add_post("/api/admin/lessons/{lid:\\d+}/delete", api_lesson_delete)
    r.add_get("/api/admin/teachers", api_teachers)
    r.add_get("/api/admin/teachers/{uid:\\d+}", api_teacher)
    r.add_post("/api/admin/teachers/{uid:\\d+}", api_teacher_save)
    r.add_post("/api/admin/teachers/{uid:\\d+}/delete", api_teacher_delete)
    r.add_get("/api/admin/groups", api_groups)
    r.add_post("/api/admin/groups", api_group_save)
    r.add_post("/api/admin/groups/{gid:\\d+}/delete", api_group_delete)
    r.add_get("/api/admin/settings", api_settings)
    r.add_post("/api/admin/settings", api_settings_save)
    r.add_post("/api/admin/qr/rotate", api_qr_rotate)
    r.add_post("/api/admin/qr/send", api_qr_send)
    r.add_post("/api/admin/report", api_report)
    r.add_post("/api/admin/broadcast", api_broadcast)
    r.add_static("/static", WEBAPP_DIR)
    return app


async def stale_loop():
    while True:
        try:
            await close_stale()
        except Exception as e:
            log.warning("close_stale: %s", e)
        await asyncio.sleep(600)


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

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Web server: http://localhost:%s  |  WEBAPP_URL=%s", PORT, WEBAPP_URL or "(yo'q)")
    asyncio.create_task(stale_loop())

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
