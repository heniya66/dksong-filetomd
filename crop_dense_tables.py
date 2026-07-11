"""p10/p11 초고밀도 표를 PNG로 크롭해 .md 오염 표를 교체 (LLM 불필요, 결정적).

- 크롭: detect_complex_tables(벡터 분석) 우선, 실패 시 본문영역 휴리스틱.
- 사이드카: type="complex_table" 항목 추가 (일반표 아님 — 자가검증 룰 통과).
- .md: 융합·오염된 거대 <table> 라인을 두 이미지 링크로 교체.
"""
import json
from pathlib import Path

import fitz

STEM = "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages"
PDF = Path("input/pdf/test_pages") / (STEM + ".pdf")
MD = Path("output/pdf_md") / (STEM + ".md")
SIDE = Path("output/pdf_md") / (STEM + "_figures.json")
FIGDIR = Path("output/pdf_md/figures")
DPI = 300

PAGES = [
    (10, "BEOL Metallization Options — Metallization Stack (원본 표 크롭)"),
    (11, "Table 21: Design Truth Table (원본 표 크롭)"),
]

try:
    from fmdw.figure_extractor import detect_complex_tables
except Exception:
    detect_complex_tables = None

doc = fitz.open(str(PDF))
new_items = []
for pg, caption in PAGES:
    page = doc[pg - 1]
    rect = page.rect
    bbox = None
    if detect_complex_tables is not None:
        try:
            boxes = detect_complex_tables(page)
            if boxes:
                # 가장 큰 박스 채택
                bbox = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        except Exception as e:
            print("detect_complex_tables 실패(p%d): %s -> 휴리스틱 사용" % (pg, e))
    if bbox is None:
        # 본문영역 휴리스틱: 상단 헤더/하단 푸터 제외
        bbox = [rect.x0 + 20, rect.y0 + rect.height * 0.10,
                rect.x1 - 20, rect.y1 - rect.height * 0.07]
    clip = fitz.Rect(*bbox)
    pix = page.get_pixmap(dpi=DPI, clip=clip)
    name = "%s__p%02d_ctable1.png" % (STEM, pg)
    out = FIGDIR / name
    pix.save(str(out))
    print("[p%d] crop %dx%d -> %s" % (pg, pix.width, pix.height, out))
    new_items.append({
        "bbox": [round(v, 1) for v in bbox],
        "caption": caption,
        "description": "",
        "figure_id": "%s__p%02d_ctable1" % (STEM, pg),
        "figure_no": None,
        "image_path": "figures/" + name,
        "page": pg,
        "snap_iou": None,
        "source": "complex_table_crop",
        "type": "complex_table",
    })

# 사이드카 갱신 (중복 방지: 같은 figure_id 있으면 제거 후 추가)
items = json.loads(SIDE.read_text(encoding="utf-8"))
ids = {it["figure_id"] for it in new_items}
items = [it for it in items if it.get("figure_id") not in ids] + new_items
SIDE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
print("사이드카 총 %d개 항목" % len(items))

# .md 오염 표 교체
md = MD.read_text(encoding="utf-8")
marker = '<table><thead><tr><th rowspan="2">Structure</th>'
lines = md.splitlines()
hit = [i for i, l in enumerate(lines) if l.startswith(marker)]
if len(hit) != 1:
    raise SystemExit("교체 대상 표 라인 탐지 실패: %d개 매치 (수동 확인 필요)" % len(hit))
repl = (
    "> ⚠️ 아래 두 표는 초고밀도(rowspan 다중·회전 헤더)로 markdown 전사 시 "
    "왜곡·환각이 발생해 **원본 이미지 크롭**으로 대체함 (fmdw 특수표 규칙).\n\n"
    "**BEOL Metallization Options — Metallization Stack (문서 p87)**\n\n"
    "![BEOL Metallization Options](figures/%s__p10_ctable1.png)\n\n"
    "**Table 21: Design Truth Table (문서 p91)**\n\n"
    "![Table 21: Design Truth Table](figures/%s__p11_ctable1.png)"
) % (STEM, STEM)
lines[hit[0]] = repl
MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(".md 교체 완료 (라인 %d)" % (hit[0] + 1))
print("[CROPFIX DONE]")
