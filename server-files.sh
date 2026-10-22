#!/usr/bin/env bash
# 서버에만 있던 코드(직원전용 질문·그룹웨어)의 레포 ↔ 서버 대응표.
#
# 회수(import-from-server.sh)와 배포(deploy-server-code.sh)가 이 파일 하나를 함께 읽는다.
# 새 파일을 git 관리에 넣으려면 여기 한 줄만 보태면 양방향이 같이 따라온다.
# (이 파일은 직접 실행하지 않는다 — source 전용)

BASE="${BASE:-/opt/sakyowon}"

# ── 낱개 파일: "레포경로|서버경로" ─────────────────────────────
# 직원전용 질문의 속살. 2026-09-29 레슨대로 app.py 에 덧붙이지 않고
# app_staff.py 가 app.py 위에 라우터만 얹는다 — app.py 가 어떻게 바뀌어도 이 기능은 산다.
SERVER_FILES=(
  "staff/staff_kb.py|$BASE/server/staff_kb.py"      # 사내 자료 색인기(개인정보 삭제·FTS5 검색)
  "staff/staff_qa.py|$BASE/server/staff_qa.py"      # 질문·문서 API 라우터(엔진 호출)
  "staff/app_staff.py|$BASE/server/app_staff.py"    # 진입점 — uvicorn app_staff:app
  "staff/임시비번.py|$BASE/server/임시비번.py"        # 임시 비밀번호 발급 도구
  "staff/hub_sso.py|$BASE/hub_sso.py"               # 허브 통합계정 SSO 공용 모듈
)

# ── 폴더 통째: "레포경로|서버경로" ─────────────────────────────
SERVER_DIRS=(
  "gw|$BASE/gw"                                     # 그룹웨어(FastAPI :8794) — static/staff.html 이 질문창 정본
)

# ── 폴더를 옮길 때 빼는 것 (비밀·데이터·찌꺼기) ────────────────
EXCLUDES=(
  ".git" ".venv" "venv" "__pycache__" "*.pyc"
  "data" "*.db" "*.db-wal" "*.db-shm"
  ".env" "*.env" "*.bak" "*.bak_*" "*.old"
  "*.log" "logs" "node_modules" "uploads" "tmp"
)

# ── 다루는 서비스 (배포 뒤 재시작·헬스체크 대상) ──────────────
#    "유닛이름|헬스체크URL|기대코드(쉼표구분, 빈칸이면 코드 검사 생략)"
SERVER_SERVICES=(
  "sakyowon-api.service|http://127.0.0.1:8787/api/health|200"
  "sakyowon-gw.service|http://127.0.0.1:8794/|200,301,302,303,307,308,401,403"
)
