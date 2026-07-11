#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_worklog.py — _worklog 빌드기
--------------------------------------------------------------
cards/*.json (카드 1장 = 1파일) + meta.json (덱 정보) 를 읽어
index.html 이 읽는 slides_data.js (const DECK = {...}) 를 자동 생성한다.

- 카드 그룹: cover → front(표지뒤 안내) → stage(번호 본문) → appendix(부록)
  · 정렬: 그룹 우선, 그룹 내 파일명(NNN_*) 순
  · stage 카드는 정렬 순서대로 1,2,3... 번호를 자동 부여해 좌측 목차 라벨로 사용
- 카드 스키마(JSON):
  { "group": "front|stage|appendix",   # cover 타입은 group 불필요
    "label": "목차에 보일 내용(번호 없이)",  # 예: "검토 결과 보고서"
    "type": "cover|info|rich",
    "title": "...", "subtitle"/"note"(cover),
    "bullets": [...](info) | "html": "..."(rich),
    "links": [ {label, kind, note, href|pdf|video} ] }

실행: python3 _worklog/build_worklog.py   (직접 / init / 스킬에서 호출)
"""
import json, os, sys, glob, datetime

GROUP_ORDER = {"cover": 0, "front": 1, "stage": 2, "appendix": 3}


def group_of(card):
    if card.get("type") == "cover":
        return "cover"
    g = card.get("group", "stage")
    return g if g in GROUP_ORDER else "stage"


def main():
    wl = os.path.dirname(os.path.abspath(__file__))
    cards_dir = os.path.join(wl, "cards")
    meta_path = os.path.join(wl, "meta.json")
    out_path = os.path.join(wl, "slides_data.js")

    meta = {}
    if os.path.exists(meta_path):
        try:
            meta = json.load(open(meta_path, encoding="utf-8"))
        except Exception as e:
            print(f"[!] meta.json 오류: {e}", file=sys.stderr); sys.exit(1)

    files = sorted(glob.glob(os.path.join(cards_dir, "*.json")))
    cards = []
    for f in files:
        try:
            c = json.load(open(f, encoding="utf-8"))
        except Exception as e:
            print(f"[!] {os.path.basename(f)} JSON 오류: {e}", file=sys.stderr); sys.exit(1)
        c["__file"] = os.path.basename(f)
        cards.append(c)

    cards.sort(key=lambda c: (GROUP_ORDER[group_of(c)], c["__file"]))

    slides, num = [], 0
    for c in cards:
        g = group_of(c)
        s = {k: v for k, v in c.items()
             if k not in ("__file", "group", "label")}
        if g == "cover":
            pass  # 표지는 kicker 없음 (뷰어가 title 사용)
        elif g == "stage":
            num += 1
            s["kicker"] = f'{num} · {c.get("label", c.get("kicker", ""))}'
        else:  # front / appendix
            s["kicker"] = c.get("label", c.get("kicker", ""))
        slides.append(s)

    updated = meta.get("updated") or datetime.date.today().isoformat()
    deck = {
        "title": meta.get("title", "작업 진행 타임라인"),
        "subtitle": meta.get("subtitle", ""),
        "updated": updated,
        "project": meta.get("project", ""),
        "slides": slides,
    }
    js = ("/* 자동 생성 파일 — 직접 수정 금지.\n"
          "   카드는 cards/NNN_*.json 편집, 덱 정보는 meta.json 편집 후\n"
          "   python3 _worklog/build_worklog.py 실행해 재생성한다. */\n"
          "const DECK = " + json.dumps(deck, ensure_ascii=False, indent=2) + ";\n")
    open(out_path, "w", encoding="utf-8").write(js)
    print(f"[OK] {len(slides)}개 카드 → slides_data.js (stage {num}개, updated {updated})")


if __name__ == "__main__":
    main()
