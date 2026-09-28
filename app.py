"""
사교원 자체 서버 — 신청·문의 백엔드 (FastAPI + SQLite)

가비아 서버에 올려 sakyowon.co.kr의 /api/* 를 처리한다.
정적 사이트(허브·망남·고향사랑 등)는 Caddy가 서빙하고, Caddy가 /api/* 만 이 서버로
리버스 프록시한다. 그래서 사이트와 API가 '같은 출처'가 되어 브라우저 CORS 문제가 없다.

설치·배포 절차는 같은 폴더의 README.md 참고.

의존성: fastapi, uvicorn, openpyxl(엑셀 업로드)  (DB는 파이썬 표준 sqlite3만 사용)

주요 엔드포인트
  GET  /api/health                      상태 확인
  POST /api/applications                신청 저장 (망남 세 교실 등 모든 사이트 공용)
  GET  /api/applications?key=…          관리자 조회(JSON) — 통합 계정 세션으로도 가능
  GET  /api/applications.csv?key=…      관리자 내보내기(CSV)
  POST /api/inquiries                   문의 저장 (기존 사교원 문의폼 호환)
  GET  /api/inquiries?key=…             관리자 조회(JSON)

통합 계정(사교원 계정 하나로 전 사이트 접속 — 연대지능 위키 제외)
  POST /api/auth/signup                 가입 신청 (관리자 승인 후 로그인 가능)
  POST /api/auth/login                  로그인 → HttpOnly 쿠키 sk_session
                                        (Domain=.sakyowon.co.kr — 모든 하위도메인 공유 = SSO)
  GET  /api/auth/me                     내 세션 확인
  POST /api/auth/logout                 로그아웃
  POST /api/auth/change-password        비밀번호 변경
  GET  /api/admin/members?status=…      회원 목록 (admin·staff)
  PATCH /api/admin/members/{id}         승인/거절/역할 변경
  GET  /api/admin/feedback?status=…     개선의견 목록 (admin·staff)
  PATCH /api/admin/feedback/{id}        상태·답변 저장
  첫 관리자 계정: signup 본문에 admin_key(=SAKYOWON_ADMIN_KEY)를 넣으면 즉시 승인+admin

환경변수 (.env / systemd)
  SAKYOWON_DB          SQLite 파일 경로 (기본 ./data/sakyowon.db)
  SAKYOWON_ADMIN_KEY   관리자 조회 비밀키 (필수 — 없으면 조회 차단)
  SAKYOWON_ALLOW_ORIGINS  쉼표구분 CORS 허용 출처 (전환기용, 같은 출처면 불필요)
  SAKYOWON_TG_TOKEN / SAKYOWON_TG_CHAT   (선택) 텔레그램 신규 알림
"""

import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone

from fastapi import FastAPI, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

DB_PATH = os.environ.get("SAKYOWON_DB", os.path.join(os.path.dirname(__file__), "data", "sakyowon.db"))
ADMIN_KEY = os.environ.get("SAKYOWON_ADMIN_KEY", "")
ALLOW_ORIGINS = [o.strip() for o in os.environ.get("SAKYOWON_ALLOW_ORIGINS", "").split(",") if o.strip()]
TG_TOKEN = os.environ.get("SAKYOWON_TG_TOKEN", "")
TG_CHAT = os.environ.get("SAKYOWON_TG_CHAT", "")

# 본진(sakyowon-site)의 AI 기능을 자체 서버에서 재구현하기 위한 설정.
# 키는 코드에 두지 않고 환경변수(/etc/sakyowon-api.env)로만 주입한다.
ANTHROPIC_KEY = os.environ.get("SAKYOWON_ANTHROPIC_KEY", "")
AI_MODEL = os.environ.get("SAKYOWON_AI_MODEL", "claude-sonnet-5")
AI_MODEL_FAST = os.environ.get("SAKYOWON_AI_MODEL_FAST", "claude-haiku-4-5-20251001")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# 품에 엔진(HPC·EXAONE) 서버 간 API — 「햇소자 ↔ 품에 엔진 연동 규격서」 6절.
# POOME_API_BASE 가 비어 있으면 아래 라우팅은 전부 꺼지고 기존 Anthropic 경로 그대로다.
# 규격서 4-6: 엔진 503/504 때 Anthropic 으로 조용히 폴백하지 않는다 — 실적 집계가 어긋난다.
POOME_API_BASE = os.environ.get("POOME_API_BASE", "").rstrip("/")
POOME_API_KEY = os.environ.get("POOME_API_KEY", "")
POOME_TIMEOUT = int(os.environ.get("POOME_TIMEOUT", "100") or "100")  # 규격 회신(9/2): 504 임계 90~110s
# 규격서 v0.2 4-3 법령 topic 8종. 목록 밖 값은 보내지 않는다.
POOME_TOPICS = ("setback", "permit", "devact", "agri", "coop", "elec", "land", "resc")

app = FastAPI(title="사교원 자체 서버 API", docs_url=None, redoc_url=None)

# 같은 출처(Caddy 뒤)로 운영하면 CORS가 필요 없다.
# 전환기(정적이 아직 GitHub Pages 등 다른 출처)에는 ALLOW_ORIGINS로 열어 준다.
if ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
        allow_credentials=True,  # 통합 계정 쿠키(sk_session)를 다른 출처에서도 쓸 수 있게
    )


# ─────────────────────────── DB ───────────────────────────

@contextmanager
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS applications (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                site        TEXT,
                program     TEXT,
                program_label TEXT,
                name        TEXT,
                phone       TEXT,
                email       TEXT,
                detail      TEXT,
                note        TEXT,
                raw         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                name        TEXT,
                contact     TEXT,
                org         TEXT,
                category    TEXT,
                message     TEXT,
                source      TEXT,
                raw         TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                message     TEXT,
                status      TEXT,
                raw         TEXT
            )
            """
        )
        # 통합 계정(사교원 계정 하나로 전 사이트 접속)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                username    TEXT NOT NULL UNIQUE COLLATE NOCASE,
                pw          TEXT NOT NULL,
                name        TEXT,
                contact     TEXT,
                org         TEXT,
                role        TEXT NOT NULL DEFAULT 'member',
                status      TEXT NOT NULL DEFAULT 'pending',
                memo        TEXT,
                applied_at  TEXT NOT NULL,
                updated_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash  TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                created_at  TEXT NOT NULL,
                expires_at  INTEGER NOT NULL
            )
            """
        )
        # 햇소자 실데이터: 마을(조합) + 회원 생성 문서
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS villages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                region      TEXT,
                members     INTEGER DEFAULT 0,
                capacity    TEXT,
                progress    INTEGER DEFAULT 0,
                phase       TEXT DEFAULT '사전 검토',
                deadline    TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                id          TEXT PRIMARY KEY,
                user_id     INTEGER NOT NULL,
                village_id  INTEGER,
                title       TEXT NOT NULL,
                type        TEXT,
                status      TEXT DEFAULT '완료',
                content     TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT
            )
            """
        )
        # 기존 테이블 컬럼 보강 (있으면 조용히 통과)
        for table, col, decl in (
            ("feedback", "msg_ko", "TEXT"), ("feedback", "page", "TEXT"),
            ("feedback", "contact", "TEXT"), ("feedback", "user_lang", "TEXT"),
            ("feedback", "reply", "TEXT"),
            ("users", "village_id", "INTEGER"),
            ("villages", "ref", "TEXT"),   # 엔진 집계용 불변 익명키 V-xxxx (규격서 v0.2 5절)
        ):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass
        # villages.ref 백필 — 한 번 부여하면 고정. id에서 매번 파생하지 않는다(규격서 v0.2 5절).
        try:
            rows = conn.execute(
                "SELECT id FROM villages WHERE ref IS NULL OR ref = '' ORDER BY id"
            ).fetchall()
            if rows:
                used = conn.execute(
                    "SELECT ref FROM villages WHERE ref LIKE 'V-%'"
                ).fetchall()
                nums = [int(r[0][2:]) for r in used if r[0] and r[0][2:].isdigit()]
                nxt = (max(nums) + 1) if nums else 1
                for r in rows:
                    conn.execute("UPDATE villages SET ref = ? WHERE id = ?", (f"V-{nxt:04d}", r[0]))
                    nxt += 1
        except sqlite3.OperationalError:
            pass


init_db()


# ─────────────────────── 유틸 ───────────────────────

def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix):
    # 초 단위 + 밀리초로 충돌 회피. 사람이 읽기 쉬운 형태.
    return prefix + "-" + datetime.now().strftime("%y%m%d-%H%M%S") + "-" + str(int(time.time() * 1000) % 1000).zfill(3)


def s(v):
    return "" if v is None else str(v).strip()


def check_admin(key):
    if not ADMIN_KEY:
        return False, "서버에 관리자 키(SAKYOWON_ADMIN_KEY)가 설정되지 않았습니다."
    if s(key) != ADMIN_KEY:
        return False, "관리자 키가 올바르지 않습니다."
    return True, ""


def notify_telegram(text):
    if not (TG_TOKEN and TG_CHAT):
        return
    try:
        payload = json.dumps({"chat_id": TG_CHAT, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        # 알림 실패가 접수를 막아서는 안 된다.
        pass


async def read_json(request: Request):
    # 프론트가 CORS 프리플라이트를 피하려고 text/plain 으로 보내는 경우도 있어
    # Content-Type과 무관하게 본문을 JSON으로 파싱한다.
    body = await request.body()
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


# ─────────────────── 통합 계정 (인증) ───────────────────
# 사교원 계정 하나로 전 사이트 접속(연대지능 위키 제외).
# 세션 쿠키 sk_session 을 Domain=.sakyowon.co.kr 로 심어 apex와 모든 하위도메인
# (vitality., bid., home. …)이 같은 로그인을 공유한다. DB에는 토큰의 해시만 저장.

SESSION_COOKIE = "sk_session"
SESSION_DAYS = 30
PBKDF2_ITER = 200_000
COOKIE_BASE = os.environ.get("SAKYOWON_COOKIE_DOMAIN", "sakyowon.co.kr").lstrip(".")


def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITER)
    return f"pbkdf2:{PBKDF2_ITER}:{salt}:{digest.hex()}"


def verify_pw(pw: str, stored: str) -> bool:
    try:
        _, iters, salt, expected = stored.split(":")
        digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(conn, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (_token_hash(token), user_id, now_iso(), now + SESSION_DAYS * 86400),
    )
    return token


def current_user(request: Request):
    """세션 쿠키로 로그인 사용자를 찾는다. 없으면 None."""
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token_hash = ? AND s.expires_at > ?",
            (_token_hash(token), int(time.time())),
        ).fetchone()
    if row is None or row["status"] != "approved":
        return None
    return dict(row)


def user_public(u) -> dict:
    return {"id": u["id"], "username": u["username"], "name": u["name"] or "", "role": u["role"]}


def _cookie_kwargs(request: Request) -> dict:
    """쿠키 Domain·Secure 를 요청에 맞게 정한다. 로컬 테스트(127.0.0.1)면 host-only·비보안."""
    host = (request.headers.get("host") or "").split(":")[0]
    kwargs = {"httponly": True, "samesite": "lax", "path": "/"}
    if host == COOKIE_BASE or host.endswith("." + COOKIE_BASE):
        kwargs["domain"] = "." + COOKIE_BASE
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    if proto == "https":
        kwargs["secure"] = True
    return kwargs


def set_session_cookie(response, request: Request, token: str):
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, **_cookie_kwargs(request))


def clear_session_cookie(response, request: Request):
    response.delete_cookie(SESSION_COOKIE, **{k: v for k, v in _cookie_kwargs(request).items() if k in ("domain", "path")})


def admin_ok(request: Request, key: str = "") -> bool:
    """관리자 조회 허용 여부 — 기존 관리자 키 또는 통합 계정(admin·staff) 세션."""
    if ADMIN_KEY and s(key) == ADMIN_KEY:
        return True
    u = current_user(request)
    return bool(u and u["role"] in ("admin", "staff"))


# 로그인 실패 속도 제한 (단일 프로세스 메모리로 충분)
_login_fails: dict = {}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _rate_limited(ip: str) -> bool:
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < 60]
    _login_fails[ip] = fails
    return len(fails) >= 8


def _record_fail(ip: str):
    _login_fails.setdefault(ip, []).append(time.time())


# 공개 접수(신청·문의·피드백) 스팸 방지 — IP당 분당 횟수 제한
_post_hits: dict = {}


def post_limited(request: Request, limit: int = 10) -> bool:
    ip = _client_ip(request)
    now = time.time()
    hits = [t for t in _post_hits.get(ip, []) if now - t < 60]
    if len(hits) >= limit:
        _post_hits[ip] = hits
        return True
    hits.append(now)
    _post_hits[ip] = hits
    return False


def err(code: str, status: int):
    return JSONResponse({"ok": False, "error": code}, status_code=status)


@app.post("/api/auth/signup")
async def auth_signup(request: Request):
    body = await read_json(request)
    username = s(body.get("username"))
    password = body.get("password") or ""
    if len(username) < 3:
        return err("invalid_username", 400)
    if len(password) < 8:
        return err("weak_password", 400)

    # 첫 관리자 부트스트랩: admin_key 가 서버 키와 일치하면 즉시 승인 + admin
    bootstrap = bool(ADMIN_KEY) and s(body.get("admin_key")) == ADMIN_KEY
    role = "admin" if bootstrap else "member"
    status = "approved" if bootstrap else "pending"

    with db() as conn:
        taken = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
        if taken:
            return err("username_taken", 409)
        conn.execute(
            "INSERT INTO users (username, pw, name, contact, org, role, status, applied_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (username, hash_pw(password), s(body.get("name")), s(body.get("contact")),
             s(body.get("org")), role, status, now_iso()),
        )
    if not bootstrap:
        notify_telegram(f"[통합계정 가입신청] {s(body.get('name')) or username} ({s(body.get('org'))})\n승인: https://sakyowon.co.kr/admin.html")
    return {"ok": True, "status": status}


@app.post("/api/auth/login")
async def auth_login(request: Request):
    ip = _client_ip(request)
    if _rate_limited(ip):
        return err("rate_limited", 429)
    body = await read_json(request)
    username = s(body.get("username"))
    password = body.get("password") or ""
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None or not verify_pw(password, row["pw"]):
            _record_fail(ip)
            return err("invalid_credentials", 401)
        if row["status"] != "approved":
            return err("not_approved", 403)
        token = create_session(conn, row["id"])
    resp = JSONResponse({"ok": True, "user": user_public(row)})
    set_session_cookie(resp, request, token)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request):
    u = current_user(request)
    if not u:
        return err("unauthorized", 401)
    return {"ok": True, "user": user_public(u)}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    resp = JSONResponse({"ok": True})
    clear_session_cookie(resp, request)
    return resp


@app.post("/api/auth/change-password")
async def auth_change_password(request: Request):
    u = current_user(request)
    if not u:
        return err("unauthorized", 401)
    body = await read_json(request)
    old = body.get("old") or ""
    new = body.get("new") or ""
    if len(new) < 8:
        return err("weak_password", 400)
    if not verify_pw(old, u["pw"]):
        return err("invalid_credentials", 400)
    token = request.cookies.get(SESSION_COOKIE, "")
    with db() as conn:
        conn.execute("UPDATE users SET pw = ?, updated_at = ? WHERE id = ?", (hash_pw(new), now_iso(), u["id"]))
        # 다른 기기의 세션은 모두 끊는다 (지금 세션만 유지)
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (u["id"], _token_hash(token)))
    return {"ok": True}


# ── 통합 관리자 화면용 (admin·staff 전용) ──

def require_staff(request: Request):
    u = current_user(request)
    if not u:
        return None, err("unauthorized", 401)
    if u["role"] not in ("admin", "staff"):
        return None, err("forbidden", 403)
    return u, None


@app.get("/api/admin/members")
def admin_members(request: Request, status: str = Query("")):
    u, e = require_staff(request)
    if e:
        return e
    sql = "SELECT id, username, name, contact, org, role, status, memo, applied_at, village_id FROM users"
    args = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY applied_at DESC"
    with db() as conn:
        items = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return {"ok": True, "items": items}


@app.patch("/api/admin/members/{member_id}")
async def admin_member_update(member_id: int, request: Request):
    u, e = require_staff(request)
    if e:
        return e
    body = await read_json(request)
    status = s(body.get("status"))
    role = s(body.get("role"))
    if status and status not in ("pending", "approved", "rejected"):
        return err("bad_request", 400)
    if role and role not in ("member", "staff", "admin"):
        return err("bad_request", 400)
    if role and u["role"] != "admin":
        return err("forbidden", 403)  # 역할 변경은 이사장(admin)만
    if member_id == u["id"] and (status or role):
        return err("forbidden", 403)  # 자기 계정의 승인상태·역할은 스스로 못 바꾼다
    with db() as conn:
        target = conn.execute("SELECT id FROM users WHERE id = ?", (member_id,)).fetchone()
        if target is None:
            return err("not_found", 404)
        sets, args = ["updated_at = ?"], [now_iso()]
        if status:
            sets.append("status = ?"); args.append(status)
        if role:
            sets.append("role = ?"); args.append(role)
        if "memo" in body:
            sets.append("memo = ?"); args.append(s(body.get("memo")))
        if "village_id" in body:
            sets.append("village_id = ?"); args.append(body.get("village_id") or None)
        args.append(member_id)
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", args)
        if status in ("pending", "rejected"):
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (member_id,))  # 즉시 접속 차단
    return {"ok": True}


FB_STATUSES = ("미확인", "검토중", "반영", "보류")


@app.get("/api/admin/feedback")
def admin_feedback(request: Request, status: str = Query("")):
    u, e = require_staff(request)
    if e:
        return e
    sql = "SELECT id, created_at, message, msg_ko, page, contact, user_lang, status, reply, raw FROM feedback"
    args = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY created_at DESC"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    items = [{
        "id": r["id"], "created_at": r["created_at"], "msg": r["message"] or "",
        "msg_ko": r["msg_ko"] or "", "page": r["page"] or "", "contact": r["contact"] or "",
        "user_lang": r["user_lang"] or "", "status": r["status"] or "미확인", "reply": r["reply"] or "",
    } for r in rows]
    return {"ok": True, "items": items}


@app.patch("/api/admin/feedback/{fb_id}")
async def admin_feedback_update(fb_id: str, request: Request):
    u, e = require_staff(request)
    if e:
        return e
    body = await read_json(request)
    status = s(body.get("status"))
    if status and status not in FB_STATUSES:
        return err("bad_request", 400)
    with db() as conn:
        target = conn.execute("SELECT id FROM feedback WHERE id = ?", (fb_id,)).fetchone()
        if target is None:
            return err("not_found", 404)
        sets, args = [], []
        if status:
            sets.append("status = ?"); args.append(status)
        if "reply" in body:
            sets.append("reply = ?"); args.append(s(body.get("reply")))
        if sets:
            args.append(fb_id)
            conn.execute(f"UPDATE feedback SET {', '.join(sets)} WHERE id = ?", args)
    return {"ok": True}


# ─────────────────────── 엔드포인트 ───────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "service": "사교원 자체 서버", "ready": True, "time": now_iso()}


@app.post("/api/applications")
async def create_application(request: Request):
    if post_limited(request):
        return JSONResponse({"ok": False, "error": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)
    body = await read_json(request)
    name = s(body.get("name"))
    phone = s(body.get("phone"))
    if not name:
        return JSONResponse({"ok": False, "error": "이름이 비어 있습니다."}, status_code=400)
    if not phone:
        return JSONResponse({"ok": False, "error": "연락처가 비어 있습니다."}, status_code=400)

    # detail: 프론트가 detailsText를 주면 그대로, 없으면 나머지 필드를 사람이 읽게 정리
    detail = s(body.get("detailsText"))
    if not detail:
        skip = {"name", "phone", "email", "note", "site", "program", "programLabel", "action", "ts", "detailsText"}
        parts = []
        for k, v in body.items():
            if k in skip or v in (None, "", [], {}):
                continue
            if isinstance(v, list):
                v = " | ".join(str(x) for x in v)
            parts.append(f"{k}: {v}")
        detail = "\n".join(parts)

    row_id = new_id("APP")
    label = s(body.get("programLabel")) or s(body.get("program")) or "신청"
    with db() as conn:
        conn.execute(
            "INSERT INTO applications (id, created_at, site, program, program_label, name, phone, email, detail, note, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id, now_iso(), s(body.get("site")), s(body.get("program")), label,
                name, phone, s(body.get("email")), detail, s(body.get("note")),
                json.dumps(body, ensure_ascii=False),
            ),
        )

    notify_telegram(f"[신청/{label}] {name} ({phone})\n{detail}\n{s(body.get('note'))}".strip())
    return {"ok": True, "id": row_id}


@app.get("/api/applications")
def list_applications(request: Request, key: str = Query(""), program: str = Query(""), site: str = Query("")):
    if not admin_ok(request, key):
        ok, msg = check_admin(key)
        return JSONResponse({"ok": False, "error": msg or "관리자 권한이 필요합니다."}, status_code=401)
    sql = "SELECT * FROM applications"
    where, args = [], []
    if program:
        where.append("program = ?"); args.append(program)
    if site:
        where.append("site = ?"); args.append(site)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC"
    with db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    return {"ok": True, "rows": rows}


@app.get("/api/applications.csv")
def export_applications(request: Request, key: str = Query("")):
    if not admin_ok(request, key):
        ok, msg = check_admin(key)
        return PlainTextResponse(msg or "관리자 권한이 필요합니다.", status_code=401)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()]
    buf = io.StringIO()
    cols = ["id", "created_at", "site", "program", "program_label", "name", "phone", "email", "detail", "note"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=applications.csv"},
    )


@app.post("/api/inquiries")
async def create_inquiry(request: Request):
    if post_limited(request):
        return JSONResponse({"ok": False, "error": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)
    body = await read_json(request)
    name = s(body.get("name"))
    message = s(body.get("message"))
    if not (name or message):
        return JSONResponse({"ok": False, "error": "내용이 비어 있습니다."}, status_code=400)
    row_id = new_id("INQ")
    with db() as conn:
        conn.execute(
            "INSERT INTO inquiries (id, created_at, name, contact, org, category, message, source, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                row_id, now_iso(), name, s(body.get("contact")), s(body.get("org")),
                s(body.get("category")), message, s(body.get("source")),
                json.dumps(body, ensure_ascii=False),
            ),
        )
    notify_telegram(f"[문의] {name} ({s(body.get('contact'))})\n{message}".strip())
    return {"ok": True, "id": row_id}


@app.get("/api/inquiries")
def list_inquiries(request: Request, key: str = Query("")):
    if not admin_ok(request, key):
        ok, msg = check_admin(key)
        return JSONResponse({"ok": False, "error": msg or "관리자 권한이 필요합니다."}, status_code=401)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM inquiries ORDER BY created_at DESC").fetchall()]
    return {"ok": True, "rows": rows}


# ─────────────────── 피드백 (본진 /api/feedback) ───────────────────

@app.post("/api/feedback")
async def create_feedback(request: Request):
    if post_limited(request):
        return JSONResponse({"ok": False, "error": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)
    body = await read_json(request)
    row_id = s(body.get("id")) or new_id("FB")
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feedback (id, created_at, message, msg_ko, page, contact, user_lang, status, reply, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (row_id, now_iso(), s(body.get("message") or body.get("text") or body.get("msg")),
             s(body.get("msg_ko") or body.get("msgKo")), s(body.get("page")), s(body.get("contact")),
             s(body.get("user_lang") or body.get("lang")), s(body.get("status")) or "미확인", "",
             json.dumps(body, ensure_ascii=False)),
        )
    return {"ok": True, "id": row_id}


@app.get("/api/feedback")
def list_feedback(request: Request, key: str = Query("")):
    if not admin_ok(request, key):
        ok, msg = check_admin(key)
        return JSONResponse({"ok": False, "error": msg or "관리자 권한이 필요합니다."}, status_code=401)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY created_at DESC").fetchall()]
    return {"ok": True, "rows": rows}


# ─────────────── 햇소자 실데이터: 마을(조합) · 생성 문서 ───────────────
# 마을 등록·수정은 운영진(admin·staff), 열람은 승인된 회원 전체(연합체 현황판의 투명성 취지).
# 문서는 본인 것만 읽고 쓴다. 운영진은 마을별 자료실로 열람.

VILLAGE_FIELDS = ("name", "region", "members", "capacity", "progress", "phase", "deadline")


def require_member(request: Request):
    u = current_user(request)
    if not u:
        return None, err("unauthorized", 401)
    return u, None


def village_row(r) -> dict:
    d = {k: r[k] for k in ("id", "created_at", "updated_at", *VILLAGE_FIELDS)}
    try:
        d["ref"] = r["ref"]
    except (IndexError, KeyError):
        d["ref"] = None
    return d


# ── 엔진 연동용 마을 표현 (규격서 v0.2 5절) ─────────────────────────────
# 엔진에는 ref·sido·sigungu·capacity_kw 만 간다. 마을 실명·주소·주민 정보는 보내지 않는다.

def next_village_ref(conn) -> str:
    """V-0001 부터 한 칸씩. 최초 부여 후 고정이라 재사용·재계산하지 않는다."""
    used = conn.execute("SELECT ref FROM villages WHERE ref LIKE 'V-%'").fetchall()
    nums = [int(r[0][2:]) for r in used if r[0] and r[0][2:].isdigit()]
    return f"V-{(max(nums) + 1) if nums else 1:04d}"


_SIDO_FULL = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시", "인천": "인천광역시",
    "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
    "경기": "경기도", "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}


def parse_sido_sigungu(region: str):
    """'전남 완도군' · '전라남도 완도군 신지면' → ('전라남도', '완도군'). 못 가르면 (원문, '')."""
    t = (region or "").strip().split()
    if not t:
        return "", ""
    head = t[0]
    sido = _SIDO_FULL.get(head, head)
    if head not in _SIDO_FULL:
        for short, full in _SIDO_FULL.items():
            if head.startswith(short):
                sido = full
                break
    sigungu = t[1] if len(t) > 1 else ""
    return sido, sigungu


def parse_capacity_kw(capacity) -> int:
    """'500kW' · '1MW' · '0.5 MW' · 500 → kW 정수. 못 읽으면 0."""
    if isinstance(capacity, (int, float)):
        return int(capacity)
    txt = (capacity or "").strip().replace(",", "")
    if not txt:
        return 0
    num = ""
    for ch in txt:
        if ch.isdigit() or (ch == "." and "." not in num):
            num += ch
        elif num:
            break
    if not num:
        return 0
    try:
        val = float(num)
    except ValueError:
        return 0
    low = txt.lower()
    if "mw" in low:
        val *= 1000
    elif "gw" in low:
        val *= 1000000
    return int(round(val))


def village_engine_context(r) -> dict:
    """엔진 요청의 context/village 에 넣을 값. name 은 일부러 넣지 않는다."""
    if r is None:
        return {}
    d = village_row(r)
    sido, sigungu = parse_sido_sigungu(d.get("region") or "")
    out = {"ref": d.get("ref") or ""}
    if sido:
        out["sido"] = sido
    if sigungu:
        out["sigungu"] = sigungu
    kw = parse_capacity_kw(d.get("capacity"))
    if kw:
        out["capacity_kw"] = kw
    return out


def village_ref_of_user(u) -> str:
    """로그인 사용자의 배정 마을 ref. 프런트가 보낸 값을 믿지 않고 서버에서 조회한다."""
    if not u or not u.get("village_id"):
        return ""
    with db() as conn:
        r = conn.execute("SELECT * FROM villages WHERE id = ?", (u["village_id"],)).fetchone()
    return (village_engine_context(r) or {}).get("ref", "")


@app.get("/api/villages")
def list_villages(request: Request):
    u, e = require_member(request)
    if e:
        return e
    with db() as conn:
        rows = conn.execute("SELECT * FROM villages ORDER BY created_at").fetchall()
    return {"ok": True, "items": [village_row(r) for r in rows], "my_village_id": u["village_id"]}


@app.post("/api/villages")
async def create_village(request: Request):
    u, e = require_staff(request)
    if e:
        return e
    body = await read_json(request)
    if not s(body.get("name")):
        return err("bad_request", 400)
    with db() as conn:
        ref = next_village_ref(conn)
        cur = conn.execute(
            "INSERT INTO villages (name, region, members, capacity, progress, phase, deadline, created_at, ref)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (s(body.get("name")), s(body.get("region")), int(body.get("members") or 0),
             s(body.get("capacity")), int(body.get("progress") or 0),
             s(body.get("phase")) or "사전 검토", s(body.get("deadline")) or "미정", now_iso(), ref),
        )
        vid = cur.lastrowid
    return {"ok": True, "id": vid, "ref": ref}


@app.patch("/api/villages/{vid}")
async def update_village(vid: int, request: Request):
    u, e = require_staff(request)
    if e:
        return e
    body = await read_json(request)
    sets, args = ["updated_at = ?"], [now_iso()]
    for f in VILLAGE_FIELDS:
        if f in body:
            val = body[f]
            if f in ("members", "progress"):
                val = int(val or 0)
            else:
                val = s(val)
            sets.append(f"{f} = ?"); args.append(val)
    with db() as conn:
        target = conn.execute("SELECT id FROM villages WHERE id = ?", (vid,)).fetchone()
        if target is None:
            return err("not_found", 404)
        args.append(vid)
        conn.execute(f"UPDATE villages SET {', '.join(sets)} WHERE id = ?", args)
    return {"ok": True}


@app.delete("/api/villages/{vid}")
def delete_village(vid: int, request: Request):
    u = current_user(request)
    if not u:
        return err("unauthorized", 401)
    if u["role"] != "admin":
        return err("forbidden", 403)  # 마을 삭제는 이사장(admin)만
    with db() as conn:
        target = conn.execute("SELECT id FROM villages WHERE id = ?", (vid,)).fetchone()
        if target is None:
            return err("not_found", 404)
        conn.execute("DELETE FROM villages WHERE id = ?", (vid,))
        conn.execute("UPDATE users SET village_id = NULL WHERE village_id = ?", (vid,))
    return {"ok": True}


@app.get("/api/my/village")
def my_village(request: Request):
    u, e = require_member(request)
    if e:
        return e
    if not u["village_id"]:
        return {"ok": True, "village": None}
    with db() as conn:
        r = conn.execute("SELECT * FROM villages WHERE id = ?", (u["village_id"],)).fetchone()
    return {"ok": True, "village": village_row(r) if r else None}


@app.get("/api/my/documents")
def my_documents(request: Request):
    u, e = require_member(request)
    if e:
        return e
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE user_id = ? ORDER BY created_at DESC", (u["id"],)
        ).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


@app.post("/api/my/documents")
async def save_document(request: Request):
    u, e = require_member(request)
    if e:
        return e
    body = await read_json(request)
    title = s(body.get("title"))
    if not title:
        return err("bad_request", 400)
    doc_id = s(body.get("id")) or new_id("DOC")
    with db() as conn:
        # 같은 id가 내 문서면 갱신, 아니면 신규
        mine = conn.execute("SELECT user_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if mine and mine["user_id"] != u["id"] and u["role"] not in ("admin", "staff"):
            return err("forbidden", 403)
        conn.execute(
            "INSERT OR REPLACE INTO documents (id, user_id, village_id, title, type, status, content, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM documents WHERE id = ?), ?),?)",
            (doc_id, mine["user_id"] if mine else u["id"], body.get("village_id") or u["village_id"],
             title, s(body.get("type")) or "문서", s(body.get("status")) or "완료",
             body.get("content") or "", doc_id, now_iso(), now_iso()),
        )
    return {"ok": True, "id": doc_id}


@app.delete("/api/my/documents/{doc_id}")
def delete_document(doc_id: str, request: Request):
    u, e = require_member(request)
    if e:
        return e
    with db() as conn:
        row = conn.execute("SELECT user_id FROM documents WHERE id = ?", (doc_id,)).fetchone()
        if row is None:
            return err("not_found", 404)
        if row["user_id"] != u["id"] and u["role"] not in ("admin", "staff"):
            return err("forbidden", 403)
        conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    return {"ok": True}


@app.get("/api/villages/{vid}/documents")
def village_documents(vid: int, request: Request):
    u, e = require_staff(request)
    if e:
        return e
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM documents WHERE village_id = ? ORDER BY created_at DESC", (vid,)
        ).fetchall()
    return {"ok": True, "items": [dict(r) for r in rows]}


# ─────────────────── AI (본진 기능 재구현) ───────────────────
# /api/ai       : Anthropic 메시지 API 프록시(스트리밍 포함). 키만 서버에서 주입.
# /api/ai/chat  : 관리자 도우미 — history+prompt → {answer}
# /api/translate: 한국어 번역 → {translated}
# 키는 SAKYOWON_ANTHROPIC_KEY 환경변수로만 주입한다(코드/깃에 없음).

def anthropic_key_problem() -> str:
    """키가 쓸 수 없는 상태면 그 이유를 한 줄로, 쓸 수 있으면 빈 문자열.
    HTTP 헤더는 latin-1 로만 인코딩된다 — 값에 한글이 섞이면 호출이 예외로 죽는다.
    환경파일에 안내문의 자리표시자(sk-ant-여기에-키 등)를 그대로 넣은 사고가 실제로 있었다."""
    if not ANTHROPIC_KEY:
        return "미설정"
    if not ANTHROPIC_KEY.isascii():
        return "키 값에 한글 등 ASCII 밖 문자가 들어 있습니다 (자리표시자를 그대로 넣지 않았는지 확인)"
    if not ANTHROPIC_KEY.startswith("sk-"):
        return "키 형식이 아닙니다 (sk- 로 시작해야 합니다)"
    return ""


def _anthropic_request(body: dict):
    """Anthropic 메시지 API에 요청을 보내고 열린 응답 객체를 돌려준다(스트리밍 지원).
    HTTP 4xx/5xx는 urllib이 HTTPError를 던지지만 그 객체도 .read()/status 가 있어 그대로 relay 가능."""
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=payload,
        headers={
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        return urllib.request.urlopen(req, timeout=120), 200
    except urllib.error.HTTPError as e:
        return e, e.code


def _anthropic_once(body: dict):
    """비스트리밍 호출 → (status_code, response_bytes). 응답은 Anthropic 원문 그대로.
    네트워크·인코딩 실패는 status 0 으로 돌려 라우트가 500 으로 죽지 않게 한다."""
    try:
        resp, status = _anthropic_request({**body, "stream": False})
        data = resp.read()
        return status, data
    except Exception:
        return 0, b""


def _anthropic_text(data_bytes: bytes) -> str:
    try:
        d = json.loads(data_bytes)
    except Exception:
        return ""
    content = d.get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ─────────────────── 품에 엔진 어댑터 (규격서 v0.1 4절) ───────────────────
# 프런트는 그대로 /api/ai/chat 을 부른다. 서버 안쪽에서만 엔진으로 갈아탄다.
# 응답에는 항상 backend 를 붙여 어느 엔진의 답인지 사후에 가릴 수 있게 한다.

def _poome_request(path: str, body: dict | None = None, timeout: int | None = None):
    """엔진 /api/v1/* 호출 → (status, dict). 네트워크 실패는 status 0."""
    url = f"{POOME_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "X-API-Key": POOME_API_KEY,
            "Content-Type": "application/json",
            "User-Agent": "sakyowon-hatsoja/1.0",
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or POOME_TIMEOUT) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except Exception as e:  # DNS·타임아웃·터널 다운(Cloudflare 530 등)
        return 0, {"error": {"code": "unreachable", "message": str(e)[:200]}}
    try:
        return status, json.loads(raw or b"{}")
    except Exception:
        return status, {"error": {"code": "bad_json", "message": raw[:200].decode("utf-8", "ignore")}}


def _poome_unavailable_answer(status: int, d: dict) -> dict:
    """503/504/0 — 폴백 금지. 사용자에게 잠시 후 안내 + backend 표시 (규격서 4-6)."""
    msg = (d.get("error") or {}).get("message", "")
    if status == 429:
        text = "AI 엔진 호출 한도에 잠시 걸렸습니다. 1분 뒤 다시 시도해 주세요."
    elif status in (503, 504, 0):
        text = "AI 엔진이 잠시 응답하지 않습니다. 잠시 후 다시 시도해 주세요."
    elif status == 401:
        text = "AI 엔진 인증에 실패했습니다. 관리자에게 알려 주세요."
    else:
        text = "AI 엔진이 오류를 돌려주었습니다. 잠시 후 다시 시도해 주세요."
    return {"answer": text, "backend": "poome", "engine_status": status, "engine_error": msg[:200]}


@app.get("/api/ai/health")
async def ai_health():
    """연동 ①단계용 — 사교원 서버에서 엔진 health 왕복. 인증 불필요(규격서 4-2)."""
    if not POOME_API_BASE:
        return {"ok": False, "backend": "anthropic" if ANTHROPIC_KEY else "none",
                "engine": "not_configured"}
    status, d = await run_in_threadpool(_poome_request, "/api/v1/health", None, 10)
    return {"ok": status == 200 and bool(d.get("ok")), "backend": "poome",
            "engine_status": status, "engine": d}


@app.post("/api/ai")
async def ai_proxy(request: Request):
    # 🔴 로그인 필수(26-09-28). 그전까지 비로그인 누구나 부를 수 있어 사교원 AI 이용료가
    # 공개 노출돼 있었다. 햇소자는 승인된 이용자의 업무도구다.
    # 401 을 프런트(callAIDoc)가 읽는 모양({error:{message}})으로 돌려준다 — 프런트 수정 0.
    if current_user(request) is None:
        return JSONResponse(
            {"error": {"message": "로그인이 필요합니다. 화면 오른쪽 위에서 로그인한 뒤 다시 질문해 주세요. 계정이 없으면 가입 신청 후 승인을 받으시면 됩니다."}}, status_code=401)
    body = await read_json(request)
    prob = anthropic_key_problem()
    if prob:
        return JSONResponse({"error": {"message": f"AI 키(SAKYOWON_ANTHROPIC_KEY) 문제: {prob}"}})
    if AI_MODEL:
        body["model"] = AI_MODEL  # 클라이언트가 보낸 옛 모델 ID를 현재 모델로 통일
    # 프런트(서류 생성·검토)는 max_tokens 1500~3000만 보낸다. Sonnet 5 기본 thinking이 그 예산을 먼저 쓰면
    # 본문이 비거나 잘리므로, 클라이언트가 명시하지 않았을 때만 생각을 끈다(9/23 대조 시험에서 확인).
    body.setdefault("thinking", {"type": "disabled"})

    if body.get("stream"):
        def gen():
            resp, _ = _anthropic_request(body)
            try:
                for chunk in resp:
                    yield chunk
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        return StreamingResponse(gen(), media_type="text/event-stream")

    status, data = await run_in_threadpool(_anthropic_once, body)
    return Response(content=data, media_type="application/json", status_code=status)


@app.post("/api/ai/chat")
async def ai_chat(request: Request):
    # 🔴 로그인 필수(26-09-28). 이 경로는 품에 엔진(넥서스 H200)으로 나간다 —
    # 무인증으로 열어 두면 클라이언트 상한(분당 20·동시 5)을 외부가 먹고 직원이 못 쓴다.
    # 401 에 answer 를 함께 담는다 — 햇소자 프런트가 d.answer 를 그대로 띄우므로
    # 「연결 실패」가 아니라 로그인 안내가 보인다(프런트 수정 0).
    user = current_user(request)
    if user is None:
        return JSONResponse(
            {"ok": False, "error": "unauthorized",
             "answer": "로그인이 필요합니다. 화면 오른쪽 위에서 로그인한 뒤 다시 질문해 주세요. 계정이 없으면 가입 신청 후 승인을 받으시면 됩니다.",
             "backend": "none"}, status_code=401)
    body = await read_json(request)
    prompt = s(body.get("prompt"))
    context = s(body.get("context"))
    history = body.get("history") if isinstance(body.get("history"), list) else []

    # ── 엔진 경로 (규격서 v0.2 4-3 /api/v1/ask) — POOME_API_BASE 설정 시 우선 ──
    if POOME_API_BASE:
        ask = {"question": prompt or "(빈 질문)", "max_chars": 2000}
        ctx = {}
        # stage 는 짧은 단계명이다(규격 예: "신청준비"). 상담 탭이 보내는 context 는
        # 긴 설명문이라 그대로 넣지 않고, 그 안의 화면 이름만 뽑아 쓴다.
        stage = s(body.get("stage"))
        if not stage and context:
            m = re.search(r"직전에 보던 화면:\s*([^)]{1,40})", context)
            if m:
                stage = m.group(1).strip()
        if stage:
            ctx["stage"] = stage[:40]
        # 법령 topic 8종(v0.2). 상담 탭은 보내지 않는다 — 온 값만 검증해 넘긴다.
        topic = s(body.get("topic"))
        if topic in POOME_TOPICS:
            ctx["topic"] = topic
        # 마을 익명키는 서버에서 조회한다. 프런트가 보낸 값은 쓰지 않는다(실명 유출·위조 방지).
        vref = village_ref_of_user(user)
        if vref:
            ctx["village_ref"] = vref
        if ctx:
            ask["context"] = ctx
        # 최근 10턴만. 역할·본문만 남기고 나머지 필드는 떨군다(v0.2 4-3).
        hist = []
        for h in history[-10:]:
            if not isinstance(h, dict):
                continue
            role = s(h.get("role"))
            content = s(h.get("content"))
            if role in ("user", "assistant") and content:
                hist.append({"role": role, "content": content[:4000]})
        if hist:
            ask["history"] = hist
        status, d = await run_in_threadpool(_poome_request, "/api/v1/ask", ask)
        if status != 200 or "answer" not in d:
            return _poome_unavailable_answer(status, d)
        if d.get("insufficient"):
            return {"answer": "근거 자료에서 답을 찾지 못했습니다. 질문을 바꾸어 보시거나 상담신청을 이용해 주세요.",
                    "backend": d.get("backend", "poome"), "insufficient": True,
                    "sources": d.get("sources", []), "request_id": d.get("request_id")}
        return {"answer": d.get("answer", ""), "backend": d.get("backend", "poome"),
                "sources": d.get("sources", []), "insufficient": False,
                "request_id": d.get("request_id"), "elapsed_ms": d.get("elapsed_ms")}

    # ── 기존 Anthropic 경로 (엔진 미설정 시) ──
    prob = anthropic_key_problem()
    if prob:
        return {"answer": f"AI 기능이 아직 연결되지 않았습니다. (서버 SAKYOWON_ANTHROPIC_KEY — {prob})",
                "backend": "none"}
    messages = []
    for h in history[-10:]:
        role = s(h.get("role"))
        content = s(h.get("content"))
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": prompt or "(빈 질문)"})

    system = (
        "당신은 사회혁신교육원(사교원)의 실무 도우미입니다. 사회연대경제·교육컨설팅 맥락에서 "
        "정확하고 신뢰감 있게, 한국어로 간결히 답합니다."
        + (f" 현재 화면 컨텍스트: {context}." if context else "")
    )
    # Sonnet 5는 thinking이 기본으로 켜져 있어 max_tokens를 생각에 다 쓰고 빈 답을 낸다(9/23 대조 시험 15건 중 4건).
    # 상담 답변은 짧은 단발이라 생각을 끄고 본문 여유를 준다.
    areq = {"model": AI_MODEL, "max_tokens": 2500, "thinking": {"type": "disabled"},
            "system": system, "messages": messages}
    status, data = await run_in_threadpool(_anthropic_once, areq)
    answer = _anthropic_text(data)
    if not answer:
        if status == 0:
            return {"answer": "(AI 서버에 연결하지 못했습니다. 키 설정과 네트워크를 확인해 주세요.)",
                    "backend": "anthropic"}
        if status == 401:
            return {"answer": "(AI 키가 거부되었습니다. SAKYOWON_ANTHROPIC_KEY 값을 확인해 주세요.)",
                    "backend": "anthropic"}
        return {"answer": "(응답을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.)", "backend": "anthropic"}
    return {"answer": answer, "backend": "anthropic"}


@app.post("/api/translate")
async def translate(request: Request):
    body = await read_json(request)
    text = s(body.get("text"))
    if not text:
        return {"translated": ""}
    if anthropic_key_problem():
        # 키가 없거나 쓸 수 없으면 프론트가 MyMemory 공용 API로 폴백하도록 실패를 알린다.
        return JSONResponse({"ok": False, "error": "번역 백엔드 미설정"}, status_code=503)
    target = s(body.get("target")) or "ko"
    system = (
        f"You are a translator. Translate the user's text into {target}. "
        "Output ONLY the translation, with no quotes or commentary."
    )
    areq = {
        "model": AI_MODEL_FAST,
        "max_tokens": 2000,
        "system": system,
        "messages": [{"role": "user", "content": text}],
    }
    status, data = await run_in_threadpool(_anthropic_once, areq)
    out = _anthropic_text(data)
    if not out:
        return JSONResponse({"ok": False, "error": "번역 실패"}, status_code=502)
    return {"translated": out}



# --- mangnam-coop 운영 모듈 (install-on-server.sh) ---
# 망남마을협동조합 월별 회계·회의록·문서 보관·경영공시/실적 게시 (/api/mangnam/*).
# 코드: https://github.com/deka2026/mangnam-coop/blob/main/server/mangnam_api.py
try:
    from mangnam_api import install as _install_mangnam
    _install_mangnam(app, db=db, admin_ok=admin_ok, current_user=current_user,
                     now_iso=now_iso, new_id=new_id, s=s, db_path=DB_PATH)
except ImportError as _e:  # 모듈 파일이 없으면 기존 기능만 그대로 돈다
    print("mangnam_api 미탑재:", _e)


# ═══════════════════════════ 데이터 저장소 (엑셀 → 표 → API) ═══════════════════════════
# 이사장님이 엑셀/CSV를 올리면 표(테이블)로 저장하고, 수파베이스처럼 REST·SQL로 조회한다.
#   화면: https://sakyowon.co.kr/data.html (허브 레포)   CLI: tools/skdata.py
#   권한: 관리자 키·직원(staff/admin) 세션 = 읽기+쓰기 / 읽기 키(SAKYOWON_DATA_READ_KEY) = 읽기만
# 별도 DB 파일(datasets.db)을 써서 잘못된 업로드가 계정·신청 DB를 건드리지 못하게 한다.
# 표 이름은 영문 소문자·숫자·밑줄만(예: mangnam_2024). 실제 SQLite 테이블 이름도 그대로라
# SQL 조회에서 `SELECT * FROM mangnam_2024` 처럼 쓴다. 각 행에는 `_id`(자동 번호)가 붙는다.

DATA_DB_PATH = os.environ.get("SAKYOWON_DATA_DB", os.path.join(os.path.dirname(DB_PATH), "datasets.db"))
DATA_READ_KEY = os.environ.get("SAKYOWON_DATA_READ_KEY", "")
DATA_MAX_UPLOAD = 30 * 1024 * 1024
DATA_MAX_ROWS = 5000        # JSON 1회 조회 상한
DATA_MAX_CSV_ROWS = 200000  # CSV 내보내기 상한
DATA_SQL_TIMEOUT = 8        # 초
DATA_RESERVED_PARAMS = {"limit", "offset", "order", "q", "format", "key", "select"}
DATA_OPS = {"eq": "=", "neq": "!=", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "like": "LIKE", "in": "IN", "is": "IS"}
_DATA_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


@contextmanager
def data_db(readonly: bool = False):
    os.makedirs(os.path.dirname(DATA_DB_PATH), exist_ok=True)
    if readonly:
        conn = sqlite3.connect(f"file:{DATA_DB_PATH}?mode=ro", uri=True, timeout=20)
    else:
        conn = sqlite3.connect(DATA_DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        if not readonly:
            conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def init_data_db():
    with data_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _catalog (
                name        TEXT PRIMARY KEY,
                title       TEXT,
                columns     TEXT NOT NULL,
                source      TEXT,
                sheet       TEXT,
                row_count   INTEGER DEFAULT 0,
                note        TEXT,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT
            )
            """
        )


init_data_db()


def qi(name: str) -> str:
    """SQLite 식별자 인용."""
    return '"' + str(name).replace('"', '""') + '"'


def data_access(request: Request, key: str = "", write: bool = False):
    """데이터 저장소 권한. 허용되면 행위자 이름, 아니면 None."""
    k = s(key) or s(request.headers.get("x-data-key", ""))
    if ADMIN_KEY and k and hmac.compare_digest(k, ADMIN_KEY):
        return "admin-key"
    if not write and DATA_READ_KEY and k and hmac.compare_digest(k, DATA_READ_KEY):
        return "read-key"
    u = current_user(request)
    if u and u["role"] in ("admin", "staff"):
        return u["username"]
    return None


def data_slug(raw: str) -> str:
    """파일명·입력값에서 표 이름(영문 소문자·숫자·밑줄)을 만든다. 한글만 있으면 날짜 기반 이름."""
    x = s(raw).lower()
    x = re.sub(r"\.(xlsx|xlsm|xls|csv|tsv|txt)$", "", x)
    x = re.sub(r"[^a-z0-9_]+", "_", x).strip("_")
    x = re.sub(r"_+", "_", x)
    if not x:
        x = "table_" + datetime.now().strftime("%y%m%d_%H%M")
    elif not x[0].isalpha():
        x = "t_" + x
    return x[:63]


def _cell(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, datetime):
        if (v.hour, v.minute, v.second) == (0, 0, 0):
            return v.strftime("%Y-%m-%d")
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        return int(v) if v.is_integer() else v
    if isinstance(v, (int,)):
        return v
    x = str(v).strip()
    return x if x else None


_INT_RE = re.compile(r"^-?(0|[1-9]\d{0,17})$")
_NUM_RE = re.compile(r"^-?(0|[1-9]\d{0,17})\.\d+$")


def _coerce_str(v):
    """CSV처럼 전부 문자열인 값을 숫자로 살짝 바꾼다(앞자리 0·전화번호·주민번호는 문자열 유지)."""
    v = _cell(v)
    if not isinstance(v, str):
        return v
    raw = v.replace(",", "") if re.match(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$", v) else v
    if _INT_RE.match(raw):
        return int(raw)
    if _NUM_RE.match(raw):
        return float(raw)
    return v


def _read_sheets(filename: str, data: bytes):
    """파일을 시트 목록 [{sheet, rows}]로 읽는다. rows는 값 2차원 배열."""
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext in ("xlsx", "xlsm"):
        try:
            import openpyxl  # 서버 requirements.txt에 포함
        except ImportError:
            raise ValueError("openpyxl_missing")
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        out = []
        for ws in wb.worksheets:
            # 시스템에서 내보낸 엑셀은 숫자가 문자열로 들어오는 일이 많아 CSV와 같은 규칙으로 살짝 숫자화한다
            rows = [[_coerce_str(c) for c in r] for r in ws.iter_rows(values_only=True)]
            out.append({"sheet": ws.title, "rows": rows})
        wb.close()
        return out
    if ext == "xls":
        raise ValueError("xls_unsupported")
    if ext in ("csv", "tsv", "txt", ""):
        text = None
        for enc in ("utf-8-sig", "cp949", "euc-kr", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if ext == "tsv":
            delim = "\t"
        else:
            try:
                delim = csv.Sniffer().sniff(text[:4096], delimiters=",\t;|").delimiter
            except csv.Error:
                delim = ","
        rows = [[_coerce_str(c) for c in r] for r in csv.reader(io.StringIO(text), delimiter=delim)]
        return [{"sheet": "Sheet1", "rows": rows}]
    raise ValueError("unsupported_format")


def _build_table(rows, header_row=None):
    """2차원 값 배열 → (columns[{name,type}], records[list], header_index). header_row는 1부터."""
    rows = [r for r in rows]
    # 뒤쪽 완전 빈 행 제거
    while rows and all(v is None for v in rows[-1]):
        rows.pop()
    if not rows:
        raise ValueError("empty_sheet")
    counts = [sum(1 for v in r if v is not None) for r in rows[:20]]
    if header_row and 1 <= int(header_row) <= len(rows):
        hi = int(header_row) - 1
    else:
        best = max(counts) if counts else 0
        hi = next((i for i, c in enumerate(counts) if c >= 2 and c >= best * 0.6), 0)
    header = rows[hi]
    width = max(len(r) for r in rows)
    header = list(header) + [None] * (width - len(header))
    names, seen = [], set()
    for i, h in enumerate(header):
        n = re.sub(r"\s+", " ", s(h)).replace('"', "'")
        auto = not n
        if auto:
            n = f"열{i + 1}"
        if n.lower() == "_id":
            n = "id_"
        base, k = n, 2
        while n.lower() in seen:
            n = f"{base}_{k}"; k += 1
        seen.add(n.lower())
        names.append((n, auto))
    records = []
    for r in rows[hi + 1:]:
        r = list(r) + [None] * (width - len(r))
        if all(v is None for v in r):
            continue
        records.append(r[:width])
    # 자동 이름 열인데 값이 전혀 없으면 버린다
    keep = [i for i, (n, auto) in enumerate(names) if not (auto and all(rec[i] is None for rec in records))]
    columns = []
    for i in keep:
        vals = [rec[i] for rec in records if rec[i] is not None]
        if vals and all(isinstance(v, int) and not isinstance(v, bool) for v in vals):
            t = "INTEGER"
        elif vals and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            t = "REAL"
        else:
            t = "TEXT"
        columns.append({"name": names[i][0], "type": t})
    records = [[rec[i] for i in keep] for rec in records]
    return columns, records, hi + 1


def _catalog_row(r) -> dict:
    d = dict(r)
    d["columns"] = json.loads(d.get("columns") or "[]")
    return d


def _get_catalog(conn, name: str):
    r = conn.execute("SELECT * FROM _catalog WHERE name = ?", (name,)).fetchone()
    return _catalog_row(r) if r else None


def _store_table(conn, name: str, columns, records, mode: str, meta: dict, actor: str):
    """표를 만들거나(replace) 이어 붙인다(append). 카탈로그도 갱신."""
    existing = _get_catalog(conn, name)
    if mode == "append" and existing:
        have = {c["name"].lower(): c for c in existing["columns"]}
        for c in columns:
            if c["name"].lower() not in have:
                conn.execute(f"ALTER TABLE {qi(name)} ADD COLUMN {qi(c['name'])} {c['type']}")
                existing["columns"].append(c)
        columns_all = existing["columns"]
    else:
        conn.execute(f"DROP TABLE IF EXISTS {qi(name)}")
        cols_sql = ", ".join(f"{qi(c['name'])} {c['type']}" for c in columns)
        conn.execute(f"CREATE TABLE {qi(name)} (\"_id\" INTEGER PRIMARY KEY AUTOINCREMENT{', ' + cols_sql if cols_sql else ''})")
        columns_all = columns
        mode = "replace"
    if records:
        names = [c["name"] for c in columns]
        conn.executemany(
            f"INSERT INTO {qi(name)} ({', '.join(qi(n) for n in names)}) VALUES ({', '.join('?' * len(names))})",
            records,
        )
    total = conn.execute(f"SELECT COUNT(*) FROM {qi(name)}").fetchone()[0]
    now = now_iso()
    if existing:
        conn.execute(
            "UPDATE _catalog SET title = ?, columns = ?, source = ?, sheet = ?, row_count = ?, updated_at = ?, updated_by = ?"
            " WHERE name = ?",
            (meta.get("title") or existing["title"], json.dumps(columns_all, ensure_ascii=False), meta.get("source"),
             meta.get("sheet"), total, now, actor, name),
        )
    else:
        conn.execute(
            "INSERT INTO _catalog (name, title, columns, source, sheet, row_count, note, created_at, updated_at, updated_by)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (name, meta.get("title") or name, json.dumps(columns_all, ensure_ascii=False), meta.get("source"),
             meta.get("sheet"), total, "", now, now, actor),
        )
    return _get_catalog(conn, name)


async def _read_upload(request: Request):
    data = await request.body()
    if not data:
        raise ValueError("empty_file")
    if len(data) > DATA_MAX_UPLOAD:
        raise ValueError("file_too_large")
    return data


@app.get("/api/data/tables")
def data_tables(request: Request, key: str = Query("")):
    if not data_access(request, key):
        return err("unauthorized", 401)
    with data_db() as conn:
        rows = conn.execute("SELECT * FROM _catalog ORDER BY updated_at DESC").fetchall()
    return {"ok": True, "items": [_catalog_row(r) for r in rows], "read_key_set": bool(DATA_READ_KEY)}


@app.post("/api/data/inspect")
async def data_inspect(request: Request, filename: str = Query(""), key: str = Query(""), header_row: int = Query(0)):
    """파일을 저장하지 않고 시트·열·미리보기만 돌려준다(업로드 마법사용)."""
    if not data_access(request, key, write=True):
        return err("unauthorized", 401)
    filename = s(filename) or s(request.headers.get("x-filename", ""))
    try:
        data = await _read_upload(request)
        sheets = await run_in_threadpool(_read_sheets, filename, data)
    except ValueError as e:
        return err(str(e), 400)
    except Exception:
        return err("parse_failed", 400)
    out = []
    for sh in sheets:
        try:
            columns, records, hrow = _build_table(sh["rows"], header_row or None)
        except ValueError:
            out.append({"sheet": sh["sheet"], "empty": True})
            continue
        out.append({"sheet": sh["sheet"], "header_row": hrow, "columns": columns, "row_count": len(records),
                    "preview": records[:5]})
    return {"ok": True, "filename": filename, "suggested_name": data_slug(filename), "sheets": out}


@app.post("/api/data/import")
async def data_import(
    request: Request,
    name: str = Query(""),
    title: str = Query(""),
    sheet: str = Query(""),
    mode: str = Query("replace"),
    filename: str = Query(""),
    header_row: int = Query(0),
    key: str = Query(""),
):
    """엑셀/CSV 본문(raw bytes)을 표로 저장. mode=replace(기본)|append."""
    actor = data_access(request, key, write=True)
    if not actor:
        return err("unauthorized", 401)
    filename = s(filename) or s(request.headers.get("x-filename", "")) or "upload.xlsx"
    name = data_slug(name or filename)
    if not _DATA_NAME_RE.match(name) or name.startswith("sqlite_"):
        return err("bad_name", 400)
    mode = "append" if s(mode) == "append" else "replace"
    try:
        data = await _read_upload(request)
        sheets = await run_in_threadpool(_read_sheets, filename, data)
    except ValueError as e:
        return err(str(e), 400)
    except Exception:
        return err("parse_failed", 400)
    picked = None
    if s(sheet):
        picked = next((x for x in sheets if x["sheet"] == s(sheet)), None)
        if picked is None:
            return err("sheet_not_found", 400)
    else:
        picked = next((x for x in sheets if any(any(v is not None for v in r) for r in x["rows"])), sheets[0])
    try:
        columns, records, _ = _build_table(picked["rows"], header_row or None)
    except ValueError as e:
        return err(str(e), 400)
    meta = {"title": s(title), "source": filename, "sheet": picked["sheet"]}
    with data_db() as conn:
        entry = _store_table(conn, name, columns, records, mode, meta, actor)
    return {"ok": True, "table": entry, "imported": len(records), "mode": mode}


@app.get("/api/data/tables/{name}")
def data_table_info(name: str, request: Request, key: str = Query("")):
    if not data_access(request, key):
        return err("unauthorized", 401)
    with data_db() as conn:
        entry = _get_catalog(conn, name)
    if not entry:
        return err("not_found", 404)
    return {"ok": True, "table": entry}


@app.patch("/api/data/tables/{name}")
async def data_table_update(name: str, request: Request, key: str = Query("")):
    """제목·메모 수정, 또는 이름 변경(new_name)."""
    actor = data_access(request, key, write=True)
    if not actor:
        return err("unauthorized", 401)
    body = await read_json(request)
    with data_db() as conn:
        entry = _get_catalog(conn, name)
        if not entry:
            return err("not_found", 404)
        sets, args = ["updated_at = ?", "updated_by = ?"], [now_iso(), actor]
        if "title" in body:
            sets.append("title = ?"); args.append(s(body["title"]) or name)
        if "note" in body:
            sets.append("note = ?"); args.append(s(body["note"]))
        new_name = data_slug(body.get("new_name", "")) if s(body.get("new_name")) else ""
        if new_name and new_name != name:
            if not _DATA_NAME_RE.match(new_name) or _get_catalog(conn, new_name):
                return err("bad_name", 400)
            conn.execute(f"ALTER TABLE {qi(name)} RENAME TO {qi(new_name)}")
            sets.append("name = ?"); args.append(new_name)
        args.append(name)
        conn.execute(f"UPDATE _catalog SET {', '.join(sets)} WHERE name = ?", args)
        entry = _get_catalog(conn, new_name or name)
    return {"ok": True, "table": entry}


@app.delete("/api/data/tables/{name}")
def data_table_delete(name: str, request: Request, key: str = Query("")):
    if not data_access(request, key, write=True):
        return err("unauthorized", 401)
    with data_db() as conn:
        if not _get_catalog(conn, name):
            return err("not_found", 404)
        conn.execute(f"DROP TABLE IF EXISTS {qi(name)}")
        conn.execute("DELETE FROM _catalog WHERE name = ?", (name,))
    return {"ok": True}


def _parse_filters(params, colnames):
    """?열=op.값 형식 → (where_sql, args). 열 이름은 대소문자 무시."""
    lower = {c.lower(): c for c in colnames}
    where, args = [], []
    for k, v in params:
        if k in DATA_RESERVED_PARAMS:
            continue
        col = lower.get(k.lower())
        if col is None:
            raise ValueError(f"unknown_column:{k}")
        op, _, val = v.partition(".")
        if op not in DATA_OPS:
            op, val = "eq", v
        c = qi(col)
        if op == "in":
            items = [x.strip() for x in val.strip("()").split(",") if x.strip() != ""]
            if not items:
                raise ValueError("bad_filter")
            where.append(f"{c} IN ({', '.join('?' * len(items))})"); args.extend(_coerce_str(x) for x in items)
        elif op == "is":
            where.append(f"{c} IS NULL" if val.lower() in ("null", "") else f"{c} IS NOT NULL")
        elif op == "like":
            pat = val.replace("*", "%")
            if "%" not in pat and "_" not in pat:
                pat = f"%{pat}%"
            where.append(f"{c} LIKE ?"); args.append(pat)
        else:
            where.append(f"{c} {DATA_OPS[op]} ?"); args.append(_coerce_str(val))
    return where, args


@app.get("/api/data/tables/{name}/rows")
def data_rows(
    request: Request,
    name: str,
    key: str = Query(""),
    limit: int = Query(100),
    offset: int = Query(0),
    order: str = Query(""),
    q: str = Query(""),
    select: str = Query(""),
    format: str = Query("json"),
):
    """행 조회. 필터: ?열=eq.값 | neq | gt | gte | lt | lte | like.*부분* | in.(a,b) | is.null
    ?q=검색어 는 모든 열 부분일치. ?order=열.desc,열2.asc  ?select=열,열2  ?format=csv"""
    if not data_access(request, key):
        return err("unauthorized", 401)
    with data_db() as conn:
        entry = _get_catalog(conn, name)
        if not entry:
            return err("not_found", 404)
        colnames = ["_id"] + [c["name"] for c in entry["columns"]]
        lower = {c.lower(): c for c in colnames}
        try:
            where, args = _parse_filters(request.query_params.multi_items(), colnames)
        except ValueError as e:
            return err(str(e), 400)
        if s(q):
            like = f"%{s(q)}%"
            where.append("(" + " OR ".join(f"CAST({qi(c)} AS TEXT) LIKE ?" for c in colnames[1:]) + ")")
            args.extend([like] * (len(colnames) - 1))
        sel = colnames
        if s(select):
            sel = []
            for x in select.split(","):
                c = lower.get(x.strip().lower())
                if c is None:
                    return err(f"unknown_column:{x.strip()}", 400)
                sel.append(c)
        order_sql = []
        for part in [p.strip() for p in order.split(",") if p.strip()]:
            col, _, d = part.partition(".")
            c = lower.get(col.strip().lower())
            if c is None:
                return err(f"unknown_column:{col}", 400)
            order_sql.append(f"{qi(c)} {'DESC' if d.lower() == 'desc' else 'ASC'}")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        order_clause = " ORDER BY " + (", ".join(order_sql) if order_sql else '"_id"')
        total = conn.execute(f"SELECT COUNT(*) FROM {qi(name)}{where_sql}", args).fetchone()[0]
        if format == "csv":
            lim = max(1, min(limit if limit > 100 else DATA_MAX_CSV_ROWS, DATA_MAX_CSV_ROWS))
        else:
            lim = max(1, min(limit, DATA_MAX_ROWS))
        rows = conn.execute(
            f"SELECT {', '.join(qi(c) for c in sel)} FROM {qi(name)}{where_sql}{order_clause} LIMIT ? OFFSET ?",
            args + [lim, max(0, offset)],
        ).fetchall()
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(sel)
        for r in rows:
            w.writerow(["" if v is None else v for v in r])
        return Response(
            "﻿" + buf.getvalue(),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=\"{name}.csv\""},
        )
    return {"ok": True, "table": name, "columns": sel, "total": total, "limit": lim, "offset": max(0, offset),
            "items": [dict(zip(sel, r)) for r in rows]}


def _row_values(entry, body: dict):
    lower = {c["name"].lower(): c["name"] for c in entry["columns"]}
    out = {}
    for k, v in body.items():
        if k == "_id":
            continue
        c = lower.get(str(k).lower())
        if c is None:
            raise ValueError(f"unknown_column:{k}")
        out[c] = _cell(v) if not isinstance(v, str) else _coerce_str(v)
    return out


@app.post("/api/data/tables/{name}/rows")
async def data_rows_insert(name: str, request: Request, key: str = Query("")):
    """행 추가. 본문은 {열:값} 하나 또는 {"rows":[{...},...]}."""
    actor = data_access(request, key, write=True)
    if not actor:
        return err("unauthorized", 401)
    body = await read_json(request)
    rows = body.get("rows") if isinstance(body, dict) and isinstance(body.get("rows"), list) else [body]
    with data_db() as conn:
        entry = _get_catalog(conn, name)
        if not entry:
            return err("not_found", 404)
        ids = []
        try:
            for r in rows:
                if not isinstance(r, dict):
                    return err("bad_request", 400)
                vals = _row_values(entry, r)
                if not vals:
                    cur = conn.execute(f"INSERT INTO {qi(name)} DEFAULT VALUES")
                else:
                    cur = conn.execute(
                        f"INSERT INTO {qi(name)} ({', '.join(qi(c) for c in vals)}) VALUES ({', '.join('?' * len(vals))})",
                        list(vals.values()),
                    )
                ids.append(cur.lastrowid)
        except ValueError as e:
            return err(str(e), 400)
        total = conn.execute(f"SELECT COUNT(*) FROM {qi(name)}").fetchone()[0]
        conn.execute("UPDATE _catalog SET row_count = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                     (total, now_iso(), actor, name))
    return {"ok": True, "ids": ids, "row_count": total}


@app.patch("/api/data/tables/{name}/rows/{rid}")
async def data_row_update(name: str, rid: int, request: Request, key: str = Query("")):
    actor = data_access(request, key, write=True)
    if not actor:
        return err("unauthorized", 401)
    body = await read_json(request)
    with data_db() as conn:
        entry = _get_catalog(conn, name)
        if not entry:
            return err("not_found", 404)
        try:
            vals = _row_values(entry, body)
        except ValueError as e:
            return err(str(e), 400)
        if not vals:
            return err("bad_request", 400)
        cur = conn.execute(
            f"UPDATE {qi(name)} SET {', '.join(f'{qi(c)} = ?' for c in vals)} WHERE \"_id\" = ?",
            list(vals.values()) + [rid],
        )
        if cur.rowcount == 0:
            return err("not_found", 404)
        conn.execute("UPDATE _catalog SET updated_at = ?, updated_by = ? WHERE name = ?", (now_iso(), actor, name))
    return {"ok": True}


@app.delete("/api/data/tables/{name}/rows/{rid}")
def data_row_delete(name: str, rid: int, request: Request, key: str = Query("")):
    actor = data_access(request, key, write=True)
    if not actor:
        return err("unauthorized", 401)
    with data_db() as conn:
        entry = _get_catalog(conn, name)
        if not entry:
            return err("not_found", 404)
        cur = conn.execute(f"DELETE FROM {qi(name)} WHERE \"_id\" = ?", (rid,))
        if cur.rowcount == 0:
            return err("not_found", 404)
        total = conn.execute(f"SELECT COUNT(*) FROM {qi(name)}").fetchone()[0]
        conn.execute("UPDATE _catalog SET row_count = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                     (total, now_iso(), actor, name))
    return {"ok": True, "row_count": total}


_SQL_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


_SQL_ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "table_list", "index_list", "index_info", "foreign_key_list"}


def _sql_authorizer(action, arg1, arg2, dbname, trigger):
    if action in _SQL_ALLOWED_ACTIONS:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and (arg1 or "").lower() in _SQL_ALLOWED_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql.strip().rstrip(";").strip()


def _run_select(sql: str, params):
    with data_db(readonly=True) as conn:
        conn.set_authorizer(_sql_authorizer)
        deadline = time.time() + DATA_SQL_TIMEOUT
        conn.set_progress_handler(lambda: 1 if time.time() > deadline else 0, 20000)
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(DATA_MAX_ROWS + 1)
    return cols, rows


@app.post("/api/data/sql")
async def data_sql(request: Request, key: str = Query("")):
    """읽기 전용 SQL(SELECT/WITH만). 본문 {"sql": "...", "params": [...]}. 최대 5000행."""
    if not data_access(request, key):
        return err("unauthorized", 401)
    body = await read_json(request)
    sql = _strip_sql_comments(s(body.get("sql")))
    params = body.get("params") or []
    if not isinstance(params, list):
        return err("bad_request", 400)
    if not sql or not re.match(r"^(select|with)\b", sql, re.I) or ";" in sql:
        return err("select_only", 400)
    try:
        cols, rows = await run_in_threadpool(_run_select, sql, params)
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "interrupted" in msg:
            return err("timeout", 408)
        return JSONResponse({"ok": False, "error": "sql_error", "detail": msg}, status_code=400)
    except sqlite3.DatabaseError as e:
        return JSONResponse({"ok": False, "error": "sql_error", "detail": str(e)}, status_code=400)
    truncated = len(rows) > DATA_MAX_ROWS
    rows = rows[:DATA_MAX_ROWS]
    return {"ok": True, "columns": cols, "rows": [list(r) for r in rows], "count": len(rows), "truncated": truncated}


# ─────────────────── 문서 자동판독 (햇소자 자료함) ───────────────────
# 2026-09-28 신설. 원본을 저장하지 않는다 — 메모리에서 읽고 값만 돌려준다.

@app.post("/api/docs/ingest")
async def docs_ingest(request: Request):
    """자료함이 올린 서류에서 값을 뽑아 돌려준다.

    multipart/form-data 로 `kind` + `file` 을 받는다.
    파일 없이 JSON(`{kind, name}`)만 오면 '아직 못 읽는다'로 답해 화면이 수기 입력으로 넘어가게 한다.
    """
    if post_limited(request):
        return JSONResponse({"ok": False, "error": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)

    ctype = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" not in ctype:
        # 파일이 안 온 경우 — 화면은 fields 가 없으면 수기 입력으로 폴백한다
        return JSONResponse({"ok": False, "error": "파일이 없습니다. 자료함에서 파일과 함께 보내 주세요."}, status_code=400)

    try:
        form = await request.form()
    except Exception:
        return JSONResponse({"ok": False, "error": "요청을 읽지 못했습니다."}, status_code=400)

    kind = s(form.get("kind"))
    up = form.get("file")
    if not kind or up is None or not hasattr(up, "read"):
        return JSONResponse({"ok": False, "error": "kind 와 file 이 필요합니다."}, status_code=400)

    data = await up.read()
    name = s(getattr(up, "filename", "")) or "(이름없음)"
    if not data:
        return JSONResponse({"ok": False, "error": "빈 파일입니다."}, status_code=400)

    import docs_ingest as _di
    try:
        결과 = await run_in_threadpool(_di.판독, kind, name, data)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    except Exception:
        return JSONResponse({"ok": False, "error": "판독 중 오류가 났습니다."}, status_code=500)
    finally:
        del data          # 원본은 여기서 끝이다. 디스크에 남기지 않는다

    결과["ok"] = True
    return JSONResponse(결과)


@app.get("/api/docs/kinds")
async def docs_kinds():
    """어떤 자료를 자동판독할 수 있는지 — 화면이 미리 물어볼 수 있게."""
    import docs_ingest as _di
    return {"ok": True, "지원": _di.지원유형, "최대바이트": _di.MAX_BYTES}
