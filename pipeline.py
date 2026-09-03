"""End-to-end: image bytes -> TableData + Excel bytes."""
from __future__ import annotations

import logging
import time

import cv2
import numpy as np

import claude_extract
import ocr_table
import table_model
from excel_builder import build_workbook

log = logging.getLogger("imagetoexcel")


def available_engines() -> dict[str, bool]:
    return {"tesseract": True, "claude": claude_extract.credentials_available()}


def decode_image(image_bytes: bytes) -> np.ndarray:
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        # PIL handles formats OpenCV may not (e.g. some WebP/TIFF variants)
        import io
        from PIL import Image
        pil = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img = cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)
    return img


def convert_image(image_bytes: bytes, media_type: str = "image/jpeg", engine: str = "auto"):
    """Return (TableData, xlsx_bytes, timing_seconds)."""
    t0 = time.time()
    img = decode_image(image_bytes)  # validates the image early
    td = None
    use_claude = engine == "claude" or (engine == "auto" and claude_extract.credentials_available())
    if use_claude:
        try:
            res = claude_extract.extract_with_claude(image_bytes, media_type)
            td = table_model.from_claude(res, engine=f"claude ({res.get('model', '')})")
        except Exception as exc:  # noqa: BLE001 - fall back to OCR, report why
            log.exception("Claude extraction failed")
            if engine == "claude":
                raise
            note = f"Claude engine failed ({type(exc).__name__}); used Tesseract OCR instead."
            td = None
        else:
            note = None
    if td is None:
        gr = ocr_table.extract_grid(img)
        td = table_model.from_grid(gr, engine="tesseract")
        if use_claude and note:
            td.notes.insert(0, note)
    xlsx = build_workbook(td)
    return td, xlsx, time.time() - t0
