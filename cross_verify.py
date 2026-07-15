#!/usr/bin/env python
"""cross_verify.py — 독립 진실원(PDF 벡터텍스트) 셀 단위 대조/교체 도구 (2026-07-14).

doc_audit 의 한계(truth_source=find_tables 자기정합)를 메운다: 산출 MD 그리드 표의
각 셀을 원본 PDF 의 '그 셀 bbox 안 벡터텍스트'(F1 회전 워터마크 제외 — extract 모듈
`_grid_clean_cell` 재사용)와 대조한다. "증명 가능한 값 수정"의 유일 합법 경로.

usage:
  .venv/bin/python cross_verify.py <md_path> <pdf_path>            # dry-run(기본)
  .venv/bin/python cross_verify.py <md_path> <pdf_path> --apply    # 실제 교체
출력: stdout 단일 JSON. exit 0 = 불일치 0(또는 apply 성공+audit CLEAN),
      1 = 불일치 존재(dry-run) 또는 queued 존재, 2 = 실행 오류/apply 후 audit 회귀(복원됨).

교체 조건(엄격): 벡터텍스트가 해당 셀 bbox 안에 명확히 존재(비어있지 않음) + 행/열
1:1 위치 매칭이 무모호할 때만. 모호(벡터 빈 값·행수 불일치·열 정렬 불가·bbox 없음·
blindspot 페이지)는 교체하지 않고 사람검수 큐(queued)로 — --apply 시 `<stem>_qa.json`
의 `cross_verify_queue` 에 등재.

감사 추적: 모든 교체 후보/실행을 {page, table, row, col, md→vec, bbox} 로 JSON 기록.
--apply 시 대상 MD 를 `<md>.bak-crossverify` 로 백업(최초 1회 보존, 덮어쓰지 않음),
교체 후 doc_audit.py 재실행해 CLEAN 유지 확인 — 회귀 시 백업 복원 후 exit 2.
"""
import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SEP_RE = re.compile(r"^\s*\|[\s:|\-]+\|\s*$")


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def load_x():
    sys.path.insert(0, BASE)
    spec = importlib.util.spec_from_file_location(
        "xmod_cv", os.path.join(BASE, "extract_all_via_pdf.py"))
    x = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(x)
    return x


def grids_with_bboxes(x, pdf_path, page):
    """well-formed find_tables 표: [(rows_text, rows_cell_bbox, table_bbox)] (y0 순).

    셀 텍스트 = extract 모듈 `_grid_clean_cell`(셀 bbox 내 수평 벡터텍스트, F1 회전
    워터마크 제외) — 파이프라인과 동일 추출 규칙의 '독립 재실행'."""
    import fitz

    out = []
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:  # noqa: BLE001
        return out
    try:
        pg = doc[page - 1]
        try:
            tabs = pg.find_tables()
        except Exception:  # noqa: BLE001
            return out
        for t in tabs.tables:
            try:
                rows = [[x._grid_clean_cell(pg, c) for c in r.cells] for r in t.rows]
            except Exception:  # noqa: BLE001
                continue
            if x._table_well_formed(rows):
                bbs = [[tuple(c) if c else None for c in r.cells] for r in t.rows]
                # R7 F-SUBROW: extract 공유 병합 규칙을 grid 측에 동일 적용
                # (md 는 병합 렌더 — 행 1:1 대조 유지, 병합 셀 bbox 는 union)
                rows, bbs = x._subrow_merge_rows(rows, bbs)
                out.append((rows, bbs, tuple(t.bbox)))
    finally:
        doc.close()
    out.sort(key=lambda g: g[2][1])
    return out


def clean_cell_text(s):
    """벡터 셀 값 → GFM 셀 표기(공백 정규화 + 파이프 이스케이프)."""
    return re.sub(r"\s+", " ", (s or "")).strip().replace("|", "\\|")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("md_path")
    ap.add_argument("pdf_path")
    ap.add_argument("--apply", action="store_true",
                    help="실제 교체(기본 dry-run). 백업+audit 회귀가드 포함")
    args = ap.parse_args()
    os.chdir(BASE)
    x = load_x()
    import fitz

    md_path = os.path.abspath(args.md_path)
    pdf_path = os.path.abspath(args.pdf_path)
    md = open(md_path, encoding="utf-8").read()
    lines = md.split("\n")
    mk = {}
    for i, l in enumerate(lines):
        m = re.match(r"<!-- page (\d+) -->", l)
        if m:
            mk[int(m.group(1))] = i
    npages = fitz.open(pdf_path).page_count

    replace_cands = []   # 교체 후보(명확 증거): dict
    queued = []          # 사람검수 큐(모호): dict
    n_tables = 0
    n_cells = 0

    # ── R12(2026-07-15): 페이지 걸침 sub-row 이동을 grid 측에 동일 적용 ──
    # extract _xpage_registry_pass 가 continued 선두 fragment 를 이전 페이지 체인
    # 행에 병합·이동하므로(md), 행 1:1 대조도 같은 이동을 grid 표현에 적용해야
    # 어긋나지 않는다 — 공유 SSoT x._xpage_subrow_migrate (재구현 금지).
    # 체인 판정(페이지-로컬 복제): ①연속 페이지 grids 존재 ②cont 첫 표가 페이지
    # 상단(_STRADDLE_TOP_Y0_MAX) ③헤더 지문 일치(prev 마지막 grid ↔ cont 첫 grid)
    # ④cont 첫 grid 에 자기 벡터 캡션 owner 없음(신규 표 배제 — audit gcap 규칙).
    # 병합 열 bbox 는 SSoT 가 None 처리 → 불일치여도 자동 교체 없이 큐(안전측).
    g3_by_page = {}
    for p in range(1, npages + 1):
        if p in mk:
            g3_by_page[p] = grids_with_bboxes(x, pdf_path, p)
    for p in sorted(g3_by_page):
        prev, cur = g3_by_page.get(p - 1), g3_by_page.get(p)
        if not prev or not cur:
            continue
        prow, pbbs, ptb = prev[-1]
        crow, cbbs, ctb = cur[0]
        if (ctb[1] or 0.0) > x._STRADDLE_TOP_Y0_MAX:
            continue
        if len(crow[0]) != len(prow[0]) or \
                x._cc_norm_cells(crow[0]) != x._cc_norm_cells(prow[0]):
            continue
        try:
            caps, vok = x._xpage_vec_table_captions(pdf_path, p)
        except Exception:  # noqa: BLE001
            caps, vok = [], False
        if vok:
            taken = set()
            for (_num, _text, _y0, y1) in sorted(caps, key=lambda c: c[3]):
                best_gap, best_gi = None, None
                for gi2, (_r2, _b2, tb2) in enumerate(cur):
                    gap = tb2[1] - y1
                    if gap >= -5 and (best_gap is None or gap < best_gap):
                        best_gap, best_gi = gap, gi2
                if best_gi is not None and best_gi not in taken:
                    taken.add(best_gi)
            if 0 in taken:
                continue   # 첫 grid 에 자기 캡션 — 신규 표(체인 아님)
        # 적응 가드(audit 동일 규칙): md 가 선두 파편을 보존(구 R7 형식)하면 grid 측도
        # 보존 — 구 산출물 검증 오탐 방지. md 파편 부재 시에만 R12 이동 적용(md 가
        # 파편을 '유실'했다면 이전 페이지 행 대조에서 불일치로 드러남 — 건전성 유지).
        nxt_p = min([v for k2, v in mk.items() if k2 > p], default=len(lines))
        body_p = lines[mk[p] + 1:nxt_p]
        blocks_p = x._xpage_blocks_with_html(body_p)
        first_gfm = next((b for b in blocks_p if b[2] == "gfm"), None)
        md_keeps_frag = False
        if first_gfm:
            nd = [k2 for k2 in range(first_gfm[0], first_gfm[1] + 1)
                  if not SEP_RE.match(body_p[k2])][1:]
            if nd:
                md_keeps_frag = x._subrow_is_fragment(x._cc_cells(body_p[nd[0]]))
        if md_keeps_frag:
            continue
        hn = x._grid_header_rows(crow)
        pr2, cr2, nmig, pb2, cb2 = x._xpage_subrow_migrate(
            prow, crow[hn:], pbbs, cbbs[hn:])
        if nmig:
            g3_by_page[p - 1][-1] = (pr2, pb2, ptb)
            g3_by_page[p][0] = (list(crow[:hn]) + cr2,
                                list(cbbs[:hn]) + cb2, ctb)

    for p in range(1, npages + 1):
        if p not in mk:
            continue
        nxt = min([v for k, v in mk.items() if k > p], default=len(lines))
        b0, b1 = mk[p] + 1, nxt          # body 절대 라인 범위 [b0, b1)
        body = lines[b0:b1]
        g3 = g3_by_page.get(p) or []
        blocks = x._xpage_blocks_with_html(body)
        gfm_blocks = [(s, e, k) for (s, e, k) in blocks if k == "gfm"]
        if not g3:
            # ── blindspot 페이지: bbox 근거 없음 → 존재성 대조만(교체 불가) ──
            if gfm_blocks:
                vl, vok = x._xpage_page_vec_lines(pdf_path, p)
                vec_all = "".join(t for (t, _y0, _y1) in vl) if vok else ""
                for (s, e, _k) in gfm_blocks:
                    for k in range(s + 1, e + 1):
                        if SEP_RE.match(body[k]):
                            continue
                        for j, c in enumerate(x._cc_cells(body[k])):
                            n_cells += 1
                            cn = norm(c)
                            if cn and vok and cn not in vec_all:
                                queued.append({
                                    "page": p, "table": None, "row_line": b0 + k,
                                    "col": j, "md": c[:60], "vec": None,
                                    "reason": "blindspot(find_tables 미검출) — "
                                              "페이지 벡터텍스트에 셀 값 미발견, 수동 확인"})
            continue
        grids_for_map = [(rows, tb) for (rows, _bbs, tb) in g3]
        mapping = x._xpage_match_blocks_to_grids(body, blocks, grids_for_map)
        for bi, gi in sorted(mapping.items()):
            s, e, kind = blocks[bi]
            if kind != "gfm":
                continue
            rows, bbs, _tb = g3[gi]
            n_tables += 1
            # md 데이터 행(헤더 1행 + 구분선 제외)
            m_idx = [k for k in range(s, e + 1) if not SEP_RE.match(body[k])][1:]
            hdr_line_norm = norm("".join(x._cc_cells(body[s])))
            # grid 데이터 행(2단 그룹헤더의 서브헤더 행 제외 — doc_audit 동일 규칙)
            g_rows, g_bbs = [], []
            for r, rb in zip(rows[1:], bbs[1:]):
                nonempty = [norm(c) for c in r if (c or "").strip()]
                if (len(nonempty) < len(r) and nonempty
                        and all(c2 in hdr_line_norm for c2 in nonempty)):
                    continue
                g_rows.append([c or "" for c in r])
                g_bbs.append(rb)
            if len(m_idx) != len(g_rows):
                queued.append({
                    "page": p, "table": gi, "row_line": None, "col": None,
                    "md": f"{len(m_idx)} md rows", "vec": f"{len(g_rows)} grid rows",
                    "reason": "행수 불일치 — 1:1 행 매칭 불가(수동 확인)"})
                continue
            # 전 행 빈 유령 열 제거(파이프라인 동일 규칙) → 열 정렬
            ncol_g = len(rows[0])
            ghost = [j for j in range(ncol_g)
                     if all(not (r[j] if j < len(r) else "").strip()
                            for r in ([rows[0]] + g_rows))]
            keep = [j for j in range(ncol_g) if j not in ghost]
            for ri, (k, grow, gbb) in enumerate(zip(m_idx, g_rows, g_bbs)):
                mcells = x._cc_cells(body[k])
                gcells = [grow[j] if j < len(grow) else "" for j in keep]
                gcellb = [gbb[j] if j < len(gbb) else None for j in keep]
                if len(mcells) != len(gcells):
                    queued.append({
                        "page": p, "table": gi, "row_line": b0 + k, "col": None,
                        "md": f"{len(mcells)} cols", "vec": f"{len(gcells)} cols",
                        "reason": "열수 불일치 — 열 정렬 모호(수동 확인)"})
                    continue
                for j, (mc, gc, gb) in enumerate(zip(mcells, gcells, gcellb)):
                    n_cells += 1
                    if norm(mc) == norm(gc):
                        continue
                    if not (gc or "").strip():
                        queued.append({
                            "page": p, "table": gi, "row_line": b0 + k, "col": j,
                            "md": mc[:60], "vec": "",
                            "reason": "벡터 빈 값 — 교체 증거 없음(수동 확인)"})
                        continue
                    if gb is None:
                        queued.append({
                            "page": p, "table": gi, "row_line": b0 + k, "col": j,
                            "md": mc[:60], "vec": gc[:60],
                            "reason": "셀 bbox 없음(병합셀) — 위치 증거 불충분(수동 확인)"})
                        continue
                    replace_cands.append({
                        "page": p, "table": gi, "row": ri, "col": j,
                        "row_line": b0 + k, "md": mc, "vec": clean_cell_text(gc),
                        "bbox": [round(v, 1) for v in gb]})

    mode = "apply" if args.apply else "dry-run"
    replaced = 0
    audit_after = None
    if args.apply and replace_cands:
        bak = md_path + ".bak-crossverify"
        if not os.path.exists(bak):
            shutil.copy2(md_path, bak)
        by_line = {}
        for rc in replace_cands:
            by_line.setdefault(rc["row_line"], []).append(rc)
        for ln, rcs in by_line.items():
            cells = x._cc_cells(lines[ln])
            for rc in rcs:
                if rc["col"] < len(cells) and norm(cells[rc["col"]]) == norm(rc["md"]):
                    cells[rc["col"]] = rc["vec"]
                    replaced += 1
                    print(f"    [CROSS-VERIFY] p{rc['page']} t{rc['table']} "
                          f"r{rc['row']}c{rc['col']}: '{rc['md'][:40]}' → "
                          f"'{rc['vec'][:40]}' (bbox {rc['bbox']})", flush=True)
            lines[ln] = "| " + " | ".join(cells) + " |"
        # ── 회귀 가드(Advisor Minor-2: try/finally — write 이후 어떤 예외에도 복원 보장) ──
        _guard_ok = False
        try:
            open(md_path, "w", encoding="utf-8").write("\n".join(lines))
            r = subprocess.run(
                [sys.executable, os.path.join(BASE, "doc_audit.py"), md_path, pdf_path],
                capture_output=True, text=True)
            audit_after = "CLEAN" if r.returncode == 0 else "FAIL"
            _guard_ok = (r.returncode == 0)
        finally:
            if not _guard_ok:
                shutil.copy2(bak, md_path)
                audit_after = (audit_after or "FAIL") + "→REVERTED(백업 복원)"
    if args.apply and queued:
        stem = os.path.splitext(os.path.basename(md_path))[0]
        qa_p = os.path.join(os.path.dirname(md_path), f"{stem}_qa.json")
        try:
            rec = json.load(open(qa_p, encoding="utf-8")) if os.path.exists(qa_p) \
                else {"source_file": stem}
        except Exception:  # noqa: BLE001
            rec = {"source_file": stem}
        rec["cross_verify_queue"] = queued
        open(qa_p, "w", encoding="utf-8").write(
            json.dumps(rec, ensure_ascii=False, indent=2) + "\n")

    out = {
        "md": md_path, "pdf": pdf_path, "mode": mode,
        "truth_source": "pdf_vector_text(cell bbox, F1 rotation-filtered)",
        "pages": npages, "tables_checked": n_tables, "cells_checked": n_cells,
        "mismatches": len(replace_cands), "replaced": replaced,
        "queued": len(queued), "audit_after_apply": audit_after,
        "mismatch_list": replace_cands, "queued_list": queued,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if args.apply and audit_after and "REVERTED" in audit_after:
        return 2
    if replace_cands and not args.apply:
        return 1
    return 1 if queued else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"status": "ERROR", "error": str(e)}))
        sys.exit(2)
