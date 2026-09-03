"""Image -> Excel web application (Flask). Listens on port 8080."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)

import pipeline
from table_model import TableData, is_number, pivot, totals

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", BASE_DIR / "outputs"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SAMPLE_IMAGE = BASE_DIR / "data.jpeg"
RETENTION_SECONDS = 48 * 3600
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp", "image/bmp", "image/tiff", "image/gif"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("imagetoexcel")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
app.secret_key = os.environ.get("SECRET_KEY", uuid.uuid4().hex)
app.json.sort_keys = False  # keep column order in API responses (summary tiles)


# ---------------------------------------------------------------------------
def _cleanup_outputs():
    now = time.time()
    for job in OUTPUT_DIR.iterdir():
        try:
            if job.is_dir() and now - job.stat().st_mtime > RETENTION_SECONDS:
                for f in job.iterdir():
                    f.unlink()
                job.rmdir()
        except OSError:
            pass


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "image").stem
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "image"
    return stem[:60]


def _media_type(upload) -> str:
    mt = (upload.mimetype or "").lower()
    if mt in ALLOWED_TYPES:
        return "image/jpeg" if mt == "image/jpg" else mt
    ext = Path(upload.filename or "").suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff",
            ".tiff": "image/tiff", ".gif": "image/gif"}.get(ext, "")


def _run_job(image_bytes: bytes, media_type: str, filename: str, engine: str):
    td, xlsx, secs = pipeline.convert_image(image_bytes, media_type, engine)
    job_id = uuid.uuid4().hex[:12]
    job_dir = OUTPUT_DIR / job_id
    job_dir.mkdir()
    out_name = f"{_safe_stem(filename)}_converted.xlsx"
    (job_dir / out_name).write_bytes(xlsx)
    meta = {"job_id": job_id, "filename": filename, "xlsx": out_name, "seconds": round(secs, 1),
            "engine": td.engine, "table": td.to_dict()}
    (job_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False))
    return job_id, meta, td


def _chart_svg(td: TableData) -> str:
    """Small server-rendered stacked bar chart for the preview page."""
    if not td.series_cols or not td.rows:
        return ""
    by = td.category_col if td.category_col is not None else 0
    headers, rows = pivot(td, by)
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
    n_series = len(td.series_cols)
    label_w, bar_h, gap, top, right = 150, 22, 10, 30, 30
    plot_w = 560
    height = top + len(rows) * (bar_h + gap) + 60
    width = label_w + plot_w + right
    max_total = max((r[-1] for r in rows), default=0) or 1
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="Stacked bar chart by {headers[0]}" xmlns="http://www.w3.org/2000/svg">']
    # gridlines
    for k in range(0, 5):
        x = label_w + plot_w * k / 4
        parts.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{height - 52}" '
                     f'stroke="var(--grid)" stroke-width="1"/>')
        parts.append(f'<text x="{x:.1f}" y="{height - 36}" text-anchor="middle" class="axis">'
                     f'{int(round(max_total * k / 4))}</text>')
    for i, r in enumerate(rows):
        y = top + i * (bar_h + gap)
        label = str(r[0])
        parts.append(f'<text x="{label_w - 8}" y="{y + bar_h / 2 + 4}" text-anchor="end" class="lbl">'
                     f'{_esc(label[:24])}</text>')
        x = label_w
        for s in range(n_series):
            v = r[1 + s]
            w = plot_w * (v / max_total) if is_number(v) else 0
            if w > 0:
                parts.append(f'<rect x="{x:.1f}" y="{y}" width="{max(w - 2, 0.5):.1f}" height="{bar_h}" '
                             f'rx="2" fill="{colors[s % len(colors)]}"><title>{_esc(headers[1 + s])}: {v}</title></rect>')
                if w > 26:
                    parts.append(f'<text x="{x + w / 2:.1f}" y="{y + bar_h / 2 + 4}" text-anchor="middle" '
                                 f'class="val">{v}</text>')
            x += w
        parts.append(f'<text x="{x + 6:.1f}" y="{y + bar_h / 2 + 4}" class="tot">{r[-1]}</text>')
    # legend
    lx = label_w
    ly = height - 12
    for s in range(n_series):
        name = headers[1 + s]
        parts.append(f'<rect x="{lx}" y="{ly - 10}" width="12" height="12" rx="2" fill="{colors[s % len(colors)]}"/>')
        parts.append(f'<text x="{lx + 16}" y="{ly}" class="lbl">{_esc(name)}</text>')
        lx += 16 + 7 * len(name) + 24
    parts.append("</svg>")
    return "".join(parts)


def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------------------
@app.get("/")
def index():
    return render_template("index.html", engines=pipeline.available_engines(),
                           has_sample=SAMPLE_IMAGE.exists())


@app.post("/convert")
def convert():
    _cleanup_outputs()
    engine = request.form.get("engine", "auto")
    if engine not in ("auto", "tesseract", "claude"):
        engine = "auto"
    if request.form.get("sample") == "1" and SAMPLE_IMAGE.exists():
        image_bytes, media_type, filename = SAMPLE_IMAGE.read_bytes(), "image/jpeg", SAMPLE_IMAGE.name
    else:
        upload = request.files.get("image")
        if upload is None or not upload.filename:
            flash("Please choose an image file to convert.")
            return redirect(url_for("index"))
        media_type = _media_type(upload)
        if not media_type:
            flash("Unsupported file type. Upload a JPEG, PNG, WebP, BMP, TIFF or GIF image.")
            return redirect(url_for("index"))
        image_bytes, filename = upload.read(), upload.filename
    try:
        job_id, meta, td = _run_job(image_bytes, media_type, filename, engine)
    except Exception as exc:  # noqa: BLE001
        log.exception("conversion failed")
        flash(f"Conversion failed: {exc}")
        return redirect(url_for("index"))
    return redirect(url_for("result", job_id=job_id))


@app.get("/result/<job_id>")
def result(job_id: str):
    meta = _load_meta(job_id)
    td = TableData(**meta["table"])
    kpis = totals(td) if td.series_cols else {}
    preview_rows = td.rows[:200]
    return render_template("result.html", meta=meta, td=td, kpis=kpis, rows=preview_rows,
                           chart_svg=_chart_svg(td), truncated=len(td.rows) > 200)


@app.get("/download/<job_id>")
def download(job_id: str):
    meta = _load_meta(job_id)
    path = OUTPUT_DIR / job_id / meta["xlsx"]
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=meta["xlsx"],
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.post("/api/convert")
def api_convert():
    """Programmatic use: curl -F image=@table.jpg http://host:8080/api/convert -o out.xlsx"""
    upload = request.files.get("image")
    if upload is None or not upload.filename:
        return jsonify(error="multipart field 'image' is required"), 400
    media_type = _media_type(upload)
    if not media_type:
        return jsonify(error="unsupported image type"), 400
    engine = request.args.get("engine", request.form.get("engine", "auto"))
    try:
        job_id, meta, td = _run_job(upload.read(), media_type, upload.filename, engine)
    except Exception as exc:  # noqa: BLE001
        log.exception("api conversion failed")
        return jsonify(error=str(exc)), 500
    if request.args.get("format") == "json":
        return jsonify(job_id=job_id, download=url_for("download", job_id=job_id, _external=True),
                       title=td.title, engine=td.engine, source_language=td.source_language,
                       columns=td.columns, rows=td.rows, footer_rows=td.footer_rows, notes=td.notes,
                       chart_svg=_chart_svg(td), totals=(totals(td) if td.series_cols else {}))
    return send_file(OUTPUT_DIR / job_id / meta["xlsx"], as_attachment=True, download_name=meta["xlsx"])


@app.get("/health")
def health():
    return jsonify(status="ok", engines=pipeline.available_engines())


def _load_meta(job_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        abort(404)
    path = OUTPUT_DIR / job_id / "meta.json"
    if not path.exists():
        abort(404)
    return json.loads(path.read_text())


@app.after_request
def _cors(resp):
    """Allow the static front-end (GitHub Pages) to call the API from another origin."""
    if request.path.startswith(("/api/", "/download/", "/health")):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


@app.route("/api/convert", methods=["OPTIONS"])
def api_convert_preflight():
    return ("", 204)


@app.errorhandler(413)
def too_large(_e):
    flash("That file is too large (limit 25 MB).")
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False, threaded=True)
