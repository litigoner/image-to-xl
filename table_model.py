"""Turn a raw OCR grid (or Claude JSON) into a structured, English TableData."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

from translate_ne import translate, contains_nepali, NEP_DIGITS

TOTAL_RE = re.compile(r"(?<![A-Za-z])(grand\s+total|total|sum)(?![A-Za-z])|जम्मा|कुल", re.I)
INDEX_HEADER_RE = re.compile(r"^(s\.?\s?n\.?o?\.?|sn|s\.no|sr\.?\s?no\.?|no\.?|#|serial|क्र\.?\s?सं?\.?|सि\.?\s?नं?\.?)$", re.I)
NUM_RE = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?%?$|^-?\d+(\.\d+)?%?$")


@dataclass
class TableData:
    title: str = ""
    header_rows: list[list[str]] = field(default_factory=list)
    header_merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    footer_rows: list[list[Any]] = field(default_factory=list)
    numeric_cols: list[int] = field(default_factory=list)
    index_col: int | None = None
    total_col: int | None = None
    series_cols: list[int] = field(default_factory=list)
    category_col: int | None = None
    group_col: int | None = None
    raw_grid: list[list[str]] = field(default_factory=list)
    flags: list[list[int]] = field(default_factory=list)   # [row, col] cells worth checking
    source_language: str = "english"
    engine: str = "tesseract"
    notes: list[str] = field(default_factory=list)

    # ---- convenience ------------------------------------------------------
    @property
    def n_cols(self) -> int:
        return len(self.columns)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
def parse_number(text: Any):
    """Return int/float for numeric text, else the original (stripped) text."""
    if isinstance(text, (int, float)):
        return text
    original = str(text).translate(NEP_DIGITS).strip()
    s = original.replace(" ", "")
    if not s:
        return ""
    if NUM_RE.match(s):
        core = s.rstrip("%").replace(",", "")
        try:
            return int(core) if "." not in core else float(core)
        except ValueError:
            return original
    return original


def is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _looks_texty(s: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}|[ऄ-हक़-ॡ]", s or ""))


# ---------------------------------------------------------------------------
def from_grid(gr, engine: str = "tesseract") -> TableData:
    """Build TableData from ocr_table.GridResult."""
    grid = [list(r) for r in gr.grid]
    raw_grid = [list(r) for r in gr.grid]
    n_cols = gr.n_cols
    notes: list[str] = []
    if gr.mode == "lines":
        notes.append("No ruling grid detected; columns were inferred from word spacing.")

    def region_at(r, c):
        return gr.regions[gr.region_of[(r, c)]]

    # --- title: first row is one region spanning every column ---------------
    title = ""
    row_offset = 0
    if grid and n_cols > 1 and gr.mode == "grid":
        reg = region_at(0, 0)
        if reg.c1 == 0 and reg.c2 == n_cols - 1:
            title = grid[0][0]
            row_offset = reg.r2 + 1
    body = grid[row_offset:]

    # --- header rows: leading rows whose numeric-column cells contain words --
    num_cols_guess = set(gr.numeric_cols)
    header_rows: list[list[str]] = []
    i = 0
    while i < len(body):
        row = body[i]
        cells_in_num = [row[c] for c in num_cols_guess if c < len(row)]
        if cells_in_num and any(_looks_texty(v) for v in cells_in_num):
            header_rows.append(row)
            i += 1
        else:
            break
    if not header_rows and body:
        # maybe the table has no numeric columns; treat first row as header if texty
        if all(_looks_texty(v) or not v for v in body[0]) and any(body[0]):
            header_rows.append(body[0]); i = 1
    data_start = row_offset + i
    data = body[i:]

    # --- footer/total rows -----------------------------------------------------
    footer: list[list[str]] = []
    while data:
        last = data[-1]
        texts = [v for v in last if v and not NUM_RE.match(str(v).translate(NEP_DIGITS).strip())]
        if texts and TOTAL_RE.search(" ".join(texts)):
            footer.insert(0, data.pop())
        else:
            break
    data = [r for r in data if any(str(v).strip() for v in r)]

    # --- header merges (in header-row coordinates) ----------------------------
    header_merges: list[tuple[int, int, int, int]] = []
    if gr.mode == "grid" and header_rows:
        seen = set()
        h0, h1 = row_offset, row_offset + len(header_rows) - 1
        for r in range(h0, h1 + 1):
            for c in range(n_cols):
                idx = gr.region_of[(r, c)]
                if idx in seen:
                    continue
                seen.add(idx)
                reg = gr.regions[idx]
                r1, r2 = max(reg.r1, h0) - h0, min(reg.r2, h1) - h0
                if (r1, reg.c1) != (r2, reg.c2):
                    header_merges.append((r1, reg.c1, r2, reg.c2))

    return finalize(title, header_rows, data, footer, header_merges, raw_grid, engine, notes)


def from_claude(res: dict, engine: str = "claude") -> TableData:
    header_rows = [list(map(str, r)) for r in res.get("header_rows", [])]
    rows = [list(map(str, r)) for r in res.get("rows", [])]
    footer = [list(map(str, r)) for r in res.get("footer_rows", [])]
    n_cols = max([len(r) for r in header_rows + rows + footer] or [0])
    pad = lambda r: r + [""] * (n_cols - len(r))
    header_rows, rows, footer = [pad(r) for r in header_rows], [pad(r) for r in rows], [pad(r) for r in footer]
    # infer header merges from repeated adjacent text
    merges = []
    used = set()
    for r, row in enumerate(header_rows):
        for c, v in enumerate(row):
            if (r, c) in used or not v:
                continue
            c2 = c
            while c2 + 1 < n_cols and header_rows[r][c2 + 1] == v:
                c2 += 1
            r2 = r
            while r2 + 1 < len(header_rows) and all(header_rows[r2 + 1][k] == v for k in range(c, c2 + 1)):
                r2 += 1
            for rr in range(r, r2 + 1):
                for cc in range(c, c2 + 1):
                    used.add((rr, cc))
            if (r, c) != (r2, c2):
                merges.append((r, c, r2, c2))
    raw = res.get("raw_grid") or (header_rows + rows + footer)
    notes = [res["notes"]] if res.get("notes") else []
    td = finalize(res.get("title", ""), header_rows, rows, footer, merges, raw, engine, notes)
    if res.get("original_language"):
        td.source_language = res["original_language"]
    return td


# ---------------------------------------------------------------------------
def finalize(title, header_rows, data, footer, header_merges, raw_grid, engine, notes) -> TableData:
    n_cols = max([len(r) for r in header_rows + data + footer] or [0])
    pad = lambda r: list(r) + [""] * (n_cols - len(r))
    header_rows = [pad(r) for r in header_rows]
    data = [pad(r) for r in data]
    footer = [pad(r) for r in footer]

    # --- language detection ----------------------------------------------------
    all_text = " ".join([title] + [v for r in header_rows + data + footer for v in map(str, r)])
    has_ne = contains_nepali(all_text)
    has_en = bool(re.search(r"[A-Za-z]{3,}", all_text))
    source_language = "mixed" if has_ne and has_en else "nepali" if has_ne else "english"

    # --- translate text; parse numbers ---------------------------------------
    def tr(v):
        return translate(v) if contains_nepali(str(v)) else str(v).translate(NEP_DIGITS).strip()

    title_en = tr(title)
    header_en = [[tr(v) for v in r] for r in header_rows]
    rows_en = [[parse_number(tr(v)) for v in r] for r in data]
    footer_en = [[parse_number(tr(v)) for v in r] for r in footer]

    # --- flat column names (leaf header text) --------------------------------
    columns: list[str] = []
    for c in range(n_cols):
        name = ""
        for r in range(len(header_en) - 1, -1, -1):
            if header_en[r][c].strip():
                name = header_en[r][c].strip()
                break
        columns.append(name or f"Column {c + 1}")
    seen: dict[str, int] = {}
    for c, name in enumerate(columns):
        if name in seen:
            seen[name] += 1
            columns[c] = f"{name} ({seen[name]})"
        else:
            seen[name] = 1

    # --- numeric columns -------------------------------------------------------
    numeric_cols: list[int] = []
    for c in range(n_cols):
        vals = [r[c] for r in rows_en if str(r[c]).strip() != ""]
        if vals and sum(is_number(v) for v in vals) >= 0.6 * len(vals):
            numeric_cols.append(c)

    # --- index column ----------------------------------------------------------
    index_col = None
    if numeric_cols and numeric_cols[0] == 0:
        vals = [r[0] for r in rows_en if is_number(r[0])]
        monotonic = len(vals) >= 2 and all(b >= a for a, b in zip(vals, vals[1:]))
        if INDEX_HEADER_RE.match(columns[0].strip()) or (monotonic and vals and vals[-1] <= 5000
                                                          and len(set(vals)) >= 2):
            index_col = 0
    elif n_cols and INDEX_HEADER_RE.match(columns[0].strip()):
        index_col = 0

    value_cols = [c for c in numeric_cols if c != index_col]

    # --- total column ----------------------------------------------------------
    total_col = None
    for c in value_cols:
        if TOTAL_RE.search(columns[c]):
            total_col = c
            break
    if total_col is None and len(value_cols) >= 3:
        for c in value_cols:
            others = [o for o in value_cols if o != c]
            hits = tot = 0
            for r in rows_en:
                if is_number(r[c]) and all(is_number(r[o]) or r[o] == "" for o in others):
                    tot += 1
                    s = sum(r[o] for o in others if is_number(r[o]))
                    if abs(s - r[c]) < 1e-6:
                        hits += 1
            if tot and hits / tot >= 0.7:
                total_col = c
                break
    series_cols = [c for c in value_cols if c != total_col]
    if not series_cols and total_col is not None:
        series_cols, total_col = [total_col], None

    # --- category / group columns ---------------------------------------------
    first_series = series_cols[0] if series_cols else n_cols
    text_cols = [c for c in range(first_series) if c not in numeric_cols and c != index_col]
    n_rows = len(rows_en)
    category_col = None
    if text_cols:
        def uniq(c):
            return len({str(r[c]).strip() for r in rows_en if str(r[c]).strip()})
        # a grouping column repeats values; the most specific label column does not
        candidates = [c for c in text_cols if 2 <= uniq(c) <= 0.6 * n_rows] if n_rows > 3 else []
        category_col = candidates[-1] if candidates else text_cols[-1]
        group_col = None
        for c in reversed([c for c in text_cols if c < category_col]):
            if 2 <= uniq(c) < uniq(category_col):
                group_col = c
                break
    else:
        group_col = None
        if index_col is not None:
            category_col = index_col

    if not series_cols:
        notes.append("No numeric columns were detected, so no chart could be built.")

    # --- consistency checks against the printed totals ---------------------------
    flags: list[list[int]] = []
    if total_col is not None and series_cols:
        filled = mismatched = 0
        for ri, r in enumerate(rows_en):
            if not is_number(r[total_col]):
                if str(r[total_col]).strip() == "" and all(is_number(r[c]) for c in series_cols):
                    r[total_col] = sum(r[c] for c in series_cols)
                    flags.append([ri, total_col]); filled += 1
                continue
            missing = [c for c in series_cols if str(r[c]).strip() == ""]
            known = [c for c in series_cols if is_number(r[c])]
            if len(missing) == 1 and len(known) == len(series_cols) - 1:
                v = r[total_col] - sum(r[c] for c in known)
                if v >= 0:
                    r[missing[0]] = int(v) if float(v).is_integer() else v
                    flags.append([ri, missing[0]]); filled += 1
                    continue
            if len(known) == len(series_cols) and abs(sum(r[c] for c in known) - r[total_col]) > 1e-6:
                mismatched += 1
                for c in series_cols + [total_col]:
                    flags.append([ri, c])
        if filled:
            notes.append(f"{filled} unreadable cell(s) were filled in from the row total (highlighted in yellow).")
        if mismatched:
            notes.append(f"{mismatched} row(s) do not add up to their printed total (highlighted in yellow); "
                         "please check these against the image.")
    if footer_en and value_cols:
        printed = footer_en[-1]
        for c in value_cols:
            if is_number(printed[c]):
                s = sum(r[c] for r in rows_en if is_number(r[c]))
                if abs(s - printed[c]) > 1e-6:
                    notes.append(f"Column '{columns[c]}': read values sum to {s:g} but the image's total row "
                                 f"says {printed[c]:g}.")

    # unparseable cells in numeric columns
    bad = sum(1 for r in rows_en for c in value_cols if str(r[c]).strip() and not is_number(r[c]))
    if bad:
        notes.append(f"{bad} cell(s) in numeric columns could not be read as numbers; check the "
                     "'Original OCR' sheet and correct them if needed.")

    return TableData(title=title_en, header_rows=header_en, header_merges=header_merges,
                     columns=columns, rows=rows_en, footer_rows=footer_en, numeric_cols=numeric_cols,
                     index_col=index_col, total_col=total_col, series_cols=series_cols,
                     category_col=category_col, group_col=group_col, raw_grid=raw_grid, flags=flags,
                     source_language=source_language, engine=engine, notes=notes)


# ---------------------------------------------------------------------------
def pivot(td: TableData, by_col: int) -> tuple[list[str], list[list[Any]]]:
    """Aggregate series (and total) by a text column. Returns (headers, rows)."""
    agg: dict[str, list[float]] = {}
    order: list[str] = []
    cols = td.series_cols
    for r in td.rows:
        key = str(r[by_col]).strip() or "(blank)"
        if key not in agg:
            agg[key] = [0.0] * len(cols)
            order.append(key)
        for i, c in enumerate(cols):
            if is_number(r[c]):
                agg[key][i] += r[c]
    headers = [td.columns[by_col]] + [td.columns[c] for c in cols] + ["Total"]
    out = []
    for key in order:
        vals = [int(v) if float(v).is_integer() else v for v in agg[key]]
        out.append([key] + vals + [sum(vals)])
    out.sort(key=lambda r: r[-1], reverse=True)
    return headers, out


def totals(td: TableData) -> dict[str, Any]:
    res = {}
    for c in td.series_cols:
        s = sum(r[c] for r in td.rows if is_number(r[c]))
        res[td.columns[c]] = int(s) if float(s).is_integer() else s
    grand = sum(res.values())
    res["Total"] = int(grand) if float(grand).is_integer() else grand
    return res
