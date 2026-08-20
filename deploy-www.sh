#!/usr/bin/env bash
# 정적 사이트 일괄 배치 스크립트 (root로 실행)
# 사용법: bash /opt/sakyowon/src/deploy-www.sh
# 다시 실행하면 최신 내용으로 갱신된다(사이트 업데이트 시 재실행).
set -euo pipefail

WWW=/opt/sakyowon/www
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# "레포 브랜치 대상폴더" 목록 (대상폴더 '.'=루트)
SITES="
deka2026.github.io main .
mangnam-coop gh-pages mangnam-coop
hometown-love main hometown-love
academy-site main academy-site
teamlearning-site main teamlearning-site
sakyowon-wiki-site master sakyowon-wiki
mangnam-vitality gh-pages mangnam-vitality
"
# mangnam-vitality는 정적 '홍보판'(gh-pages). 관리자·DB가 있는 서버형 본편은
# vitality.sakyowon.co.kr (setup-apps.sh) 로 별도 운영한다.

echo "$SITES" | while read -r repo branch target; do
  [ -z "${repo:-}" ] && continue
  echo "→ $repo ($branch) → www/$target"
  rm -rf "$TMP/$repo"
  git clone -q --depth 1 -b "$branch" "https://github.com/deka2026/$repo.git" "$TMP/$repo"
  rm -rf "$TMP/$repo/.git"
  rm -f "$TMP/$repo/CNAME"   # GitHub Pages용 파일 — 여기선 불필요
  if [ "$target" = "." ]; then
    # 루트(허브): 다른 사이트 폴더를 지우지 않도록 내용만 덮어쓴다
    cp -r "$TMP/$repo/." "$WWW/"
  else
    rm -rf "${WWW:?}/$target"
    mkdir -p "$WWW/$target"
    cp -r "$TMP/$repo/." "$WWW/$target/"
  fi
done

chown -R sakyowon:sakyowon "$WWW"

echo
echo "===== 배치 완료 ====="
ls -la "$WWW" | head -20
echo
echo "www 전체 용량: $(du -sh "$WWW" | cut -f1)"
