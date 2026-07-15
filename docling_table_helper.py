#!/usr/bin/env python
"""docling_table_helper.py — Docling 표 구조 추출 헬퍼 (R6, 전용 .docling-venv 실행 전용).

extract_all_via_pdf.py 의 F-DOCLING 폴백이 subprocess 로 호출한다(본 venv 오염 금지).
TableFormerMode.ACCURATE 고정(FAST 금지 — 2026-07-14 실험서 행 밀림 실측), do_ocr=False
(벡터텍스트 문서), do_cell_matching=True. 출력 = stdout 단일 JSON:
  {"tables": [{"index", "page", "num_rows", "num_cols",
               "grid": [[{"text", "bbox": [x0,y0,x1,y1]|null(top-left origin, pt),
                          "col_header": bool}, ...], ...]}, ...],
   "elapsed": sec}

#6(2026-07-15, Advisor 권고): --artifacts-path 명시 — 로컬 모델 캐시 경로를 고정해
캐시 부재 시 조용한 네트워크 다운로드 시도를 차단(100% 로컬 원칙). 기본값 =
$DOCLING_ARTIFACTS_PATH 또는 ~/.cache/docling/models. 경로 부재 = exit 3 (오프라인
잠금 — 모델 캐시를 먼저 준비해야 함, 네트워크 폴백 없음).
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument(
        "--artifacts-path",
        default=os.getenv("DOCLING_ARTIFACTS_PATH")
        or str(Path.home() / ".cache" / "docling" / "models"),
        help="Docling 모델 캐시 경로(부재 시 exit 3 — 네트워크 다운로드 금지)")
    a = ap.parse_args()

    artifacts = Path(a.artifacts_path)
    if not artifacts.is_dir():
        print(json.dumps({
            "error": "artifacts_path not found: %s — 오프라인 잠금(네트워크 다운로드 "
                     "차단). 모델 캐시를 먼저 준비하세요." % artifacts}))
        return 3

    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption

    opts = PdfPipelineOptions()
    opts.artifacts_path = str(artifacts)     # #6: 캐시 고정 — 네트워크 시도 차단
    opts.do_ocr = False                      # 벡터텍스트 문서 — OCR 불필요
    opts.do_table_structure = True
    opts.table_structure_options.mode = TableFormerMode.ACCURATE   # FAST 금지
    opts.table_structure_options.do_cell_matching = True
    conv = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    t0 = time.time()
    res = conv.convert(Path(a.pdf))
    doc = res.document
    out = {"tables": [], "elapsed": round(time.time() - t0, 1)}
    for ti, tbl in enumerate(doc.tables):
        page_no = tbl.prov[0].page_no if tbl.prov else 1
        ph = None
        try:
            ph = doc.pages[page_no].size.height
        except Exception:  # noqa: BLE001
            pass
        grid = []
        for row in tbl.data.grid:
            r = []
            for c in row:
                bb = None
                try:
                    if c.bbox is not None and ph is not None:
                        b = c.bbox.to_top_left_origin(ph)
                        bb = [round(b.l, 2), round(b.t, 2),
                              round(b.r, 2), round(b.b, 2)]
                except Exception:  # noqa: BLE001
                    bb = None
                r.append({"text": c.text or "", "bbox": bb,
                          "col_header": bool(getattr(c, "column_header", False))})
            grid.append(r)
        out["tables"].append({"index": ti, "page": page_no,
                              "num_rows": tbl.data.num_rows,
                              "num_cols": tbl.data.num_cols, "grid": grid})
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
