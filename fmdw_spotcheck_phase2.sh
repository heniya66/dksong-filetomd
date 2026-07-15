#!/bin/bash
# fmdw_spotcheck_phase2.sh — STEP5 비전 스팟체크 결정론 래퍼 (#5 v6.1, 2026-07-15)
# E3 1차 실패 교훈: "JSON 전재·큐 반영"을 모델에 맡기면 누락된다 → 실행+리포트 §6
# 첨부를 스크립트가 결정론으로 수행(전재 = cat 보증, 날조 원천 차단). 발동 여부
# 판단(vision_check 조건)만 모델 몫.
# 사용: fmdw_spotcheck_phase2.sh <input_pdf> <output_subdir>
# 산출: output/<subdir>/spotcheck.json + goose_qa_report.md 에 "## 6. 비전 스팟체크"
#       (JSON 원문 + human_gate_pages 자동 큐 라인) append.
# exit: 0=all match / 1=suspect 존재(정상) / 2=실행 오류
set -u
cd /Users/dksong/workspace/filetomd

PDF="${1:?usage: fmdw_spotcheck_phase2.sh <input_pdf> <output_subdir>}"
OUTSUB="${2:?usage: fmdw_spotcheck_phase2.sh <input_pdf> <output_subdir>}"
OUTDIR="output/$OUTSUB"
PY=.venv/bin/python

[ -f "$OUTDIR/qa_scan.json" ] || { echo "SPOTCHECK-STATUS: FAIL no-qa_scan.json"; exit 2; }
[ -f "$OUTDIR/goose_qa_report.md" ] || { echo "SPOTCHECK-STATUS: FAIL no-report"; exit 2; }

"$PY" vision_spotcheck.py "$PDF" "$OUTDIR" --scan-json "$OUTDIR/qa_scan.json" \
  > "$OUTDIR/spotcheck.json" 2>"$OUTDIR/spotcheck.err"
RC=$?
if [ $RC -ge 2 ] || [ ! -s "$OUTDIR/spotcheck.json" ]; then
  echo "SPOTCHECK-STATUS: FAIL rc=$RC"; tail -5 "$OUTDIR/spotcheck.err" 2>/dev/null; exit 2
fi

"$PY" - "$OUTDIR" <<'PYEOF'
import json
import sys

outdir = sys.argv[1]
sc = json.load(open(outdir + "/spotcheck.json", encoding="utf-8"))
lines = [
    "",
    "## 6. 비전 스팟체크 (STEP5 — phase2 스크립트 자동 첨부, 수정 금지)",
    "",
    "```json",
    json.dumps(sc, ensure_ascii=False, indent=2),
    "```",
    "",
    "### 6.1 HUMAN-GATE 자동 반영 (human_gate_pages 기준)",
    "",
]
pages = sc.get("human_gate_pages") or []
if pages:
    reasons = {r.get("page"): r.get("reason", "") for r in sc.get("results", [])}
    for p in pages:
        lines.append("- p%s 비전 스팟체크 suspect: %s" % (p, reasons.get(p, "")))
else:
    lines.append("- 해당 없음 (suspect 페이지 0)")
lines.append("")
with open(outdir + "/goose_qa_report.md", "a", encoding="utf-8") as f:
    f.write("\n".join(lines))
s = sc.get("summary", {})
print("SPOTCHECK-STATUS: OK match=%s suspect=%s unknown=%s human_gate_pages=%s"
      % (s.get("match"), s.get("suspect"), s.get("unknown"), pages))
PYEOF
exit $RC
