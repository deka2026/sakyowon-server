# 서버에만 있던 코드를 git 안으로 — 직원전용 질문·그룹웨어

사교원 서버의 일부 코드는 레포에 없고 `/opt/sakyowon/` 아래에만 있었다. 이 문서는 그것을
**이 레포로 회수해 버전관리하고, 다시 서버로 배포하는 절차**다. 서버 설치 자체는 `README.md`,
사이트 이관 계획은 `MIGRATION.md` 를 본다.

## 왜 git 안으로 넣나

2026-09-29, 직원전용 질문 라우터를 `app.py` 끝에 붙여 놓았는데 같은 시각 다른 작업이 그 파일을
통째로 다시 쓰면서 **오류 하나 없이 사라졌다.** 파일 크기만 늘어 있어서 한참 뒤에야 알았다.
그때 라우터를 `app_staff.py` 로 갈라 두어 다시는 덮이지 않게 했지만, 정작 그 파일들이 git 밖에
있어서 **이력도, 백업도, 되돌리기도 없는 상태**는 그대로였다.

git 안에 들어오면 세 가지가 생긴다 — 누가 언제 무엇을 고쳤는지, 잘못됐을 때 돌아갈 자리,
그리고 서버에 들어가지 않고도 코드를 읽고 고칠 수 있는 길.

## 무엇이 들어오나

대응표는 **`server-files.sh` 한 곳**에 있다. 회수와 배포가 같은 표를 읽으므로, 새 파일을 넣고
싶으면 거기 한 줄만 보태면 양방향이 따라온다.

| 레포 | 서버 | 하는 일 |
|---|---|---|
| `staff/staff_kb.py` | `/opt/sakyowon/server/staff_kb.py` | 사내 자료 색인기 — 개인정보 삭제·조각 나누기·한국어 검색(FTS5) |
| `staff/staff_qa.py` | `/opt/sakyowon/server/staff_qa.py` | 질문·문서 API 라우터, 엔진 호출 |
| `staff/app_staff.py` | `/opt/sakyowon/server/app_staff.py` | 진입점 — `app.py` 위에 라우터만 얹는다 |
| `staff/임시비번.py` | `/opt/sakyowon/server/임시비번.py` | 임시 비밀번호 발급 |
| `staff/hub_sso.py` | `/opt/sakyowon/hub_sso.py` | 허브 통합계정 SSO 공용 모듈 |
| `gw/` | `/opt/sakyowon/gw/` | 그룹웨어(FastAPI :8794). `gw/static/staff.html` 이 직원전용 질문창 **정본** |

서비스는 `sakyowon-api.service`(:8787, `uvicorn app_staff:app`)와 `sakyowon-gw.service`(:8794)다.
두 유닛이 시스템 서비스인지 사용자 서비스(`systemctl --user`)인지는 스크립트가 알아서 가린다.

## 최초 1회 — 서버에서 회수

```bash
# 레포 클론이 아직 없다면
sudo git clone https://github.com/deka2026/sakyowon-server.git /opt/sakyowon/src

cd /opt/sakyowon/src
git fetch origin && git checkout <이 브랜치>
bash import-from-server.sh                 # 가져오기만 — 무엇이 들어왔는지 확인
bash import-from-server.sh --commit --push # 확인됐으면 커밋·푸시
```

서버에 없는 파일은 건너뛰고 알려 준다. 경로가 다르면 `server-files.sh` 를 고친다.

## 평소 흐름

**레포에서 고쳐 서버로** (권장)

```bash
cd /opt/sakyowon/src
bash deploy-server-code.sh --dry-run   # 무엇이 바뀌는지 먼저 본다
bash deploy-server-code.sh             # git pull → 문법검사 → 백업 → 배포 → 재시작 → 헬스체크
```

**서버에서 급히 고쳤을 때** — 그대로 두면 다음 배포가 덮어쓴다. 바로 회수한다.

```bash
bash /opt/sakyowon/src/import-from-server.sh --commit --push
```

## 안전장치

배포는 네 단계로 막는다.

1. **문법 검사** — 올리기 전에 파이썬 문법을 본다. 죽는 코드는 서버에 닿지 않는다.
2. **백업** — 바꾸는 파일을 `/opt/sakyowon/backup/<시각>/` 에 통째로 남긴다.
3. **헬스체크** — 재시작 뒤 `:8787/api/health` 와 `:8794/` 를 찔러 본다.
4. **자동 되돌리기** — 헬스체크가 실패하면 백업으로 복구하고 다시 올린 뒤 **0이 아닌 코드로 끝난다.**

회수에도 두 가지를 막아 둔다.

- **비밀 검사** — 앤트로픽 키(`sk-ant-…`)·터널 토큰(`eyJ…`)·개인키·코드에 박힌 키·주민등록번호가
  보이면 **가져온 것을 되돌리고 멈춘다.** 값은 코드가 아니라 `/etc/sakyowon-api.env` 에 둔다.
- **덩치 검사** — 5MB 넘는 파일이 섞이면 멈춘다. 색인 DB(`staff_kb.db`, 476MB)가 딸려오는 사고를 막는다.

## git 에 넣지 않는 것

`.gitignore` 와 `server-files.sh` 의 `EXCLUDES` 가 함께 막는다.

- **비밀** — `.env`, `/etc/sakyowon-api.env`
- **데이터** — `*.db`(색인·회원·신청), `data/`
- **흔적** — `*.bak_*`, `__pycache__`, `*.log`, `.venv/`, 업로드 파일

배포는 레포에 없는 서버 파일을 지우지 않는다. 업로드·설정처럼 서버에서만 자라는 것은 그대로 산다.

## 되돌리기

```bash
# 회수를 물리기
git -C /opt/sakyowon/src reset --hard <회수 전 커밋>     # 스크립트가 끝에 알려 준다

# 배포를 물리기 (헬스체크는 통과했는데 뒤늦게 문제를 봤을 때)
sudo cp -a /opt/sakyowon/backup/<시각>/opt/sakyowon/. /opt/sakyowon/
sudo systemctl restart sakyowon-api sakyowon-gw        # 사용자 서비스면 systemctl --user
```

## 아직 git 밖에 있는 것

같은 방법으로 넣을 수 있다 — `server-files.sh` 에 줄을 보태고 회수하면 된다.

| 서버 경로 | 무엇 | 주의 |
|---|---|---|
| `/opt/sakyowon/home/server.py` | 사교원 홈페이지(home.sakyowon.co.kr) 서버 | 화면은 `haeory-cyber/sakyowon-site` 에 있고 서버 코드만 밖에 있다 |
| `/opt/sakyowon/hr/` | 인사기록(hr.sakyowon.co.kr) | 개인정보를 다루므로 회수 전 비밀 검사 결과를 특히 꼼꼼히 볼 것 |
| `/opt/sakyowon/www-src/server.py` | 정적 사이트 앞단 프록시 | |
| `/opt/sakyowon/gw/개통.py` | 클라우드플레어 터널 개통 | **터널 토큰이 코드에 박혀 있으면 회수가 멈춘다.** 환경변수로 먼저 뺄 것 |
| `sakyowon-gw.service` · `sakyowon-staff-kb.service`/`.timer` | systemd 유닛 | 시스템 유닛인지 사용자 유닛인지 확인한 실제 경로를 표에 적는다 |
