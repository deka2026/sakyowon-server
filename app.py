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
  GET  /api/applications?key=…          관리자 조회(JSON)
  GET  /api/applications.csv?key=…      관리자 내보내기(CSV)
  POST /api/inquiries                   문의 저장 (기존 사교원 문의폼 호환)
  GET  /api/inquiries?key=…             관리자 조회(JSON)

환경변수 (.env / systemd)
  SAKYOWON_DB          SQLite 파일 경로 (기본 ./data/sakyowon.db)
  SAKYOWON_ADMIN_KEY   관리자 조회 비밀키 (필수 — 없으면 조회 차단)
  SAKYOWON_ALLOW_ORIGINS  쉼표구분 CORS 허용 출처 (전환기용, 같은 출처면 불필요)
  SAKYOWON_TG_TOKEN / SAKYOWON_TG_CHAT   (선택) 텔레그램 신규 알림
"""

import csv
import io
import json
import os
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

app = FastAPI(title="사교원 자체 서버 API", docs_url=None, redoc_url=None)

# 같은 출처(Caddy 뒤)로 운영하면 CORS가 필요 없다.
# 전환기(정적이 아직 GitHub Pages 등 다른 출처)에는 ALLOW_ORIGINS로 열어 준다.
if ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type"],
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


# ─────────────────────── 엔드포인트 ───────────────────────

@app.get("/api/health")
def health():
    return {"ok": True, "service": "사교원 자체 서버", "ready": True, "time": now_iso()}


@app.post("/api/applications")
async def create_application(request: Request):
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
def list_applications(key: str = Query(""), program: str = Query(""), site: str = Query("")):
    ok, err = check_admin(key)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
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
def export_applications(key: str = Query("")):
    ok, err = check_admin(key)
    if not ok:
        return PlainTextResponse(err, status_code=401)
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
def list_inquiries(key: str = Query("")):
    ok, err = check_admin(key)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM inquiries ORDER BY created_at DESC").fetchall()]
    return {"ok": True, "rows": rows}


# ─────────────────── 피드백 (본진 /api/feedback) ───────────────────

@app.post("/api/feedback")
async def create_feedback(request: Request):
    body = await read_json(request)
    row_id = s(body.get("id")) or new_id("FB")
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO feedback (id, created_at, message, status, raw) VALUES (?,?,?,?,?)",
            (row_id, now_iso(), s(body.get("message") or body.get("text")), s(body.get("status")) or "미확인",
             json.dumps(body, ensure_ascii=False)),
        )
    return {"ok": True, "id": row_id}


@app.get("/api/feedback")
def list_feedback(key: str = Query("")):
    ok, err = check_admin(key)
    if not ok:
        return JSONResponse({"ok": False, "error": err}, status_code=401)
    with db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM feedback ORDER BY created_at DESC").fetchall()]
    return {"ok": True, "rows": rows}


# ─────────────────── AI (본진 기능 재구현) ───────────────────
# /api/ai       : Anthropic 메시지 API 프록시(스트리밍 포함). 키만 서버에서 주입.
# /api/ai/chat  : 관리자 도우미 — history+prompt → {answer}
# /api/translate: 한국어 번역 → {translated}
# 키는 SAKYOWON_ANTHROPIC_KEY 환경변수로만 주입한다(코드/깃에 없음).

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
    """비스트리밍 호출 → (status_code, response_bytes). 응답은 Anthropic 원문 그대로."""
    resp, status = _anthropic_request({**body, "stream": False})
    data = resp.read()
    return status, data


def _anthropic_text(data_bytes: bytes) -> str:
    try:
        d = json.loads(data_bytes)
    except Exception:
        return ""
    content = d.get("content")
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


@app.post("/api/ai")
async def ai_proxy(request: Request):
    body = await read_json(request)
    if not ANTHROPIC_KEY:
        return JSONResponse({"error": {"message": "AI 키(SAKYOWON_ANTHROPIC_KEY)가 설정되지 않았습니다."}})
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
    if not ANTHROPIC_KEY:
        return {"answer": "AI 기능이 아직 연결되지 않았습니다. (서버에 SAKYOWON_ANTHROPIC_KEY 미설정)"}
    prompt = s(body.get("prompt"))
    context = s(body.get("context"))
    history = body.get("history") if isinstance(body.get("history"), list) else []
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
        return {"answer": "(응답을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.)"}
    return {"answer": answer}


@app.post("/api/translate")
async def translate(request: Request):
    body = await read_json(request)
    text = s(body.get("text"))
    if not text:
        return {"translated": ""}
    if not ANTHROPIC_KEY:
        # 키가 없으면 프론트가 MyMemory 공용 API로 폴백하도록 실패를 알린다.
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
