#!/usr/bin/env python
"""doc_audit.py — 산출 MD ↔ 원본 PDF 결정론 감사(LLM 0, 사용자 표준 2026-07-14).

usage: .venv/bin/python doc_audit.py <md_path> <pdf_path>
출력: stdout 단일 JSON. exit code 0 = 전 항목 클린, 1 = 실패 존재, 2 = 실행 오류.

검사(= "원본과 같음"의 기계적 정의):
  a_captions   : 벡터 'Table/Figure N:' 캡션 ↔ MD 캡션 1:1 (누락/중복/오형식/오위치/무증거)
  b_continued  : 모든 '(continued)' 캡션의 헤더 지문 일치 + 체인 번호 일치 (유효성)
  b2_complete  : 지문 검증 연속분(CONT) 페이지 전수에 continued 캡션 존재 (완전성)
  c_cells      : 그리드 표 데이터 행 지문 ↔ MD 표 행 지문 전수 대조
  d_structure  : 잘-형성 그리드 전수의 GFM 반영(HTML 폴백=실패), 빈 표 파편 0
  e_order      : 페이지 내 표/캡션/헤딩의 MD 순서 = 벡터 y 순서
  f_coverage   : 페이지 마커 전수, partial/MISSING/TRUNCATED/coverage-low 0
  blindspot    : find_tables 미검출/실패 페이지에 MD 표 블록 존재 → 셀 대조 불가 명시
  inverse_blindspot(R7) : grid 존재 페이지의 'grid 미매핑' MD gfm 표 블록 → 중복
                 미니표/창작 의심(F-DUP 제거 후 0 이어야 CLEAN)
  g_prose(R9)  : 페이지 벡터 '산문 블록'(표 bbox 밖·회전 워터마크/러닝헤더푸터/캡션
                 제외) 커버리지 — extract 하이브리드 가드 _line_covered(유의미 토큰
                 60% 겹침)를 import 재사용(SSoT). 블록의 유의미 라인 '전부'가
                 미커버일 때만 fail(문장 단위 paraphrase 오탐 방지). Note 블록
                 통째 소실 같은 산문 유실을 결정론 검출한다.
  (R7 F-SUBROW: c_cells 는 extract 의 _subrow_merge_rows 를 grid 측에 동일 적용해
   병합 렌더와 1:1 대조 — 규칙 SSoT 는 extract 모듈, 재구현 금지)
  (R12: 페이지 걸침 sub-row 는 extract 의 _xpage_subrow_migrate 를 체인(cont_pages)
   grid 측에 동일 적용 — 이동 행은 이전 페이지 grid 마지막 행에서 대조되고, 소진된
   grid(데이터 0)는 md 블록/continued 캡션 부재가 정상 계약)

한계(해석 주의, Advisor 2026-07-14 / R9 갱신 2026-07-15): 표 계열 검사(c/d/e 등)의
"CLEAN"은 **find_tables(PyMuPDF) 추출 결과와의 자기정합** 증명이고, g_prose(R9)는
**PDF 벡터 텍스트(표 밖 산문)** 를 진실원으로 하는 별도 축이다(JSON `truth_source:
"find_tables+vector_text(g_prose)"`). find_tables 자체가 셀을 오추출/미검출하는
페이지는 여전히 보증 범위 밖 — 그런 페이지는 blindspot 레코드로 가시화만 하며,
독립 대조는 cross_verify.py(벡터텍스트 직접 대조)와 Docling 폴백이 담당한다.
"""
import importlib.util
import json
import os
import re
import sys


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


CAP_RE = re.compile(r"^\s*(#{1,6}\s*)?\**\s*(Table|Figure)\s+(\d+)\s*[:.]", re.IGNORECASE)
SEP_RE = re.compile(r"^\s*\|[\s:|\-]+\|\s*$")
HEAD_RE = re.compile(r"^\s*#{1,6}\s+\S")


# ── R13c(2026-07-16): heading_dup WARN 검출용 — qa_fix.py `scan_heading_dedup` /
#    extract_all_via_pdf.py `_dedup_consecutive_headings` 와 자구 동치(SSoT).
#    doc_audit 는 독립 실행 파일(단일 프로세스, import 의존 최소)이라 함수를
#    복제한다(재구현이 아니라 이식 — 규칙 변경 시 3곳 동시 갱신 필요, 출처 명시).
HD_FENCE_RE = re.compile(r"^\s*(```|~~~)")
HD_HEADING_LINE_RE = re.compile(r"^(#{1,6})\s+(\S.*)$")
HD_LEADING_SECTION_NUM_RE = re.compile(r"^\d+(?:\.\d+)*\s+")


def _hd_has_leading_number(title):
    """제목 선두에 절 번호(`N.N.N `) 토큰이 있는지 여부."""
    return bool(HD_LEADING_SECTION_NUM_RE.match((title or "").strip()))


def _hd_norm_title(title):
    """헤딩 제목 정규화(절번호 제거판) — 규칙(a)(뒤 헤딩이 무번호일 때만) 용."""
    t = (title or "").strip()
    t = HD_LEADING_SECTION_NUM_RE.sub("", t, count=1)
    t = re.sub(r"\s+", " ", t).strip()
    return t.casefold()


def _hd_norm_raw(title):
    """헤딩 제목 정규화(절번호 보존판) — 규칙(b)(원문 완전동일) 용."""
    t = re.sub(r"\s+", " ", (title or "").strip()).strip()
    return t.casefold()


def _scan_heading_dup(lines):
    """연속(사이 공백 줄만) 동일제목 헤딩을 검출(수정하지 않음, 검출 전용).

    중복 판정 = 규칙(a) OR 규칙(b)(R13b 최종본과 자구 동치):
      (a) 뒤 헤딩 B 가 무번호이고 그 정규화 제목이 앞 헤딩 A 의 절번호 제거 후
          정규화 제목과 같을 때.
      (b) A·B 의 원문 제목(절번호 보존, 레벨만 무시)이 완전히 같을 때.
      → 양쪽 다 번호가 있고 번호가 다르면(`1 Overview` vs `1.1 Overview`) 미검출.
    코드펜스 내부·헤딩 사이 비공백 내용(페이지 마커 포함) 개재 시 미검출.

    반환: [(line_idx(0-based, 뒤엣것 B), b_title), ...]
    """
    found = []
    n = len(lines)
    in_fence = False
    i = 0
    while i < n:
        if HD_FENCE_RE.match(lines[i]):
            in_fence = not in_fence
            i += 1
            continue
        if in_fence:
            i += 1
            continue
        m = HD_HEADING_LINE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        anchor_title = m.group(2)
        anchor_core = _hd_norm_title(anchor_title)
        anchor_raw = _hd_norm_raw(anchor_title)
        i += 1
        while True:
            j = i
            while j < n and lines[j].strip() == "":
                j += 1
            if j >= n or HD_FENCE_RE.match(lines[j]):
                break
            m2 = HD_HEADING_LINE_RE.match(lines[j])
            if not m2:
                break
            b_title = m2.group(2)
            b_has_num = _hd_has_leading_number(b_title)
            b_raw = _hd_norm_raw(b_title)
            is_dup = ((not b_has_num and anchor_core == b_raw)
                      or (anchor_raw == b_raw))
            if not is_dup:
                break
            found.append((j, b_title))
            i = j + 1
    return found


def _page_of_line(mk, li):
    """라인 인덱스(0-based)가 속한 페이지 번호(첫 마커 이전 라인은 None)."""
    p = None
    for pg, idx in sorted(mk.items(), key=lambda kv: kv[1]):
        if idx <= li:
            p = pg
        else:
            break
    return p


USAGE = ("usage: .venv/bin/python doc_audit.py <md_path> <pdf_path>\n"
         "       (검사 항목·해석은 --help 참조. exit 0=CLEAN, 1=FAIL, 2=실행 오류)")


def main():
    # R10 CLI 위생(2026-07-15): --help / 인자 부족 시 IndexError 크래시 대신 usage.
    argv = sys.argv[1:]
    if any(a in ("-h", "--help") for a in argv):
        print(__doc__.strip())
        print()
        print(USAGE)
        return 0
    if len(argv) < 2:
        print(USAGE, file=sys.stderr)
        return 2
    md_path, pdf_path = argv[0], argv[1]
    base = os.path.dirname(os.path.abspath(__file__))
    os.chdir(base)
    sys.path.insert(0, base)
    spec = importlib.util.spec_from_file_location(
        "xmod", os.path.join(base, "extract_all_via_pdf.py"))
    x = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(x)
    import fitz

    md = open(md_path, encoding="utf-8").read()
    lines = md.split("\n")
    mk = {}
    for i, l in enumerate(lines):
        m = re.match(r"<!-- page (\d+) -->", l)
        if m:
            mk[int(m.group(1))] = i
    pdoc = fitz.open(pdf_path)   # R9 g_prose 에서 페이지 재사용(1회 오픈)
    npages = pdoc.page_count
    fails = []

    def fail(check, page, detail):
        fails.append({"check": check, "page": page, "detail": detail})

    # R13(2026-07-16): 경고(WARN) 채널 — CLEAN/FAIL 판정·exit code 와 무관(비차단).
    warns = []

    def warn(check, page, detail):
        warns.append({"check": check, "page": page, "detail": detail})

    # ── heading_dup(R13c) WARN: 연속 동일제목 헤딩 잔존 검출 ──
    #    R13b(extract `_dedup_consecutive_headings`)의 예방 게이트에 대응하는 검출
    #    게이트 — Goose QA 체인(qa_scan+doc_audit+cross_verify)이 이 결함 유형을
    #    인식하도록 가시화. 비차단(경고 채널, status/exit code 불변). SSoT = 위
    #    _scan_heading_dup(qa_fix.py `scan_heading_dedup` 과 자구 동치).
    try:
        for (li, b_title) in _scan_heading_dup(lines):
            warn("heading_dup", _page_of_line(mk, li),
                 f"consecutive duplicate heading: '{b_title[:70]}'")
    except Exception as e4:  # noqa: BLE001 — 비차단(경고 채널)
        warn("heading_dup", None, f"heading_dup scan error: {e4}")

    # ── f_coverage ──
    missing_mk = [p for p in range(1, npages + 1) if p not in mk]
    if missing_mk:
        fail("f_coverage", None, f"missing page markers: {missing_mk}")
    for pat in ("MISSING", "TRUNCATED", "fmdw:coverage-low"):
        n = len(re.findall(pat, md))
        if n:
            fail("f_coverage", None, f"{pat} marker x{n}")
    if md_path.endswith(".partial.md") or os.path.exists(
            md_path.replace(".md", ".partial.md")):
        fail("f_coverage", None, "partial artifact present")

    def seg(p):
        nxt = min([v for k, v in mk.items() if k > p], default=len(lines))
        return lines[mk[p] + 1:nxt]

    # ── 페이지별 원장 + 체인 ──
    open_tbl, prev_hdr = None, None
    cont_pages = {}
    page_data = {}
    for p in range(1, npages + 1):
        if p not in mk:
            continue
        body = seg(p)
        try:
            grids, _ph, _n = x._grid_page_info(pdf_path, p)
        except Exception:  # noqa: BLE001
            grids = []
        grids = sorted(grids, key=lambda gb: gb[1][1])
        caps, vok = x._xpage_vec_table_captions(pdf_path, p)
        gcap = {}
        for (num, text, _y0, y1) in sorted(caps, key=lambda c: c[3]):
            bg, bgap = None, None
            for gi, (_g, b) in enumerate(grids):
                gap = b[1] - y1
                if gap >= -5 and (bgap is None or gap < bgap):
                    bgap, bg = gap, gi
            if bg is not None and bg not in gcap:
                gcap[bg] = (num, text)
        is_cont = False
        if grids and vok:
            fh = grids[0][0][0]
            # 지문 = 프로덕션(_xpage_registry_pass)과 동일 함수(_cc_norm_cells:
            # 셀별 정규화 + '|' join — 셀 경계 보존)로 판정(Advisor Minor-1).
            is_cont = (open_tbl is not None and 0 not in gcap
                       and (grids[0][1][1] or 0) <= 110.0
                       and prev_hdr is not None and len(fh) == len(prev_hdr)
                       and x._cc_norm_cells(fh) == x._cc_norm_cells(prev_hdr))
        if is_cont:
            cont_pages[p] = open_tbl
        page_data[p] = (body, grids, caps, gcap, vok)
        if grids:
            last_gi = len(grids) - 1
            owner = None
            for gi in range(last_gi, -1, -1):
                if gi in gcap:
                    owner = gcap[gi]
                    break
            if owner is not None:
                open_tbl = (owner[0], re.sub(r"^Table\s+\d+\s*[:.]\s*", "",
                                             owner[1], flags=re.I).strip())
            elif not is_cont:
                open_tbl = None
            prev_hdr = grids[-1][0][0]
        else:
            open_tbl, prev_hdr = None, None

    # ── R12(2026-07-15): 페이지 걸침 sub-row 이동을 grid 측에 동일 적용 ──
    #    extract _xpage_registry_pass 가 continued 선두 fragment 행을 이전 페이지
    #    체인 행에 병합·이동하므로(md 측), 페이지별 행 대조(c_cells)도 같은 이동을
    #    grid 표현에 적용해야 1:1 이 유지된다 — 공유 SSoT x._xpage_subrow_migrate
    #    (재구현 금지, R7 _subrow_merge_rows 패턴의 체인 확장판). 누락 시 cont
    #    페이지는 잉여 grid 행(거짓 FAIL), 이전 페이지는 md 잉여 행으로 어긋난다.
    #    bbox(grids[i][1])는 유지 — g_prose 의 표 영역 제외는 벡터 사실 기준.
    for p in sorted(cont_pages):
        pd, prevd = page_data.get(p), page_data.get(p - 1)
        if not pd or not prevd or not pd[1] or not prevd[1]:
            continue
        # 적응 가드: md 측이 실제로 이동했는지 감지 — 구(R7) 산출물은 md 가 선두
        # 파편을 '보존'하므로 grid 측도 보존해야 참(FAIL 오탐 방지). md 선두 파편이
        # 없는데 grid 에 있으면 R12 이동본 → grid 측도 이동. md 가 파편을 '유실'한
        # 경우에는 이동해도 이전 페이지 행 지문이 어긋나 c_cells 가 FAIL — 건전성 유지.
        body_p = pd[0]
        blocks_p = x._xpage_blocks_with_html(body_p)
        first_gfm = next((b for b in blocks_p if b[2] == "gfm"), None)
        md_keeps_frag = False
        if first_gfm:
            nd = [k for k in range(first_gfm[0], first_gfm[1] + 1)
                  if not SEP_RE.match(body_p[k])][1:]
            if nd:
                md_keeps_frag = x._subrow_is_fragment(x._cc_cells(body_p[nd[0]]))
        if md_keeps_frag:
            continue   # 구 형식(R7 보존) 산출물 — grid 측도 그대로
        prev_g, prev_b = prevd[1][-1]
        cont_g, cont_b = pd[1][0]
        hn = x._grid_header_rows(cont_g)
        prev_m, _ = x._subrow_merge_rows(prev_g)   # extract 렌더 상태와 동일화(멱등)
        pr2, cr2, nmig, _pb, _cb = x._xpage_subrow_migrate(prev_m, cont_g[hn:])
        if nmig:
            prevd[1][-1] = (pr2, prev_b)
            pd[1][0] = (list(cont_g[:hn]) + cr2, cont_b)

    for p, (body, grids, caps, gcap, vok) in sorted(page_data.items()):
        if not vok:
            fail("a_captions", p, "vector read failed (manual review)")
            continue
        blocks = x._xpage_blocks_with_html(body)
        mapping = x._xpage_match_blocks_to_grids(body, blocks, grids) if grids else {}
        # ── blindspot: find_tables 미검출인데 MD 에 표 블록 존재 → 셀 대조 불가 명시
        #    (Advisor Minor-2 — 조용한 미검사 금지. 수리 아님, 가시화만) ──
        if not grids and blocks:
            fail("blindspot", p,
                 f"find_tables 미검출 — md 표 블록 {len(blocks)}개 셀 대조 불가 "
                 "(cross_verify.py/Docling 대기열 소관)")
        # ── inverse_blindspot(R7 F-DUP): grid 존재 페이지의 grid 미매핑 gfm 블록 ──
        #    (registry (7) 중복 미니표 제거 후 0 이어야 함 — 조용한 잔존/창작 표 가시화)
        if grids:
            for bi, (s, e, kind) in enumerate(blocks):
                if kind == "gfm" and bi not in mapping:
                    fail("inverse_blindspot", p,
                         f"md gfm 표 블록(line {s}, 행 {e - s})이 어느 grid 에도 "
                         "미매핑 — 중복 미니표/창작 의심(F-DUP)")
        mdcaps = []   # (line_idx, num, text, continued?)
        for i, l in enumerate(body):
            mm = CAP_RE.match(l)
            if mm and not l.lstrip().startswith("|") and mm.group(2).lower() == "table":
                mdcaps.append((i, int(mm.group(3)), l.strip(),
                               "(continued)" in l.lower()))
        # ── a_captions ──
        vnums = {}
        for (num, text, _y0, _y1) in caps:
            vnums.setdefault(num, text)
        for num, text in sorted(vnums.items()):
            hits = [(i, t) for (i, n, t, c) in mdcaps if n == num and not c]
            if len(hits) != 1:
                fail("a_captions", p,
                     f"'Table {num}' md count={len(hits)} (expect 1)")
                continue
            i, t = hits[0]
            if not (t.startswith("**") and t.endswith("**")):
                fail("a_captions", p, f"'Table {num}' not bold: '{t[:60]}'")
            if norm(t) != norm(text):
                fail("a_captions", p,
                     f"'Table {num}' text mismatch md='{t[:50]}' vec='{text[:50]}'")
            own_gi = next((gi for gi, v in gcap.items() if v[0] == num), None)
            if own_gi is not None:
                bi = next((b for b, g in mapping.items() if g == own_gi), None)
                if bi is not None:
                    s_t = blocks[bi][0]
                    ok_pos = (i < s_t) and not any(
                        i < bs < s_t for (bs, _be, _k) in blocks)
                    if not ok_pos:
                        fail("a_captions", p, f"'Table {num}' misplaced "
                                              f"(line {i}, table at {s_t})")
        for (i, n, t, c) in mdcaps:
            if not c and n not in vnums:
                fail("a_captions", p, f"md caption without vector evidence: '{t[:60]}'")
        # ── b_continued / b2_complete ──
        for (i, n, t, c) in mdcaps:
            if not c:
                continue
            if p not in cont_pages:
                fail("b_continued", p, f"'(continued)' on non-CONT page: '{t[:60]}'")
            elif str(cont_pages[p][0]) != str(n):
                fail("b_continued", p, f"chain num {cont_pages[p][0]} != md {n}")
        # R12: 연속분 첫 grid 가 파편 이동으로 소진(데이터 행 0)되면 md 에 continued
        # 블록/캡션이 없는 것이 정상 계약 — b2 완전성 검사 면제.
        _g0_consumed = False
        if p in cont_pages and grids:
            _g0m, _ = x._subrow_merge_rows(grids[0][0])
            _g0_consumed = len(_g0m) <= x._grid_header_rows(_g0m)
        if p in cont_pages and not _g0_consumed:
            b0 = blocks[0] if blocks else None
            ok2 = False
            if b0:
                k = b0[0] - 1
                while k >= 0 and (not body[k].strip() or body[k].strip() == "---"
                                  or body[k].startswith("<!--")):
                    k -= 1
                if k >= 0:
                    mm = CAP_RE.match(body[k])
                    ok2 = bool(mm and "(continued)" in body[k].lower()
                               and int(mm.group(3)) == int(cont_pages[p][0]))
            if not ok2:
                fail("b2_complete", p,
                     f"CONT page missing '(continued)' caption for Table {cont_pages[p][0]}")
        # ── c_cells / d_structure ──
        for gi, (g, _b) in enumerate(grids):
            bi = next((b for b, gg in mapping.items() if gg == gi), None)
            if bi is None:
                # R12: 파편 이동으로 데이터 행이 0 이 된 grid(소진)는 md 블록이
                # 없는 것이 정상(내용은 이전 페이지 체인 행으로 이관됨) — 면제.
                _gm, _ = x._subrow_merge_rows(g)
                if len(_gm) <= x._grid_header_rows(_gm):
                    continue
                fail("d_structure", p, f"grid {gi} (rows={len(g)}) has no md table")
                continue
            s, e, kind = blocks[bi]
            if kind == "html":
                fail("d_structure", p, f"grid {gi} rendered as raw HTML fallback")
                continue
            hdr_line_norm = norm("".join(x._cc_cells(body[s])))
            # R7 F-SUBROW: grid 측에 extract 공유 병합 규칙 적용(md 는 병합 렌더됨)
            g, _gb2 = x._subrow_merge_rows(g)
            gfps = []
            for r in g[1:]:
                cells = [c or "" for c in r]
                nonempty = [norm(c) for c in cells if (c or "").strip()]
                # 2단 그룹헤더의 서브헤더 행(빈 셀 포함 + 전 셀이 md 완전수식 헤더의
                # 부분문자열)은 데이터 행이 아님 — 평탄화로 md 에 별도 행이 없다.
                if (len(nonempty) < len(cells) and nonempty
                        and all(c2 in hdr_line_norm for c2 in nonempty)):
                    continue
                gfps.append(norm("".join(cells)))
            mfps = []
            first = True
            for k in range(s, e + 1):
                if SEP_RE.match(body[k]):
                    continue
                if first:
                    first = False   # 헤더 행 제외(그룹헤더 평탄화로 표기가 다름)
                    continue
                mfps.append(norm("".join(x._cc_cells(body[k]))))
            gl = [f for f in gfps if f]
            ml = [f for f in mfps if f]
            gset, mset = set(gl), set(ml)
            miss = gset - mset
            extra = mset - gset
            if miss:
                fail("c_cells", p, f"grid {gi}: {len(miss)} data rows missing in md")
            if extra and len(extra) > len(gset):
                fail("c_cells", p, f"grid {gi}: md has {len(extra)} unmatched rows")
            # 순서 보존 비교(Advisor 부수 지적): 집합 동일이어도 행 순서가 다르면 실패
            if not miss and not extra and gl != ml:
                fail("c_cells", p, f"grid {gi}: row order/multiplicity mismatch "
                                   "(같은 행 집합, 순서 또는 중복 수 상이)")
        for (s, e, kind) in blocks:
            if kind != "gfm":
                continue
            data = [k for k in range(s, e + 1) if not SEP_RE.match(body[k])]
            if len(data) <= 1 and (e - s) <= 2:
                fail("d_structure", p, f"empty table fragment at line {s}")
        # ── e_order ──
        vec_lines, vl_ok = x._xpage_page_vec_lines(pdf_path, p)
        if vl_ok and grids:
            items = []
            for (i, n, t, c) in mdcaps:
                if c:
                    continue
                if n in vnums:
                    vy = next(y0 for (num, text, y0, y1) in caps if num == n)
                    items.append((i, vy, f"cap T{n}"))
            for i, l in enumerate(body):
                if HEAD_RE.match(l) and not CAP_RE.match(l):
                    hn = norm(re.sub(r"^\s*#{1,6}\s*", "", l))
                    hits = [(y0, y1) for (t2, y0, y1) in vec_lines if t2 == hn]
                    if len(hits) == 1:
                        items.append((i, hits[0][0], f"head '{l.strip()[:30]}'"))
            for bi, (s, e, kind) in enumerate(blocks):
                gi = mapping.get(bi)
                if gi is not None:
                    items.append((s, grids[gi][1][1], f"table g{gi}"))
            items.sort()
            ys = [it[1] for it in items]
            for a in range(1, len(ys)):
                if ys[a] < ys[a - 1] - 2.0:
                    fail("e_order", p, f"order violation: {items[a - 1][2]}"
                                       f"(y={ys[a - 1]:.0f}) before {items[a][2]}"
                                       f"(y={ys[a]:.0f})")
        # ── g_prose(R9 F5): 페이지 벡터 '산문 블록' 커버리지 게이트 ──
        #    진실원 = PDF 벡터 텍스트(find_tables 자기정합과 별개 축). extract 의
        #    하이브리드 완전성 가드와 동일 로직(_line_covered 60% 토큰겹침 +
        #    _norm_present substring)을 import 재사용 — SSoT, 재구현 금지.
        #    제외: 표 bbox 내 라인(c_cells 소관) / 회전 워터마크·러닝헤더푸터·페이지
        #    번호(_twocol_lines_from_dict F1/F2 필터 SSoT) / 캡션(a_captions 소관) /
        #    유의미 토큰 <2 라인(라벨·숫자 — extract 커버리지 계약과 동일하게 중립).
        #    오탐 방지: '블록 전체'(유의미 라인 전부)가 미커버일 때만 fail —
        #    문장 단위 paraphrase 는 블록 부분커버로 통과. 블록에 유의미 토큰 ≥3
        #    라인이 하나도 없으면 스킵(스트레이 단어 블록 배제, extract 가드 계약).
        try:
            pg = pdoc[p - 1]
            d = pg.get_text("dict")
            clean = x._twocol_lines_from_dict(
                d, pg.rect.height or 1.0, pg.rect.width or 1.0)
            clean_join = " || ".join(x._norm_present(l[4]) for l in clean)
            body_tokens = set()
            for bl in body:
                body_tokens.update(x._sig_tokens(bl))
            body_norm = x._norm_present("\n".join(body))
            gboxes = [b for (_g2, b) in grids]

            def _in_grid(bb):
                cx, cy = (bb[0] + bb[2]) / 2.0, (bb[1] + bb[3]) / 2.0
                return any(gb[0] - 2 <= cx <= gb[2] + 2
                           and gb[1] - 2 <= cy <= gb[3] + 2 for gb in gboxes)

            for blk in d.get("blocks", []):
                if blk.get("type", 0) != 0:
                    continue
                sig_lines = []
                for ln in blk.get("lines", []):
                    t = "".join(s2.get("text", "")
                                for s2 in ln.get("spans", [])).strip()
                    if not t or _in_grid(ln["bbox"]) or CAP_RE.match(t):
                        continue
                    nt = x._norm_present(t)
                    if not nt or nt not in clean_join:
                        continue  # F1/F2 필터 제거분(워터마크/러닝헤더/페이지번호)
                    if len(x._sig_tokens(t)) < 2:
                        continue  # 라벨/짧은 조각 — 커버리지 중립(extract 계약)
                    sig_lines.append((t, nt))
                if not sig_lines or not any(
                        len(x._sig_tokens(t)) >= 3 for (t, _n) in sig_lines):
                    continue
                unc = [t for (t, nt) in sig_lines
                       if nt not in body_norm
                       and not x._line_covered(t, body_tokens)]
                if len(unc) == len(sig_lines):
                    fail("g_prose", p,
                         f"prose block fully missing ({len(sig_lines)} lines): "
                         f"'{sig_lines[0][0][:70]}'")
        except Exception as e2:  # noqa: BLE001 — 게이트 실행 실패는 가시화(수동검수)
            fail("g_prose", p, f"prose scan error (manual review): {e2}")

        # ── dup_prose(R13) WARN: 표 셀 문장(≥5 단어)이 표 밖 평문으로 그대로 중복 ──
        #    경고 수준(비차단, CLEAN/FAIL 불변). 원인은 완전성 가드가 grid 치환에서
        #    드롭된 표 셀 텍스트를 loose-prose 로 회수한 뒤 doc-level 표 복원과 겹친 것.
        #    판정: grid 셀(≥5 단어) 정규화 문자열을, 표 블록 밖 body 라인(≥5 단어)의
        #    정규화가 부분문자열로 포함하면(다문장 셀의 문장 단위 중복 포함) WARN.
        try:
            tbl_line_idx = set()
            for (bs, be, _bk) in blocks:
                tbl_line_idx.update(range(bs, be + 1))
            cell_norms = set()
            for (g, _b) in grids:
                for r in g:
                    for c in r:
                        ct = (c or "").strip()
                        if len(ct.split()) >= 5:
                            cn = norm(ct)
                            if cn:
                                cell_norms.add(cn)
            if cell_norms:
                for i, l in enumerate(body):
                    if i in tbl_line_idx:
                        continue
                    ls = l.strip()
                    if (not ls or ls.startswith("|") or ls.startswith("<!--")
                            or ls.startswith("#") or ls == "---"):
                        continue
                    if len(ls.split()) < 5:
                        continue
                    ln = norm(ls)
                    if ln and any(ln in cn for cn in cell_norms):
                        warn("dup_prose", p,
                             f"table cell sentence duplicated as loose prose: "
                             f"'{ls[:70]}'")
        except Exception as e3:  # noqa: BLE001 — 비차단(경고 채널)
            warn("dup_prose", p, f"dup_prose scan error: {e3}")

    counts = {}
    for f2 in fails:
        counts[f2["check"]] = counts.get(f2["check"], 0) + 1
    warn_counts = {}
    for w2 in warns:
        warn_counts[w2["check"]] = warn_counts.get(w2["check"], 0) + 1
    out = {
        "md": os.path.abspath(md_path),
        "pdf": os.path.abspath(pdf_path),
        "pages": npages,
        # 해석 주의: 표 계열 검사 CLEAN = find_tables 추출과의 자기정합(독립 진실원
        # 아님), g_prose(R9) = PDF 벡터 텍스트 별도 축 — 헤더 주석 참조.
        "truth_source": "find_tables+vector_text(g_prose)",
        "cont_pages": {str(k): f"Table {v[0]}" for k, v in sorted(cont_pages.items())},
        # status 는 fails 만 반영(R13 dup_prose 경고는 비차단 — CLEAN 판정 불변).
        "status": "CLEAN" if not fails else "FAIL",
        "summary": counts,
        "failures": fails,
        "warn_summary": warn_counts,
        "warnings": warns,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if not fails else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "error": str(e)}))
        sys.exit(2)
