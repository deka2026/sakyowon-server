# -*- coding: utf-8 -*-
"""문서 자동판독 — 햇소자 자료함이 올린 서류에서 값을 뽑는다.

설계 원칙 (2026-09-28, 데카):
  1. **아무것도 저장하지 않는다.** 메모리에서 읽고 값만 돌려준다. 원본은 디스크에 안 남긴다.
  2. **못 읽은 필드는 키를 뺀다.** 빈 문자열을 주면 "못 읽음"과 "0"을 구분할 수 없다.
  3. **확신도를 같이 준다.** 화면이 확신 낮은 값에 표시를 달아 사람이 먼저 보게 한다.
  4. **완벽을 노리다 실패하지 않는다.** 한 필드라도 읽었으면 그것만 돌려준다.

돌려주는 모양:
  {"fields": {"허가일": {"값": "2026-09-01", "확신": 0.9, "위치": "1쪽"}, ...},
   "읽은쪽수": 3, "engine": "pypdf"}

지원 형식: PDF(pypdf) · HWPX(zip+xml) · XLSX(openpyxl) · CSV/TXT
미지원: 구형 HWP(olefile 없음) · DOCX(python-docx 없음) · 이미지(OCR 없음)
"""
import io
import re
import zipfile

MAX_BYTES = 15 * 1024 * 1024


# ─────────────────────────── 본문 꺼내기 ───────────────────────────

def _pdf_pages(data):
    from pypdf import PdfReader
    try:
        r = PdfReader(io.BytesIO(data))
    except Exception as e:          # 깨진 PDF·암호 걸린 PDF — 원시 오류를 사람 말로 바꾼다
        raise ValueError("PDF 를 열지 못했습니다(손상되었거나 암호가 걸려 있습니다).") from e
    out = []
    for p in r.pages:
        try:
            out.append(p.extract_text() or "")
        except Exception:
            out.append("")
    return out


def _hwpx_pages(data):
    """HWPX 는 zip 안의 XML 이다 — 별도 라이브러리가 필요 없다."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = [n for n in z.namelist() if re.search(r"section\d*\.xml$", n)]
        names.sort()
        out = []
        for n in names:
            xml = z.read(n).decode("utf-8", "replace")
            # 문단(<hp:p>)마다 줄을 바꾼다 — 한 줄로 붙이면 라벨이 다음 문단 값을 물어
            # 「사업명」에 뒤 문장까지 딸려 들어간다(2026-09-28 실제로 당했다).
            줄 = []
            for 문단 in re.findall(r"<hp:p[ >].*?</hp:p>", xml, re.S) or [xml]:
                t = " ".join(re.findall(r"<hp:t[^>]*>(.*?)</hp:t>", 문단, re.S))
                t = re.sub(r"<[^>]+>", "", t).strip()
                if t:
                    줄.append(t)
            out.append("\n".join(줄))
        return out or [""]


def _xlsx_pages(data):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        줄 = []
        for row in ws.iter_rows(values_only=True):
            줄.append(" ".join("" if c is None else str(c) for c in row))
        out.append("\n".join(줄))
    wb.close()
    return out or [""]


def 본문꺼내기(name, data):
    n = (name or "").lower()
    if n.endswith(".pdf") or data[:5] == b"%PDF-":
        return _pdf_pages(data), "pypdf"
    if n.endswith(".hwpx"):
        return _hwpx_pages(data), "hwpx-zip"
    if n.endswith((".xlsx", ".xlsm")):
        return _xlsx_pages(data), "openpyxl"
    if n.endswith((".csv", ".txt")):
        for enc in ("utf-8-sig", "cp949", "utf-8"):
            try:
                return [data.decode(enc)], "text:" + enc
            except UnicodeDecodeError:
                continue
        return [data.decode("utf-8", "replace")], "text:replace"
    raise ValueError("읽을 수 없는 형식입니다 (지원: PDF·HWPX·XLSX·CSV·TXT)")


# ─────────────────────────── 값 다듬기 ───────────────────────────

_숫자 = re.compile(r"[^\d.]")


def _수(s):
    v = _숫자.sub("", str(s or ""))
    return v.rstrip(".") or None


def _날짜(덩이):
    """'2026. 9. 1.' · '2026-09-01' · '2026년 9월 1일' → '2026-09-01'"""
    m = re.search(r"(20\d{2})\s*[.\-년/]\s*(\d{1,2})\s*[.\-월/]\s*(\d{1,2})", 덩이)
    if not m:
        return None
    y, mo, d = m.groups()
    if not (1 <= int(mo) <= 12 and 1 <= int(d) <= 31):
        return None
    return "%s-%02d-%02d" % (y, int(mo), int(d))


def _달(덩이):
    m = re.search(r"(20\d{2})\s*[.\-년/]\s*(\d{1,2})\s*월?", 덩이)
    if not m:
        return None
    y, mo = m.groups()
    if not (1 <= int(mo) <= 12):
        return None
    return "%s-%02d" % (y, int(mo))


def _찾기(쪽들, 패턴, 뽑기=None, 확신=0.8):
    """쪽을 돌며 첫 일치를 찾는다. (값, 확신, '3쪽') 또는 None."""
    for i, 쪽 in enumerate(쪽들):
        if not 쪽:
            continue
        m = re.search(패턴, 쪽, re.S)
        if not m:
            continue
        원 = m.group(m.lastindex or 0)
        값 = 뽑기(원) if 뽑기 else re.sub(r"\s+", " ", 원).strip()
        if 값:
            return {"값": 값, "확신": 확신, "위치": "%d쪽" % (i + 1)}
    return None


def _넣기(표, 이름, 결과):
    if 결과:
        표[이름] = 결과


# ─────────────────────────── 유형별 추출 ───────────────────────────

def _kepco(쪽들):
    f = {}
    _넣기(f, "정산월", _찾기(쪽들, r"(?:정산|거래|공급)\s*(?:월|기간|년월)[^\d\n]{0,25}(20\d{2}\s*[.\-년/]\s*\d{1,2})", _달, 0.85)
              or _찾기(쪽들, r"(20\d{2}\s*[.\-년/]\s*\d{1,2}\s*월)", _달, 0.5))
    _넣기(f, "발전량kWh", _찾기(쪽들, r"(?:거래|판매|발전|공급)?\s*전력량[^\d\n]{0,25}([\d,]+(?:\.\d+)?)", _수, 0.8)
                    or _찾기(쪽들, r"발전량[^\d\n]{0,25}([\d,]+(?:\.\d+)?)", _수, 0.75))
    _넣기(f, "SMP", _찾기(쪽들, r"SMP[^\d\n]{0,25}([\d,]+(?:\.\d+)?)", _수, 0.8))
    _넣기(f, "REC", _찾기(쪽들, r"REC[^\d\n]{0,25}([\d,]+(?:\.\d+)?)", _수, 0.8))
    _넣기(f, "정산금액", _찾기(쪽들, r"(?:정산\s*금액|지급\s*금액|합\s*계\s*금액|공급가액)[^\d\n]{0,25}([\d,]+)", _수, 0.8))
    return f


def _permit(쪽들):
    f = {}
    _넣기(f, "허가번호", _찾기(쪽들, r"제\s*([0-9]{2,4}\s*[-–]\s*[0-9]+)\s*호", lambda s: re.sub(r"\s", "", s), 0.85))
    _넣기(f, "허가일", _찾기(쪽들, r"(?:허가|발급)\s*(?:일자|일)?[^\d\n]{0,25}(20\d{2}\s*[.\-년/]\s*\d{1,2}\s*[.\-월/]\s*\d{1,2})", _날짜, 0.85)
                or _찾기(쪽들, r"(20\d{2}\s*[.\-년/]\s*\d{1,2}\s*[.\-월/]\s*\d{1,2})", _날짜, 0.45))
    _넣기(f, "설비용량", _찾기(쪽들, r"(?:설비\s*)?용량[^\d\n]{0,25}([\d,]+(?:\.\d+)?)\s*(?:kW|KW|킬로와트)", _수, 0.85)
                  or _찾기(쪽들, r"([\d,]+(?:\.\d+)?)\s*(?:kW|KW|킬로와트)", _수, 0.6))
    _넣기(f, "허가권자", _찾기(쪽들, r"(전남광주통합특별시장|[가-힣]{2,10}(?:특별시장|광역시장|도지사|시장|군수|구청장)|기후에너지환경부장관|산업통상자원부장관)", None, 0.8))
    return f


def _selection(쪽들):
    f = {}
    _넣기(f, "선정일", _찾기(쪽들, r"(?:선정|통보|결정|시행)\s*(?:일자|일)?[^\d\n]{0,25}(20\d{2}\s*[.\-년/]\s*\d{1,2}\s*[.\-월/]\s*\d{1,2})", _날짜, 0.8)
                or _찾기(쪽들, r"(20\d{2}\s*[.\-년/]\s*\d{1,2}\s*[.\-월/]\s*\d{1,2})", _날짜, 0.45))
    _넣기(f, "사업명", _찾기(쪽들, r"사\s*업\s*명[^\S\n]*[:：]?\s*([^\n]{3,60})", None, 0.75)
                or _찾기(쪽들, r"(20\d{2}\s*년?\s*햇빛소득마을[^\n]{0,40})", None, 0.6))
    _넣기(f, "통보기관", _찾기(쪽들, r"(행정안전부|전남광주통합특별시|[가-힣]{2,10}(?:시장|군수|구청장|도지사|장관))", None, 0.7))
    return f


_지목 = "전|답|과수원|목장용지|임야|광천지|염전|대|공장용지|학교용지|주차장|주유소용지|창고용지|도로|철도용지|제방|하천|구거|유지|양어장|수도용지|공원|체육용지|유원지|종교용지|사적지|묘지|잡종지"


def _land(쪽들):
    f = {}
    _넣기(f, "소재지", _찾기(쪽들, r"(?:소\s*재\s*지|토지소재)[^\S\n]*[:：]?\s*([^\n]{3,60})", None, 0.8))
    _넣기(f, "지목", _찾기(쪽들, r"지\s*목[^\S\n]*[:：]?\s*(" + _지목 + r")", None, 0.85)
              or _찾기(쪽들, r"\b(" + _지목 + r")\b", None, 0.45))
    _넣기(f, "면적", _찾기(쪽들, r"면\s*적[^\d\n]{0,25}([\d,]+(?:\.\d+)?)\s*(?:㎡|m2|m²|제곱미터)", _수, 0.85))
    _넣기(f, "소유구분", _찾기(쪽들, r"(국유지|공유지|사유지|시유지|군유지|도유지)", None, 0.7))
    return f


추출기 = {
    "kepco": _kepco,
    "permit": _permit,
    "selection": _selection,
    "land": _land,
}
지원유형 = sorted(추출기)


def 판독(kind, name, data):
    if len(data) > MAX_BYTES:
        raise ValueError("파일이 너무 큽니다 (최대 %dMB)" % (MAX_BYTES // 1024 // 1024))
    fn = 추출기.get(kind)
    if not fn:
        raise ValueError("아직 자동판독하지 않는 자료입니다: %s (지원: %s)" % (kind, ", ".join(지원유형)))
    쪽들, 엔진 = 본문꺼내기(name, data)
    글자수 = sum(len(p or "") for p in 쪽들)
    if 글자수 < 20:
        raise ValueError("글자를 찾지 못했습니다. 스캔 이미지 PDF 라면 자동판독이 안 됩니다 — 값을 직접 넣어 주세요.")
    return {"fields": fn(쪽들), "읽은쪽수": len(쪽들), "글자수": 글자수, "engine": 엔진}
