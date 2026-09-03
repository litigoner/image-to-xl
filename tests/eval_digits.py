"""Score digit-OCR strategies on data.jpeg against known values."""
import sys, re, time
sys.path.insert(0, ".")
import cv2, numpy as np, pytesseract
import ocr_table as ot

TRUTH = """0 6 0 6
0 1 0 1
0 11 0 11
5 73 0 78
0 9 0 9
1 21 0 22
4 1 22 27
0 3 23 26
1 6 0 7
20 72 85 177
12 8 335 355
0 0 9 9
1 0 13 14
0 0 15 15
1 3 50 54
1 1 2 4
4 0 7 11
2 0 5 7
1 0 55 56
3 1 65 69
12 4 25 41
1 0 36 37
0 1 1 2
0 0 2 2
0 0 4 4
3 14 111 128
1 39 25 65
0 0 3 3
1 5 0 6
0 0 6 6
74 279 899 1252"""
truth = [r.split() for r in TRUTH.splitlines()]

img = cv2.imread("data.jpeg")
gray = ot.to_gray(img)
ys, xs, horiz, vert = ot.detect_lines(gray)
print("rows", len(ys)-1, "cols", len(xs)-1)
med = float(np.median(np.diff(ys)))
DATA_ROW0 = 3  # title + 2 header rows
NUM_COLS = [4, 5, 6, 7]

def variants(cellbox):
    prep = ot._prep_cell(gray, cellbox, med)
    big = prep["big"]
    out = {}
    if big is None:
        return out, prep
    x1, y1, x2, y2 = cellbox
    cell = gray[y1+4:y2-4, x1+4:x2-4]
    for s in (2.0, 3.0, 4.0):
        v = cv2.resize(cell, None, fx=s, fy=s, interpolation=cv2.INTER_CUBIC)
        v = cv2.copyMakeBorder(v, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
        out[f"raw_x{s:.0f}"] = v
    out["prep48"] = big
    _, bw = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    out["prep48_bin"] = bw
    return out, prep

def run(variant_img, psm):
    t, c = ot._ocr_text(variant_img, psm, lang="eng", extra=ot.DIGIT_CFG)
    return t

t0 = time.time()
strategies = {}
cells = []
for ri, trow in enumerate(truth):
    r = DATA_ROW0 + ri
    for ci, c in enumerate(NUM_COLS):
        box = (xs[c], ys[r], xs[c+1], ys[r+1])
        vs, prep = variants(box)
        res = {"pipeline": ot._ocr_digits(prep) if prep["big"] is not None else ""}
        for name, vimg in vs.items():
            for psm in (7,):
                res[f"{name}/psm{psm}"] = run(vimg, psm)
        cells.append((ri, ci, trow[ci], res, prep["comps"]))
        for k, v in res.items():
            strategies.setdefault(k, []).append(v == trow[ci])

print(f"time {time.time()-t0:.1f}s, cells {len(cells)}")
for k, v in sorted(strategies.items(), key=lambda kv: -sum(kv[1])):
    print(f"{k:22s} {sum(v):3d}/{len(v)}")
print("\nErrors of pipeline strategy:")
for ri, ci, tv, res, comps in cells:
    if res["pipeline"] != tv:
        print(f"row {ri+1} col {ci}: truth={tv!r} pipeline={res['pipeline']!r} raw_x2/7={res.get('raw_x2/psm7')!r} raw_x3/7={res.get('raw_x3/psm7')!r} prep48/7={res.get('prep48/psm7')!r} comps={comps}")
