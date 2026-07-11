"""크롭된 초고밀도 표 2장에 정밀 설명 생성 → 사이드카 description + .md 주입."""
import json
from pathlib import Path

from fmdw.figure_extractor import _maybe_describe_figure

STEM = "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages"
MD = Path("output/pdf_md") / (STEM + ".md")
SIDE = Path("output/pdf_md") / (STEM + "_figures.json")
FIGDIR = Path("output/pdf_md/figures")

TARGETS = [
    ("%s__p10_ctable1" % STEM, "BEOL Metallization Options — Metallization Stack"),
    ("%s__p11_ctable1" % STEM, "Table 21: Design Truth Table"),
]

items = json.loads(SIDE.read_text(encoding="utf-8"))
md = MD.read_text(encoding="utf-8")

for fid, caption in TARGETS:
    png = FIGDIR / (fid + ".png")
    print("[describe 시작] %s" % png.name, flush=True)
    desc = _maybe_describe_figure(png, caption=caption)
    if not desc or not desc.strip():
        print("[!] 설명 생성 실패/비활성: %s" % fid, flush=True)
        continue
    desc = desc.strip()
    print("[describe 완료] %s (%d자)" % (fid, len(desc)), flush=True)
    # 사이드카 description 갱신
    for it in items:
        if it.get("figure_id") == fid:
            it["description"] = desc
    # .md: 해당 이미지 링크 바로 아래에 설명 블록 주입 (중복 방지)
    link = "![%s](figures/%s.png)" % (caption.replace("(원본 표 크롭)", "").strip(), fid)
    # 실제 링크 라인 탐색 (캡션 표기 차이 대비 figure_id로 찾음)
    lines = md.splitlines()
    for i, l in enumerate(lines):
        if fid + ".png" in l and l.startswith("!["):
            if i + 2 < len(lines) and lines[i + 2].startswith("> **표 설명"):
                break  # 이미 주입됨
            block = "\n> **표 설명 (AI 정밀분석)**: " + desc.replace("\n", "\n> ")
            lines[i] = l + "\n" + block
            md = "\n".join(lines)
            break

SIDE.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
MD.write_text(md if md.endswith("\n") else md + "\n", encoding="utf-8")
print("[DESCRIBE DONE]")
