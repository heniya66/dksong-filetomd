#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
작업 타임라인 뷰어 — localhost 서버 (프로젝트별 고유 포트 자동 분배)
--------------------------------------------------------------
이 폴더(_worklog)를 localhost 웹사이트로 띄웁니다.
설치할 것 없음 (파이썬 기본 기능만 사용).

★ 포트는 프로젝트마다 자동으로 다르게 배정됩니다 (겹침 방지).
  - 중앙 등록부: ~/workspace/_shared/worklog/worklog_ports.json
  - 등록부에 내 프로젝트가 있으면 그 포트를, 없으면 빈 포트를 새로 받아 등록합니다.
  - 실행하면 실제 주소가 출력되고 Chrome 으로 자동으로 열립니다.

실행:
  python3 serve.py
끄기:
  터미널에서 Ctrl + C
"""
import http.server, webbrowser, os, sys, json, socket, subprocess

PORT_BASE = 8765
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 프로젝트 루트(예: 04_ai-edge-box)를 서빙 → 03_DOC 등 다른 폴더 문서도 링크로 열람 가능
DIRECTORY = os.path.dirname(SCRIPT_DIR)
PROJECT_KEY = DIRECTORY  # 절대경로를 등록부 키로 사용 (프로젝트 고유)
REGISTRY = os.path.expanduser("~/workspace/_shared/worklog/worklog_ports.json")


def _load_registry():
    try:
        with open(REGISTRY, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_registry(reg):
    try:
        os.makedirs(os.path.dirname(REGISTRY), exist_ok=True)
        tmp = REGISTRY + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(reg, f, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, REGISTRY)
    except Exception:
        pass


def _is_free(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def resolve_port():
    """등록부에서 내 포트를 찾고, 없으면 (기존 최대+1)부터 빈 포트를 새로 배정·등록."""
    reg = _load_registry()
    if PROJECT_KEY in reg:
        return int(reg[PROJECT_KEY])
    used = set()
    for v in reg.values():
        try:
            used.add(int(v))
        except (TypeError, ValueError):
            pass
    cand = (max(used) + 1) if used else PORT_BASE
    while cand in used or not _is_free(cand):
        cand += 1
    reg[PROJECT_KEY] = cand
    _save_registry(reg)
    return cand


PORT = resolve_port()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    # 갱신한 내용이 새로고침에 바로 반영되도록 캐시 끔
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    # 텍스트 파일(.md 등)을 UTF-8로 표기 → 크롬에서 한글 안 깨짐
    def guess_type(self, path):
        ctype = super().guess_type(path)
        p = str(path).lower()
        if p.endswith((".md", ".markdown", ".txt", ".py", ".js", ".json", ".csv", ".yaml", ".yml")):
            base = (ctype or "text/plain").split(";")[0]
            if p.endswith((".md", ".markdown", ".txt")):
                base = "text/plain"
            return base + "; charset=utf-8"
        return ctype

    # PDF 뷰어가 연결을 중간에 끊어도(BrokenPipe) 조용히 넘어감
    def copyfile(self, source, outputfile):
        try:
            super().copyfile(source, outputfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass  # 접속 로그 숨김 (조용히 실행)


def main():
    os.chdir(DIRECTORY)
    # 멀티스레드 서버: 요청 하나가 막혀도 다른 요청은 정상 처리 (PDF 등 멈춤 방지)
    try:
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        print(f"포트 {PORT} 사용 중입니다. 이미 이 프로젝트 worklog 가 실행 중일 수 있어요.")
        print(f"브라우저에서 http://localhost:{PORT}/_worklog/index.html 를 직접 열어보세요.")
        sys.exit(1)

    url = f"http://localhost:{PORT}/_worklog/index.html"
    print("=" * 52)
    print("  작업 타임라인 뷰어 실행 중")
    print(f"  프로젝트: {os.path.basename(DIRECTORY)}")
    print(f"  주소: {url}")
    print("  끄기: Ctrl + C")
    print("=" * 52)
    # 워크로그는 항상 크롬에서 열기 (그래야 내부 링크도 모두 크롬 새 탭에서 열림)
    opened = False
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", url])
        opened = True
        print("  (Google Chrome 로 열기)")
    except Exception:
        pass
    if not opened:
        try:
            webbrowser.open(url)  # 크롬이 없으면 기본 브라우저로 폴백
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료했습니다.")
        httpd.server_close()


if __name__ == "__main__":
    main()
