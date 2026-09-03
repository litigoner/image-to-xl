"""Optional high-accuracy extraction engine using Claude vision.

Activated only when Anthropic credentials are available (ANTHROPIC_API_KEY or an
`ant auth login` profile). Returns the same dict shape consumed by
table_model.from_claude().
"""
from __future__ import annotations

import base64
import json
import os

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Table title in English ('' if none)."},
        "header_rows": {
            "type": "array", "items": {"type": "array", "items": {"type": "string"}},
            "description": "Header rows, top to bottom. Every row has the same number of "
                           "columns; repeat the text of a merged header cell in every cell it spans."
        },
        "rows": {
            "type": "array", "items": {"type": "array", "items": {"type": "string"}},
            "description": "Data rows (no header, no total rows). Repeat the text of a vertically "
                           "merged cell in every row it spans. Numbers as plain ASCII digits."
        },
        "footer_rows": {
            "type": "array", "items": {"type": "array", "items": {"type": "string"}},
            "description": "Total / grand-total rows, if any."
        },
        "raw_grid": {
            "type": "array", "items": {"type": "array", "items": {"type": "string"}},
            "description": "The same header+data+footer rows in the ORIGINAL language, untranslated."
        },
        "original_language": {"type": "string", "enum": ["nepali", "english", "mixed"]},
        "notes": {"type": "string"},
    },
    "required": ["title", "header_rows", "rows", "footer_rows", "raw_grid", "original_language", "notes"],
    "additionalProperties": False,
}

PROMPT = """Extract the table in this image exactly as structured data.

Rules:
- Keep English text exactly as written. Translate any Nepali (Devanagari) text into natural English
  (place names transliterated the way they are commonly written in English, e.g. काठमाडौं -> Kathmandu,
  अस्पताल -> Hospital, शनाखत भएको -> Identified, शनाखत नभएको -> Unidentified, व्यवस्थापन शव -> Managed Bodies).
- Convert Devanagari digits to ASCII digits. Keep numbers as plain digit strings without thousands separators.
- Merged cells: repeat their text in every cell they span so all rows have the same number of columns.
- Do not invent or recompute values; copy what is printed. Put total rows in footer_rows.
- raw_grid must hold the untranslated original text in the same layout (header rows, data rows, footer rows).
"""


def credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    cfg = os.path.expanduser("~/.config/anthropic")
    return os.path.isdir(cfg) and any(os.scandir(cfg))


def extract_with_claude(image_bytes: bytes, media_type: str = "image/jpeg") -> dict:
    import anthropic  # imported lazily so the app runs without the SDK configured

    client = anthropic.Anthropic()
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                     "data": base64.standard_b64encode(image_bytes).decode("utf-8")}},
        {"type": "text", "text": PROMPT},
    ]
    kwargs = dict(
        model=MODEL,
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
    )
    try:
        # server-side refusal fallback routes a declined request to another model automatically
        response = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except (TypeError, anthropic.BadRequestError):
        response = client.messages.create(**kwargs)

    if response.stop_reason == "refusal":
        raise RuntimeError("Claude declined to process this image.")
    text = "".join(block.text for block in response.content if block.type == "text")
    data = json.loads(text)
    data["model"] = getattr(response, "model", MODEL)
    return data
