#!/usr/bin/env bash
# Node 서버형 앱 설치: 망남 활력사이트 (vitality.sakyowon.co.kr, :3100)
# 사용법: bash /opt/sakyowon/src/setup-apps.sh   (root, 여러 번 실행해도 안전)
set -euo pipefail

APPS=/opt/sakyowon/apps
APP=$APPS/mangnam-vitality

echo "[1/5] Node.js 20 LTS 설치"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash - >/dev/null
  apt -y install nodejs >/dev/null
fi
echo "    -> node $(node --version) / npm $(npm --version)"

echo "[2/5] 앱 코드 내려받기/갱신"
if [ -d "$APP/.git" ]; then
  git config --global --add safe.directory "$APP" 2>/dev/null || true
  git -C "$APP" pull -q
else
  git clone -q https://github.com/deka2026/mangnam-vitality.git "$APP"
fi

echo "[3/5] 의존성 설치 + 빌드 (수 분 소요)"
cd "$APP"
npm ci --no-audit --no-fund 2>&1 | tail -1
npm run build 2>&1 | tail -3
mkdir -p "$APP/data"   # DB 폴더 (지미 스냅샷 오면 data/app.db 교체)

echo "[4/5] systemd 서비스 등록"
cp /opt/sakyowon/src/mangnam-vitality.service /etc/systemd/system/
chown -R sakyowon:sakyowon "$APPS"
systemctl daemon-reload
systemctl enable --now mangnam-vitality
sleep 3
systemctl is-active mangnam-vitality && echo "vitality 서비스 실행 중 ✅"

echo "[5/5] Caddy에 vitality 서브도메인 추가"
if ! grep -q "vitality.sakyowon.co.kr" /etc/caddy/Caddyfile; then
  cat >>/etc/caddy/Caddyfile <<'EOF'

# 망남 활력사이트 (서버형 본편)
vitality.sakyowon.co.kr {
	encode gzip zstd
	reverse_proxy 127.0.0.1:3100
}
EOF
  caddy validate --config /etc/caddy/Caddyfile >/dev/null && systemctl reload caddy
  echo "    -> Caddy 블록 추가·reload 완료"
else
  echo "    -> 이미 등록돼 있음"
fi

echo
echo "===== 로컬 확인 ====="
curl -s -o /dev/null -w "127.0.0.1:3100 → %{http_code}\n" --max-time 10 http://127.0.0.1:3100/
echo
echo "다음: 가비아 DNS에 A레코드 [호스트 vitality → 1.201.116.225] 추가하면"
echo "https://vitality.sakyowon.co.kr 이 자동 HTTPS로 열립니다."