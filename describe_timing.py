"""oversized 표 이미지 1장의 정밀분석(describe) 벽시계 시간 측정.

GPU가 비었을 때(재변환 종료 후) 실행해야 정확. p11 표 이미지를 원본에서 크롭 후
_maybe_describe_figure(qwen3-vl:32b, complex_table 경로) 호출까지의 시간을 초로 측정.
env EXTRACT_DESCRIBE_COMPLEX_TABLES=1, EXTRACT_FIGURE_DESC_MAX_TOKENS_COMPLEX=16384,
OLLAMA_CLOUD_TIMEOUT=1800 을 실행 커맨드에서 주입.
"""
import time
from pathlib import Path

import fitz
import fmdw.figure_extractor as fe

STEM = "LN08LPU_Design_Manual_A00-V0.9.2.0_testpages"
PDF = Path("input/pdf/test_pages") / (STEM + ".pdf")
TMP = Path("timing_p11_crop.png")
DPI = 300

# 1) p11 oversized 표 크롭 (결정적, 시간 무시 수준)
doc = fitz.open(str(PDF))
page = doc[10]  # p11 (0-index)
boxes = fe.detect_oversized_matrix_tables(page)
print("p11 oversized 검출:", len(boxes), "박스", flush=True)
if not boxes:
    raise SystemExit("검출 실패 — 게이트/임계 확인")
bbox = max(boxes, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
t_crop0 = time.time()
w, h = fe._crop_and_save(page, bbox, TMP, DPI)
print("크롭 완료 %dx%d px (%.2fs)" % (w, h, time.time() - t_crop0), flush=True)

# 2) describe 벽시계 측정
print("=== describe 시작 (qwen3-vl:32b, complex_table) ===", flush=True)
t0 = time.time()
desc = fe._maybe_describe_figure(TMP, caption="Table 21: Design Truth Table",
                                 item_type="complex_table")
elapsed = time.time() - t0
print("=== describe 완료 ===")
print("소요시간: %.1f초 (%.1f분)" % (elapsed, elapsed / 60))
print("설명 길이: %d자" % len(desc or ""))
print("--- 설명 앞 400자 ---")
print((desc or "<EMPTY>")[:400])
print("[TIMING DONE]")
