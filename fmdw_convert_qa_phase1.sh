#!/bin/bash
# fmdw_convert_qa_phase1.sh — Goose 2-페이즈 Phase 1 (v2, #5 최종 라운드 2026-07-15)
#
# 설계 원칙(E/E3/E3b 교훈): "절차적 의무를 모델에 맡기지 않는다."
#   v1 = 변환 동기화 + QA 3종 + 리포트 스캐폴드(§3/§4는 모델 몫) → E3/E3b에서 모델이
#   툴 결과 후 무턴 종료해 §3/§4 미완성 2연속. v2 = §3 CLASSIFY / §4 HUMAN-GATE 도
#   **템플릿 기반 결정론 생성**(LLM 0, JSON 필드 전재+고정 문구만 — 창작 0) + 조건부
#   STEP5 비전 스팟체크(fmdw_spotcheck_phase2.sh)까지 내장. 모델 몫 = 요약 발화뿐.
#
# 사용: fmdw_convert_qa_phase1.sh <input_pdf> <domain> <output_subdir> [mode] [vision_check]
#   mode: full(기본) | qa_only(변환 생략)
#   vision_check: on_low_confidence(기본, human_gate_required=true 일 때만) | always | off
# 산출(output/<subdir>/): qa_scan.json / doc_audit.json / cross_verify.json /
#   phase1_summary.json / goose_qa_report.md(§1~§5 완성본, TODO 0) [+ spotcheck.json + §6]
# 마지막 줄: "PHASE1-STATUS: ..." (스팟체크 발동 시 그 요약 포함)
# exit: 0=PASS_CLEAN / 1=PASS_WITH_FLAGS / 2=사용 오류 / 3=프로세스 충돌 / 4=변환 실패
set -u
cd /Users/dksong/workspace/filetomd

PDF="${1:?usage: fmdw_convert_qa_phase1.sh <input_pdf> <domain> <output_subdir> [mode] [vision_check]}"
DOMAIN="${2:-datasheet}"
OUTSUB="${3:?usage: fmdw_convert_qa_phase1.sh <input_pdf> <domain> <output_subdir> [mode] [vision_check]}"
MODE="${4:-full}"
VISION_CHECK="${5:-on_low_confidence}"
OUTDIR="output/$OUTSUB"
LOG="output/convert_${OUTSUB}.log"
STEM="$(basename "$PDF" .pdf)"
MD="$OUTDIR/$STEM.md"
PY=.venv/bin/python

if [ ! -f "$PDF" ]; then
  echo "PHASE1-STATUS: FAIL usage(input_pdf not found: $PDF)"; exit 2
fi

CONV_RC=0
if [ "$MODE" != "qa_only" ]; then
  if pgrep -f "extract_all_via_pdf|convert_project|goose_convert_one" >/dev/null 2>&1; then
    echo "[phase1] 충돌 프로세스:"; pgrep -fl "extract_all_via_pdf|convert_project|goose_convert_one" || true
    echo "PHASE1-STATUS: FAIL process-conflict"; exit 3
  fi
  mkdir -p "$OUTDIR"
  echo "[phase1] CONVERT(동기·포그라운드) 시작: $PDF → $OUTDIR (domain=$DOMAIN)"
  ./goose_convert_one.sh "$PDF" "$OUTSUB" > "$LOG" 2>&1
  CONV_RC=$?
  if ! grep -q "ALL DONE" "$LOG"; then
    echo "[phase1] 변환 로그 마지막 20줄:"; tail -20 "$LOG"
    echo "PHASE1-STATUS: FAIL convert-not-finished (log: $LOG)"; exit 4
  fi
  echo "[phase1] CONVERT 종료 rc=$CONV_RC all_done=true (log=$LOG)"
else
  echo "[phase1] mode=qa_only — CONVERT 생략"
fi

if [ ! -f "$MD" ]; then
  echo "PHASE1-STATUS: FAIL converted-md-missing($MD)"; exit 4
fi

# ── 결정론 QA 3종(순차) — stdout 의 비-JSON 배너는 첫 '{' 부터 잘라 저장 ──
echo "[phase1] QA 1/3 qa_scan.py"
"$PY" qa_scan.py "$OUTDIR" --log "$LOG" > "$OUTDIR/qa_scan.json" 2>/dev/null
QA_RC=$?
echo "[phase1] QA 2/3 doc_audit.py"
"$PY" doc_audit.py "$MD" "$PDF" 2>/dev/null | sed -n '/^{/,$p' > "$OUTDIR/doc_audit.json"
AUDIT_RC=${PIPESTATUS[0]}
echo "[phase1] QA 3/3 cross_verify.py (dry-run)"
"$PY" cross_verify.py "$MD" "$PDF" 2>/dev/null | sed -n '/^{/,$p' > "$OUTDIR/cross_verify.json"
CV_RC=${PIPESTATUS[0]}
MD_SHA=$(shasum -a 256 "$MD" | awk '{print $1}')
echo "[phase1] md sha256=$MD_SHA (rc: qa=$QA_RC audit=$AUDIT_RC crossverify=$CV_RC)"

# ── 통합 요약 + 리포트 §1~§5 결정론 생성(§3/§4 = 템플릿+JSON 전재, 창작 0) ──
"$PY" - "$PDF" "$DOMAIN" "$OUTSUB" "$MODE" "$CONV_RC" "$QA_RC" "$AUDIT_RC" "$CV_RC" "$MD_SHA" "$STEM" <<'PYEOF'
import datetime
import json
import sys

pdf, domain, outsub, mode = sys.argv[1:5]
conv_rc, qa_rc, audit_rc, cv_rc = (int(v) for v in sys.argv[5:9])
md_sha, stem = sys.argv[9], sys.argv[10]
outdir = "output/" + outsub


def load(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"parse_error": str(e)}


qa = load(outdir + "/qa_scan.json")
audit = load(outdir + "/doc_audit.json")
cv = load(outdir + "/cross_verify.json")
sidecar = load(outdir + "/" + stem + "_qa.json")

clean = (audit.get("status") == "CLEAN" and cv.get("mismatches") == 0
         and cv.get("queued") == 0 and qa_rc == 0)
flags = bool(qa.get("human_gate_required")) or qa_rc == 1
status = "PASS_CLEAN" if clean and not flags else "PASS_WITH_FLAGS"

summary = {
    "phase1_version": "v2-260715 (deterministic sec3/sec4)",
    "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
    "input_pdf": pdf, "domain": domain, "output_subdir": outsub, "mode": mode,
    "convert": {"rc": conv_rc, "all_done": True,
                "log": "output/convert_%s.log" % outsub},
    "qa_scan": {"rc": qa_rc, "human_gate_required": qa.get("human_gate_required"),
                "summary": qa.get("summary")},
    "doc_audit": {"rc": audit_rc, "status": audit.get("status"),
                  "summary": audit.get("summary")},
    "cross_verify": {"rc": cv_rc, "tables_checked": cv.get("tables_checked"),
                     "cells_checked": cv.get("cells_checked"),
                     "mismatches": cv.get("mismatches"), "queued": cv.get("queued")},
    "md_sha256": md_sha,
    "final_status": status,
}
open(outdir + "/phase1_summary.json", "w", encoding="utf-8").write(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n")

# ── §3 CLASSIFY (결정론: JSON 필드 전재 + Skill 처방표 고정 문구) ──
sec3a = []
for d in qa.get("duplicate_captions", []):
    sec3a.append("- 중복 캡션 '%s' ×%s (%s) — 처방: qa_fix.py(값 불변 dedup) "
                 "[근거: qa_scan.json .duplicate_captions]"
                 % (d.get("caption"), d.get("count"),
                    "; ".join("%s:L%s" % (loc.get("file"), loc.get("line"))
                              for loc in d.get("locations", []))))
for d in qa.get("broken_image_refs", []):
    sec3a.append("- 깨진 이미지 링크 %s (%s) — 처방: qa_fix.py(유일 후보 경로 교정) "
                 "[근거: qa_scan.json .broken_image_refs]"
                 % (d.get("link"), d.get("file")))
sec3b = []
if qa.get("html_table_violations"):
    sec3b.append("- raw HTML 표 %d건 → FMDW_TABLE_FALLBACK=docling 폴백 재변환 "
                 "[근거: qa_scan.json .html_table_violations]"
                 % len(qa["html_table_violations"]))
if qa.get("coverage_low") or qa.get("partial_md") or qa.get("missing_markers"):
    sec3b.append("- 커버리지 저하/부분 산출/마커 결손 → EXTRACT_CHUNK_SIZE=3 재변환 "
                 "[근거: qa_scan.json .coverage_low(%d)/.partial_md(%d)/"
                 ".missing_markers(%d)]"
                 % (len(qa.get("coverage_low", [])), len(qa.get("partial_md", [])),
                    len(qa.get("missing_markers", []))))
if qa.get("unreadable_markers"):
    sec3b.append("- [판독 불가] %d건 → EXTRACT_RENDER_DPI=400 재변환 검토 "
                 "[근거: qa_scan.json .unreadable_markers]"
                 % qa["unreadable_markers"])
if qa.get("incomplete_conversion"):
    sec3b.append("- 변환 미완결 → 재실행 필요 [근거: qa_scan.json "
                 ".incomplete_reasons=%s]" % qa.get("incomplete_reasons"))
sec3c = []
qj = qa.get("qa_json", {}) or {}
if qj.get("requires_human_vision_qa"):
    sec3c.append("- 문서 전체 사람 비전 검수 대상 — requires_human_vision_qa=true"
                 "(사유: %s) [근거: qa_scan.json .qa_json]" % qj.get("reason"))
if qa.get("unreadable_markers"):
    sec3c.append("- [판독 불가] 마커 %d건 — 사람 판독 필요 "
                 "[근거: qa_scan.json .unreadable_markers]" % qa["unreadable_markers"])
if cv.get("queued"):
    sec3c.append("- cross_verify 사람검수 큐 %s건 — §4 목록 참조 "
                 "[근거: cross_verify.json .queued]" % cv.get("queued"))
for lst in (sec3a, sec3b, sec3c):
    if not lst:
        lst.append("- 해당 없음")

# ── §4 HUMAN-GATE 큐 (결정론: 항목별 전재) ──
sec4 = []
if qj.get("requires_human_vision_qa"):
    sec4.append("- 문서 전체: AI생성-미검증 + requires_human_vision_qa=true "
                "(사유: %s, 저신뢰 모델: %s) [근거: qa_scan.json .qa_json]"
                % (qj.get("reason"), qj.get("low_confidence_model")))
for it in (cv.get("queued_list") or []):
    sec4.append("- p%s table %s col %s: %s (md='%s') [근거: cross_verify.json "
                ".queued_list]" % (it.get("page"), it.get("table"), it.get("col"),
                                   it.get("reason"), str(it.get("md"))[:40]))
for n in (sidecar.get("xpage_registry_notes") or []):
    sec4.append("- xpage: %s [근거: %s_qa.json .xpage_registry_notes]" % (n, stem))
if qa.get("unreadable_markers"):
    sec4.append("- [판독 불가] 마커 %d건 [근거: qa_scan.json .unreadable_markers]"
                % qa["unreadable_markers"])
if not sec4:
    sec4.append("- 해당 없음")
sec4.append("- (비전 스팟체크 발동 시 suspect 페이지는 §6.1 에 자동 반영)")


def jblock(title, obj):
    return "### %s\n\n```json\n%s\n```\n" % (
        title, json.dumps(obj, ensure_ascii=False, indent=2))


parts = [
    "# fmdw 변환-QA 리포트 — %s" % outsub,
    "",
    "## 1. Parameters (phase1 스크립트 자동 기록)",
    "",
    "- input_pdf: %s" % pdf,
    "- domain: %s" % domain,
    "- output_subdir: %s" % outsub,
    "- mode: %s" % mode,
    "- 변환 완결: ALL DONE (rc=%d), md sha256=%s" % (conv_rc, md_sha),
    "",
    "## 2. 결정론 스캐너/감사 JSON 원문 (phase1 자동 삽입 — 수정 금지)",
    "",
    jblock("2.1 qa_scan.json", qa),
    jblock("2.2 doc_audit.json", audit),
    jblock("2.3 cross_verify.json (dry-run)", cv),
    jblock("2.4 phase1_summary.json", summary),
    "## 3. CLASSIFY (결정론 생성 — phase1 v2, JSON 필드 전재·창작 0)",
    "",
    "### (a) AUTO-FIX 후보(값 불변 서식) — 이 파일럿에선 목록만(자동 적용 없음)",
    "",
]
parts += sec3a
parts += ["", "### (b) 재변환 처방(파라미터 변경 — 수기 편집 금지)", ""]
parts += sec3b
parts += ["", "### (c) HUMAN-GATE(사람검수 — 자동 해소 금지)", ""]
parts += sec3c
parts += ["", "## 4. HUMAN-GATE 큐 (결정론 생성 — phase1 v2)", ""]
parts += sec4
parts += ["", "## 5. Provenance", "",
          "- 본 변환 MD 는 **AI생성-미검증** — 사람 승인 전까지 검증 완료로 간주하지 "
          "않는다.", ""]
open(outdir + "/goose_qa_report.md", "w", encoding="utf-8").write(
    "\n".join(parts) + "\n")
print("[phase1] report §1~§5 결정론 생성 완료: %s/goose_qa_report.md" % outdir)
PYEOF
GEN_RC=$?
if [ $GEN_RC -ne 0 ]; then
  echo "PHASE1-STATUS: FAIL report-generation(rc=$GEN_RC)"; exit 2
fi

# ── STEP5 비전 스팟체크(조건부, 결정론 — §6 자동 첨부) ──
HG=$(grep -o '"human_gate_required": [a-z]*' "$OUTDIR/phase1_summary.json" | head -1 | awk '{print $2}' | tr -d ',')
SC_NOTE="spotcheck=skipped(vision_check=$VISION_CHECK, human_gate_required=$HG)"
RUN_SC=false
case "$VISION_CHECK" in
  always) RUN_SC=true ;;
  off)    RUN_SC=false ;;
  *)      [ "$HG" = "true" ] && RUN_SC=true ;;
esac
if [ "$RUN_SC" = "true" ]; then
  echo "[phase1] STEP5 비전 스팟체크 실행(조건 충족)"
  bash ./fmdw_spotcheck_phase2.sh "$PDF" "$OUTSUB"
  SC_RC=$?
  if [ $SC_RC -ge 2 ]; then
    SC_NOTE="spotcheck=ERROR(rc=$SC_RC)"
  else
    SUSP=$(grep -o '"suspect": [0-9]*' "$OUTDIR/spotcheck.json" | head -1 | grep -o '[0-9]*')
    SC_NOTE="spotcheck=done(suspect=$SUSP, §6 반영)"
  fi
fi

FS=$(grep -o '"final_status": "[A-Z_]*"' "$OUTDIR/phase1_summary.json" | head -1 | cut -d'"' -f4)
TODO_N=$(grep -c "_TODO" "$OUTDIR/goose_qa_report.md" 2>/dev/null || true)
echo "PHASE1-STATUS: $FS (convert=ALL_DONE report=§1-§5+cond§6 TODO=$TODO_N $SC_NOTE)"
if [ "$FS" = "PASS_CLEAN" ]; then exit 0; else exit 1; fi
