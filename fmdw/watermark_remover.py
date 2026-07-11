"""
watermark_remover.py — offline PDF tracking-watermark removal for fmdw.

Targets DRM / viewer-tracking watermarks that stamp rotated, faint text onto
every page via Optional-Content (OCG) Form XObjects — e.g. the Samsung design
manual's "Samsung Confidential" + per-viewer "ml.ko at <timestamp>" stamp.

How the watermark is built (reverse-engineered on LN08LPU_Design_Manual):
  * Each page's /Contents array carries two tiny dedicated streams, each
    `/OC /PropN BDC  q <45deg-rotation cm> /GSn gs /ImX Do  Q  EMC`.
  * /ImX is a small Form XObject whose own stream is
    `/OC /PropM BDC q <light-blue rg> BT /F0 20 Tf [<hex text>] TJ ET Q EMC`.
  * The OCGs are NOT registered in /OCProperties/D, so standard layer toggles
    (set_layer / layer_ui_configs / basestate) cannot reach them, and PyMuPDF
    renders the form content regardless of the page-level marked-content state.

The watermark is drawn in TWO visible layers that must BOTH be removed:
  (1) faint light-blue *text* stamps rendered via Form XObjects (the selectable
      "Samsung Confidential" + "ml.ko at <timestamp>"), and
  (2) a pink "Samsung Confidential" *raster image* (a large soft-masked Image
      XObject reused on ~every page) — this is the dominant visible layer.
Removing only (1) leaves the pink image; removing only (2) leaves the text.

Winning strategy (lossless, Direction B), three complementary steps:
  1. Blank the watermark *Form XObject* text streams (faint text layer). The
     stamp then disappears from the text layer and from any renderer that draws
     the form.
  2. Strip the now-dangling page-level rotated OC marked-content blocks (tidy).
  3. Make the watermark *raster image* invisible by zeroing its soft-mask
     (SMask) alpha to fully transparent. The image is identified as a large
     (>=400 px) soft-masked Image XObject that is drawn on many pages — a
     textbook repeated overlay, never one-off page content.
Body text, vector drawings and raster figures are left untouched.

Public API:
  is_enabled()                              -> bool  (reads EXTRACT_REMOVE_WATERMARK)
  remove_watermarks_in_doc(doc)             -> dict  (in-place; counts)
  make_clean_pdf(src, dst=None)             -> str   (save cleaned temp PDF)
  maybe_clean_pdf(src)                      -> str   (cached; original path if disabled)
  diagnose_page(doc, pno)                   -> dict
  scrub_pixmap_color(pix, ...)              -> Pixmap (Direction A raster fallback)

Offline only — no network, no external binaries; requires PyMuPDF (fitz).
"""
from __future__ import annotations
import atexit
import logging
import os
import re
import tempfile

_log = logging.getLogger(__name__)

# ── env flag ────────────────────────────────────────────────────────────────
_TRUTHY = {"1", "true", "on", "yes", "y"}

# ── temp-file lifecycle ───────────────────────────────────────────────────────
# make_clean_pdf 가 tempfile.mkstemp 로 만든 정제 PDF 경로를 추적한다. 캐시(_CLEAN_CACHE)
# 덕분에 문서당 1회만 생성되고 프로세스 수명 동안 재사용되므로 즉시 삭제하지 않고,
# 프로세스 종료 시 atexit 으로 일괄 정리해 /tmp 누수를 막는다(장기 실행 ON 대비).
_TEMP_FILES: set[str] = set()


def _cleanup_temp_files() -> None:
    """프로세스 종료 시 생성한 정제 PDF 임시파일을 모두 unlink(누수 방지)."""
    for path in list(_TEMP_FILES):
        try:
            os.unlink(path)
        except OSError:
            pass  # 이미 삭제/이동되었으면 무시
        finally:
            _TEMP_FILES.discard(path)


# import 시 1회 등록(핸들러는 _TEMP_FILES 가 비면 no-op → OFF 경로 무해).
atexit.register(_cleanup_temp_files)


def is_enabled() -> bool:
    """True iff EXTRACT_REMOVE_WATERMARK requests watermark removal (default OFF)."""
    return os.getenv("EXTRACT_REMOVE_WATERMARK", "").strip().lower() in _TRUTHY


# ── signatures ────────────────────────────────────────────────────────────────
# 45-degree rotation matrix (cos45 == sin45 ~= 0.7071) used by the page-level
# stamp blocks. Ordinary tagged content (/Artifact, /Span) lacks this matrix.
_ROT45 = re.compile(rb'-?0\.7071\d*\s+-?0\.7071\d*\s+-?0\.7071\d*\s+-?0\.7071\d*')

# Page-level Optional-Content marked-content block: /OC /PropN BDC ... EMC
_OC_BDC_BLOCK = re.compile(rb'/OC\s*/[A-Za-z0-9]+\s+BDC.*?EMC', re.DOTALL)

# An RGB fill "r g b rg" with every channel light (>= 0.6) — watermark faintness.
_RG_FILL = re.compile(rb'(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+(-?\d*\.?\d+)\s+rg')

# Upper bound on a watermark *stamp* form's stream size (real content forms are
# far larger; the stamps observed are ~120-160 bytes).
_MAX_STAMP_BYTES = 2048


def _is_watermark_form(stream: bytes) -> bool:
    """Heuristic: is this Form XObject stream a faint optional-content text stamp?

    Requires ALL of: small size, optional-content marked (BDC), a text-show
    (BT + TJ/Tj), and a light RGB fill (all channels >= 0.6). This precisely
    matches tracking/confidential watermark stamps while never matching ordinary
    page content forms (which are large and not OC-wrapped faint single stamps).
    """
    if not stream or len(stream) > _MAX_STAMP_BYTES:
        return False
    if b'BDC' not in stream:
        return False
    if b'BT' not in stream or (b'TJ' not in stream and b'Tj' not in stream):
        return False
    for m in _RG_FILL.finditer(stream):
        try:
            vals = [float(x) for x in m.groups()]
        except ValueError:
            continue
        if all(0.6 <= v <= 1.0 for v in vals):
            return True
    return False


def _clean_page_oc_blocks(data: bytes):
    """Remove page-level rotated OC marked-content blocks. Returns (new, n)."""
    if b'BDC' not in data:
        return data, 0
    removed = 0

    def _repl(m: "re.Match") -> bytes:
        nonlocal removed
        if _ROT45.search(m.group(0)):
            removed += 1
            return b' '
        return m.group(0)

    return _OC_BDC_BLOCK.sub(_repl, data), removed


_NAME_REF = r'/([A-Za-z0-9.]+)\s+%d 0 R'


def _find_watermark_images(doc, min_dim: int = 400, min_draws: int = 10):
    """Identify watermark *raster* images: large (>= min_dim px) soft-masked
    Image XObjects that are drawn (via their resource name's `Do`) on at least
    `min_draws` pages — i.e. a repeated full-page overlay, not one-off content.
    Returns a set of image xrefs. Robust against shared/inherited resources
    (counts content-stream draws, not object references)."""
    cands = []
    for xref in range(1, doc.xref_length()):
        try:
            if doc.xref_get_key(xref, "Subtype") != ("name", "/Image"):
                continue
            keys = doc.xref_get_keys(xref)
            if "SMask" not in keys:
                continue
            ww = int(doc.xref_get_key(xref, "Width")[1])
            hh = int(doc.xref_get_key(xref, "Height")[1])
        except Exception:
            continue
        if ww >= min_dim and hh >= min_dim:
            cands.append(xref)
    if not cands:
        return set()
    # Resolve each candidate's resource name(s) by scanning object sources once.
    name_map: dict = {}
    for ox in range(1, doc.xref_length()):
        try:
            src = doc.xref_object(ox, compressed=True)
        except Exception:
            continue
        if " 0 R" not in src:
            continue
        for c in cands:
            for m in re.finditer(_NAME_REF % c, src):
                name_map.setdefault(c, set()).add(m.group(1))
    # Count `/name Do` invocations across all page content streams.
    try:
        blob = b"\n".join((doc.xref_stream(x) or b'')
                          for page in doc for x in page.get_contents())
    except Exception:
        blob = b''
    out = set()
    for c in cands:
        draws = 0
        for nm in name_map.get(c, ()):  # tolerant of whitespace incl. newline
            draws += len(re.findall((r'/%s\s+Do' % re.escape(nm)).encode(), blob))
        if draws >= min_draws:
            out.add(c)
    return out


def _neutralize_image(doc, xref) -> bool:
    """Make a watermark image fully transparent by zeroing its soft-mask alpha.
    Version-independent (uses only update_stream/xref_set_key). Returns success."""
    sm = doc.xref_get_key(xref, "SMask")
    if not (sm and sm[0] == "xref"):
        return False
    try:
        smx = int(sm[1].split()[0])
        sw = int(doc.xref_get_key(smx, "Width")[1])
        sh = int(doc.xref_get_key(smx, "Height")[1])
    except Exception:
        return False
    # Normalise the mask to an 8-bit DeviceGray with no inverting /Decode, then
    # fill with 0x00 (alpha 0 => fully transparent everywhere).
    for k, v in (("Decode", "null"), ("BitsPerComponent", "8"),
                 ("ColorSpace", "/DeviceGray"), ("Interpolate", "null")):
        try:
            doc.xref_set_key(smx, k, v)
        except Exception:
            pass
    try:
        doc.update_stream(smx, b"\x00" * (sw * sh), compress=True)
        return True
    except Exception:
        return False


def remove_watermarks_in_doc(doc) -> dict:
    """Direction B (in place). Blank watermark Form XObjects, strip page-level
    rotated OC blocks, and neutralise the watermark raster image(s).
    Returns {'forms': n, 'page_blocks': n, 'images': n, 'image_xrefs': [..]}.
    'image_xrefs' lists the neutralised raster watermark xrefs so callers can
    surface (log) exactly which images were touched — false-positive observable."""
    forms = 0
    # 1) Blank every watermark stamp Form XObject (faint text layer).
    for xref in range(1, doc.xref_length()):
        try:
            st = doc.xref_stream(xref)
        except Exception:
            st = None
        if not st:
            continue
        if doc.xref_get_key(xref, "Subtype") != ("name", "/Form"):
            continue
        if _is_watermark_form(st):
            try:
                doc.update_stream(xref, b" ")
                forms += 1
            except Exception:
                pass
    # 2) Strip dangling page-level OC stamp blocks (tidy output; helps other renderers).
    blocks = 0
    for page in doc:
        for xref in page.get_contents():
            data = doc.xref_stream(xref) or b''
            new, n = _clean_page_oc_blocks(data)
            if n:
                doc.update_stream(xref, new)
                blocks += n
    # 3) Neutralise the watermark raster image(s) — the dominant visible layer.
    images = 0
    image_xrefs: list[int] = []
    try:
        for xr in _find_watermark_images(doc):
            if _neutralize_image(doc, xr):
                images += 1
                image_xrefs.append(xr)
    except Exception:
        pass
    return {"forms": forms, "page_blocks": blocks, "images": images,
            "image_xrefs": image_xrefs}


def make_clean_pdf(src_path: str, dst_path: str | None = None) -> str:
    """Open src, remove watermarks, save a cleaned PDF (garbage-collected to drop
    orphaned watermark XObjects). Returns dst path. Always returns a valid PDF."""
    import fitz
    doc = fitz.open(src_path)
    try:
        counts = remove_watermarks_in_doc(doc)
        is_temp = dst_path is None
        if dst_path is None:
            fd, dst_path = tempfile.mkstemp(prefix="fmdw_wmclean_", suffix=".pdf")
            os.close(fd)
            # Register for atexit cleanup *immediately* after the temp file exists,
            # before doc.save — so a save failure still leaves the (already-created)
            # temp file tracked and reclaimed at exit. set.add is idempotent.
            # (caller-supplied dst is the caller's own → never auto-tracked.)
            _TEMP_FILES.add(dst_path)
        doc.save(dst_path, garbage=3, deflate=True)
    finally:
        doc.close()
    # Surface what was invalidated so over-/under-removal is observable (no secrets).
    _log.info(
        "watermark removal on %s → forms=%d page_blocks=%d images=%d "
        "image_xrefs=%s tmp=%s",
        os.path.basename(src_path), counts.get("forms", 0),
        counts.get("page_blocks", 0), counts.get("images", 0),
        counts.get("image_xrefs", []), (dst_path if is_temp else "caller-dst"),
    )
    return dst_path


# ── cached path swap for the fmdw render pipeline ─────────────────────────────
_CLEAN_CACHE: dict = {}


def maybe_clean_pdf(src_path: str) -> str:
    """Return a watermark-free PDF path for `src_path`.

    * If EXTRACT_REMOVE_WATERMARK is off -> returns `src_path` unchanged
      (zero overhead, zero behavioural change — the default).
    * If on -> builds (once, cached per path+mtime+size) a cleaned temp PDF and
      returns its path. On any failure, falls back to the original path so the
      conversion never breaks.
    """
    if not is_enabled():
        return src_path
    try:
        key = os.path.abspath(src_path)
        stt = os.stat(key)
        sig = (key, int(stt.st_mtime), stt.st_size)
    except OSError:
        return src_path
    cached = _CLEAN_CACHE.get(key)
    if cached and cached[0] == sig and os.path.exists(cached[1]):
        return cached[1]
    try:
        clean = make_clean_pdf(src_path)
        _CLEAN_CACHE[key] = (sig, clean)
        return clean
    except Exception:
        return src_path


def diagnose_page(doc, pno: int) -> dict:
    """Count rotated text lines + page-level watermark OC blocks on one page."""
    page = doc[pno]
    rot = 0
    for b in page.get_text("rawdict").get("blocks", []):
        for l in b.get("lines", []):
            d = l.get("dir", (1, 0))
            if abs(d[0] - 1.0) > 0.01 or abs(d[1]) > 0.01:
                rot += 1
    wm = 0
    for xref in page.get_contents():
        for m in _OC_BDC_BLOCK.finditer(doc.xref_stream(xref) or b''):
            if _ROT45.search(m.group(0)):
                wm += 1
    return {"page": pno, "rotated_text_lines": rot, "watermark_oc_blocks": wm}


# ── Direction A: raster fallback (only if B ever removes nothing) ──────────────
def scrub_pixmap_color(pix, samples=None, tol=40, target=(255, 255, 255)):
    """Replace watermark-coloured pixels in a rendered pixmap with white.
    Conservative, to protect figures. Requires numpy. Returns a new fitz.Pixmap."""
    import numpy as np
    import fitz
    if samples is None:
        samples = [(0xC5, 0xD9, 0xF1)]  # observed watermark fill (light blue)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
        pix.height, pix.width, pix.n).copy()
    rgb = arr[:, :, :3].astype(np.int16)
    mask = np.zeros((pix.height, pix.width), dtype=bool)
    for (r, g, b) in samples:
        mask |= ((np.abs(rgb[:, :, 0] - r) <= tol)
                 & (np.abs(rgb[:, :, 1] - g) <= tol)
                 & (np.abs(rgb[:, :, 2] - b) <= tol))
    arr[mask, 0], arr[mask, 1], arr[mask, 2] = target
    return fitz.Pixmap(fitz.csRGB, pix.width, pix.height,
                       arr[:, :, :3].tobytes(), False)
