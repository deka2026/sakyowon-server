"""
사교원 자체 서버 — 신청·문의 백엔드 (FastAPI + SQLite)

가비아 서버에 올려 sakyowon.co.kr의 /api/* 를 처리한다.
정적 사이트(허브·망남·고향사랑 등)는 Caddy가 서빙하고, Caddy가 /api/* 만 이 서버로
리버스 프록시한다. 그래서 사이트와 API가 '같은 출처'가 되어 브라우저 CORS 문제가 없다.

설치·배포 절차는 같은 폴더의 README.md 참고.

의존성: fastapi, uvicorn  (DB는 파이썬 표준 sqlite3만 사용)

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
from datetime import datetime, timezone

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
        allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
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
    body = await read_json(request)
    prob = anthropic_key_problem()
    if prob:
        return JSONResponse({"error": {"message": f"AI 키(SAKYOWON_ANTHROPIC_KEY) 문제: {prob}"}})
    if AI_MODEL:
        body["model"] = AI_MODEL  # 클라이언트가 보낸 옛 모델 ID를 현재 모델로 통일

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
        vref = village_ref_of_user(current_user(request))
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
    areq = {"model": AI_MODEL, "max_tokens": 1500, "system": system, "messages": messages}
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
