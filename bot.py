"""
Davomat bot — QR kod orqali davomat qiluvchi Telegram bot + Mini App + Admin panel.

Ishga tushirish:
    pip install -r requirements.txt
    python bot.py

Sozlamalar .env faylida yoki server muhit o'zgaruvchilarida
(BOT_TOKEN, WEBAPP_URL, ADMIN_IDS, PORT, USE_WEBHOOK, DATA_DIR).
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
    CREATE TABLE IF NOT EXISTS groups(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
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
        manual INTEGER DEFAULT 0,
        UNIQUE(session_id, user_id)
    );
    """
)
# eski bazalar uchun yangi ustunlar
for table, col in (("users", "group_id INTEGER"), ("sessions", "group_id INTEGER"), ("attendance", "manual INTEGER DEFAULT 0")):
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass
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


def group_name(gid):
    if not gid:
        return None
    r = q1("SELECT name FROM groups WHERE id=?", gid)
    return r["name"] if r else None


def expected_students(session) -> list[sqlite3.Row]:
    """Darsga kelishi kerak bo'lgan talabalar (dars guruhi bo'yicha, admin emas)."""
    if session["group_id"]:
        return q("SELECT * FROM users WHERE is_admin=0 AND group_id=? ORDER BY full_name", session["group_id"])
    return q("SELECT * FROM users WHERE is_admin=0 ORDER BY full_name")


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
    if sess["group_id"]:
        u = q1("SELECT group_id, is_admin FROM users WHERE id=?", user_id)
        if u and not u["is_admin"] and u["group_id"] != sess["group_id"]:
            return False, f"Bu dars «{group_name(sess['group_id'])}» guruhi uchun. Profilingizda guruhni tekshiring", None
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
            await m.answer("Ism-familiyangiz va guruhingizni ilovada kiritib qo'ying — hisobotda shunday ko'rinadi.")
        return
    role = "👑 Siz adminsiz: ilovadagi admin panelda dars yaratib, QR kodni ekranga chiqarasiz." if user["is_admin"] else (
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


def fmt_dt(ts: int, f="%d.%m.%Y %H:%M") -> str:
    return datetime.fromtimestamp(ts, TZ).strftime(f)


def csv_file(rows: list[list], name: str) -> BufferedInputFile:
    buf = io.StringIO()
    csv.writer(buf, delimiter=";").writerows(rows)
    return BufferedInputFile(("﻿" + buf.getvalue()).encode("utf-8"), filename=name)  # BOM — Excel uchun


async def send_session_csv(admin_id: int, session_id: int):
    sess = q1("SELECT * FROM sessions WHERE id=?", session_id)
    present = {r["user_id"]: r for r in q("SELECT * FROM attendance WHERE session_id=?", session_id)}
    expected = expected_students(sess)
    rows = [["№", "F.I.Sh", "Guruh", "Username", "Telegram ID", "Holat", "Vaqt"]]
    seen = set()
    people = list(expected) + [u for u in q("SELECT u.* FROM attendance a JOIN users u ON u.id=a.user_id WHERE a.session_id=?", session_id)]
    for u in people:
        if u["id"] in seen:
            continue
        seen.add(u["id"])
        a = present.get(u["id"])
        state = ("Keldi (qo'lda)" if a["manual"] else "Keldi") if a else "Kelmadi"
        rows.append([len(rows), u["full_name"], group_name(u["group_id"]) or "", "@" + u["username"] if u["username"] else "",
                     u["id"], state, fmt_dt(a["ts"], "%H:%M:%S") if a else ""])
    came = len(present)
    await bot.send_document(
        admin_id,
        csv_file(rows, f"davomat_{fmt_dt(sess['created_at'], '%Y-%m-%d')}_{session_id}.csv"),
        caption=f"📋 {sess['title']} ({fmt_dt(sess['created_at'])})\n✅ Keldi: {came}   ❌ Kelmadi: {len(seen) - came}",
    )


async def send_range_report(admin_id: int, group_id: int | None, d_from: str, d_to: str):
    start = int(datetime.strptime(d_from, "%Y-%m-%d").replace(tzinfo=TZ).timestamp())
    end = int(datetime.strptime(d_to, "%Y-%m-%d").replace(tzinfo=TZ).timestamp()) + 86400
    if group_id:
        sessions = q("SELECT * FROM sessions WHERE created_at>=? AND created_at<? AND (group_id=? OR group_id IS NULL) ORDER BY created_at",
                     start, end, group_id)
        students = q("SELECT * FROM users WHERE is_admin=0 AND group_id=? ORDER BY full_name", group_id)
    else:
        sessions = q("SELECT * FROM sessions WHERE created_at>=? AND created_at<? ORDER BY created_at", start, end)
        students = q("SELECT * FROM users WHERE is_admin=0 ORDER BY full_name")
    if not sessions:
        return 0
    marks = {(r["session_id"], r["user_id"]) for r in q(
        f"SELECT session_id, user_id FROM attendance WHERE session_id IN ({','.join('?' * len(sessions))})",
        *[s["id"] for s in sessions])}
    header = ["№", "F.I.Sh", "Guruh"] + [f"{fmt_dt(s['created_at'], '%d.%m %H:%M')} {s['title']}" for s in sessions] + ["Keldi", "Kelmadi", "%"]
    rows = [header]
    for i, u in enumerate(students, 1):
        cells, came, total = [], 0, 0
        for s in sessions:
            if s["group_id"] and s["group_id"] != u["group_id"]:
                cells.append("—")
                continue
            total += 1
            if (s["id"], u["id"]) in marks:
                came += 1
                cells.append("+")
            else:
                cells.append("-")
        rows.append([i, u["full_name"], group_name(u["group_id"]) or ""] + cells +
                    [came, total - came, round(came / total * 100) if total else ""])
    gname = group_name(group_id) or "Barcha"
    await bot.send_document(
        admin_id,
        csv_file(rows, f"hisobot_{gname}_{d_from}_{d_to}.csv".replace(" ", "_")),
        caption=f"📊 Hisobot: {gname}\n🗓 {d_from} — {d_to}\n📚 Darslar: {len(sessions)}   👥 Talabalar: {len(students)}\n(+ keldi, - kelmadi, — boshqa guruh darsi)",
    )
    return len(sessions)


async def broadcast(user_ids: list[int], text: str) -> tuple[int, int]:
    ok = fail = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text, reply_markup=app_keyboard())
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # Telegram limitlari uchun
    return ok, fail


# ---------------------------------------------------------------- HTTP API
def fmt_ts(ts: int) -> dict:
    d = datetime.fromtimestamp(ts, TZ)
    return {"ts": ts, "date": d.strftime("%Y-%m-%d"), "time": d.strftime("%H:%M")}


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


# ---------- talaba
def user_stats(u) -> dict:
    attended = q1("SELECT COUNT(*) c FROM attendance WHERE user_id=?", u["id"])["c"]
    total = q1(
        "SELECT COUNT(*) c FROM sessions WHERE created_at>=? AND (group_id IS NULL OR group_id=?)",
        u["created_at"] - 3600, u["group_id"] or -1,
    )["c"]
    total = max(total, attended)
    return {"attended": attended, "total": total, "missed": total - attended,
            "rate": round(attended / total * 100) if total else 0}


async def api_me(request):
    u = request["user"]
    hist = q(
        """SELECT s.id, s.title, a.ts FROM attendance a JOIN sessions s ON s.id=a.session_id
           WHERE a.user_id=? ORDER BY a.ts DESC LIMIT 60""",
        u["id"],
    )
    return web.json_response({
        "user": {"id": u["id"], "full_name": u["full_name"], "username": u["username"],
                 "is_admin": bool(u["is_admin"]), "registered": bool(u["registered"]),
                 "group_id": u["group_id"], "group": group_name(u["group_id"])},
        "stats": user_stats(u),
        "history": [{"session_id": r["id"], "title": r["title"], **fmt_ts(r["ts"])} for r in hist],
        "groups": [{"id": g["id"], "name": g["name"]} for g in q("SELECT * FROM groups ORDER BY name")],
        "bot_username": BOT_USERNAME,
    })


async def api_register(request):
    b = await body(request)
    name = clean(b.get("full_name"))
    if len(name) < 3:
        return jerr("Ism-familiya juda qisqa")
    gid = opt_int(b.get("group_id"))
    if gid and not q1("SELECT 1 FROM groups WHERE id=?", gid):
        return jerr("Guruh topilmadi")
    if q1("SELECT 1 FROM groups") and not gid and not request["user"]["is_admin"]:
        return jerr("Guruhingizni tanlang")
    run("UPDATE users SET full_name=?, group_id=?, registered=1 WHERE id=?", name, gid, request["user"]["id"])
    return web.json_response({"ok": True, "full_name": name, "group_id": gid, "group": group_name(gid)})


async def api_checkin(request):
    b = await body(request)
    ok, text, info = check_in(request["user"]["id"], str(b.get("code", "")))
    return web.json_response({"ok": ok, "message": text, "session": info}, status=200 if ok else 400)


# ---------- admin: statistika
async def api_stats(request):
    now = datetime.now(TZ)
    today0 = int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    students = q1("SELECT COUNT(*) c FROM users WHERE is_admin=0")["c"]
    days = []
    for i in range(6, -1, -1):
        d0 = today0 - i * 86400
        ss = q("SELECT * FROM sessions WHERE created_at>=? AND created_at<?", d0, d0 + 86400)
        came = q1("SELECT COUNT(*) c FROM attendance a JOIN sessions s ON s.id=a.session_id WHERE s.created_at>=? AND s.created_at<?",
                  d0, d0 + 86400)["c"]
        expected = sum(len(expected_students(s)) for s in ss)
        days.append({"date": datetime.fromtimestamp(d0, TZ).strftime("%Y-%m-%d"), "sessions": len(ss),
                     "came": came, "expected": expected,
                     "rate": round(came / expected * 100) if expected else None})
    groups = []
    for g in q("SELECT * FROM groups ORDER BY name"):
        members = q1("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND group_id=?", g["id"])["c"]
        ss = q("SELECT id FROM sessions WHERE group_id=? OR group_id IS NULL", g["id"])
        came = q1(f"""SELECT COUNT(*) c FROM attendance a JOIN users u ON u.id=a.user_id
                      WHERE u.group_id=? AND a.session_id IN (SELECT id FROM sessions WHERE group_id=? OR group_id IS NULL)""",
                  g["id"], g["id"])["c"]
        exp = members * len(ss)
        groups.append({"id": g["id"], "name": g["name"], "members": members, "sessions": len(ss),
                       "rate": round(came / exp * 100) if exp else None})
    # eng ko'p qoldirganlar
    absentees = []
    for u in q("SELECT * FROM users WHERE is_admin=0"):
        s = user_stats(u)
        if s["missed"]:
            absentees.append({"id": u["id"], "full_name": u["full_name"], "group": group_name(u["group_id"]), **s})
    absentees.sort(key=lambda x: (-x["missed"], x["rate"]))
    today = days[-1]
    return web.json_response({
        "students": students,
        "unregistered": q1("SELECT COUNT(*) c FROM users WHERE is_admin=0 AND registered=0")["c"],
        "groups_count": len(groups),
        "sessions_total": q1("SELECT COUNT(*) c FROM sessions")["c"],
        "today": today,
        "days": days,
        "groups": groups,
        "absentees": absentees[:7],
        "admins": [{"id": a["id"], "full_name": a["full_name"], "username": a["username"]}
                   for a in q("SELECT * FROM users WHERE is_admin=1 ORDER BY full_name")],
    })


# ---------- admin: guruhlar
async def api_groups(request):
    rows = q("""SELECT g.*, (SELECT COUNT(*) FROM users u WHERE u.group_id=g.id AND u.is_admin=0) members
                FROM groups g ORDER BY g.name""")
    return web.json_response({"groups": [{"id": g["id"], "name": g["name"], "members": g["members"]} for g in rows]})


async def api_group_save(request):
    b = await body(request)
    name = clean(b.get("name"), 40)
    if len(name) < 1:
        return jerr("Guruh nomini kiriting")
    gid = opt_int(b.get("id"))
    if q1("SELECT 1 FROM groups WHERE lower(name)=lower(?) AND id IS NOT ?", name, gid):
        return jerr("Bunday guruh bor")
    if gid:
        run("UPDATE groups SET name=? WHERE id=?", name, gid)
    else:
        gid = run("INSERT INTO groups(name, created_at) VALUES(?,?)", name, int(time.time())).lastrowid
    return web.json_response({"ok": True, "id": gid, "name": name})


async def api_group_delete(request):
    gid = int(request.match_info["gid"])
    run("UPDATE users SET group_id=NULL WHERE group_id=?", gid)
    run("UPDATE sessions SET group_id=NULL WHERE group_id=?", gid)
    run("DELETE FROM groups WHERE id=?", gid)
    return web.json_response({"ok": True})


# ---------- admin: talabalar
async def api_students(request):
    gid = request.query.get("group")
    qs = (request.query.get("q") or "").strip().lower()
    sql, args = "SELECT * FROM users WHERE 1=1", []
    if gid == "none":
        sql += " AND group_id IS NULL AND is_admin=0"
    elif gid == "admins":
        sql += " AND is_admin=1"
    elif gid:
        sql += " AND group_id=?"
        args.append(int(gid))
    rows = q(sql + " ORDER BY is_admin DESC, full_name COLLATE NOCASE", *args)
    out = []
    for u in rows:
        if qs and qs not in (u["full_name"] or "").lower() and qs not in (u["username"] or "").lower() and qs != str(u["id"]):
            continue
        out.append({"id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
                    "registered": bool(u["registered"]), "group_id": u["group_id"], "group": group_name(u["group_id"]),
                    **user_stats(u)})
    return web.json_response({"students": out})


async def api_student(request):
    uid = int(request.match_info["uid"])
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return jerr("Topilmadi", 404)
    sess = q("""SELECT s.*, a.ts ats, a.manual FROM sessions s
                LEFT JOIN attendance a ON a.session_id=s.id AND a.user_id=?
                WHERE s.created_at>=? AND (s.group_id IS NULL OR s.group_id=? OR a.ts IS NOT NULL)
                ORDER BY s.created_at DESC LIMIT 80""", uid, u["created_at"] - 3600, u["group_id"] or -1)
    return web.json_response({
        "id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
        "registered": bool(u["registered"]), "group_id": u["group_id"], "group": group_name(u["group_id"]),
        "joined": fmt_ts(u["created_at"]), **user_stats(u),
        "history": [{"session_id": s["id"], "title": s["title"], **fmt_ts(s["created_at"]),
                     "present": s["ats"] is not None, "manual": bool(s["manual"]),
                     "at": fmt_ts(s["ats"])["time"] if s["ats"] else None} for s in sess],
    })


async def api_student_save(request):
    uid = int(request.match_info["uid"])
    b = await body(request)
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return jerr("Topilmadi", 404)
    if "full_name" in b:
        name = clean(b["full_name"])
        if len(name) < 3:
            return jerr("Ism-familiya juda qisqa")
        run("UPDATE users SET full_name=?, registered=1 WHERE id=?", name, uid)
    if "group_id" in b:
        run("UPDATE users SET group_id=? WHERE id=?", opt_int(b["group_id"]), uid)
    if "is_admin" in b:
        if not b["is_admin"] and uid == request["user"]["id"]:
            return jerr("O'zingizni adminlikdan olib bo'lmaydi")
        if not b["is_admin"] and uid in ADMIN_IDS:
            return jerr("Bu admin server sozlamalarida (ADMIN_IDS) belgilangan")
        run("UPDATE users SET is_admin=? WHERE id=?", 1 if b["is_admin"] else 0, uid)
    return web.json_response({"ok": True})


async def api_student_delete(request):
    uid = int(request.match_info["uid"])
    if uid == request["user"]["id"]:
        return jerr("O'zingizni o'chira olmaysiz")
    run("DELETE FROM attendance WHERE user_id=?", uid)
    run("DELETE FROM users WHERE id=?", uid)
    return web.json_response({"ok": True})


# ---------- admin: darslar
async def api_sessions(request):
    gid = request.query.get("group")
    sql, args = "SELECT * FROM sessions", []
    if gid:
        sql += " WHERE group_id=?"
        args.append(int(gid))
    rows = q(sql + " ORDER BY id DESC LIMIT 100", *args)
    out = []
    for s in rows:
        cnt = q1("SELECT COUNT(*) c FROM attendance WHERE session_id=?", s["id"])["c"]
        out.append({"id": s["id"], "title": s["title"], "closed": bool(s["closed"]), "count": cnt,
                    "expected": len(expected_students(s)), "group_id": s["group_id"], "group": group_name(s["group_id"]),
                    **fmt_ts(s["created_at"])})
    return web.json_response({"sessions": out})


async def api_create_session(request):
    b = await body(request)
    gid = opt_int(b.get("group_id"))
    title = clean(b.get("title")) or (group_name(gid) or "Dars") + datetime.now(TZ).strftime(" %d.%m %H:%M")
    cur = run("INSERT INTO sessions(title, created_by, created_at, group_id) VALUES(?,?,?,?)",
              title, request["user"]["id"], int(time.time()), gid)
    return web.json_response({"id": cur.lastrowid, "title": title})


async def api_session(request):
    sid = int(request.match_info["sid"])
    s = q1("SELECT * FROM sessions WHERE id=?", sid)
    if not s:
        return jerr("Dars topilmadi", 404)
    rows = q(
        """SELECT u.id, u.full_name, u.username, u.group_id, a.ts, a.manual FROM attendance a JOIN users u ON u.id=a.user_id
           WHERE a.session_id=? ORDER BY a.ts DESC""",
        sid,
    )
    here = {r["id"] for r in rows}
    absent = [u for u in expected_students(s) if u["id"] not in here]
    return web.json_response({
        "id": sid, "title": s["title"], "closed": bool(s["closed"]), **fmt_ts(s["created_at"]),
        "group_id": s["group_id"], "group": group_name(s["group_id"]),
        "qr": None if s["closed"] else f"https://t.me/{BOT_USERNAME}?start=c_{make_token(sid)}",
        "rotate": QR_ROTATE,
        "attendees": [{"id": r["id"], "full_name": r["full_name"], "username": r["username"],
                       "group": group_name(r["group_id"]), "manual": bool(r["manual"]), **fmt_ts(r["ts"])} for r in rows],
        "absent": [{"id": u["id"], "full_name": u["full_name"], "username": u["username"],
                    "group": group_name(u["group_id"])} for u in absent],
    })


async def api_session_update(request):
    sid = int(request.match_info["sid"])
    b = await body(request)
    if "closed" in b:
        run("UPDATE sessions SET closed=? WHERE id=?", 1 if b["closed"] else 0, sid)
    if "title" in b and clean(b["title"]):
        run("UPDATE sessions SET title=? WHERE id=?", clean(b["title"]), sid)
    return web.json_response({"ok": True})


async def api_session_mark(request):
    sid = int(request.match_info["sid"])
    b = await body(request)
    uid = int(b["user_id"])
    if not q1("SELECT 1 FROM sessions WHERE id=?", sid) or not q1("SELECT 1 FROM users WHERE id=?", uid):
        return jerr("Topilmadi", 404)
    if b.get("present"):
        run("INSERT OR IGNORE INTO attendance(session_id, user_id, ts, manual) VALUES(?,?,?,1)", sid, uid, int(time.time()))
    else:
        run("DELETE FROM attendance WHERE session_id=? AND user_id=?", sid, uid)
    return web.json_response({"ok": True})


async def api_session_delete(request):
    sid = int(request.match_info["sid"])
    run("DELETE FROM attendance WHERE session_id=?", sid)
    run("DELETE FROM sessions WHERE id=?", sid)
    return web.json_response({"ok": True})


async def api_export(request):
    await send_session_csv(request["user"]["id"], int(request.match_info["sid"]))
    return web.json_response({"ok": True, "message": "CSV fayl botga yuborildi 📩"})


async def api_notify_absent(request):
    sid = int(request.match_info["sid"])
    s = q1("SELECT * FROM sessions WHERE id=?", sid)
    here = {r["user_id"] for r in q("SELECT user_id FROM attendance WHERE session_id=?", sid)}
    ids = [u["id"] for u in expected_students(s) if u["id"] not in here]
    if not ids:
        return web.json_response({"ok": True, "message": "Kelmaganlar yo'q 🎉"})
    text = f"⚠️ Siz bugungi «{s['title']}» darsida davomatdan o'tmadingiz ({fmt_dt(s['created_at'])}).\nSababini o'qituvchiga bildiring."
    ok, fail = await broadcast(ids, text)
    return web.json_response({"ok": True, "message": f"{ok} ta talabaga xabar yuborildi" + (f", {fail} tasiga yetmadi" if fail else "")})


# ---------- admin: hisobot va xabar
async def api_report(request):
    b = await body(request)
    d_from, d_to = str(b["from"]), str(b["to"])
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    n = await send_range_report(request["user"]["id"], opt_int(b.get("group_id")), d_from, d_to)
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
        ids = [r["id"] for r in q("SELECT id FROM users WHERE is_admin=0")]
    elif target == "unregistered":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE is_admin=0 AND registered=0")]
    else:
        ids = [r["id"] for r in q("SELECT id FROM users WHERE is_admin=0 AND group_id=?", int(target))]
    if not ids:
        return jerr("Qabul qiluvchilar yo'q")
    ok, fail = await broadcast(ids, "📢 " + text)
    return web.json_response({"ok": True, "message": f"{ok} kishiga yuborildi" + (f", {fail} tasiga yetmadi" if fail else "")})


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw])
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", lambda req: web.Response(text="ok"))
    r.add_get("/api/me", api_me)
    r.add_post("/api/register", api_register)
    r.add_post("/api/checkin", api_checkin)
    # admin
    r.add_get("/api/admin/stats", api_stats)
    r.add_get("/api/admin/groups", api_groups)
    r.add_post("/api/admin/groups", api_group_save)
    r.add_post("/api/admin/groups/{gid:\\d+}/delete", api_group_delete)
    r.add_get("/api/admin/students", api_students)
    r.add_get("/api/admin/students/{uid:\\d+}", api_student)
    r.add_post("/api/admin/students/{uid:\\d+}", api_student_save)
    r.add_post("/api/admin/students/{uid:\\d+}/delete", api_student_delete)
    r.add_get("/api/admin/sessions", api_sessions)
    r.add_post("/api/admin/sessions", api_create_session)
    r.add_get("/api/admin/sessions/{sid:\\d+}", api_session)
    r.add_post("/api/admin/sessions/{sid:\\d+}", api_session_update)
    r.add_post("/api/admin/sessions/{sid:\\d+}/mark", api_session_mark)
    r.add_post("/api/admin/sessions/{sid:\\d+}/delete", api_session_delete)
    r.add_post("/api/admin/sessions/{sid:\\d+}/export", api_export)
    r.add_post("/api/admin/sessions/{sid:\\d+}/notify", api_notify_absent)
    r.add_post("/api/admin/report", api_report)
    r.add_post("/api/admin/broadcast", api_broadcast)
    r.add_static("/static", WEBAPP_DIR)
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
