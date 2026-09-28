#!/usr/bin/env python3
"""skdata — 사교원 데이터 저장소(sakyowon.co.kr/api/data) 명령줄 도구. 표준 라이브러리만 사용.

설정: ~/.config/sakyowon/data.env (또는 환경변수)
    SAKYOWON_URL=https://sakyowon.co.kr
    SAKYOWON_DATA_KEY=<읽기 전용 키 또는 관리자 키>

사용:
    skdata tables                                  표 목록
    skdata info <표>                               열·형식·건수
    skdata rows <표> [--where 열=op.값 ...] [--q 검색] [--order 열.desc] [--select 열,열]
                     [--limit N] [--offset N] [--csv] [--json]
    skdata sql "SELECT ... LIMIT 100" [--csv] [--json]
    skdata import <파일.xlsx|csv> [--name 표] [--title 제목] [--sheet 시트] [--append] [--header-row N]
    skdata inspect <파일>                          저장하지 않고 시트·열 미리보기
    skdata delete-table <표>                       표 삭제(관리자 키 필요)

기본 출력은 보기 좋은 표. --json 은 원본 JSON, --csv 는 CSV(엑셀용 BOM 포함).
"""
import argparse
import csv
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

CONF = os.path.expanduser("~/.config/sakyowon/data.env")


def load_conf():
    if os.path.exists(CONF):
        for line in open(CONF, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))
    base = os.environ.get("SAKYOWON_URL", "https://sakyowon.co.kr").rstrip("/")
    key = os.environ.get("SAKYOWON_DATA_KEY", "")
    if not key:
        sys.exit(f"키가 없습니다. {CONF} 에 SAKYOWON_DATA_KEY=... 를 적어 주세요.")
    return base, key


def call(method, path, params=None, body=None, raw=None, headers=None):
    base, key = load_conf()
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params if v not in (None, "")}, doseq=True)
    # 🔴 User-Agent 필수: sakyowon.co.kr 이 Cloudflare 뒤로 들어간 뒤(26-09-28)
    # urllib 기본 UA(Python-urllib/3.x)는 봇으로 막혀 403 error code 1010 이 난다.
    h = {"X-Data-Key": key, "User-Agent": "skdata/1.0 (+sakyowon.co.kr)"}
    if headers:
        h.update(headers)
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    elif raw is not None:
        data = raw
        h["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            ctype = r.headers.get("Content-Type", "")
            payload = r.read()
    except urllib.error.HTTPError as e:
        payload = e.read()
        try:
            j = json.loads(payload.decode("utf-8"))
        except Exception:
            j = {"error": payload.decode("utf-8", "ignore")[:300]}
        sys.exit(f"오류 {e.code}: {j.get('error')} {j.get('detail') or ''}".strip())
    if "text/csv" in ctype:
        return payload.decode("utf-8-sig")
    return json.loads(payload.decode("utf-8"))


def print_table(columns, rows):
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    if not rows:
        print("(행 없음)")
        return
    def dw(x):  # 표시 폭(한글은 2)
        return sum(2 if ord(ch) > 0x2E7F else 1 for ch in x)

    def cut(x, w):
        x = x.replace("\n", " ")
        if dw(x) <= w:
            return x
        out = ""
        for ch in x:
            if dw(out + ch) > w - 1:
                break
            out += ch
        return out + "…"

    def pad(x, w):
        return x + " " * max(0, w - dw(x))

    widths = [min(40, max(dw(str(c)), *(dw(r[i]) for r in rows))) for i, c in enumerate(columns)]

    print("  ".join(pad(cut(str(c), w), w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(pad(cut(v, w), w) for v, w in zip(r, widths)))


def to_csv(columns, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return "﻿" + buf.getvalue()


def cmd_tables(a):
    r = call("GET", "/api/data/tables")
    if a.json:
        return print(json.dumps(r, ensure_ascii=False, indent=2))
    print_table(["name", "title", "rows", "cols", "updated", "source"],
                [[t["name"], t["title"], t["row_count"], len(t["columns"]), (t["updated_at"] or "")[:16], t.get("source") or ""] for t in r["items"]])
    if not r.get("read_key_set"):
        print("\n(서버에 읽기 전용 키가 아직 설정되지 않았습니다)")


def cmd_info(a):
    r = call("GET", f"/api/data/tables/{a.table}")
    t = r["table"]
    if a.json:
        return print(json.dumps(t, ensure_ascii=False, indent=2))
    print(f"{t['title']}  ({t['name']})  {t['row_count']}행  원본: {t.get('source') or '-'} / {t.get('sheet') or '-'}  갱신: {t['updated_at']}")
    if t.get("note"):
        print("메모:", t["note"])
    print_table(["열", "형식"], [[c["name"], c["type"]] for c in t["columns"]])


def cmd_rows(a):
    params = [("limit", a.limit), ("offset", a.offset), ("q", a.q), ("order", a.order), ("select", a.select)]
    for w in a.where or []:
        k, _, v = w.partition("=")
        params.append((k, v))
    if a.csv:
        params.append(("format", "csv"))
        return sys.stdout.write(call("GET", f"/api/data/tables/{a.table}/rows", params))
    r = call("GET", f"/api/data/tables/{a.table}/rows", params)
    if a.json:
        return print(json.dumps(r, ensure_ascii=False, indent=2))
    print_table(r["columns"], [[it.get(c) for c in r["columns"]] for it in r["items"]])
    print(f"\n{r['offset'] + 1 if r['total'] else 0}–{r['offset'] + len(r['items'])} / 전체 {r['total']}행")


def cmd_sql(a):
    r = call("POST", "/api/data/sql", body={"sql": a.sql})
    if a.json:
        return print(json.dumps(r, ensure_ascii=False, indent=2))
    if a.csv:
        return sys.stdout.write(to_csv(r["columns"], r["rows"]))
    print_table(r["columns"], r["rows"])
    print(f"\n{r['count']}행" + (" (5000행에서 잘림)" if r.get("truncated") else ""))


def cmd_inspect(a):
    data = open(a.file, "rb").read()
    r = call("POST", "/api/data/inspect", [("filename", os.path.basename(a.file)), ("header_row", a.header_row or "")], raw=data)
    if a.json:
        return print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"파일 {r['filename']}  제안 이름: {r['suggested_name']}")
    for sh in r["sheets"]:
        if sh.get("empty"):
            print(f"\n[시트 {sh['sheet']}] 비어 있음")
            continue
        print(f"\n[시트 {sh['sheet']}] {sh['row_count']}행, 제목줄 {sh['header_row']}")
        print_table([f"{c['name']} ({c['type']})" for c in sh["columns"]], sh["preview"])


def cmd_import(a):
    data = open(a.file, "rb").read()
    params = [("filename", os.path.basename(a.file)), ("name", a.name or ""), ("title", a.title or ""),
              ("sheet", a.sheet or ""), ("mode", "append" if a.append else "replace"), ("header_row", a.header_row or "")]
    r = call("POST", "/api/data/import", params, raw=data)
    t = r["table"]
    print(f"{'이어붙임' if r['mode'] == 'append' else '저장'}: {t['name']} ({t['title']}) — 이번 {r['imported']}행, 총 {t['row_count']}행, {len(t['columns'])}열")


def cmd_delete_table(a):
    if input(f"표 {a.table} 을(를) 완전히 삭제합니다. 표 이름을 다시 입력: ") != a.table:
        sys.exit("취소")
    call("DELETE", f"/api/data/tables/{a.table}")
    print("삭제됨")


def main():
    p = argparse.ArgumentParser(prog="skdata", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--json", action="store_true")
        sp.add_argument("--csv", action="store_true")

    common(sub.add_parser("tables"))
    s = sub.add_parser("info"); s.add_argument("table"); common(s)
    s = sub.add_parser("rows"); s.add_argument("table"); s.add_argument("--where", action="append", metavar="열=op.값")
    s.add_argument("--q"); s.add_argument("--order"); s.add_argument("--select"); s.add_argument("--limit", type=int, default=100)
    s.add_argument("--offset", type=int, default=0); common(s)
    s = sub.add_parser("sql"); s.add_argument("sql"); common(s)
    s = sub.add_parser("inspect"); s.add_argument("file"); s.add_argument("--header-row", type=int); common(s)
    s = sub.add_parser("import"); s.add_argument("file"); s.add_argument("--name"); s.add_argument("--title"); s.add_argument("--sheet")
    s.add_argument("--append", action="store_true"); s.add_argument("--header-row", type=int)
    s = sub.add_parser("delete-table"); s.add_argument("table")
    a = p.parse_args()
    {"tables": cmd_tables, "info": cmd_info, "rows": cmd_rows, "sql": cmd_sql, "inspect": cmd_inspect,
     "import": cmd_import, "delete-table": cmd_delete_table}[a.cmd](a)


if __name__ == "__main__":
    main()
