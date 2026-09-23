#!/usr/bin/env python3
"""대조 시험 — 같은 문항을 품에 엔진(/api/v1/ask)과 Anthropic 양쪽에 던져 나란히 기록한다.

연동규격서 v0.2 7절 ②단계. 사교원 서버에서 실행해 호출 기록이 사교원 client로 남게 한다.
키는 /etc/sakyowon-api.env 에서 읽고 화면·파일 어디에도 쓰지 않는다. 표준 라이브러리만 쓴다.

사용:
    sudo python3 compare_ask.py                 # 15문항 전부, 결과는 /opt/sakyowon/data/compare/
    sudo python3 compare_ask.py --only A-1 D-2  # 일부만
    sudo python3 compare_ask.py --list          # 문항 목록만
    sudo python3 compare_ask.py --engine-only   # Anthropic 호출 없이 엔진만 (비용 0)

환경(env 파일): SAKYOWON_ANTHROPIC_KEY, POOME_API_KEY 필수. POOME_API_BASE 없으면 기본 주소.
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("SAKYOWON_ENV_FILE", "/etc/sakyowon-api.env")
DEFAULT_BASE = "https://chat.solarshare.kr"
OUT_DIR = os.environ.get("COMPARE_OUT_DIR", "/opt/sakyowon/data/compare")
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

# 햇소자 상담 탭(/api/ai/chat)이 쓰는 시스템 프롬프트와 같게 — 이용자가 실제로 받는 답과 대조하기 위함
CHAT_SYSTEM = (
    "당신은 사회혁신교육원(사교원)의 실무 도우미입니다. 사회연대경제·교육컨설팅 맥락에서 "
    "정확하고 신뢰감 있게, 한국어로 간결히 답합니다."
)

# docs/대조시험_문항_사교원_15문_20260919.md 의 문항을 그대로 옮겼다. 문장을 손보면 대조가 무의미해진다.
QUESTIONS = [
    ("A-1", "permit", "마을협동조합이 1MW 태양광 발전사업을 하려면 어느 법 몇 조에 따라 허가를 받아야 하며, 허가권자는 누구입니까?"),
    ("A-2", "devact", "마을 부지에 태양광 발전설비를 설치할 때 받아야 하는 개발행위허가는 어느 법 몇 조에 근거합니까?"),
    ("A-3", "agri", "농지에 태양광 발전설비를 설치하려 할 때 농지전용허가의 근거 조문은 무엇입니까?"),
    ("A-4", "coop", "협동조합 정관에 반드시 적어야 할 사항은 어느 법 몇 조에 있으며, 설립신고의 근거 조문은 무엇입니까?"),
    ("A-5", "setback", "재생에너지 발전설비의 이격거리 규제는 어느 법 몇 조가 정하고 있으며, 그 조문은 이격거리를 적용하라는 것입니까, 적용하지 말라는 것입니까? 시행일도 알려 주십시오."),
    ("A-6", "agri", "영농형 태양광 발전사업의 근거 법률과 조문을 알려 주십시오."),
    ("B-1", None, "햇빛소득마을 협동조합의 조합원이 될 수 있는 사람의 요건을 공고문 기준으로 말해 주십시오. 나이·거주기간·기준일을 포함해 주십시오."),
    ("B-2", None, "햇빛소득마을을 신청하려면 협동조합에 최소 몇 명이 조합원으로 참여해야 하며, 발기인은 몇 명이 필요합니까? 한 가구에서 여러 명이 발기인이 될 수 있습니까?"),
    ("B-3", None, "햇빛소득마을에 쓸 태양광 모듈의 탄소배출량 기준은 얼마이며, 그것을 무엇으로 증명합니까?"),
    ("B-4", None, "햇빛소득마을로 선정된 뒤 수입 확보를 위해 언제까지 무엇을 해야 합니까?"),
    ("B-5", None, "햇빛소득마을이 받을 수 있는 REC 주민참여 추가 가중치는 최대 얼마이며, 어떤 조건에서 최대치가 됩니까? 여기서 말하는 \"이격거리 기준 준수\"란 무엇을 뜻합니까?"),
    ("C-1", None, "한전 계통연계 검토의견서를 받았습니다. 사업계획서를 쓰기 위해 이 문서에서 반드시 확인해야 할 항목을 알려 주십시오."),
    ("C-2", None, "협동조합 정관 초안을 검토합니다. 공고문이 요구하는 정관 필수요건은 무엇입니까?"),
    ("D-1", None, "2027년도 햇빛소득마을 공모의 접수 마감일과 평가 배점표를 알려 주십시오."),
    ("D-2", None, "완도군 ○○리의 한전 여유용량은 몇 kW입니까?"),
]


def read_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    for k in ("SAKYOWON_ANTHROPIC_KEY", "POOME_API_KEY", "POOME_API_BASE", "SAKYOWON_AI_MODEL"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def post_json(url, body, headers, timeout):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "sakyowon-compare/1.0", **headers})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status, hdr = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        raw, status, hdr = e.read(), e.code, dict(e.headers)
    except Exception as e:
        return 0, {"error": str(e)[:200]}, {}, int((time.time() - t0) * 1000)
    ms = int((time.time() - t0) * 1000)
    try:
        return status, json.loads(raw or b"{}"), hdr, ms
    except Exception:
        return status, {"error": raw[:300].decode("utf-8", "ignore")}, hdr, ms


def ask_engine(base, key, question, topic):
    body = {"question": question, "max_chars": 2000}
    if topic:
        body["context"] = {"topic": topic}
    for attempt in range(3):
        status, d, hdr, ms = post_json(f"{base}/api/v1/ask", body, {"X-API-Key": key}, 65)
        if status == 429:
            wait = int(hdr.get("Retry-After", "20") or 20)
            print(f"    엔진 429 — {wait}s 대기", flush=True)
            time.sleep(wait)
            continue
        break
    return {
        "status": status, "elapsed_ms": d.get("elapsed_ms", ms), "wall_ms": ms,
        "answer": d.get("answer", "") if status == 200 else "",
        "sources": d.get("sources", []) if status == 200 else [],
        "insufficient": bool(d.get("insufficient")) if status == 200 else None,
        "backend": d.get("backend"), "request_id": d.get("request_id"),
        "error": None if status == 200 else json.dumps(d, ensure_ascii=False)[:300],
    }


def ask_anthropic(key, model, question):
    body = {"model": model, "max_tokens": 1500, "system": CHAT_SYSTEM,
            "messages": [{"role": "user", "content": question}]}
    status, d, _, ms = post_json(ANTHROPIC_URL, body, {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}, 120)
    text = ""
    if status == 200:
        text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    usage = d.get("usage", {}) if status == 200 else {}
    return {"status": status, "wall_ms": ms, "answer": text, "model": d.get("model", model),
            "tokens_in": usage.get("input_tokens"), "tokens_out": usage.get("output_tokens"),
            "error": None if status == 200 else json.dumps(d, ensure_ascii=False)[:300]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="문항 번호 (예: A-1 D-2)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--engine-only", action="store_true")
    ap.add_argument("--out", default=OUT_DIR)
    a = ap.parse_args()

    qs = [q for q in QUESTIONS if not a.only or q[0] in a.only]
    if a.list:
        for qid, topic, text in qs:
            print(f"{qid:4} topic={topic or '-':8} {text}")
        return

    env = read_env(ENV_FILE)
    base = (env.get("POOME_API_BASE") or DEFAULT_BASE).rstrip("/")
    ekey, akey = env.get("POOME_API_KEY", ""), env.get("SAKYOWON_ANTHROPIC_KEY", "")
    model = env.get("SAKYOWON_AI_MODEL") or "claude-sonnet-5"
    if not ekey:
        sys.exit("POOME_API_KEY 가 env 파일에 없습니다.")
    if not a.engine_only and not akey:
        sys.exit("SAKYOWON_ANTHROPIC_KEY 가 env 파일에 없습니다. (--engine-only 로 엔진만 돌릴 수 있음)")

    print(f"엔진 {base} · 모델 {model} · 문항 {len(qs)}개 · Anthropic {'생략' if a.engine_only else '호출'}", flush=True)

    os.makedirs(a.out, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    rows = []
    for qid, topic, text in qs:
        print(f"[{qid}] 엔진…", end=" ", flush=True)
        e = ask_engine(base, ekey, text, None)  # 상담 탭 대조: topic 없이 (규격 문서 '열어 둔 것')
        print(f"{e['status']} {'insufficient' if e['insufficient'] else str(len(e['answer'])) + '자'} {e['wall_ms']}ms", end="", flush=True)
        an = None
        if not a.engine_only:
            print(" · Anthropic…", end=" ", flush=True)
            an = ask_anthropic(akey, model, text)
            print(f"{an['status']} {len(an['answer'])}자 {an['wall_ms']}ms", end="", flush=True)
        print(flush=True)
        rows.append({"id": qid, "topic": topic, "question": text, "engine": e, "anthropic": an})
        time.sleep(1.0)

    jpath = os.path.join(a.out, f"compare_{stamp}.json")
    mpath = os.path.join(a.out, f"compare_{stamp}.md")
    with open(jpath, "w", encoding="utf-8") as f:
        json.dump({"stamp": stamp, "engine_base": base, "anthropic_model": model, "rows": rows}, f, ensure_ascii=False, indent=1)

    with open(mpath, "w", encoding="utf-8") as f:
        f.write(f"# 대조 시험 결과 {stamp}\n\n엔진 `{base}` · Anthropic `{model}` · 문항 {len(rows)}개\n\n")
        f.write("## 요약\n\n| 문항 | 엔진 상태 | insufficient | sources | 엔진 ms | Anthropic 상태 | Anthropic ms | 토큰 in/out |\n|---|---|---|---|---|---|---|---|\n")
        for r in rows:
            e, an = r["engine"], r["anthropic"] or {}
            f.write(f"| {r['id']} | {e['status']} | {e['insufficient']} | {len(e['sources'])} | {e['elapsed_ms']} | "
                    f"{an.get('status', '-')} | {an.get('wall_ms', '-')} | {an.get('tokens_in', '-')}/{an.get('tokens_out', '-')} |\n")
        f.write("\n## 판정표 (사람이 채움)\n\n| 문항 | 엔진 답 맞나 | 엔진 근거 실재 | 엔진 지어냄 | Anthropic 답 맞나 | Anthropic 지어냄 | 비고 |\n|---|---|---|---|---|---|---|\n")
        for r in rows:
            f.write(f"| {r['id']} |  |  |  |  |  |  |\n")
        f.write("\n## 문항별 원문\n")
        for r in rows:
            e, an = r["engine"], r["anthropic"]
            f.write(f"\n### {r['id']}\n\n**문항**: {r['question']}\n\n")
            f.write(f"**엔진** (status {e['status']}, backend {e['backend']}, insufficient {e['insufficient']}, {e['elapsed_ms']}ms, request_id {e['request_id']})\n\n")
            f.write((e["answer"] or e["error"] or "(빈 답)") + "\n\n")
            if e["sources"]:
                f.write("sources:\n" + "\n".join(f"- {json.dumps(s, ensure_ascii=False)}" for s in e["sources"]) + "\n\n")
            if an is not None:
                f.write(f"**Anthropic** (status {an['status']}, {an['model']}, {an['wall_ms']}ms, tokens {an['tokens_in']}/{an['tokens_out']})\n\n")
                f.write((an["answer"] or an["error"] or "(빈 답)") + "\n")
    print(f"\n저장: {mpath}\n      {jpath}")
    print("다음: 판정표를 채운 뒤 md 파일을 편지함에 첨부. 키는 결과 파일에 들어가지 않는다.")


if __name__ == "__main__":
    main()
