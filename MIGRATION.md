# 사교원 사이트 → 가비아 자체 서버 이관 계획

**목표**: 연대지능 공동위키(`wiki.poomasi.org`)를 **제외한** 모든 사교원 계열 사이트를
가비아 자체 서버 한 대로 옮긴다. 지미(본부 GCP)·GitHub Pages·Cloudflare Pages 의존을 걷어낸다.

기본 서버 세팅·자체 API 설치는 같은 폴더 **README.md** 참고. 이 문서는 "여러 사이트를 어떻게
한 서버에 얹느냐"만 다룬다.

> ⚠️ 서버 설치·DNS 전환은 root 권한이 필요해 데카(격리 계정)가 실행할 수 없습니다.
> 아래 표·명령을 근거로 이사장님/후니님이 진행하시고, 막히면 화면을 알려주세요.

---

## 1. 사이트 인벤토리 (성격 분류)

| 사이트 | 레포 | 성격 | 현재 주소 | 이관 방식 |
|---|---|---|---|---|
| 사교원 허브 | sakyowon-hub | 정적 HTML | sakyowon.co.kr/ | 파일 서빙(**루트 유지**) |
| 사교원 본진 | sakyowon-site | 정적 HTML **+ 백엔드**(문의·AI) | sakyowon.poomasi.org | **home.sakyowon.co.kr**로 재배치 · 파일+/api(문의·AI 자체 재구현) |
| 아카데미 | academy-site | 정적 | sakyowon.co.kr/academy-site/ | 파일 서빙 |
| 고향사랑기부 | hometown-love | 정적(생성물) | sakyowon.co.kr/hometown-love/ | 파일 서빙(+2주 갱신 크론) |
| 망남마을협동조합 | mangnam-coop | Next 정적export + /api 신청 | sakyowon.co.kr/mangnam-coop/ | 파일 서빙 + /api(자체서버) |
| 망남 활력사이트 | mangnam-vitality | **Next 서버형**(better-sqlite3) | sakyowon.co.kr/mangnam-vitality/ | **vitality.sakyowon.co.kr** · Node 서비스(:3100) |
| 입찰메이트 | bid-helper | **Next 서버형**(멀티테넌트) | bid.poomasi.org | **bid.sakyowon.co.kr** · Node 서비스(:3200) |
| 팀러닝 | teamlearning-site | 정적 | sakyowon.co.kr/teamlearning-site/ | 파일 서빙 |
| 사교원 위키(Quartz) | sakyowon-wiki | 정적(Quartz build) | sakyowon.co.kr/sakyowon-wiki/ | **파일 서빙(이관 포함)** |
| ~~서남해 그랜드~~ | (외부) | 정적 | seonamhae-grand.pages.dev | **제외** |
| ~~citysafe~~ | citysafe | 정적 | (미등록) | **제외** |
| **연대지능 공동위키** | (본부) | — | **wiki.poomasi.org** | **제외 — 그대로 유지** |

**두 부류만 기억하면 됩니다.**
- **정적**: 빌드 결과 파일을 `/opt/sakyowon/www` 아래에 두면 Caddy가 그냥 서빙.
- **서버형(Node)**: `next start`를 systemd 서비스로 상시 구동하고 Caddy가 그 포트로 프록시.

---

## 2. 디렉토리·포트 배치 (권장)

```
/opt/sakyowon/
├── server/            자체 API (FastAPI)      → 127.0.0.1:8787   (/api/*)
├── www/               정적 사이트 루트
│   ├── index.html …   허브(sakyowon-hub)      → sakyowon.co.kr/
│   ├── academy-site/                          → /academy-site/
│   ├── hometown-love/                         → /hometown-love/
│   ├── mangnam-coop/                          → /mangnam-coop/
│   ├── teamlearning-site/                     → /teamlearning-site/
│   └── sakyowon-wiki/                         → /sakyowon-wiki/   (Quartz build)
└── apps/              서버형 Node 앱
    ├── mangnam-vitality/  next start          → 127.0.0.1:3100
    └── bid-helper/        next start          → 127.0.0.1:3200
```

라우팅은 `Caddyfile`(같은 폴더) 참고. 서버형 2개는 서브도메인
(`vitality.sakyowon.co.kr`, `bid.sakyowon.co.kr`)으로 두는 것을 권장 — Next 앱은
하위경로 서빙에 `basePath` 설정이 얽혀서 서브도메인이 깔끔하다.

---

## 3. 확정된 방침 (2026-08-19 이사장님 결정)

1. **도메인**: poomasi.org DNS는 건드리지 않는다. 본진·입찰은 `sakyowon.co.kr` **서브도메인으로 재배치**.
   - 본진 → `home.sakyowon.co.kr` · 입찰 → `bid.sakyowon.co.kr` · 활력 → `vitality.sakyowon.co.kr`
   - 필요 DNS: `sakyowon.co.kr`, `www`, `home`, `bid`, `vitality` 모두 A레코드를 가비아 IP로.
2. **루트(`sakyowon.co.kr/`)** = **허브** 유지.
3. **사교원 위키**(Quartz, `/sakyowon-wiki/`) = **이관 포함**(정적 파일). 제외는 `wiki.poomasi.org`(연대지능 공동위키)뿐.
4. **본진 AI** = **자체 서버에 재구현 완료**. `app.py`에 `/api/ai`(Anthropic 프록시·스트리밍), `/api/ai/chat`,
   `/api/translate`, `/api/feedback` 구현. 활성화하려면 `SAKYOWON_ANTHROPIC_KEY`(사교원 키)만 env에 넣으면 됨.
   문의(`/api/inquiries`)도 자체 서버가 처리. → 본진은 **키 투입만으로 완전 이관** 가능.
5. **seonamhae-grand, citysafe = 제외**.

### 서버형 앱 2개 준비
- **mangnam-vitality**(:3100), **bid-helper**(:3200): 가비아에 **Node.js(LTS) 설치** 후 `npm ci && npm run build`,
  systemd(`mangnam-vitality.service`/`bid-helper.service`)로 `next start` 상시 구동.
  bid는 **G2B 공공데이터 키**가 별도로 필요(`/etc/bid-helper.env`). 각 앱 SQLite(`data/app.db`+업로드)는 현 서빙본 스냅샷 이관.

---

## 4. 단계별 순서 (무중단 지향)

- **0단계** — 가비아 서버 기본 세팅 + 자체 API 가동 (README 1~5단계). `/api/health` 확인.
- **1단계(쉬움·리스크 낮음)** — `sakyowon.co.kr` 정적 일괄 이관:
  허브·academy·hometown·mangnam-coop·teamlearning·sakyowon-wiki 빌드를 `www/`에 배치 → 준비되면 DNS 전환.
- **2단계** — Node 서비스(vitality, bid) systemd 등록 + 서브도메인 프록시 + 데이터 이관.
- **3단계** — 본진(sakyowon-site) 정적을 `home.sakyowon.co.kr`로. 문의·AI는 자체 서버가 처리하므로
  `SAKYOWON_ANTHROPIC_KEY`만 넣으면 AI까지 켜짐(키 없으면 안내/폴백으로 무장애).
- **4단계** — 기존 poomasi 주소(본진·bid)는 새 주소로 안내(리다이렉트)만. `wiki.poomasi.org`는 그대로 둠.

### 각 정적 사이트 빌드/배치 메모
```bash
WWW=/opt/sakyowon/www
# 허브 (정적 파일 그대로)
git clone https://github.com/deka2026/sakyowon-hub.git   && cp -r sakyowon-hub/*      $WWW/
# academy / teamlearning (정적 HTML 레포)  ※citysafe·seonamhae는 이관 제외
#   → 각 레포 내용을 $WWW/<name>/ 로 복사
# 본진(sakyowon-site): 정적 파일을 별도 루트(예: /opt/sakyowon/www-home)에 두고
#   home.sakyowon.co.kr 로 서빙 (Caddyfile 하단 블록 참고)
# 고향사랑: 데이터 갱신형 → make_data.py + gen.py 재생성 후 결과를 $WWW/hometown-love/
# 망남마을협동조합: Next 정적 → 레포에서 `npm ci && npm run build` 후 out/ 을 $WWW/mangnam-coop/
#   (또는 gh-pages 브랜치 내용을 그대로 복사)
# 사교원 위키: Quartz → `npx quartz build` 결과(public/)를 $WWW/sakyowon-wiki/
```

### 서버형 앱 배치 메모
```bash
APPS=/opt/sakyowon/apps
# Node LTS 설치(예: nvm 또는 nodesource) 필요
git clone https://github.com/deka2026/mangnam-vitality.git $APPS/mangnam-vitality
cd $APPS/mangnam-vitality && npm ci && npm run build     # data/app.db 는 기존 스냅샷 복사
# systemd 유닛: server/mangnam-vitality.service  (포트 3100)
# bid-helper 도 동일 패턴 (포트 3200, server/bid-helper.service)
```

---

## 5. 가비아 DNS 레코드 (이 서버 IP로)

| 호스트 | 타입 | 값 | 용도 |
|---|---|---|---|
| `@` (sakyowon.co.kr) | A | 서버 IP | 허브 + 정적 + `/api` |
| `www` | A | 서버 IP | 허브 별칭 |
| `home` | A | 서버 IP | 사교원 본진 |
| `vitality` | A | 서버 IP | 망남 활력사이트 |
| `bid` | A | 서버 IP | 입찰메이트 |

`wiki.poomasi.org`(연대지능 공동위키)는 **건드리지 않는다**. seonamhae·citysafe는 이관 제외.
