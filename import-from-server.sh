#!/usr/bin/env bash
# 서버 → 레포 회수 (git 밖에 있던 직원전용 질문·그룹웨어 코드를 git 안으로)
#
# 사용법 (서버에서, 레포 클론이 /opt/sakyowon/src 에 있다고 보고):
#   bash /opt/sakyowon/src/import-from-server.sh             # 가져오기만 (git status 로 확인)
#   bash /opt/sakyowon/src/import-from-server.sh --commit    # 가져온 뒤 커밋까지
#   bash /opt/sakyowon/src/import-from-server.sh --commit --push
#
# 비밀(API 키·터널 토큰·개인키·주민등록번호)이 섞이면 **가져온 것을 되돌리고 멈춘다**.
# 비밀은 코드가 아니라 /etc/sakyowon-api.env 같은 환경변수 파일에만 둔다.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
source "$SRC/server-files.sh"

do_commit=0; do_push=0; force=0
for a in "$@"; do
  case "$a" in
    --commit) do_commit=1 ;;
    --push)   do_commit=1; do_push=1 ;;
    --force)  force=1 ;;            # 비밀 검사에 걸린 것이 오탐일 때만
    *) echo "모르는 옵션: $a"; exit 2 ;;
  esac
done

git config --global --add safe.directory "$SRC" 2>/dev/null || true
cd "$SRC"
before="$(git rev-parse HEAD)"
got=(); skipped=()

echo "[1/4] 낱개 파일 가져오기"
for line in "${SERVER_FILES[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  if [ -f "$srv" ]; then
    mkdir -p "$(dirname "$SRC/$repo")"
    cp -p "$srv" "$SRC/$repo"
    got+=("$repo")
    echo "    + $repo  <-  $srv"
  else
    skipped+=("$repo (서버에 없음: $srv)")
    echo "    - $repo  건너뜀 — $srv 없음"
  fi
done

echo "[2/4] 폴더 가져오기 (비밀·데이터·찌꺼기 제외)"
tar_ex=(); for p in "${EXCLUDES[@]}"; do tar_ex+=(--exclude="$p"); done
for line in "${SERVER_DIRS[@]}"; do
  repo="${line%%|*}"; srv="${line##*|}"
  if [ -d "$srv" ]; then
    rm -rf "${SRC:?}/$repo"            # 서버에서 지운 파일이 레포에 남지 않게
    mkdir -p "$SRC/$repo"
    tar -C "$srv" "${tar_ex[@]}" -cf - . | tar -C "$SRC/$repo" -xf -
    got+=("$repo")
    echo "    + $repo/  <-  $srv/  ($(find "$SRC/$repo" -type f | wc -l)개 파일)"
  else
    skipped+=("$repo/ (서버에 없음: $srv)")
    echo "    - $repo/  건너뜀 — $srv 없음"
  fi
done

if [ ${#got[@]} -eq 0 ]; then
  echo; echo "가져온 것이 없습니다. BASE 경로가 맞는지 보세요 (지금 BASE=$BASE)."
  exit 1
fi

echo "[3/4] 비밀·덩치 검사"
targets=(); for p in "${got[@]}"; do targets+=("$SRC/$p"); done

# (a) 큰 파일 — 색인 DB 같은 것이 실수로 딸려오는 것을 막는다 (5MB 초과)
bigfiles="$(find "${targets[@]}" -type f -size +5M 2>/dev/null || true)"

# (b) 비밀 — 앤트로픽 키·터널 토큰·개인키·코드에 박힌 키·주민등록번호
pat='sk-ant-[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]{40,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(ADMIN_KEY|API_KEY|SECRET|TOKEN|PASSWORD)[A-Z_]*[[:space:]]*=[[:space:]]*["'"'"'][^"'"'"'$[:space:]]{8,}|[0-9]{6}-[1-4][0-9]{6}'
secrets="$(grep -rInE "$pat" "${targets[@]}" 2>/dev/null | sed "s|^$SRC/||" | cut -c1-120 || true)"

if [ -n "$bigfiles" ] || { [ -n "$secrets" ] && [ "$force" -eq 0 ]; }; then
  echo
  echo "[중단] 가져온 내용을 되돌립니다."
  [ -n "$bigfiles" ] && { echo "  · 5MB 넘는 파일:"; echo "$bigfiles" | sed "s|^$SRC/||" | sed 's/^/      /'; }
  [ -n "$secrets" ]  && { echo "  · 비밀로 보이는 줄 (앞 120자만):"; echo "$secrets" | sed 's/^/      /'; }
  # 이번에 가져온 자리만 되돌린다 (레포의 다른 작업물은 건드리지 않는다)
  git checkout -- "${targets[@]}" 2>/dev/null || true
  git clean -fdq -- "${targets[@]}" 2>/dev/null || true
  echo
  echo "  고치는 법: 값을 코드에서 빼 os.environ 으로 읽고 /etc/sakyowon-api.env 에 넣은 뒤 다시 실행하세요."
  echo "  정말 오탐이면 --force 로 다시 실행합니다(권하지 않음)."
  exit 1
fi
echo "    걸린 것 없음"

echo "[4/4] 결과"
git add -A -- "${targets[@]}"
git status --short -- "${targets[@]}" | sed 's/^/    /'
if [ "$do_commit" -eq 1 ]; then
  if git diff --cached --quiet; then
    echo "    바뀐 것이 없어 커밋하지 않았습니다."
  else
    git commit -q -m "서버 코드 회수: 직원전용 질문·그룹웨어 ($(date +%F))"
    echo "    커밋 $(git rev-parse --short HEAD)"
    branch="$(git rev-parse --abbrev-ref HEAD)"
    if [ "$do_push" -eq 1 ]; then
      git push -u origin "$branch" && echo "    푸시 완료"
    else
      echo "    푸시는: git -C $SRC push -u origin $branch"
    fi
  fi
fi

if [ ${#skipped[@]} -gt 0 ]; then echo; echo "건너뛴 것:"; printf '    - %s\n' "${skipped[@]}"; fi
echo
echo "되돌리려면:  git -C $SRC reset --hard $before"
