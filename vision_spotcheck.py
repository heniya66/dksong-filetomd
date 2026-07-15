#!/usr/bin/env python3
"""vision_spotcheck.py — 변환 MD 대 원본 PDF 페이지 비전 스팟체크 (Phase D' 2단계, 2026-07-14 신규).

결정론 하네스: 페이지 선정 = 결정론(랜덤 금지), LLM 비전 = "의심 플래깅"까지만(수정·확정 금지).
비전 = 로컬 Ollama qwen3-vl (/api/chat 네이티브 + num_ctx). anti-fabrication: 보이는 것만, 불확실=unknown.

사용:
    .venv/bin/python vision_spotcheck.py <pdf_path> <output_dir> \
        [--scan-json <qa_scan JSON 파일>] [--pages 1,8,13] [--k 3] [--dpi 150] \
        [--model qwen3-vl:8b-instruct-q8_0] [--host http://localhost:11434] [--num-ctx 16384]

출력: stdout 단일 JSON. exit 0 = all match / 1 = suspect 존재 / 2 = 사용 오류.
"""
import argparse
import base64
import json
import re
import sys
import urllib.request
from pathlib import Path

PAGE_MARKER_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")

PROMPT = (
    "You compare a rendered PDF page image with the Markdown excerpt that was "
    "transcribed from that same page.\n"
    "Answer with ONLY one JSON object, no other text:\n"
    '{"verdict": "match" | "suspect" | "unknown", "reason": "<one short sentence>", '
    '"checked_aspects": ["body_text", "captions", "table_titles", "headings"]}\n'
    "Rules (STRICT):\n"
    "- verdict=suspect ONLY if you can clearly SEE in the image a body text block, "
    "caption, table title or heading that is ABSENT from or CONTRADICTS the Markdown excerpt.\n"
    "- Ignore formatting differences, line wrapping, table layout style, bold/italics, "
    "page headers/footers and watermarks.\n"
    "- If the image is unreadable, low quality, or you are not sure: verdict=unknown.\n"
    "- Report only what you can actually see. NEVER guess or invent content.\n"
    "\nMarkdown excerpt for this page:\n----------\n{md}\n----------\n"
)


def select_pages(n_pages, scan_json, figures_json, k):
    """결정론 페이지 선정: 스캔 지목 페이지 + 첫/마지막 + 균등 K."""
    pages, sources = set(), {}

    def add(p, why):
        if 1 <= p <= n_pages and p not in pages:
            pages.add(p)
            sources[p] = why

    if scan_json:
        for v in scan_json.get("html_table_violations", []):
            if v.get("page"):
                add(int(v["page"]), "html-table-violation")
        for c in scan_json.get("coverage_low", []):
            m = re.findall(r"\d+", str(c.get("pages") or ""))
            for p in m:
                add(int(p), "coverage-low")
    if figures_json:
        for f in figures_json if isinstance(figures_json, list) else []:
            kind = f.get("kind") or ""
            fid = f.get("figure_id") or ""
            if "oversized" in kind or "omtbl" in fid:
                if f.get("page"):
                    add(int(f["page"]), "oversized-table")
    add(1, "first-page")
    add(n_pages, "last-page")
    if k > 0 and n_pages > 2:
        step = (n_pages - 1) / (k + 1)
        for i in range(1, k + 1):
            add(int(round(1 + step * i)), "even-spacing")
    return sorted(pages), sources


def md_excerpt_for_page(md_text, page):
    """<!-- page N --> 마커 기준 해당 페이지 구간 추출."""
    parts = PAGE_MARKER_RE.split(md_text)
    # parts = [prefix, "1", content1, "2", content2, ...]
    for i in range(1, len(parts) - 1, 2):
        if int(parts[i]) == page:
            return parts[i + 1].strip()[:6000]
    return ""


def ollama_vision(host, model, num_ctx, prompt, png_bytes, timeout=240):
    payload = {
        "model": model,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0},
        "messages": [{
            "role": "user",
            "content": prompt,
            "images": [base64.b64encode(png_bytes).decode()],
        }],
    }
    req = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["message"]["content"]


def parse_verdict(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {"verdict": "unknown", "reason": "model returned no JSON", "checked_aspects": []}
    try:
        d = json.loads(m.group(0))
    except Exception:
        return {"verdict": "unknown", "reason": "model JSON unparseable", "checked_aspects": []}
    v = d.get("verdict")
    if v not in ("match", "suspect", "unknown"):
        v = "unknown"
    return {"verdict": v,
            "reason": str(d.get("reason", ""))[:200],
            "checked_aspects": d.get("checked_aspects", [])}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf_path")
    ap.add_argument("output_dir")
    ap.add_argument("--scan-json", default=None)
    ap.add_argument("--pages", default=None, help="쉼표 구분 페이지 목록 (선정 로직 override)")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--model", default="qwen3-vl:8b-instruct-q8_0")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--num-ctx", type=int, default=16384)
    args = ap.parse_args()

    try:
        import fitz  # PyMuPDF (fmdw venv)
    except ImportError:
        print(json.dumps({"error": "PyMuPDF(fitz) not available - run with fmdw venv python"}))
        return 2

    pdf = Path(args.pdf_path)
    out_dir = Path(args.output_dir)
    if not pdf.exists() or not out_dir.is_dir():
        print(json.dumps({"error": "pdf_path or output_dir not found"}))
        return 2

    md_files = sorted(p for p in out_dir.glob("*.md")
                      if not p.name.startswith("goose_qa_report")
                      and not p.name.endswith(".partial.md"))
    if not md_files:
        print(json.dumps({"error": "no converted md in output_dir"}))
        return 2
    md_text = md_files[0].read_text(encoding="utf-8", errors="replace")

    scan = None
    if args.scan_json and Path(args.scan_json).exists():
        scan = json.loads(Path(args.scan_json).read_text(encoding="utf-8"))
    figures = None
    figs_path = out_dir / f"{md_files[0].stem}_figures.json"
    if figs_path.exists():
        try:
            figures = json.loads(figs_path.read_text(encoding="utf-8"))
        except Exception:
            figures = None

    doc = fitz.open(pdf)
    n = doc.page_count
    if args.pages:
        pages = sorted({int(x) for x in args.pages.split(",") if x.strip()})
        sources = {p: "manual" for p in pages}
    else:
        pages, sources = select_pages(n, scan, figures, args.k)

    results = []
    for p in pages:
        page = doc.load_page(p - 1)
        pix = page.get_pixmap(dpi=args.dpi)
        png = pix.tobytes("png")
        excerpt = md_excerpt_for_page(md_text, p)
        if not excerpt:
            results.append({"page": p, "verdict": "suspect", "selected_by": sources.get(p),
                            "reason": "MD에 해당 페이지 구간(<!-- page N -->)이 없음",
                            "checked_aspects": ["page_presence"]})
            continue
        try:
            raw = ollama_vision(args.host, args.model, args.num_ctx,
                                PROMPT.replace("{md}", excerpt), png)
            v = parse_verdict(raw)
        except Exception as e:  # noqa: BLE001
            v = {"verdict": "unknown", "reason": f"vision call failed: {e}", "checked_aspects": []}
        v["page"] = p
        v["selected_by"] = sources.get(p)
        results.append(v)

    summary = {s: sum(1 for r in results if r["verdict"] == s) for s in ("match", "suspect", "unknown")}
    out = {
        "pdf": pdf.name,
        "md": md_files[0].name,
        "page_count": n,
        "dpi": args.dpi,
        "model": args.model,
        "pages_checked": pages,
        "selection_sources": {str(k): v for k, v in sources.items()},
        "results": results,
        "summary": summary,
        "human_gate_pages": [r["page"] for r in results if r["verdict"] == "suspect"],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if summary["suspect"] else 0


if __name__ == "__main__":
    sys.exit(main())
