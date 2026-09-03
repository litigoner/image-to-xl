"""Table extraction from an image using OpenCV grid detection + Tesseract.

Pipeline:
  1. Binarise image, isolate horizontal and vertical ruling lines.
  2. Derive row/column boundaries from line projections.
  3. Detect merged cells by checking whether a ruling line actually exists
     between neighbouring cells; union neighbours into regions.
  4. OCR each region once (Nepali+English pass and a digits-only pass).
  5. Decide per column whether it is numeric; return a grid of strings.
Falls back to line-based OCR when no ruling grid is found.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import cv2
import numpy as np
import pytesseract

DEVANAGARI_LETTER_RE = re.compile("[\\u0904-\\u0939\\u0958-\\u0961\\u0972-\\u097F]")
NEP_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
NUMERIC_RE = re.compile(r"^[\d][\d,.\s/-]*%?$")


@dataclass
class Region:
    r1: int
    c1: int
    r2: int
    c2: int  # inclusive cell ranges
    box: tuple[int, int, int, int] = (0, 0, 0, 0)  # x1,y1,x2,y2 in pixels
    text_ne: str = ""
    text_digits: str = ""
    text: str = ""
    conf: float = 0.0
    ink: float = 0.0

    @property
    def rows(self):
        return range(self.r1, self.r2 + 1)

    @property
    def cols(self):
        return range(self.c1, self.c2 + 1)


@dataclass
class GridResult:
    grid: list[list[str]]                 # text per cell (merged regions replicated)
    regions: list[Region]
    region_of: dict[tuple[int, int], int]  # (row, col) -> index in regions
    numeric_cols: set[int] = field(default_factory=set)
    n_rows: int = 0
    n_cols: int = 0
    mode: str = "grid"


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------
def to_gray(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img.shape
    # Upscale small images so lines/text are detectable
    if w < 1400:
        scale = 1400 / w
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return img


def _cluster(positions: np.ndarray, gap: int = 4) -> list[int]:
    if positions.size == 0:
        return []
    groups: list[list[int]] = [[int(positions[0])]]
    for p in positions[1:]:
        if p - groups[-1][-1] <= gap:
            groups[-1].append(int(p))
        else:
            groups.append([int(p)])
    return [int(round(sum(g) / len(g))) for g in groups]


def _dedupe(vals: list[int], min_gap: int) -> list[int]:
    out: list[int] = []
    for v in vals:
        if not out or v - out[-1] >= min_gap:
            out.append(v)
    return out


def _filter_segments(mask: np.ndarray, anchors: np.ndarray, horizontal: bool, tol: int) -> np.ndarray:
    """Drop text strokes that look like ruling lines (Devanagari headlines).

    A ruling line is long, or ends where a perpendicular ruling line is; a
    word's headline is short with arbitrary endpoints.  Long segments are always
    trusted so a table whose outer border is missing or cropped still works.
    """
    dim = mask.shape[1] if horizontal else mask.shape[0]
    n, labels, stats, _ = cv2.connectedComponentsWithStats((mask > 0).astype(np.uint8), connectivity=8)
    keep = np.zeros(n, dtype=bool)
    for i in range(1, n):
        x, y, w, h, _area = stats[i]
        a, b, length = (x, x + w - 1, w) if horizontal else (y, y + h - 1, h)
        hits = int((np.abs(anchors - a) <= tol).any()) + int((np.abs(anchors - b) <= tol).any())
        if length >= 0.3 * dim or hits == 2 or (hits == 1 and length >= 0.08 * dim):
            keep[i] = True
    return np.where(keep[labels], mask, 0).astype(np.uint8)


def detect_lines(gray: np.ndarray):
    h, w = gray.shape
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 21, 10)
    hk_len = int(min(max(20, w // 40), 60))
    vk_len = int(min(max(15, h // 40), 40))
    horiz = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hk_len, 1)))
    horiz = cv2.dilate(horiz, cv2.getStructuringElement(cv2.MORPH_RECT, (7, 1)))
    vert = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk_len)))
    vert = cv2.dilate(vert, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 7)))

    # x positions that carry some vertical ruling (plus the image edges)
    vx = np.where((vert > 0).any(axis=0))[0]
    vx = np.concatenate([vx, [0, w - 1]])
    tol = int(max(12, 0.01 * max(w, h)))
    horiz = _filter_segments(horiz, vx, horizontal=True, tol=tol)
    hy = np.where((horiz > 0).any(axis=1))[0]
    hy = np.concatenate([hy, [0, h - 1]])
    vert = _filter_segments(vert, hy, horizontal=False, tol=tol)

    row_profile = (horiz > 0).sum(axis=1)
    col_profile = (vert > 0).sum(axis=0)
    ys = _dedupe(_cluster(np.where(row_profile > 0.15 * w)[0]), min_gap=8)
    xs = _dedupe(_cluster(np.where(col_profile > 0.25 * h)[0]), min_gap=8)

    # Unbordered outer rows/columns: add the image edge if ink lies beyond the last line
    ink = bw > 0
    if xs and xs[0] > 0.015 * w and ink[:, : xs[0] - 4].mean() > 0.004:
        xs.insert(0, 0)
    if xs and xs[-1] < w - 0.015 * w and ink[:, xs[-1] + 4:].mean() > 0.004:
        xs.append(w - 1)
    if ys and ys[0] > 0.015 * h and ink[: ys[0] - 4, :].mean() > 0.004:
        ys.insert(0, 0)
    if ys and ys[-1] < h - 0.015 * h and ink[ys[-1] + 4:, :].mean() > 0.004:
        ys.append(h - 1)

    # drop boundaries that create implausibly thin rows (double lines)
    if len(ys) > 3:
        med = float(np.median(np.diff(ys)))
        ys = [ys[0]] + [y for prev, y in zip(ys, ys[1:]) if y - prev >= 0.4 * med]
    return ys, xs, horiz, vert


def _line_present(mask: np.ndarray, x1: int, x2: int, y1: int, y2: int, axis: int) -> bool:
    """Return True if a ruling line exists in the given band of the mask."""
    x1, x2 = max(0, x1), min(mask.shape[1], x2)
    y1, y2 = max(0, y1), min(mask.shape[0], y2)
    if x2 <= x1 or y2 <= y1:
        return True
    band = mask[y1:y2, x1:x2] > 0
    if axis == 0:   # horizontal line: fraction of columns covered
        coverage = band.any(axis=0).mean()
    else:           # vertical line: fraction of rows covered
        coverage = band.any(axis=1).mean()
    return coverage > 0.5


# ---------------------------------------------------------------------------
# OCR helpers
# ---------------------------------------------------------------------------
def _prep_cell(gray: np.ndarray, box, median_row_h: float):
    """Crop a cell to its ink and normalise text height.

    Returns dict(big=image|None, ink=float, psm=int, comps=[(w,h),...]).
    """
    x1, y1, x2, y2 = box
    pad = 4
    cell = gray[y1 + pad:y2 - pad, x1 + pad:x2 - pad]
    empty = {"big": None, "ink": 0.0, "psm": 7, "comps": [], "cell": None}
    if cell.size == 0 or cell.shape[0] < 6 or cell.shape[1] < 6:
        return empty
    ch, cw = cell.shape
    if int((cell < 110).sum()) < 6:
        return empty          # nothing genuinely dark in the cell: it is blank
    _thr, th = cv2.threshold(cell, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    n, labels, stats, _ = cv2.connectedComponentsWithStats((th > 0).astype(np.uint8), connectivity=8)
    good = np.zeros(n, dtype=bool)
    comps: list[tuple[int, int]] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        near_edge = x <= 2 or y <= 2 or x + w >= cw - 2 or y + h >= ch - 2
        # ruling-line remnants hug the crop edge and are long and thin; a Devanagari
        # headline sits in the interior, so it is kept.
        line_like = near_edge and ((w >= 0.5 * cw and h <= 8) or (h >= 0.5 * ch and w <= 8))
        sliver = near_edge and (w <= 4 or h <= 4)
        if line_like or sliver:
            continue          # whiten it out
        good[i] = True
        if area >= 6 and not near_edge:
            comps.append((int(x), int(y), int(w), int(h)))
    ink_mask = good[labels]
    real = [i for i in range(1, n) if good[i] and stats[i][cv2.CC_STAT_AREA] >= 6]
    if not real or max(stats[i][cv2.CC_STAT_HEIGHT] for i in real) < 5:
        return {"big": None, "ink": 0.0, "psm": 7, "comps": [], "cell": None}
    ink = float(ink_mask.mean())
    if ink < 0.0004 or ink_mask.sum() < 12:
        return {"big": None, "ink": ink, "psm": 7, "comps": comps, "cell": None}
    # erase only the removed components (line remnants); keep the glyphs' soft
    # anti-aliased edges, which Tesseract needs for thin strokes such as "1"
    removed = (labels > 0) & ~good[labels]
    removed = cv2.dilate(removed.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))) > 0
    clean = np.where(removed, 255, cell).astype(np.uint8)
    rows_with_ink = np.where(ink_mask.any(axis=1))[0]
    cols_with_ink = np.where(ink_mask.any(axis=0))[0]
    ry1, ry2 = max(0, rows_with_ink[0] - 4), min(ch, rows_with_ink[-1] + 5)
    rx1, rx2 = max(0, cols_with_ink[0] - 6), min(cw, cols_with_ink[-1] + 7)
    crop = clean[ry1:ry2, rx1:rx2]
    proj = ink_mask[ry1:ry2].any(axis=1)
    n_lines, in_run, blank = 0, False, 0
    for v in proj:
        if v:
            if not in_run and (n_lines == 0 or blank >= 2):
                n_lines += 1
            in_run, blank = True, 0
        else:
            in_run, blank = False, blank + 1
    n_lines = max(1, n_lines)
    line_h = crop.shape[0] / n_lines
    scale = float(np.clip(48.0 / max(line_h, 1.0), 1.5, 6.0))
    big = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    big = cv2.copyMakeBorder(big, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    return {"big": big, "ink": ink, "psm": 6 if n_lines > 1 else 7, "comps": comps, "cell": clean}


def _clean(text: str) -> str:
    text = text.replace("\x0c", "")
    text = re.sub(r"[|\[\]_~`^]+", " ", text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    out = " ".join(ln for ln in lines if ln).strip()
    return out if re.search(r"[0-9A-Za-z\u0900-\u097F]", out) else ""


DIGIT_CFG = "-c tessedit_char_whitelist=0123456789.,-/%"
LOOKALIKES = str.maketrans({"l": "1", "I": "1", "|": "1", "i": "1", "!": "1", "o": "0", "O": "0",
                            "S": "5", "s": "5", "Z": "2", "z": "2", "B": "8", "g": "9", "q": "9",
                            "उ": "3", "ठ": "0", "०": "0", "१": "1", "२": "2", "३": "3", "४": "4",
                            "५": "5", "६": "6", "७": "7", "८": "8", "९": "9"})
MIN_TEXT_CONF = 25.0


def _ocr_text(big: np.ndarray, psm: int, lang: str = "nep+eng", extra: str = "") -> tuple[str, float]:
    """OCR returning (text, mean word confidence)."""
    data = pytesseract.image_to_data(big, lang=lang, config=f"--psm {psm} {extra}".strip(),
                                     output_type=pytesseract.Output.DICT)
    words, confs = [], []
    for txt, conf in zip(data["text"], data["conf"]):
        txt = txt.strip()
        if not txt:
            continue
        words.append(txt)
        try:
            c = float(conf)
        except (TypeError, ValueError):
            c = -1.0
        if c >= 0:
            confs.append(c)
    text = _clean(" ".join(words))
    conf = float(np.mean(confs)) if confs else 0.0
    return text, conf


def _group_glyphs(comps):
    """Merge components that belong to one glyph (e.g. a '0' split into two arcs)."""
    boxes = sorted([list(c) for c in comps if c[3] >= 6], key=lambda b: b[0])
    if not boxes:
        return []
    ref_h = max(b[3] for b in boxes)
    boxes = [b for b in boxes if b[3] >= 0.55 * ref_h]        # drop dots / commas
    merged = []
    for b in boxes:
        if merged:
            p = merged[-1]
            gap = b[0] - (p[0] + p[2])
            union_w = max(p[0] + p[2], b[0] + b[2]) - min(p[0], b[0])
            if gap <= 1 or union_w < 0.75 * ref_h:
                x = min(p[0], b[0]); y = min(p[1], b[1])
                merged[-1] = [x, y, union_w, max(p[1] + p[3], b[1] + b[3]) - y]
                continue
        merged.append(b)
    return merged


def _digit_variants(prep):
    yield prep["big"]
    cell = prep.get("cell")
    if cell is not None:
        for s in (2.0, 3.0):
            v = cv2.resize(cell, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
            yield cv2.copyMakeBorder(v, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)


def _ocr_digits(prep) -> str:
    """Digit OCR by voting over image variants, with glyph-shape sanity checks."""
    glyphs = _group_glyphs(prep["comps"])
    n_glyphs = len(glyphs)
    narrow = bool(glyphs) and all(w / max(h, 1) < 0.42 for _x, _y, w, h in glyphs)
    digits_only = lambda s: re.sub(r"[^0-9]", "", s)

    votes: dict[str, list[float]] = {}
    variants = list(_digit_variants(prep))
    for i, v in enumerate(variants):
        t, c = _ocr_text(v, 7, lang="eng", extra=DIGIT_CFG)
        if t:
            votes.setdefault(t, []).append(c)
        # early exit when the first two variants agree
        if i == 1 and len(votes) == 1 and len(next(iter(votes.values()))) == 2:
            break
    if votes:
        def score(item):
            t, confs = item
            n = len(digits_only(t))
            # glyph count is a reliable lower bound on the digit count: honour it first
            return (n >= n_glyphs, len(confs), n, sum(confs) / len(confs))
        best = max(votes.items(), key=score)[0]
        # far more glyphs than digits read: this is text (e.g. a Devanagari word), not a number
        if n_glyphs > 2 * len(digits_only(best)) + 1:
            return ""
        return best
    if narrow:
        return "1" * n_glyphs                 # thin bars that OCR keeps missing are ones
    for v in variants[:2]:
        for psm in (8, 10):
            t, _c = _ocr_text(v, psm, lang="eng", extra=DIGIT_CFG)
            if t:
                return t
    return ""


def _plausible_text(text: str, conf: float) -> bool:
    """Reject OCR junk. Tesseract's confidence is unreliable for Devanagari (a clean
    'जम्मा शव' can score 2), so judge mostly by character make-up."""
    core = re.sub(r"\s", "", text)
    if not core:
        return False
    letters = len(re.findall(r"[0-9A-Za-z\u0900-\u097F]", core))
    if letters / len(core) < 0.5:
        return False
    if conf < 15 and len(core) <= 3:
        return False
    return True


def _ocr_region(args):
    prep = args
    big = prep["big"]
    if big is None:
        return "", "", 0.0, prep["ink"]
    text_ne, conf = _ocr_text(big, prep["psm"])
    # second opinion on a plain 3x upscale of the cleaned cell; keep the more confident read
    cell = prep.get("cell")
    if cell is not None and conf < 80:
        alt = cv2.resize(cell, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
        alt = cv2.copyMakeBorder(alt, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        t2, c2 = _ocr_text(alt, prep["psm"])
        if t2 and c2 > conf:
            text_ne, conf = t2, c2
    if not _plausible_text(text_ne, conf):
        text_ne = ""
    text_digits = _ocr_digits(prep)
    return text_ne, text_digits, conf, prep["ink"]


def _is_numeric_text(s: str) -> bool:
    s = s.translate(NEP_DIGITS).strip()
    return bool(s) and bool(NUMERIC_RE.match(s)) and not DEVANAGARI_LETTER_RE.search(s)


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------
def extract_grid(img: np.ndarray, workers: int = 8) -> GridResult:
    gray = to_gray(img)
    ys, xs, horiz, vert = detect_lines(gray)
    if len(ys) < 3 or len(xs) < 2:
        return extract_lines_fallback(gray)

    n_rows, n_cols = len(ys) - 1, len(xs) - 1
    median_row_h = float(np.median(np.diff(ys)))

    # --- merged-cell detection via union-find --------------------------------
    parent = {(r, c): (r, c) for r in range(n_rows) for c in range(n_cols)}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for r in range(n_rows):
        for c in range(n_cols):
            # boundary below (r,c)?
            if r + 1 < n_rows:
                y = ys[r + 1]
                if not _line_present(horiz, xs[c] + 6, xs[c + 1] - 6, y - 4, y + 5, axis=0):
                    union((r, c), (r + 1, c))
            # boundary right of (r,c)?
            if c + 1 < n_cols:
                x = xs[c + 1]
                if not _line_present(vert, x - 4, x + 5, ys[r] + 6, ys[r + 1] - 6, axis=1):
                    union((r, c), (r, c + 1))

    groups: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for cell in parent:
        groups.setdefault(find(cell), []).append(cell)

    regions: list[Region] = []
    region_of: dict[tuple[int, int], int] = {}
    for cells in groups.values():
        rs = [c[0] for c in cells]
        cs = [c[1] for c in cells]
        reg = Region(min(rs), min(cs), max(rs), max(cs))
        reg.box = (xs[reg.c1], ys[reg.r1], xs[reg.c2 + 1], ys[reg.r2 + 1])
        idx = len(regions)
        regions.append(reg)
        for cell in cells:
            region_of[cell] = idx

    # --- OCR every region (parallel) -----------------------------------------
    prepared = [_prep_cell(gray, reg.box, median_row_h) for reg in regions]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(_ocr_region, prepared))
    for reg, (t_ne, t_dig, conf, ink) in zip(regions, results):
        reg.text_ne, reg.text_digits, reg.conf, reg.ink = t_ne, t_dig, conf, ink

    # --- decide numeric columns (majority of single-cell regions) ------------
    numeric_cols: set[int] = set()
    for c in range(n_cols):
        votes_num = votes_txt = 0
        for r in range(n_rows):
            reg = regions[region_of[(r, c)]]
            if reg.c1 != reg.c2 or reg.r1 != r:
                continue  # skip horizontally merged or replicated rows
            if not reg.text_ne and not reg.text_digits:
                continue
            if DEVANAGARI_LETTER_RE.search(reg.text_ne) or re.search(r"[A-Za-z]{2,}", reg.text_ne):
                votes_txt += 1
            elif reg.text_digits:
                votes_num += 1
        if votes_num > 0 and votes_num >= votes_txt:
            numeric_cols.add(c)

    # --- choose final text per region ----------------------------------------
    for reg in regions:
        single_col = reg.c1 == reg.c2
        short_ne = len(reg.text_ne.replace(" ", "")) <= 3
        if single_col and reg.c1 in numeric_cols and (
                short_ne or (not DEVANAGARI_LETTER_RE.search(reg.text_ne)
                             and not re.search(r"[A-Za-z]{2,}", reg.text_ne))):
            reg.text = reg.text_digits or reg.text_ne.translate(LOOKALIKES)
        else:
            reg.text = reg.text_ne
            # a text column cell that is purely digits (e.g. S.N.) -> use digit pass
            if reg.text_ne and _is_numeric_text(reg.text_ne) and reg.text_digits:
                reg.text = reg.text_digits
            if reg.text_ne and not DEVANAGARI_LETTER_RE.search(reg.text_ne) and \
                    _is_numeric_text(reg.text_ne.translate(LOOKALIKES)) and reg.text_digits:
                reg.text = reg.text_digits

    grid = [[regions[region_of[(r, c)]].text for c in range(n_cols)] for r in range(n_rows)]
    return GridResult(grid=grid, regions=regions, region_of=region_of,
                      numeric_cols=numeric_cols, n_rows=n_rows, n_cols=n_cols, mode="grid")


def extract_lines_fallback(gray: np.ndarray) -> GridResult:
    """No ruling lines: OCR words and split lines on large horizontal gaps."""
    data = pytesseract.image_to_data(gray, lang="nep+eng", config="--psm 6",
                                     output_type=pytesseract.Output.DICT)
    words = []
    for i, txt in enumerate(data["text"]):
        txt = txt.strip()
        if not txt:
            continue
        words.append((data["block_num"][i], data["par_num"][i], data["line_num"][i],
                      data["left"][i], data["width"][i], txt))
    lines: dict[tuple, list] = {}
    for b, p, l, x, w, t in words:
        lines.setdefault((b, p, l), []).append((x, w, t))
    rows: list[list[str]] = []
    for key in sorted(lines):
        ws = sorted(lines[key])
        cells, cur = [], [ws[0][2]]
        for (x0, w0, _), (x1, _, t1) in zip(ws, ws[1:]):
            if x1 - (x0 + w0) > 35:
                cells.append(" ".join(cur))
                cur = [t1]
            else:
                cur.append(t1)
        cells.append(" ".join(cur))
        rows.append([_clean(c) for c in cells])
    n_cols = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (n_cols - len(r)) for r in rows]
    regions, region_of = [], {}
    for r, row in enumerate(rows):
        for c, txt in enumerate(row):
            reg = Region(r, c, r, c, text=txt, text_ne=txt)
            region_of[(r, c)] = len(regions)
            regions.append(reg)
    numeric_cols = set()
    for c in range(n_cols):
        vals = [rows[r][c] for r in range(len(rows)) if rows[r][c]]
        if vals and sum(_is_numeric_text(v) for v in vals) >= len(vals) / 2:
            numeric_cols.add(c)
    return GridResult(grid=rows, regions=regions, region_of=region_of, numeric_cols=numeric_cols,
                      n_rows=len(rows), n_cols=n_cols, mode="lines")


def debug_image(img: np.ndarray, out_path: str) -> None:
    gray = to_gray(img)
    ys, xs, horiz, vert = detect_lines(gray)
    vis = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    vis[horiz > 0] = (0, 0, 255)
    vis[vert > 0] = (255, 0, 0)
    for y in ys:
        cv2.line(vis, (0, y), (vis.shape[1] - 1, y), (0, 200, 0), 1)
    for x in xs:
        cv2.line(vis, (x, 0), (x, vis.shape[0] - 1), (0, 200, 0), 1)
    cv2.imwrite(out_path, vis)


if __name__ == "__main__":
    import sys, time
    t0 = time.time()
    if len(sys.argv) > 2:
        debug_image(cv2.imread(sys.argv[1]), sys.argv[2])
    res = extract_grid(cv2.imread(sys.argv[1]))
    print(f"mode={res.mode} rows={res.n_rows} cols={res.n_cols} numeric={sorted(res.numeric_cols)} "
          f"regions={len(res.regions)} time={time.time()-t0:.1f}s")
    for row in res.grid:
        print(" | ".join(c[:28] for c in row))
