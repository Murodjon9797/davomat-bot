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
CREATE TABLE IF NOT EXISTS employees(
    id {serial},
    full_name TEXT NOT NULL,
    position TEXT,
    user_id BIGINT,
    sort INTEGER DEFAULT 0,
    created_at BIGINT
);
CREATE TABLE IF NOT EXISTS invites(
    id {serial},
    code TEXT UNIQUE NOT NULL,
    role TEXT DEFAULT 'teacher',
    max_uses INTEGER DEFAULT 1,
    used INTEGER DEFAULT 0,
    expires BIGINT,
    note TEXT,
    created_by BIGINT,
    created_at BIGINT
);
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
    # ish rejimi: work — hodimlar ishga kelib-ketishi, lesson — darsga kirib-chiqish
    "mode": "work",
    "work_start": "08:00", "grace": "10", "lunch_start": "12:30", "lunch_end": "14:00", "work_end": "15:00",
    "workdays": "1,2,3,4,5,6",     # 1=dushanba ... 7=yakshanba
    "work_from": "2026-11-18",     # hisob shu kundan boshlanadi (oldingi kunlar sinov — hisobotga kirmaydi)
}


import re as _re
_LESSONS_RE = _re.compile(r"FROM lessons\b(\s+l\b)?")
_MODE_CACHE = {"v": None, "t": 0.0}


def KIND() -> str:
    """Joriy rejim yozuvlari turi: 'work' yoki 'lesson'."""
    if time.time() - _MODE_CACHE["t"] > 5:
        r = _exec("SELECT value FROM settings WHERE key='mode'", ()).fetchone()
        _MODE_CACHE["v"] = (r["value"] if r else "work")
        _MODE_CACHE["t"] = time.time()
    return "work" if _MODE_CACHE["v"] == "work" else "lesson"


def _scope(sql: str) -> str:
    if "FROM lessons" not in sql:
        return sql
    k = KIND()
    return _LESSONS_RE.sub(lambda m: f"FROM (SELECT * FROM lessons WHERE kind='{k}') " + ("l" if m.group(1) else "lessons"), sql)


def q(sql, *args):
    return _exec(_scope(sql), args).fetchall()


def q1(sql, *args):
    return _exec(_scope(sql), args).fetchone()


def run(sql, *args):
    return _exec(sql, args)


def insert(sql, *args) -> int:
    """INSERT qiladi va yangi yozuv id sini qaytaradi."""
    if PG:
        return _exec(sql + " RETURNING id", args).fetchone()["id"]
    return _exec(sql, args).lastrowid


def set_mode_cache_dirty():
    _MODE_CACHE["t"] = 0.0


def setting(key: str) -> str:
    r = q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else DEFAULTS[key]


def set_setting(key: str, value) -> None:
    run("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, str(value))


def has_column(table: str, col: str) -> bool:
    if PG:
        return _exec("SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s", (table, col)).fetchone() is not None
    return any(r["name"] == col for r in _exec(f"PRAGMA table_info({table})", ()).fetchall())


def migrate():
    """Eski bazaga yangi ustunlarni qo'shadi. Mavjud foydalanuvchilar kodsiz qoladi."""
    if not has_column("users", "approved"):
        run("ALTER TABLE users ADD COLUMN approved INTEGER DEFAULT 0")
        run("UPDATE users SET approved=1")
    if not has_column("users", "is_owner"):
        run("ALTER TABLE users ADD COLUMN is_owner INTEGER DEFAULT 0")
    if not has_column("lessons", "kind"):
        run("ALTER TABLE lessons ADD COLUMN kind TEXT DEFAULT 'lesson'")
        run("UPDATE lessons SET kind='lesson'")
    if not has_column("users", "joined_via"):
        run("ALTER TABLE users ADD COLUMN joined_via TEXT")
    if not q1("SELECT 1 FROM users WHERE is_owner=1"):
        first = q1("SELECT id FROM users WHERE is_admin=1 ORDER BY created_at LIMIT 1")
        if first:
            run("UPDATE users SET is_owner=1 WHERE id=?", first["id"])


migrate()


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
        # birinchi foydalanuvchi (yoki ADMIN_IDS) — bosh admin; qolganlar kod bilan kiradi
        boss = uid in ADMIN_IDS or (not ADMIN_IDS and not has_admin)
        owner = boss and q1("SELECT 1 FROM users WHERE is_owner=1") is None
        run("INSERT INTO users(id, full_name, username, is_admin, approved, is_owner, created_at) VALUES(?,?,?,?,?,?,?)",
            uid, full_name, username, int(boss), int(boss), int(owner), int(time.time()))
    else:
        if uid in ADMIN_IDS and not (row["is_admin"] and row["approved"] and row["is_owner"]):
            run("UPDATE users SET is_admin=1, approved=1, is_owner=1 WHERE id=?", uid)
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


# ---------------------------------------------------------------- xodimlar ro'yxati
def name_key(name: str) -> set:
    """Ism solishtirish uchun: familiya va ism (kichik harf, tutuq belgisiz)."""
    t = [w for w in str(name or "").lower().replace("'", "").replace("`", "").replace("ʻ", "").split() if w]
    return set(t[:2])


def emp_dict(r, users: dict | None = None) -> dict:
    u = (users or {}).get(r["user_id"]) if r["user_id"] else None
    return {"id": r["id"], "full_name": r["full_name"], "position": r["position"] or "", "user_id": r["user_id"],
            "linked": bool(r["user_id"]), "tg": ({"full_name": u["full_name"], "username": u["username"]} if u else None)}


def employee_of(uid: int):
    return q1("SELECT * FROM employees WHERE user_id=?", uid)


def roster_size() -> int:
    return q1("SELECT COUNT(*) c FROM employees")["c"]


def autolink_employees() -> int:
    """Ro'yxatdagi ism bilan mos keladigan, allaqachon ro'yxatdan o'tgan foydalanuvchilarni bog'laydi."""
    linked = 0
    users = q("SELECT * FROM users WHERE approved=1 AND registered=1")
    taken = {r["user_id"] for r in q("SELECT user_id FROM employees WHERE user_id IS NOT NULL")}
    for e in q("SELECT * FROM employees WHERE user_id IS NULL"):
        ek = name_key(e["full_name"])
        for u in users:
            if u["id"] in taken:
                continue
            uk = set(str(u["full_name"] or "").lower().replace("'", "").replace("`", "").replace("ʻ", "").split())
            if len(ek) == 2 and ek <= uk:
                run("UPDATE employees SET user_id=? WHERE id=?", u["id"], e["id"])
                run("UPDATE users SET full_name=? WHERE id=?", e["full_name"], u["id"])
                taken.add(u["id"])
                linked += 1
                break
    return linked


# ---------------------------------------------------------------- kirish kodlari
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # chalkash belgilar (0/O, 1/I) yo'q


def norm_code(raw: str) -> str:
    raw = unquote(str(raw or "")).strip()
    if "inv_" in raw:
        raw = raw.split("inv_", 1)[1]
    return "".join(ch for ch in raw.upper() if ch.isalnum())[:16]


def invite_link(code: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=inv_{code}"


def invite_dict(r) -> dict:
    active = r["used"] < r["max_uses"] and (not r["expires"] or r["expires"] > now())
    return {"id": r["id"], "code": r["code"], "role": r["role"], "max_uses": r["max_uses"], "used": r["used"],
            "expires": fmt_ts(r["expires"]) if r["expires"] else None, "note": r["note"] or "",
            "active": active, "link": invite_link(r["code"]), "created": fmt_ts(r["created_at"])}


def new_invite(role: str, max_uses: int, days: float, note: str, by: int) -> dict:
    code = ""
    for _ in range(10):
        code = "".join(CODE_ALPHABET[b % len(CODE_ALPHABET)] for b in os.urandom(8))
        if not q1("SELECT 1 FROM invites WHERE code=?", code):
            break
    expires = now() + int(days * 86400) if days else None
    iid = insert("""INSERT INTO invites(code, role, max_uses, used, expires, note, created_by, created_at)
                    VALUES(?,?,?,0,?,?,?,?)""", code, role, max_uses, expires, note, by, now())
    return invite_dict(q1("SELECT * FROM invites WHERE id=?", iid))


_bad_tries: dict = {}


def redeem(uid: int, raw: str) -> tuple:
    """Foydalanuvchiga kod bo'yicha kirish ruxsatini beradi. (ok, xabar)"""
    tries = [t for t in _bad_tries.get(uid, []) if time.time() - t < 600]
    if len(tries) >= 5:
        return False, "Juda ko'p noto'g'ri urinish. 10 daqiqadan keyin qayta urining"
    code = norm_code(raw)
    r = q1("SELECT * FROM invites WHERE code=?", code) if len(code) >= 6 else None
    if not r or r["used"] >= r["max_uses"] or (r["expires"] and r["expires"] < now()):
        tries.append(time.time())
        _bad_tries[uid] = tries
        return False, "Kod noto'g'ri yoki muddati o'tgan. Admindan yangi kod so'rang"
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if u and u["approved"] and (r["role"] != "admin" or u["is_admin"]):
        return True, "Siz allaqachon ro'yxatdan o'tgansiz"
    run("UPDATE invites SET used=used+1 WHERE id=?", r["id"])
    if r["role"] == "admin":
        run("UPDATE users SET approved=1, is_admin=1, joined_via=? WHERE id=?", code, uid)
        return True, "Siz admin sifatida ro'yxatdan o'tdingiz 👑"
    run("UPDATE users SET approved=1, joined_via=? WHERE id=?", code, uid)
    return True, "Ro'yxatdan o'tdingiz ✅"


NEED_CODE_TEXT = ("🔒 Assalomu alaykum!\n\nBu bot faqat texnikum o'qituvchilari uchun. "
                  "Kirish uchun admin bergan <b>kirish kodini</b> shu yerga yuboring.\n\n"
                  "Kod bo'lmasa, botdan foydalana olmaysiz — o'quv bo'limi yoki adminga murojaat qiling.")
JOINED_TEXT = "\n\nIlovani ochib, ro'yxatdan o'z ismingizni tanlang 👇"


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


# ---------------------------------------------------------------- ish jadvali (hodimlar)
def sched() -> dict:
    return {"start": setting("work_start"), "grace": int(setting("grace")), "lunch_start": setting("lunch_start"),
            "lunch_end": setting("lunch_end"), "end": setting("work_end"),
            "workdays": [int(x) for x in setting("workdays").split(",") if x.strip()]}


def at(day0: int, hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return day0 + h * 3600 + m * 60


def is_workday(day0: int) -> bool:
    return datetime.fromtimestamp(day0 + 43200, TZ).isoweekday() in sched()["workdays"]


def worked_seconds(a: int, b: int, day0: int, sc: dict) -> int:
    """[a, b] oralig'idagi ish vaqti (tushlik ayirilgan)."""
    if b <= a:
        return 0
    l1, l2 = at(day0, sc["lunch_start"]), at(day0, sc["lunch_end"])
    overlap = max(0, min(b, l2) - max(a, l1))
    return (b - a) - overlap


def work_from0() -> int:
    """Hisob boshlanadigan kun (00:00). Undan oldingi kunlar sinov hisoblanadi."""
    try:
        return parse_day(setting("work_from").strip())
    except Exception:
        return 0


def counted(days: list[dict]) -> list[dict]:
    """Faqat hisobga kiradigan (sinov bo'lmagan) kunlar."""
    return [d for d in days if not d["trial"]]


def work_days(rows, names: dict | None = None) -> list[dict]:
    """Yozuvlarni hodim+kun bo'yicha jamlaydi: keldi, ketdi, kechikish, erta ketish, ishlangan vaqt."""
    sc = sched()
    wf = work_from0()
    groups: dict = {}
    for r in sorted(rows, key=lambda r: r["start_ts"]):
        d0 = day_start(datetime.fromtimestamp(r["start_ts"], TZ))
        groups.setdefault((r["user_id"], d0), []).append(r)
    out = []
    t = now()
    for (uid, d0), rs in groups.items():
        first = rs[0]["start_ts"]
        is_open = any(r["status"] == "open" for r in rs)
        forgot = any(r["status"] == "forgot" for r in rs)
        ends = [r["end_ts"] for r in rs if r["end_ts"]]
        last_out = max(ends) if ends and not is_open else None
        secs = 0
        for r in rs:
            if r["status"] == "done" and r["end_ts"]:
                secs += worked_seconds(r["start_ts"], r["end_ts"], d0, sc)
            elif r["status"] == "open":
                secs += worked_seconds(r["start_ts"], t, d0, sc)
        start_ts, end_ts = at(d0, sc["start"]), at(d0, sc["end"])
        late = (first - start_ts) // 60 if first > start_ts + sc["grace"] * 60 else 0
        early = (end_ts - last_out) // 60 if last_out and last_out < end_ts - 60 else 0
        trial = d0 < wf
        if trial:
            late = early = 0
        out.append({
            "user_id": uid, "full_name": (names or {}).get(uid), "date": fmt_dt(d0, "%Y-%m-%d"), "day0": d0,
            "in": fmt_dt(first, "%H:%M"), "in_ts": first, "out": fmt_dt(last_out, "%H:%M") if last_out else None,
            "open": is_open, "forgot": forgot and not is_open and not last_out,
            "worked": secs, "worked_text": dur_text(secs), "late_min": int(late), "early_min": int(early),
            "manual": any(r["manual"] for r in rs), "ids": [r["id"] for r in rs], "last_id": rs[-1]["id"],
            "dist": rs[0]["start_dist"], "trial": trial,
        })
    out.sort(key=lambda x: (-x["day0"], x["in_ts"]))
    return out


def workdays_between(d_from0: int, d_to0: int) -> list[int]:
    days, d = [], d_from0
    while d <= d_to0:
        if is_workday(d):
            days.append(d)
        d += 86400
    return days


def work_month(uid: int | None = None) -> dict:
    """Oylik hisob: kelgan kunlar, kechikishlar, ishlangan soat, kelmagan kunlar."""
    m0 = day_start(datetime.now(TZ).replace(day=1))
    sql, args = "SELECT * FROM lessons WHERE start_ts>=?", [m0]
    if uid:
        sql += " AND user_id=?"
        args.append(uid)
    by: dict = {}
    for d in counted(work_days(q(sql, *args))):
        by.setdefault(d["user_id"], []).append(d)
    users = {r["id"]: r for r in q("SELECT id, created_at FROM users")}
    yday = day_start() - 86400
    res = {}
    for u_id in ([uid] if uid else list(by.keys())):
        ds = by.get(u_id, [])
        present = {d["day0"] for d in ds}
        joined = max(work_from0(), day_start(datetime.fromtimestamp((users.get(u_id) or {"created_at": m0})["created_at"], TZ)))
        wdays = workdays_between(max(m0, joined), yday) if yday >= max(m0, joined) else []
        secs = sum(d["worked"] for d in ds)
        res[u_id] = {"days": len(ds), "late": sum(1 for d in ds if d["late_min"]), "late_min": sum(d["late_min"] for d in ds),
                     "early": sum(1 for d in ds if d["early_min"]), "absent": len([d for d in wdays if d not in present]),
                     "seconds": secs, "hours_text": dur_text(secs), "forgot": sum(1 for d in ds if d["forgot"]),
                     # dars rejimi bilan moslik uchun
                     "lessons": len(ds)}
    return res


EMPTY_WORK = {"days": 0, "late": 0, "late_min": 0, "early": 0, "absent": 0, "seconds": 0, "hours_text": "",
              "forgot": 0, "lessons": 0}


def late_text(m: int) -> str:
    return f"{m // 60} soat {m % 60} daq" if m >= 60 else f"{m} daq"


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


def is_work() -> bool:
    return KIND() == "work"


async def close_stale():
    """Chiqish belgilanmay qolgan darslarni «unutilgan» deb yopadi va o'qituvchiga xabar beradi."""
    work = is_work()
    limit = day_start() if work else now() - int(float(setting("max_hours")) * 3600)
    rows = q("SELECT * FROM lessons WHERE status='open' AND start_ts<?", limit)
    for r in rows:
        run("UPDATE lessons SET status='forgot' WHERE id=?", r["id"])
        msg = (f"⚠️ {fmt_dt(r['start_ts'], '%d.%m.%Y')} kuni ishdan ketishni belgilamadingiz.\n"
               f"Kun «ketish belgilanmagan» deb yopildi. Kerak bo'lsa, ma'muriyatga murojaat qiling.") if work else (
               f"⚠️ {fmt_dt(r['start_ts'])} da boshlangan darsingiz uchun chiqish belgilanmadi.\n"
               f"Dars «chiqish belgilanmagan» deb yopildi. Kerak bo'lsa, ma'muriyatga murojaat qiling.")
        try:
            await bot.send_message(r["user_id"], msg)
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
    if payload.startswith("inv_"):
        ok, msg = redeem(m.from_user.id, payload)
        if not ok:
            return await m.answer("⚠️ " + msg + "\n\n" + NEED_CODE_TEXT, parse_mode="HTML")
        return await m.answer("✅ " + msg + JOINED_TEXT, reply_markup=app_keyboard())
    if not user["approved"]:
        return await m.answer(NEED_CODE_TEXT, parse_mode="HTML")
    if payload.startswith("t_"):
        # telefon kamerasi bilan skanerlangan — joylashuvni tekshirish uchun ilovani ochamiz
        if not check_code(payload):
            return await m.answer("⚠️ Bu QR kod eskirgan. Texnikumdagi yangi QR kodni skanerlang.")
        lesson = open_lesson(m.from_user.id)
        if is_work():
            what = "ishdan ketishni" if lesson else "ishga kelishni"
        else:
            what = "darsdan chiqishni" if lesson else "darsga kirishni"
        return await m.answer(f"📍 {what.capitalize()} tasdiqlash uchun ilovani oching — joylashuvingiz tekshiriladi 👇",
                              reply_markup=app_keyboard(payload, "📍 Tasdiqlash"))
    sc = sched()
    how = (f"Ishga kelganda va ishdan ketganda texnikumdagi QR kodni ilova orqali skanerlang.\n"
           f"🕗 Ish vaqti: {sc['start']}–{sc['end']} (tushlik {sc['lunch_start']}–{sc['lunch_end']}), "
           f"{sc['grace']} daqiqagacha kechikishga ruxsat.") if is_work() else (
        "Darsga kirganda va darsdan chiqqanda texnikumdagi QR kodni ilova orqali skanerlang.")
    role = ("👑 Siz adminsiz: ilovadagi admin panelda jurnal, statistika va sozlamalar bor. "
            "Avval «Sozlamalar»da texnikum lokatsiyasini kiriting.\n\n" + how) if user["is_admin"] else how
    await m.answer(f"Assalomu alaykum, {user['full_name']}!\n\n{role}", reply_markup=app_keyboard())


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"Sizning ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.message(F.text)
async def any_text(m: Message):
    user = upsert_user(m.from_user.id, tg_name(m.from_user), m.from_user.username)
    if not user["approved"]:
        ok, msg = redeem(m.from_user.id, m.text)
        if not ok:
            return await m.answer("⚠️ " + msg + "\n\n" + NEED_CODE_TEXT, parse_mode="HTML")
        return await m.answer("✅ " + msg + JOINED_TEXT, reply_markup=app_keyboard())
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


async def send_work_report(admin_id: int, d_from: str, d_to: str, user_id: int | None):
    start, end = parse_day(d_from), parse_day(d_to) + 86400
    sql, args = "SELECT * FROM lessons WHERE start_ts>=? AND start_ts<?", [start, end]
    if user_id:
        sql += " AND user_id=?"
        args.append(user_id)
    rows = q(sql, *args)
    users = q("SELECT * FROM users WHERE approved=1" + (" AND id=?" if user_id else " AND is_admin=0"),
              *([user_id] if user_id else []))
    names = {u["id"]: u["full_name"] for u in q("SELECT id, full_name FROM users")}
    days = sorted(counted(work_days(rows, names)), key=lambda d: (d["day0"], d["full_name"] or ""))
    if not days and not users:
        return 0
    sc = sched()
    det = [["№", "Sana", "Hodim", "Keldi", "Ketdi", "Kechikish (daq)", "Erta ketish (daq)", "Ishlagan (soat)", "Holat"]]
    for i, d in enumerate(days, 1):
        state = "Ishda" if d["open"] else ("Ketish belgilanmagan" if d["forgot"] else "Yakunlangan")
        if d["manual"]:
            state += " (tahrirlangan)"
        det.append([i, fmt_dt(d["day0"], "%d.%m.%Y"), d["full_name"], d["in"], d["out"] or "", d["late_min"] or "",
                    d["early_min"] or "", round(d["worked"] / 3600, 2), state])
    last_day = min(parse_day(d_to), day_start())
    wd = workdays_between(max(parse_day(d_from), work_from0()), last_day)
    by: dict = {}
    for d in days:
        by.setdefault(d["user_id"], []).append(d)
    sm = [["№", "Hodim", "Ish kunlari", "Kelgan", "Kelmagan", "Kechikishlar", "Jami kechikish (daq)",
           "Erta ketishlar", "Ketish belgilanmagan", "Jami ish vaqti (soat)"]]
    people = {u["id"]: u for u in users}
    for uid in by:
        people.setdefault(uid, {"id": uid, "full_name": names.get(uid), "created_at": 0})
    for i, u in enumerate(sorted(people.values(), key=lambda x: x["full_name"] or ""), 1):
        ds = by.get(u["id"], [])
        joined = day_start(datetime.fromtimestamp(u["created_at"] or 0, TZ))
        mine = [x for x in wd if x >= joined]
        present = {d["day0"] for d in ds}
        sm.append([i, u["full_name"], len(mine), len(ds), len([x for x in mine if x not in present]),
                   sum(1 for d in ds if d["late_min"]), sum(d["late_min"] for d in ds),
                   sum(1 for d in ds if d["early_min"]), sum(1 for d in ds if d["forgot"]),
                   round(sum(d["worked"] for d in ds) / 3600, 1)])
    title = f"{d_from}_{d_to}"
    await bot.send_document(admin_id, csv_file(sm, f"hodimlar_umumiy_{title}.csv"),
                            caption=(f"📊 Hodimlar davomati: {d_from} — {d_to}\n"
                                     f"🕗 Ish vaqti {sc['start']}–{sc['end']}, {sc['grace']} daq ruxsat, tushlik {sc['lunch_start']}–{sc['lunch_end']}\n"
                                     f"👥 Hodimlar: {len(people)}   📅 Ish kunlari: {len(wd)}"))
    await bot.send_document(admin_id, csv_file(det, f"hodimlar_kunlik_{title}.csv"), caption="📋 Kunlik (batafsil)")
    return max(len(days), 1)


async def send_report(admin_id: int, d_from: str, d_to: str, user_id: int | None):
    if is_work():
        return await send_work_report(admin_id, d_from, d_to, user_id)
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
    if not request["user"]["approved"] and request.path not in ("/api/me", "/api/join"):
        return web.json_response({"error": "Kirish kodi kerak", "need_code": True}, status=403)
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
async def api_join(request):
    b = await body(request)
    ok, msg = redeem(request["user"]["id"], b.get("code", ""))
    if not ok:
        return jerr(msg)
    return web.json_response({"ok": True, "message": msg})


async def api_me(request):
    u = request["user"]
    if not u["approved"]:
        return web.json_response({"need_code": True, "org": setting("org_name"),
                                  "user": {"id": u["id"], "full_name": u["full_name"]}})
    t0 = day_start()
    today = q("SELECT * FROM lessons WHERE user_id=? AND start_ts>=? ORDER BY start_ts DESC", u["id"], t0)
    hist = q("SELECT * FROM lessons WHERE user_id=? AND start_ts<? ORDER BY start_ts DESC LIMIT 30", u["id"], t0)
    days = sorted({fmt_dt(r["start_ts"], "%Y-%m-%d") for r in q(
        "SELECT start_ts FROM lessons WHERE user_id=? AND start_ts>=?", u["id"], t0 - 14 * 86400)})
    ol = open_lesson(u["id"])
    last = q1("SELECT group_id, subject FROM lessons WHERE user_id=? ORDER BY start_ts DESC LIMIT 1", u["id"])
    g = geo_conf()
    work = {}
    if is_work():
        wd = work_days(q("SELECT * FROM lessons WHERE user_id=? AND start_ts>=?", u["id"], t0 - 40 * 86400))
        work = {"today_work": next((d for d in wd if d["day0"] == t0), None),
                "work_history": [d for d in wd if d["day0"] != t0][:30],
                "work_month": work_month(u["id"]).get(u["id"], EMPTY_WORK),
                "workday_today": is_workday(t0)}
    return web.json_response({
        "mode": KIND(), "schedule": sched(), **work,
        "trial_today": day_start() < work_from0(),
        "roster_size": roster_size(),
        "employee": (lambda e: {"id": e["id"], "full_name": e["full_name"], "position": e["position"] or ""} if e else None)(employee_of(u["id"])),
        "user": {"id": u["id"], "full_name": u["full_name"], "username": u["username"],
                 "is_admin": bool(u["is_admin"]), "is_owner": bool(u["is_owner"]),
                 "registered": bool(u["registered"])},
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


async def api_roster(request):
    """Ro'yxatdan o'tish uchun bo'sh (hali tanlanmagan) ismlar."""
    me = request["user"]["id"]
    rows = q("SELECT * FROM employees ORDER BY sort, full_name")
    return web.json_response({"employees": [{"id": r["id"], "full_name": r["full_name"], "position": r["position"] or "",
                                             "mine": r["user_id"] == me} for r in rows if not r["user_id"] or r["user_id"] == me]})


async def api_register(request):
    b = await body(request)
    me = request["user"]
    if b.get("employee_id"):
        eid = int(b["employee_id"])
        e = q1("SELECT * FROM employees WHERE id=?", eid)
        if not e:
            return jerr("Bu ism ro'yxatda yo'q")
        if e["user_id"] and e["user_id"] != me["id"]:
            return jerr("Bu ism boshqa odam tomonidan tanlangan. Xato bo'lsa, adminga murojaat qiling")
        cur = employee_of(me["id"])
        if cur and cur["id"] != eid and me["registered"] and not me["is_admin"]:
            return jerr("Siz ro'yxatdan o'tgansiz. Ismni o'zgartirish uchun adminga murojaat qiling")
        run("UPDATE employees SET user_id=NULL WHERE user_id=?", me["id"])
        run("UPDATE employees SET user_id=? WHERE id=?", me["id"], eid)
        run("UPDATE users SET full_name=?, registered=1 WHERE id=?", e["full_name"], me["id"])
        return web.json_response({"ok": True, "full_name": e["full_name"]})
    if roster_size():
        return jerr("Ro'yxatdan o'z ismingizni tanlang")
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
            return jerr("Siz allaqachon ishdasiz. Ketishda yana skanerlang" if is_work()
                        else "Sizda tugallanmagan dars bor. Avval darsdan chiqishni belgilang")
        work = is_work()
        gid = None if work else opt_int(b.get("group_id"))
        lid = insert("""INSERT INTO lessons(user_id, group_id, subject, start_ts, start_dist, start_lat, start_lon, kind)
                        VALUES(?,?,?,?,?,?,?,?)""", u["id"], gid, "" if work else clean(b.get("subject"), 60), now(), dist, lat, lon,
                     "work" if work else "lesson")
        les = q1("SELECT * FROM lessons WHERE id=?", lid)
        if work:
            t0 = day_start()
            d = next((x for x in work_days(q("SELECT * FROM lessons WHERE user_id=? AND start_ts>=?", u["id"], t0)) if x["day0"] == t0), None)
            first_today = d and d["ids"][0] == lid
            tm = fmt_dt(les["start_ts"], "%H:%M")
            if not first_today:
                msg = f"Qaytib keldingiz: {tm}"
            elif d["late_min"]:
                msg = f"Ishga keldingiz: {tm}. ⏰ {late_text(d['late_min'])} kechikdingiz"
            else:
                msg = f"Ishga keldingiz: {tm}. ✅ O'z vaqtida"
            return web.json_response({"ok": True, "message": msg, "late_min": d["late_min"] if d and first_today else 0,
                                      "lesson": lesson_dict(les)})
        return web.json_response({"ok": True, "message": f"Darsga kirish qayd etildi: {fmt_dt(les['start_ts'], '%H:%M')}",
                                  "lesson": lesson_dict(les)})
    if action == "end":
        if not ol:
            return jerr("Avval ishga kelishni belgilang" if is_work() else "Ochiq dars yo'q. Avval darsga kirishni belgilang")
        if now() - ol["start_ts"] < 60:
            return jerr("Hozirgina keldingiz. Ketishni birozdan keyin belgilang" if is_work()
                        else "Dars endigina boshlandi. Chiqishni birozdan keyin belgilang")
        run("UPDATE lessons SET end_ts=?, end_dist=?, end_lat=?, end_lon=?, status='done' WHERE id=?",
            now(), dist, lat, lon, ol["id"])
        les = q1("SELECT * FROM lessons WHERE id=?", ol["id"])
        if is_work():
            t0 = day_start(datetime.fromtimestamp(les["start_ts"], TZ))
            wd = next((x for x in work_days(q("SELECT * FROM lessons WHERE user_id=? AND start_ts>=? AND start_ts<?",
                                                u["id"], t0, t0 + 86400)) if x["day0"] == t0), None)
            msg = f"Ishdan ketdingiz: {fmt_dt(les['end_ts'], '%H:%M')}. Bugun ishlagan vaqt: {wd['worked_text'] if wd else ''}"
            if wd and wd["early_min"]:
                msg += f"\n⚠️ Ish vaqti tugashiga {late_text(wd['early_min'])} qolgan edi"
            return web.json_response({"ok": True, "message": msg, "lesson": lesson_dict(les)})
        d = lesson_dict(les)
        return web.json_response({"ok": True, "message": f"Darsdan chiqish qayd etildi. Davomiyligi: {d['duration_text']}",
                                  "lesson": d})
    return jerr("Noto'g'ri amal")


# ---------- admin: statistika
async def api_work_stats(request):
    t0 = day_start()
    users = q("SELECT * FROM users WHERE approved=1 ORDER BY full_name")
    names = {u["id"]: u["full_name"] for u in users}
    staff = [u for u in users if not u["is_admin"]]
    week = work_days(q("SELECT * FROM lessons WHERE start_ts>=?", t0 - 6 * 86400), names)
    today = [d for d in week if d["day0"] == t0]
    here = {d["user_id"] for d in today}
    days = []
    for i in range(6, -1, -1):
        d0 = t0 - i * 86400
        ds = [d for d in week if d["day0"] == d0]
        days.append({"date": fmt_dt(d0, "%Y-%m-%d"), "lessons": len(ds), "present": len(ds),
                     "late": sum(1 for d in ds if d["late_min"]), "workday": is_workday(d0)})
    ms = work_month()
    ranking = sorted(({"id": u["id"], "full_name": u["full_name"], **ms[u["id"]]} for u in users if u["id"] in ms and ms[u["id"]]["late"]),
                     key=lambda x: (-x["late"], -x["late_min"]))
    sc = sched()
    started = now() >= at(t0, sc["start"]) + sc["grace"] * 60
    absent = [{"id": u["id"], "full_name": u["full_name"]} for u in staff
              if u["registered"] and u["id"] not in here] if is_workday(t0) and t0 >= work_from0() else []
    return web.json_response({
        "mode": "work", "schedule": sc, "workday_today": is_workday(t0), "work_started": started,
        "work_from": setting("work_from"), "trial_today": t0 < work_from0(),
        "teachers": len(users), "staff": len(staff),
        "unregistered": sum(not u["registered"] for u in users),
        "geo": geo_conf(),
        "today": {"present": len(today), "late": sum(1 for d in today if d["late_min"]),
                  "now_in": sum(1 for d in today if d["open"]), "left": sum(1 for d in today if d["out"]),
                  "hours": round(sum(d["worked"] for d in today) / 3600, 1), "lessons": len(today), "forgot": 0},
        "late_today": [d for d in sorted(today, key=lambda d: -d["late_min"]) if d["late_min"]],
        "now_in": [d for d in today if d["open"]],
        "today_list": today,
        "absent_today": absent,
        "days": days,
        "ranking": ranking[:10],
        "month_forgot": sum(v["forgot"] for v in ms.values()),
        "admins": [{"id": a["id"], "full_name": a["full_name"], "username": a["username"], "is_owner": bool(a["is_owner"])}
                   for a in users if a["is_admin"]],
        "pending": q1("SELECT COUNT(*) c FROM users WHERE approved=0")["c"],
        "roster_total": roster_size(),
        "roster_unlinked": q1("SELECT COUNT(*) c FROM employees WHERE user_id IS NULL")["c"],
    })


async def api_stats(request):
    if is_work():
        return await api_work_stats(request)
    t0 = day_start()
    m0 = day_start(datetime.now(TZ).replace(day=1))
    teachers = q("SELECT * FROM users WHERE approved=1 ORDER BY full_name")
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
        "admins": [{"id": a["id"], "full_name": a["full_name"], "username": a["username"], "is_owner": bool(a["is_owner"])}
                   for a in teachers if a["is_admin"]],
        "pending": q1("SELECT COUNT(*) c FROM users WHERE approved=0")["c"],
        "roster_total": roster_size(),
        "roster_unlinked": q1("SELECT COUNT(*) c FROM employees WHERE user_id IS NULL")["c"],
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
    rows = q(sql + " ORDER BY l.start_ts DESC LIMIT 1000", *args)
    if is_work():
        names = {r["user_id"]: r["full_name"] for r in rows}
        days = work_days(rows, names)
        st = qy.get("wstate")
        if st == "late":
            days = [d for d in days if d["late_min"]]
        elif st == "early":
            days = [d for d in days if d["early_min"]]
        elif st == "open":
            days = [d for d in days if d["open"]]
        elif st == "forgot":
            days = [d for d in days if d["forgot"]]
        return web.json_response({"mode": "work", "days": days[:400]})
    return web.json_response({"lessons": [{**lesson_dict(r), "full_name": r["full_name"]} for r in rows[:300]]})


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
    ms = work_month() if is_work() else all_month_stats()
    empty = EMPTY_WORK if is_work() else EMPTY_STATS
    in_lesson = {r["user_id"] for r in q("SELECT user_id FROM lessons WHERE status='open'")}
    for u in rows:
        if qs and qs not in (u["full_name"] or "").lower() and qs not in (u["username"] or "").lower() and qs != str(u["id"]):
            continue
        out.append({"id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
                    "is_owner": bool(u["is_owner"]), "approved": bool(u["approved"]),
                    "registered": bool(u["registered"]), "in_lesson": u["id"] in in_lesson,
                    "month": ms.get(u["id"], empty)})
    return web.json_response({"teachers": out})


async def api_teacher(request):
    uid = int(request.match_info["uid"])
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if not u:
        return jerr("Topilmadi", 404)
    hist = q("SELECT * FROM lessons WHERE user_id=? ORDER BY start_ts DESC LIMIT 120", uid)
    extra = {}
    if is_work():
        extra = {"work_history": work_days(hist)[:45], "work_month": work_month(uid).get(uid, EMPTY_WORK)}
    return web.json_response({
        "id": u["id"], "full_name": u["full_name"], "username": u["username"], "is_admin": bool(u["is_admin"]),
        "is_owner": bool(u["is_owner"]), "approved": bool(u["approved"]), "joined_via": u["joined_via"],
        "registered": bool(u["registered"]), "joined": fmt_ts(u["created_at"]), "month": user_month_stats(uid),
        "history": [lesson_dict(r) for r in hist[:60]], **extra,
    })


async def api_teacher_save(request):
    uid = int(request.match_info["uid"])
    b = await body(request)
    target = q1("SELECT * FROM users WHERE id=?", uid)
    if not target:
        return jerr("Topilmadi", 404)
    me = request["user"]
    if target["is_owner"] and uid != me["id"]:
        return jerr("Bosh adminni o'zgartirib bo'lmaydi")
    if target["is_admin"] and uid != me["id"] and not me["is_owner"]:
        return jerr("Boshqa adminni faqat bosh admin o'zgartira oladi")
    if "approved" in b:
        run("UPDATE users SET approved=? WHERE id=?", 1 if b["approved"] else 0, uid)
        if b["approved"] and not target["approved"]:
            try:
                await bot.send_message(uid, "✅ Admin sizga botdan foydalanishga ruxsat berdi." + JOINED_TEXT,
                                       reply_markup=app_keyboard())
            except Exception:
                pass
    if "full_name" in b:
        name = clean(b["full_name"])
        if len(name) < 3:
            return jerr("Ism-familiya juda qisqa")
        run("UPDATE users SET full_name=?, registered=1 WHERE id=?", name, uid)
    if "is_admin" in b and bool(b["is_admin"]) != bool(target["is_admin"]):
        if not me["is_owner"]:
            return jerr("Admin qo'shish yoki olib tashlash faqat bosh admin uchun")
        if not b["is_admin"] and uid == request["user"]["id"]:
            return jerr("O'zingizni adminlikdan olib bo'lmaydi")
        if not b["is_admin"] and uid in ADMIN_IDS:
            return jerr("Bu admin server sozlamalarida (ADMIN_IDS) belgilangan")
        run("UPDATE users SET is_admin=?, approved=1 WHERE id=?", 1 if b["is_admin"] else 0, uid)
    return web.json_response({"ok": True})


async def api_teacher_delete(request):
    uid = int(request.match_info["uid"])
    if uid == request["user"]["id"]:
        return jerr("O'zingizni o'chira olmaysiz")
    t = q1("SELECT * FROM users WHERE id=?", uid)
    if t and t["is_owner"]:
        return jerr("Bosh adminni o'chirib bo'lmaydi")
    if t and t["is_admin"] and not request["user"]["is_owner"]:
        return jerr("Adminni faqat bosh admin o'chira oladi")
    run("DELETE FROM lessons WHERE user_id=?", uid)
    run("UPDATE employees SET user_id=NULL WHERE user_id=?", uid)
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
                              "qr": qr_link(), "qr_version": int(setting("qr_version")),
                              "mode": KIND(), "schedule": sched()})


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
    if "mode" in b:
        if b["mode"] not in ("work", "lesson"):
            return jerr("Noto'g'ri rejim")
        set_setting("mode", b["mode"])
        set_mode_cache_dirty()
    if "schedule" in b:
        sc = b["schedule"]
        tm = {}
        for k in ("start", "lunch_start", "lunch_end", "end"):
            v = str(sc.get(k, "")).strip()
            try:
                h, m = map(int, v.split(":"))
                assert 0 <= h < 24 and 0 <= m < 60
            except Exception:
                return jerr("Vaqtni SS:DD ko'rinishida kiriting")
            tm[k] = f"{h:02d}:{m:02d}"
        if not (tm["start"] < tm["lunch_start"] <= tm["lunch_end"] < tm["end"]):
            return jerr("Vaqtlar tartibi noto'g'ri: ish boshlanishi < tushlik < ish tugashi")
        grace = int(sc.get("grace", 10))
        if not 0 <= grace <= 120:
            return jerr("Kechikish ruxsati 0–120 daqiqa bo'lishi kerak")
        wdays = sorted({int(x) for x in sc.get("workdays", []) if 1 <= int(x) <= 7})
        if not wdays:
            return jerr("Kamida bitta ish kunini tanlang")
        set_setting("work_start", tm["start"])
        set_setting("lunch_start", tm["lunch_start"])
        set_setting("lunch_end", tm["lunch_end"])
        set_setting("work_end", tm["end"])
        set_setting("grace", grace)
        set_setting("workdays", ",".join(map(str, wdays)))
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
    who = "Hodimlar ishga kelganda va ishdan ketganda" if is_work() else "O'qituvchilar darsga kirganda va chiqqanda"
    await bot.send_document(request["user"]["id"], BufferedInputFile(png, filename="davomat_qr.png"),
                            caption=f"🖨 Chop etib, kirish joyiga osib qo'ying.\n{who} shu kodni skanerlaydi.")
    return web.json_response({"ok": True, "message": "QR rasm botga yuborildi 📩"})


# ---------- admin: hisobot va xabar
async def api_report(request):
    b = await body(request)
    d_from, d_to = sorted([str(b["from"]), str(b["to"])])
    parse_day(d_from), parse_day(d_to)
    n = await send_report(request["user"]["id"], d_from, d_to, opt_int(b.get("user_id")))
    if not n:
        return jerr("Bu oraliqda ma'lumot yo'q")
    return web.json_response({"ok": True, "message": "Hisobot botga yuborildi 📩"})


async def api_broadcast(request):
    b = await body(request)
    text = str(b.get("text", "")).strip()[:3500]
    if len(text) < 2:
        return jerr("Xabar matnini yozing")
    target = b.get("target") or "all"
    if target == "all":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE approved=1 AND id<>?", request["user"]["id"])]
    elif target == "unregistered":
        ids = [r["id"] for r in q("SELECT id FROM users WHERE approved=1 AND registered=0")]
    elif target == "absent_today":
        t0 = day_start()
        ids = [r["id"] for r in q("SELECT id FROM users WHERE approved=1 AND is_admin=0 AND id NOT IN (SELECT user_id FROM lessons WHERE start_ts>=?)", t0)]
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
    work = is_work()
    sc = sched()
    for r in rows:
        if r["start_ts"] >= t0:
            title = " · ".join(x for x in (group_name(r["group_id"]), r["subject"]) if x)
            if work:
                late = (r["start_ts"] - at(t0, sc["start"])) // 60
                title = f"{late} daq kechikdi" if r["start_ts"] > at(t0, sc["start"]) + sc["grace"] * 60 and r["start_ts"] < at(t0, sc["lunch_start"]) else ""
            events.append({"type": "in", "ts": r["start_ts"], "full_name": r["full_name"], "title": title})
        if r["end_ts"] and r["end_ts"] >= t0 and r["status"] == "done":
            secs = worked_seconds(r["start_ts"], r["end_ts"], day_start(datetime.fromtimestamp(r["start_ts"], TZ)), sc) \
                if work else r["end_ts"] - r["start_ts"]
            events.append({"type": "out", "ts": r["end_ts"], "full_name": r["full_name"], "title": dur_text(secs)})
    events.sort(key=lambda e: -e["ts"])
    for e in events:
        e["time"] = fmt_dt(e["ts"], "%H:%M")
    return web.json_response({
        "org": setting("org_name"), "qr": qr_link(), "server_time": now(), "geo": geo_conf()["configured"],
        "mode": KIND(), "schedule": sc,
        "now_in": [{**lesson_dict(r), "full_name": r["full_name"]} for r in now_in],
        "today": {"lessons": len([r for r in rows if r["start_ts"] >= t0]), "teachers": len({r["user_id"] for r in rows if r["start_ts"] >= t0})},
        "events": events[:12],
    })


# ---------- admin: xodimlar ro'yxati
async def api_employees(request):
    users = {u["id"]: u for u in q("SELECT id, full_name, username FROM users")}
    rows = q("SELECT * FROM employees ORDER BY sort, full_name")
    return web.json_response({"employees": [emp_dict(r, users) for r in rows],
                              "linked": sum(1 for r in rows if r["user_id"]), "total": len(rows)})


async def api_employee_save(request):
    b = await body(request)
    name = clean(b.get("full_name"), 100)
    if len(name.split()) < 2:
        return jerr("Familiya va ismni to'liq yozing")
    pos = clean(b.get("position"), 120)
    eid = opt_int(b.get("id"))
    if any(name_key(r["full_name"]) == name_key(name) and r["id"] != eid for r in q("SELECT id, full_name FROM employees")):
        return jerr("Bu xodim ro'yxatda bor")
    if eid:
        run("UPDATE employees SET full_name=?, position=? WHERE id=?", name, pos, eid)
        e = q1("SELECT * FROM employees WHERE id=?", eid)
        if e and e["user_id"]:
            run("UPDATE users SET full_name=? WHERE id=?", name, e["user_id"])
    else:
        n = q1("SELECT COALESCE(MAX(sort),0) m FROM employees")["m"]
        eid = insert("INSERT INTO employees(full_name, position, sort, created_at) VALUES(?,?,?,?)", name, pos, n + 1, now())
    return web.json_response({"ok": True, "id": eid})


async def api_employee_import(request):
    """Ko'p qatorli matn: har qatorda «F.I.Sh | Lavozim» (lavozim ixtiyoriy)."""
    b = await body(request)
    lines = [ln.strip() for ln in str(b.get("text", "")).splitlines() if ln.strip()]
    if not lines:
        return jerr("Ro'yxat bo'sh")
    have = {frozenset(name_key(r["full_name"])) for r in q("SELECT full_name FROM employees")}
    n = q1("SELECT COALESCE(MAX(sort),0) m FROM employees")["m"]
    added = skipped = 0
    for ln in lines[:500]:
        for sep in ("|", "\t", " — ", " - "):
            if sep in ln:
                name, pos = ln.split(sep, 1)
                break
        else:
            name, pos = ln, ""
        name = clean(name.lstrip("0123456789.) "), 100)
        if len(name.split()) < 2 or frozenset(name_key(name)) in have:
            skipped += 1
            continue
        n += 1
        insert("INSERT INTO employees(full_name, position, sort, created_at) VALUES(?,?,?,?)", name, clean(pos, 120), n, now())
        have.add(frozenset(name_key(name)))
        added += 1
    linked = autolink_employees()
    msg = f"{added} ta xodim qo'shildi" + (f", {skipped} tasi o'tkazib yuborildi" if skipped else "") + \
          (f", {linked} tasi botdagi foydalanuvchi bilan bog'landi" if linked else "")
    return web.json_response({"ok": True, "added": added, "skipped": skipped, "linked": linked, "message": msg})


async def api_employee_unlink(request):
    e = q1("SELECT * FROM employees WHERE id=?", int(request.match_info["eid"]))
    if not e:
        return jerr("Topilmadi", 404)
    if e["user_id"]:
        run("UPDATE users SET registered=0 WHERE id=? AND is_owner=0", e["user_id"])
        run("UPDATE employees SET user_id=NULL WHERE id=?", e["id"])
    return web.json_response({"ok": True, "message": "Bog'lanish olib tashlandi — u odam ismini qaytadan tanlaydi"})


async def api_employee_delete(request):
    run("DELETE FROM employees WHERE id=?", int(request.match_info["eid"]))
    return web.json_response({"ok": True})


# ---------- admin: kirish kodlari
async def api_invites(request):
    rows = q("SELECT * FROM invites ORDER BY id DESC LIMIT 100")
    joined = {}
    for u in q("SELECT full_name, joined_via FROM users WHERE joined_via IS NOT NULL"):
        joined.setdefault(u["joined_via"], []).append(u["full_name"])
    pending = q("SELECT id, full_name, username, created_at FROM users WHERE approved=0 ORDER BY created_at DESC LIMIT 50")
    return web.json_response({
        "invites": [{**invite_dict(r), "people": joined.get(r["code"], [])} for r in rows],
        "pending": [{"id": u["id"], "full_name": u["full_name"], "username": u["username"], "since": fmt_ts(u["created_at"])}
                    for u in pending],
        "is_owner": bool(request["user"]["is_owner"]),
    })


async def api_invite_create(request):
    b = await body(request)
    role = "admin" if b.get("role") == "admin" else "teacher"
    if role == "admin" and not request["user"]["is_owner"]:
        return jerr("Admin kodini faqat bosh admin yarata oladi")
    uses = 1 if role == "admin" else max(1, min(int(b.get("uses") or 1), 500))
    days = b.get("days")
    days = 7.0 if days in (None, "") else max(0.0, min(float(days), 365))
    inv = new_invite(role, uses, days, clean(b.get("note"), 60), request["user"]["id"])
    return web.json_response({"ok": True, "invite": inv})


async def api_invite_delete(request):
    run("DELETE FROM invites WHERE id=?", int(request.match_info["iid"]))
    return web.json_response({"ok": True})


async def api_invite_send(request):
    r = q1("SELECT * FROM invites WHERE id=?", int(request.match_info["iid"]))
    if not r:
        return jerr("Kod topilmadi", 404)
    d = invite_dict(r)
    who = "admin" if r["role"] == "admin" else "o'qituvchi"
    till = f"\n⏳ {d['expires']['date']} gacha amal qiladi" if d["expires"] else ""
    note = f"👤 {d['note']}\n" if d["note"] else ""
    text = (f"🔑 {setting('org_name')} — davomat botiga taklif ({who})\n{note}\n"
            f"Havolani bosing: {d['link']}\n\n"
            f"yoki botga /start bosib, shu kodni yuboring: <code>{d['code']}</code>{till}")
    await bot.send_message(request["user"]["id"], text, parse_mode="HTML", disable_web_page_preview=True)
    await bot.send_message(request["user"]["id"], "☝️ Yuqoridagi xabarni o'qituvchiga uzating (Forward).")
    return web.json_response({"ok": True, "message": "Taklif botga yuborildi — uni o'qituvchiga uzating 📩"})


async def index(request):
    return web.FileResponse(WEBAPP_DIR / "index.html", headers={"Cache-Control": "no-cache"})


def build_app() -> web.Application:
    app = web.Application(middlewares=[auth_mw], client_max_size=6 * 1024 * 1024)
    r = app.router
    r.add_get("/", index)
    r.add_get("/health", lambda req: web.Response(text="ok"))
    r.add_get("/api/me", api_me)
    r.add_post("/api/register", api_register)
    r.add_get("/api/roster", api_roster)
    r.add_get("/api/admin/employees", api_employees)
    r.add_post("/api/admin/employees", api_employee_save)
    r.add_post("/api/admin/employees/import", api_employee_import)
    r.add_post("/api/admin/employees/{eid:\\d+}/unlink", api_employee_unlink)
    r.add_post("/api/admin/employees/{eid:\\d+}/delete", api_employee_delete)
    r.add_post("/api/join", api_join)
    r.add_get("/api/admin/invites", api_invites)
    r.add_post("/api/admin/invites", api_invite_create)
    r.add_post("/api/admin/invites/{iid:\\d+}/delete", api_invite_delete)
    r.add_post("/api/admin/invites/{iid:\\d+}/send", api_invite_send)
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


async def keepalive_loop():
    """Render bepul serveri 15 daqiqa tashqi so'rovsiz qolsa uxlaydi.
    Server o'z ochiq manziliga har 4 daqiqada so'rov yuboradi — so'rov Render orqali
    kirgani uchun «faollik» hisoblanadi va server uxlamaydi."""
    import aiohttp
    url = WEBAPP_URL + "/health"
    await asyncio.sleep(30)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
        while True:
            try:
                async with s.get(url) as r:
                    if r.status != 200:
                        log.warning("keepalive: %s", r.status)
            except Exception as e:
                log.warning("keepalive: %s", e)
            await asyncio.sleep(240)


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
    if WEBAPP_URL.startswith("https://") and (os.getenv("RENDER") or os.getenv("KEEPALIVE") == "1"):
        asyncio.create_task(keepalive_loop())
        log.info("Keepalive yoqildi: har 4 daqiqada %s/health", WEBAPP_URL)

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
