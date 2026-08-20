#!/usr/bin/env bash
# Caddy 설치 + 사교원 라우팅 적용 (root로 실행)
# 사용법: bash /opt/sakyowon/src/setup-caddy.sh
set -euo pipefail

echo "[1/3] Caddy 설치"
if ! command -v caddy >/dev/null 2>&1; then
  apt -y install debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt update >/dev/null && apt -y install caddy
else
  echo "    -> 이미 설치됨: $(caddy version | head -1)"
fi

echo "[2/3] Caddyfile 적용 (1차: 메인 도메인만)"
cat >/etc/caddy/Caddyfile <<'EOF'
# 사교원 자체 서버 — 1차 라우팅 (sakyowon.co.kr 메인만)
# 서브도메인(home/vitality/bid)은 해당 앱 준비 후 활성화한다.

sakyowon.co.kr, www.sakyowon.co.kr {
	encode gzip zstd

	# 자체 API (신청·문의·AI)
	handle /api/* {
		reverse_proxy 127.0.0.1:8787
	}

	# 정적 사이트 (허브=루트, 하위폴더=각 사이트)
	# {path}.html: 위키(Quartz) 등 확장자 없는 내부링크를 GitHub Pages처럼 처리
	handle {
		root * /opt/sakyowon/www
		try_files {path} {path}.html {path}/ {path}/index.html
		file_server
	}
}
EOF
caddy validate --config /etc/caddy/Caddyfile >/dev/null && echo "    -> 설정 문법 OK"

echo "[3/3] Caddy 재시작"
systemctl enable --now caddy >/dev/null 2>&1 || true
systemctl restart caddy
sleep 2
systemctl is-active caddy && echo "Caddy 실행 중 ✅"

echo
echo "===== 로컬 확인 (도메인 헤더로 접근) ====="
curl -s -o /dev/null -w "http://localhost (Host:sakyowon.co.kr) → %{http_code} (308이면 정상: HTTPS로 유도)\n" -H "Host: sakyowon.co.kr" http://127.0.0.1/
echo
echo "다음 단계: 가비아 DNS에서 sakyowon.co.kr / www 의 A레코드를 이 서버 IP로 변경하면"
echo "Caddy가 자동으로 HTTPS 인증서를 발급합니다. (발급까지 DNS 전파 후 ~1분)"
