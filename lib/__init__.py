"""`lib` 하위호환 shim (Deprecated) — 실제 구현은 `fmdw` 패키지.

배경:
  패키지화 과정에서 `lib/` 디렉토리를 `fmdw/` 로 rename 했다(외부 프로젝트에서
  `import lib` 충돌 방지). 워크스페이스 내부 코드는 전부 `fmdw` 로 전환했으나,
  일부 골든(golden) 테스트(tests/test_migration_phase*.py, tests/_pipeline_recorder.py)는
  git 히스토리의 *통합 직전* extract_*.py 원본 소스를 런타임에 exec 한다.
  그 히스토리 소스들은 아직 `from lib import ...` / `from lib.pdf_pipeline import ...`
  를 사용하므로, 이를 깨지 않으려면 `lib` 이름이 살아 있어야 한다.

동작:
  이 shim 은 `fmdw.<submodule>` 을 그대로 `lib.<submodule>` 로 별칭(alias)한다.
  즉 `from lib import ollama_extractor` 와 `from lib.ollama_extractor import X`
  모두 실제 `fmdw` 모듈 객체를 반환한다(동일 인스턴스 — sys.modules 공유).
  import 시 1회 DeprecationWarning 을 발생시킨다.

신규 코드는 `fmdw` 를 직접 import 하라. 이 shim 은 골든 테스트 호환만을 위한 것이다.
"""

from __future__ import annotations

import importlib
import sys
import warnings

warnings.warn(
    "`lib` 패키지는 deprecated 입니다. `fmdw` 를 직접 import 하세요 "
    "(`from lib import X` → `from fmdw import X`). "
    "이 shim 은 골든 테스트 하위호환만을 위해 유지됩니다.",
    DeprecationWarning,
    stacklevel=2,
)

# fmdw 의 공개 서브모듈 — 히스토리 소스가 import 하는 것들을 포괄.
_SUBMODULES = (
    "config",
    "net_crosscheck",
    "ollama_extractor",
    "page_tier",
    "pdf_pipeline",
    "resume_cache",
    "vision_qa",
    "vision_qa_ensemble",
    "figure_extractor",
)

# 각 fmdw.<name> 을 import 한 뒤 lib.<name> 으로도 등록(sys.modules 공유) +
# 이 패키지의 속성으로 노출 → `from lib import <name>` / `from lib.<name> import ...`
# 양쪽 모두 동작한다. 일부 모듈이 (예: 광학 의존성 미설치로) import 실패해도
# 나머지는 계속 등록한다(degrade-safe).
for _name in _SUBMODULES:
    try:
        _mod = importlib.import_module(f"fmdw.{_name}")
    except Exception:  # noqa: BLE001 - 선택적 의존성 부재 등은 무시
        continue
    sys.modules[f"{__name__}.{_name}"] = _mod
    setattr(sys.modules[__name__], _name, _mod)

del importlib, sys, warnings, _name
