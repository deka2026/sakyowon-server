#!/usr/bin/env bash
# 사교원 자체 서버 설치 스크립트 (Ubuntu, root로 실행)
# 사용법:
#   git clone https://github.com/deka2026/sakyowon-server.git /opt/sakyowon/src
#   bash /opt/sakyowon/src/setup.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
BASE=/opt/sakyowon
SRV="$BASE/server"

echo "[1/6] 서버 코드 배치"
mkdir -p "$SRV" "$BASE/data" "$BASE/www" "$BASE/www-home" "$BASE/apps"
cp "$SRC/app.py" "$SRC/requirements.txt" "$SRV/"

echo "[2/6] 파이썬 가상환경 + 의존성 설치 (1~2분)"
python3 -m venv "$SRV/.venv"
"$SRV/.venv/bin/pip" install -q -U pip
"$SRV/.venv/bin/pip" install -q -r "$SRV/requirements.txt"

echo "[3/6] 전용 계정(sakyowon) 준비"
id sakyowon >/dev/null 2>&1 || useradd -r -s /usr/sbin/nologin sakyowon

echo "[4/6] 환경변수 파일 (/etc/sakyowon-api.env)"
if [ ! -f /etc/sakyowon-api.env ]; then
  KEY=$(openssl rand -hex 16 2>/dev/null || tr -dc 'a-f0-9' </dev/urandom | head -c 32)
  cat >/etc/sakyowon-api.env <<EOF
SAKYOWON_DB=$BASE/data/sakyowon.db
SAKYOWON_ADMIN_KEY=$KEY
EOF
  chmod 600 /etc/sakyowon-api.env
  echo "    -> 새 관리자 키를 생성했습니다."
else
  echo "    -> 기존 /etc/sakyowon-api.env 를 유지합니다."
fi

echo "[5/6] 권한 정리 + 서비스 등록"
chown -R sakyowon:sakyowon "$BASE"
cp "$SRC/sakyowon-api.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now sakyowon-api

echo "[6/6] 상태 확인"
sleep 2
echo "----- /api/health 응답 -----"
curl -s http://127.0.0.1:8787/api/health || echo "(응답 없음 — 아래 로그 확인)"
echo
echo
echo "======================================================"
echo " 설치 완료! 관리자 키(SAKYOWON_ADMIN_KEY)를 안전히 보관하세요:"
grep SAKYOWON_ADMIN_KEY /etc/sakyowon-api.env
echo "======================================================"
echo "문제 시 로그 보기:  journalctl -u sakyowon-api -n 30 --no-pager"
