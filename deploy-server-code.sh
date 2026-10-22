#!/usr/bin/env bash
# 레포 → 서버 배포 (직원전용 질문·그룹웨어)
#
# 사용법 (서버에서):
#   bash /opt/sakyowon/src/deploy-server-code.sh              # git pull → 배포 → 재시작 → 확인
#   bash /opt/sakyowon/src/deploy-server-code.sh --no-pull    # 지금 레포 내용 그대로 배포
#   bash /opt/sakyowon/src/deploy-server-code.sh --dry-run    # 무엇이 바뀌는지만 보기
#
# 안전장치 셋:
#   1) 올리기 전에 파이썬 문법을 먼저 본다 (죽는 코드를 올리지 않는다)
#   2) 바꾸기 전 파일을 /opt/sakyowon/backup/<시각>/ 에 남긴다
#   3) 재시작 뒤 헬스체크가 실패하면 **자동으로 되돌리고 다시 올린다**
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
source "$SRC/server-files.sh"

do_pull=1; dry=0
for a in "$@"; do
  case "$a" in
    --no-pull) do_pull=0 ;;
    --dry-run) dry=1; do_pull=0 ;;
    *) echo "모르는 옵션: $a"; exit 2 ;;
  esac
done

SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BASE/backup/$STAMP"

# 유닛이 사용자 서비스인지 시스템 서비스인지 가려 쓴다 (서버는 둘을 섞어 쓴다)
unit_scope() {
  if systemctl --user cat "$1" >/dev/null 2>&1; then echo user
  elif systemctl cat "$1" >/dev/null 2>&1; then echo system
  else echo none; fi
}
ctl() { # ctl <유닛> <동작…>
  local unit="$1"; shift
  case "$(unit_scope "$unit")" in
    user)   systemctl --user "$@" "$unit" ;;
    system) $SUDO systemctl "$@" "$unit" ;;
    none)   echo "    (유닛 $unit 없음 — 건너뜀)"; return 0 ;;
  esac
}

echo "[1/6] 레포 최신화"
git config --global --add safe.directory "$SRC" 2>/dev/null || true
if [ "$do_pull" -eq 1 ]; then
  git -C "$SRC" pull --ff-only
else
  echo "    건너뜀 (--no-pull)"
fi
echo "    $(git -C "$SRC" rev-parse --short HEAD)  $(git -C "$SRC" log -1 --format=%s)"

echo "[2/6] 파이썬 문법 검사"
pyfiles=()
for line in "${SERVER_FILES[@]}"; do
  repo="${line%%|*}"; [ -f "$SRC/$repo" ] && [[ "$repo" == *.py ]] && pyfiles+=("$SRC/$repo")
done
for line in "${SERVER_DIRS[@]}"; do
  repo="${line%%|*}"
  [ -d "$SRC/$repo" ] && while IFS= read -r f; do pyfiles+=("$f"); done < <(find "$SRC/$repo" -name '*.py' -type f)
done
if [ ${#pyfiles[@]} -gt 0 ]; then
  PY="$BASE/server/.venv/bin/python"; [ -x "$PY" ] || PY=python3
  # py_compile 은 레포에 __pycache__ 를 남긴다 — 문법만 보고 아무것도 쓰지 않는다
  "$PY" -c 'import sys
for f in sys.argv[1:]:
    compile(open(f, "rb").read(), f, "exec")' "${pyfiles[@]}"
  echo "    ${#pyfiles[@]}개 파일 통과"
else
  echo "    검사할 파이썬 파일 없음"
fi

echo "[3/6] 바뀌는 것"
changed=0
show_diff() { # show_diff <레포경로> <서버경로>
  if [ ! -e "$2" ]; then echo "    새로 설치: $2"; changed=1
  elif ! diff -rq "$1" "$2" >/dev/null 2>&1; then
    # diff 는 다를 때 1 을 돌려준다 — pipefail 과 set -e 에 걸리지 않게 받아 둔다
    echo "    바뀜: $2"; { diff -rq "$1" "$2" 2>/dev/null || true; } | sed 's/^/        /' | head -20; changed=1
  fi
}
for line in "${SERVER_FILES[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  [ -f "$SRC/$repo" ] && show_diff "$SRC/$repo" "$srv"
done
for line in "${SERVER_DIRS[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  [ -d "$SRC/$repo" ] && show_diff "$SRC/$repo" "$srv"
done
[ "$changed" -eq 0 ] && { echo "    바뀌는 것이 없습니다. 끝."; exit 0; }
[ "$dry" -eq 1 ] && { echo; echo "(--dry-run 이라 여기서 멈춥니다)"; exit 0; }

echo "[4/6] 백업 → $BACKUP"
$SUDO mkdir -p "$BACKUP"
for line in "${SERVER_FILES[@]}"; do
  srv="${line##*|}"
  [ -f "$srv" ] && { $SUDO mkdir -p "$BACKUP$(dirname "$srv")"; $SUDO cp -p "$srv" "$BACKUP$srv"; }
done
for line in "${SERVER_DIRS[@]}"; do
  srv="${line##*|}"
  [ -d "$srv" ] && { $SUDO mkdir -p "$BACKUP$srv"; $SUDO cp -a "$srv/." "$BACKUP$srv/"; }
done
echo "    남김"

echo "[5/6] 배포 + 재시작"
install_one() { # install_one <레포경로> <서버경로>
  local from="$1" to="$2" owner=""
  [ -e "$to" ] && owner="$(stat -c '%U:%G' "$to" 2>/dev/null || true)"
  if [ -d "$from" ]; then
    $SUDO mkdir -p "$to"
    $SUDO cp -a "$from/." "$to/"        # 레포에 없는 서버 파일(업로드·설정)은 지우지 않는다
  else
    $SUDO mkdir -p "$(dirname "$to")"
    $SUDO cp -p "$from" "$to"
  fi
  [ -n "$owner" ] && $SUDO chown -R "$owner" "$to" 2>/dev/null || true
}
for line in "${SERVER_FILES[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  [ -f "$SRC/$repo" ] && { install_one "$SRC/$repo" "$srv"; echo "    -> $srv"; }
done
for line in "${SERVER_DIRS[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  [ -d "$SRC/$repo" ] && { install_one "$SRC/$repo" "$srv"; echo "    -> $srv/"; }
done
for line in "${SERVER_SERVICES[@]}"; do
  unit="${line%%|*}"
  echo "    재시작 $unit"; ctl "$unit" restart || true
done
sleep 4

echo "[6/6] 헬스체크"
bad=0; checked=0
for line in "${SERVER_SERVICES[@]}"; do
  IFS='|' read -r unit url want <<<"$line"
  [ "$(unit_scope "$unit")" = none ] && continue
  checked=$((checked + 1))
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" || echo 000)"
  if [ -z "$want" ] || [[ ",$want," == *",$code,"* ]]; then
    echo "    OK   $unit  $url -> $code"
  else
    echo "    실패 $unit  $url -> $code (기대: $want)"; bad=1
  fi
done

[ "$checked" -eq 0 ] && echo "    확인할 유닛이 없습니다 (서비스가 아직 등록되지 않은 서버입니다)"

if [ "$bad" -eq 1 ]; then
  echo
  echo "[되돌리기] 헬스체크가 실패해 백업으로 복구합니다."
  for line in "${SERVER_FILES[@]}"; do
    srv="${line##*|}"; [ -f "$BACKUP$srv" ] && $SUDO cp -p "$BACKUP$srv" "$srv"
  done
  for line in "${SERVER_DIRS[@]}"; do
    srv="${line##*|}"; [ -d "$BACKUP$srv" ] && $SUDO cp -a "$BACKUP$srv/." "$srv/"
  done
  for line in "${SERVER_SERVICES[@]}"; do ctl "${line%%|*}" restart || true; done
  sleep 4
  for line in "${SERVER_SERVICES[@]}"; do
    IFS='|' read -r unit url want <<<"$line"
    [ "$(unit_scope "$unit")" = none ] && continue
    echo "    복구 후 $unit -> $(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$url" || echo 000)"
  done
  echo
  echo "원인 보기:  journalctl -u <유닛> -n 50 --no-pager   (사용자 서비스면 --user 를 붙입니다)"
  echo "백업 자리:  $BACKUP"
  exit 1
fi

echo
echo "배포 완료. 백업은 $BACKUP 에 있습니다."
