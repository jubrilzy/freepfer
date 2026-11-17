"""
FreePfer — Flask backend
Endpoints:
  GET  /                      → editor UI
  GET  /wake                  → wake-up landing page
  GET  /ping                  → health check
  POST /upload                → upload PDF, returns session_id + page 0 image + spans
  GET  /page/<sid>/<n>        → render page n as base64 PNG
  POST /edit                  → apply a text edit, returns updated page image
  GET  /download/<sid>        → download edited PDF
  GET  /gfont/<family>        → download & cache a Google Font, return TTF as base64
"""

import os
import io
import re
import uuid
import base64
import tempfile
import threading
import urllib.request
from pathlib import Path
from flask import (Flask, request, jsonify, send_file,
                   render_template, abort)

from core import PDFEditor
from font_matcher import FontMatcher

# Google Font cache directory
GFONT_CACHE = "/tmp/gfonts"
os.makedirs(GFONT_CACHE, exist_ok=True)

def download_gfont(family: str) -> str | None:
    """Download a Google Font TTF, cache it, return local path or None."""
    safe = re.sub(r'[^a-zA-Z0-9_-]', '_', family)
    path = os.path.join(GFONT_CACHE, f"{safe}.ttf")
    if os.path.exists(path):
        return path
    try:
        css_url = f"https://fonts.googleapis.com/css2?family={family.replace(' ', '+')}:wght@400&display=swap"
        req = urllib.request.Request(css_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            css = r.read().decode()
        # Prefer TTF, fall back to woff2
        m = re.search(r'src:\s*url\(([^)]+\.ttf)\)', css)
        if not m:
            m = re.search(r'url\(([^)]+)\)\s*format\([\'"]truetype[\'"]\)', css)
        if not m:
            m = re.search(r'url\(([^)]+\.woff2)\)', css)
        if not m:
            m = re.search(r'url\(([^)]+)\)', css)
        if not m:
            return None
        font_url = m.group(1).strip("'\"")
        req2 = urllib.request.Request(font_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req2, timeout=15) as r2:
            data = r2.read()
        with open(path, "wb") as f:
            f.write(data)
        return path
    except Exception as e:
        print(f"[gfont] download failed for '{family}': {e}")
        return None

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

# ------------------------------------------------------------------ #
#  Session store — disk-backed so sessions survive restarts/deploys   #
# ------------------------------------------------------------------ #

SESSION_DIR = "/tmp/freepfer_sessions"
os.makedirs(SESSION_DIR, exist_ok=True)

_sessions: dict[str, dict] = {}   # sid → {editor, pdf_path}
_sessions_lock = threading.Lock()

def _session_pdf_path(sid: str) -> str:
    d = os.path.join(SESSION_DIR, sid)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "current.pdf")

def _new_session(editor: PDFEditor, tmp_path: str) -> str:
    sid = uuid.uuid4().hex
    pdf_path = _session_pdf_path(sid)
    # Persist to disk immediately
    _save_editor(editor, pdf_path)
    with _sessions_lock:
        _sessions[sid] = {"editor": editor, "pdf_path": pdf_path}
    # Clean up original tmp file
    try:
        if tmp_path != pdf_path:
            os.unlink(tmp_path)
    except Exception:
        pass
    return sid

def _save_editor(editor: PDFEditor, path: str) -> None:
    """Save editor state to disk without closing the document."""
    tmp = path + ".tmp"
    editor.save(tmp)
    os.replace(tmp, path)

def _get_session(sid: str) -> dict:
    with _sessions_lock:
        s = _sessions.get(sid)
    if s:
        return s
    # Not in memory — try to reload from disk (survives restarts)
    pdf_path = os.path.join(SESSION_DIR, sid, "current.pdf")
    if os.path.exists(pdf_path):
        try:
            editor = PDFEditor(pdf_path)
            with _sessions_lock:
                _sessions[sid] = {"editor": editor, "pdf_path": pdf_path}
            return _sessions[sid]
        except Exception as e:
            print(f"[session] reload failed for {sid}: {e}")
    abort(404, "Session not found or expired")

# ------------------------------------------------------------------ #
#  Font matcher (singleton, loaded once at startup)                   #
# ------------------------------------------------------------------ #

_matcher: FontMatcher | None = None
_matcher_lock = threading.Lock()

def get_matcher() -> FontMatcher:
    global _matcher
    if _matcher is None:
        with _matcher_lock:
            if _matcher is None:
                _matcher = FontMatcher()
    return _matcher

# Pre-load in background so first request isn't slow
threading.Thread(target=get_matcher, daemon=True).start()

# ------------------------------------------------------------------ #
#  Helpers                                                             #
# ------------------------------------------------------------------ #

def _page_to_b64(editor: PDFEditor, page_num: int, zoom: float = 1.5) -> str:
    pix = editor.render_page(page_num, zoom)
    png_bytes = pix.tobytes("png")
    return base64.b64encode(png_bytes).decode()

def _images_to_json(editor: PDFEditor, page_num: int) -> list:
    out = []
    for img in editor.get_images(page_num):
        out.append({
            "bbox":   list(img.bbox),
            "width":  img.width,
            "height": img.height,
            "xref":   img.xref,
            "ext":    img.ext,
        })
    return out

def _spans_to_json(editor: PDFEditor, page_num: int) -> list:
    out = []
    for s in editor.get_spans(page_num):
        import re
        clean_font = re.sub(r'^[A-Z]{6}\+', '', s.font_name)
        out.append({
            "text":      s.text,
            "font":      clean_font,
            "size":      round(s.font_size, 1),
            "bbox":      list(s.bbox),   # [x0,y0,x1,y1] in PDF points
            "page":      s.page_num,
            "color":     s.color,
            "flags":     s.flags,
        })
    return out

# ------------------------------------------------------------------ #
#  Routes                                                              #
# ------------------------------------------------------------------ #

@app.route("/wake")
def wake():
    return render_template("wake.html")

@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f = request.files["file"]
    if not f.filename.lower().endswith(".pdf"):
        return jsonify({"error": "Only PDF files accepted"}), 400

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    f.save(tmp.name)
    tmp.close()

    try:
        editor = PDFEditor(tmp.name)
    except Exception as e:
        os.unlink(tmp.name)
        return jsonify({"error": str(e)}), 400

    sid = _new_session(editor, tmp.name)

    return jsonify({
        "session_id":  sid,
        "page_count":  editor.page_count,
        "page_image":  _page_to_b64(editor, 0),
        "spans":       _spans_to_json(editor, 0),
        "images":      _images_to_json(editor, 0),
        "fonts":       editor.get_unique_fonts(),
    })

@app.route("/page/<sid>/<int:page_num>")
def get_page(sid, page_num):
    s = _get_session(sid)
    editor: PDFEditor = s["editor"]
    if page_num < 0 or page_num >= editor.page_count:
        abort(400, "Page out of range")
    return jsonify({
        "page_image": _page_to_b64(editor, page_num),
        "spans":      _spans_to_json(editor, page_num),
        "images":     _images_to_json(editor, page_num),
    })

@app.route("/edit", methods=["POST"])
def edit():
    data = request.get_json()
    sid       = data.get("session_id")
    page_num  = data.get("page")
    bbox      = data.get("bbox")        # [x0,y0,x1,y1]
    new_text  = data.get("new_text", "").strip()
    font_name = data.get("font", "")

    if not all([sid, bbox, new_text]):
        return jsonify({"error": "Missing fields"}), 400

    s = _get_session(sid)
    editor: PDFEditor = s["editor"]
    matcher = get_matcher()

    # Find the matching span by bbox
    target = None
    for span in editor.get_spans(page_num):
        sx0, sy0, sx1, sy1 = span.bbox
        bx0, by0, bx1, by1 = bbox
        if (abs(sx0 - bx0) < 1 and abs(sy0 - by0) < 1 and
                abs(sx1 - bx1) < 1 and abs(sy1 - by1) < 1):
            target = span
            break

    if not target:
        return jsonify({"error": "Span not found — page may have changed"}), 404

    # Google Font override?
    gfont_name = data.get("gfont", "").strip()
    if gfont_name:
        font_path = download_gfont(gfont_name)
    else:
        font_path = matcher.match(target.font_name)
    result = editor.replace_text(target, new_text, font_path)

    _save_editor(editor, s["pdf_path"])  # persist after edit
    return jsonify({
        "success":        result.success,
        "font_preserved": result.font_preserved,
        "font_used":      result.font_used,
        "message":        result.message,
        "page_image":     _page_to_b64(editor, page_num),
        "spans":          _spans_to_json(editor, page_num),
        "images":         _images_to_json(editor, page_num),
    })

@app.route("/move", methods=["POST"])
def move():
    data      = request.get_json()
    sid       = data.get("session_id")
    page_num  = data.get("page")
    bbox      = data.get("bbox")
    new_x     = data.get("new_x")
    new_y     = data.get("new_y")
    if not all([sid, bbox is not None, new_x is not None, new_y is not None]):
        return jsonify({"error": "Missing fields"}), 400
    s = _get_session(sid)
    editor: PDFEditor = s["editor"]
    target = None
    for span in editor.get_spans(page_num):
        sx0, sy0, sx1, sy1 = span.bbox
        if abs(sx0 - bbox[0]) < 1 and abs(sy0 - bbox[1]) < 1:
            target = span; break
    if not target:
        return jsonify({"error": "Span not found"}), 404
    result = editor.move_text(target, new_x, new_y)
    return jsonify({
        "success":    result.success,
        "page_image": _page_to_b64(editor, page_num),
        "spans":      _spans_to_json(editor, page_num),
        "images":     _images_to_json(editor, page_num),
    })


@app.route("/image_op", methods=["POST"])
def image_op():
    op       = request.form.get("op")          # replace | resize | delete
    sid      = request.form.get("session_id")
    page_num = int(request.form.get("page", 0))
    bbox     = [float(v) for v in request.form.get("bbox", "").split(",")]
    s = _get_session(sid)
    editor: PDFEditor = s["editor"]

    if op == "delete":
        editor.delete_image(page_num, bbox)
    elif op == "resize":
        new_bbox = [float(v) for v in request.form.get("new_bbox", "").split(",")]
        editor.resize_image(page_num, bbox, new_bbox)
    elif op == "replace":
        f = request.files.get("image")
        if not f:
            return jsonify({"error": "No image file"}), 400
        new_bbox_str = request.form.get("new_bbox", "")
        new_bbox = [float(v) for v in new_bbox_str.split(",")] if new_bbox_str else None
        editor.replace_image(page_num, bbox, f.read(), new_bbox)
    else:
        return jsonify({"error": "Unknown op"}), 400

    _save_editor(editor, s["pdf_path"])  # persist after image op
    return jsonify({
        "success":    True,
        "page_image": _page_to_b64(editor, page_num),
        "spans":      _spans_to_json(editor, page_num),
        "images":     _images_to_json(editor, page_num),
    })


@app.route("/gfont/<path:family>")
def gfont(family):
    """Return cached Google Font TTF as base64, or 404."""
    path = download_gfont(family)
    if not path:
        return jsonify({"error": "Font not found"}), 404
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode()
    return jsonify({"family": family, "ttf_b64": data})

@app.route("/download/<sid>")
def download(sid):
    # Try disk-first so download works even if memory session expired
    pdf_path = os.path.join(SESSION_DIR, sid, "current.pdf")
    if not os.path.exists(pdf_path):
        # Try loading into memory (may 404 if truly gone)
        _get_session(sid)
        pdf_path = os.path.join(SESSION_DIR, sid, "current.pdf")

    # Save latest in-memory edits to disk before serving
    with _sessions_lock:
        s = _sessions.get(sid)
    if s:
        _save_editor(s["editor"], pdf_path)

    if not os.path.exists(pdf_path):
        abort(404, "Session not found or expired")

    # Read into BytesIO so the file isn't locked
    out = io.BytesIO()
    with open(pdf_path, "rb") as fh:
        out.write(fh.read())
    out.seek(0)

    return send_file(
        out,
        mimetype="application/pdf",
        as_attachment=True,
        download_name="freepfer-edited.pdf",
    )

# ------------------------------------------------------------------ #
#  Run                                                                 #
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
