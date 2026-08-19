# 사교원 자체 서버 (가비아) — 설치·운영 가이드

지미(본부 GCP)를 거치지 않고 **사교원이 직접** 신청·문의 데이터를 받기 위한 자체 서버입니다.
`sakyowon.co.kr`을 이 서버로 향하게 하고, Caddy가 `/api/*`는 FastAPI로, 나머지는 정적 사이트로
서빙합니다. 사이트와 API가 **같은 출처**가 되므로 브라우저 CORS 문제가 없습니다.

```
브라우저 ──▶ Caddy(443, sakyowon.co.kr)
                 ├─ /api/*  ──▶ FastAPI(127.0.0.1:8787)  ──▶ SQLite
                 └─ 그 외    ──▶ 정적 사이트(허브·망남·고향사랑…)
```

구성 파일: `app.py`(서버) · `requirements.txt` · `sakyowon-api.service`(systemd) ·
`Caddyfile` · `.env.example`.

---

## 0. 준비물

- 가비아 서버(리눅스, Ubuntu 22.04/24.04 기준). SSH 접속 가능.
- 도메인 `sakyowon.co.kr` DNS 관리 권한.
- 서버 공인 IP 한 개.

> 이 서버 설치는 **root/sudo가 필요**해 격리 계정(데카)이 대신 실행할 수 없습니다.
> 아래 명령을 이사장님/후니님이 SSH에서 순서대로 붙여넣으시면 됩니다. 막히면 화면을 알려주세요.

---

## 1. DNS 연결

가비아 DNS 관리에서 A 레코드를 서버 IP로 지정합니다.

| 호스트 | 타입 | 값 |
|---|---|---|
| `@` (sakyowon.co.kr) | A | 서버 공인 IP |
| `www` | A | 서버 공인 IP |

> 지금 `sakyowon.co.kr`은 GitHub Pages를 가리키고 있습니다. A 레코드를 가비아 IP로 바꾸는
> 순간부터 이 서버가 응답합니다. 전파에 몇 분~수십 분 걸릴 수 있습니다.
> (전환 전에 4번 정적 배치를 먼저 끝내 두면 무중단에 가깝게 넘어갑니다.)

---

## 2. 서버 기본 세팅 (SSH, root 또는 sudo)

```bash
# 패키지 최신화 + 파이썬/도구
sudo apt update && sudo apt -y upgrade
sudo apt -y install python3 python3-venv python3-pip git ufw

# 전용 계정 생성(권장)
sudo useradd -r -m -d /opt/sakyowon -s /usr/sbin/nologin sakyowon || true
sudo mkdir -p /opt/sakyowon/server /opt/sakyowon/data /opt/sakyowon/www

# 방화벽: SSH + 웹만 개방
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw --force enable
```

## 3. API 서버 설치

```bash
# 이 server/ 폴더를 서버로 복사 (아래는 git 사용 예)
sudo git clone https://github.com/haeory-cyber/sakyowon-site.git /tmp/sakyowon-site
sudo cp /tmp/sakyowon-site/server/app.py /opt/sakyowon/server/
sudo cp /tmp/sakyowon-site/server/requirements.txt /opt/sakyowon/server/

# 가상환경 + 의존성
sudo python3 -m venv /opt/sakyowon/server/.venv
sudo /opt/sakyowon/server/.venv/bin/pip install -U pip
sudo /opt/sakyowon/server/.venv/bin/pip install -r /opt/sakyowon/server/requirements.txt

# 환경변수 파일 작성 (관리자 키는 길고 무작위하게!)
sudo tee /etc/sakyowon-api.env >/dev/null <<'ENV'
SAKYOWON_DB=/opt/sakyowon/data/sakyowon.db
SAKYOWON_ADMIN_KEY=여기에-길고-무작위한-키
# 같은 출처 서빙이면 CORS 불필요. 전환기 임시 개방이 필요하면 주석 해제:
# SAKYOWON_ALLOW_ORIGINS=https://sakyowon.co.kr,https://deka2026.github.io
ENV
sudo chmod 600 /etc/sakyowon-api.env

# 소유권 정리 + 서비스 등록
sudo chown -R sakyowon:sakyowon /opt/sakyowon
sudo cp /tmp/sakyowon-site/server/sakyowon-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sakyowon-api

# 동작 확인 (로컬)
curl -s http://127.0.0.1:8787/api/health
# → {"ok":true,"service":"사교원 자체 서버",...}
```

## 4. 정적 사이트 배치 (같은 출처 서빙)

각 사이트 빌드 결과를 `/opt/sakyowon/www` 아래에 둡니다. 허브는 루트, 하위 사이트는
하위 폴더로. (예: 망남은 `/mangnam-coop`, 고향사랑은 `/hometown-love`)

```bash
# 예시: 이미 빌드된 gh-pages 결과물을 그대로 가져오기
sudo git clone -b gh-pages https://github.com/deka2026/mangnam-coop.git /tmp/mn
sudo mkdir -p /opt/sakyowon/www/mangnam-coop
sudo cp -r /tmp/mn/. /opt/sakyowon/www/mangnam-coop/
# 허브(루트)도 동일하게 sakyowon-hub 등을 /opt/sakyowon/www/ 로
sudo chown -R sakyowon:sakyowon /opt/sakyowon/www
```

> 전환기에는 Caddyfile의 (B) 대안(정적을 GitHub Pages로 프록시)을 써서 먼저 API만 붙이고,
> 정적은 나중에 옮겨도 됩니다.

## 5. Caddy 설치 + 실행

```bash
sudo apt -y install debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt -y install caddy

# Caddyfile 배치 (도메인 A레코드가 이 서버를 가리킨 뒤에 실행해야 인증서가 발급됨)
sudo cp /tmp/sakyowon-site/server/Caddyfile /etc/caddy/Caddyfile
sudo systemctl restart caddy

# 외부 확인
curl -s https://sakyowon.co.kr/api/health
```

---

## 6. 확인·운영

```bash
# 서비스 상태/로그
sudo systemctl status sakyowon-api
sudo journalctl -u sakyowon-api -f

# 신청 데이터 조회(관리자 키 필요)
curl -s "https://sakyowon.co.kr/api/applications?key=관리자키" | head
# CSV 내려받기
curl -s "https://sakyowon.co.kr/api/applications.csv?key=관리자키" -o applications.csv

# 백업: SQLite 파일 하나만 복사하면 됨
sudo cp /opt/sakyowon/data/sakyowon.db ~/sakyowon-backup-$(date +%F).db
```

### 코드 업데이트
```bash
sudo git -C /tmp/sakyowon-site pull
sudo cp /tmp/sakyowon-site/server/app.py /opt/sakyowon/server/
sudo systemctl restart sakyowon-api
```

---

## API 규약 (프론트가 이 형식으로 보냄)

**신청** — `POST /api/applications` (JSON)
```json
{
  "site": "mangnam-coop",
  "program": "teen | senior | youth",
  "programLabel": "연두교실 · 청소년 스킴보드 캠프",
  "name": "홍길동", "phone": "010-...", "email": "",
  "detailsText": "보호자 성함: ...\n수영 가능 여부: ...",
  "note": "문의·요청"
}
```
응답 `{ "ok": true, "id": "APP-..." }`. `name`·`phone`만 필수, 나머지는 자유.
`detailsText`가 없으면 서버가 남은 필드를 사람이 읽게 정리해 저장합니다.

**문의** — `POST /api/inquiries` : `{ name, contact, org, category, message, source }`
(기존 사교원 문의폼 페이로드와 호환)

**관리자 조회** — `GET /api/applications?key=…[&program=&site=]`,
`GET /api/applications.csv?key=…`, `GET /api/inquiries?key=…`

**AI (본진 기능 재구현)** — 사교원 Anthropic 키(`SAKYOWON_ANTHROPIC_KEY`)를 넣으면 활성:
- `POST /api/ai` — Anthropic 메시지 API 프록시(스트리밍 지원). 본문 `{model,max_tokens,system,messages,stream}` 그대로 전달, 서버가 키만 주입하고 모델은 `SAKYOWON_AI_MODEL`로 통일.
- `POST /api/ai/chat` — 관리자 도우미. `{context,prompt,history[]}` → `{answer}`.
- `POST /api/translate` — `{text,target}` → `{translated}` (미설정 시 503 → 프론트가 MyMemory로 폴백).
- `POST /api/feedback` — 피드백 저장. `GET /api/feedback?key=…` 조회.

> 키는 코드/깃에 두지 않고 `/etc/sakyowon-api.env` 에만 넣습니다. 키가 없으면 AI는
> 안내 메시지/폴백으로 안전하게 동작합니다(장애 없음).
