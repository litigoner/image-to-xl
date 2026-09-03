# Image → Excel (table image to spreadsheet with chart)

Web application on **port 8080** that converts an image of a table (for example `data.jpeg`)
into an Excel workbook containing the data table, a summary table and charts.
English text is kept as it is; Nepali (Devanagari) text is translated to English.

## Run

The app is installed as a systemd service:

```bash
sudo systemctl status imagetoexcel     # status / logs: journalctl -u imagetoexcel -f
sudo systemctl restart imagetoexcel
```

Manual start (development):

```bash
.venv/bin/python app.py                # http://<server-ip>:8080
```

## Use

* Open `http://<server-ip>:8080`, upload a table image (JPEG/PNG/WebP/BMP/TIFF), click **Convert to Excel**.
* Or via API: `curl -F image=@table.jpg http://<server-ip>:8080/api/convert -o table.xlsx`
  (`?format=json` returns the extracted rows plus a download link).

## Output workbook

| Sheet | Content |
|---|---|
| Data | Translated table with headers (merged like the original) and the total row exactly as printed in the image (nothing is added) |
| Summary | Totals, a table aggregated by the category column (e.g. District), a stacked bar chart, and, when present, a second table + column chart by the group column (e.g. Province) |
| Original OCR | The untranslated text exactly as read from the image, plus notes |

## Engines

* **Tesseract OCR** (default, offline): OpenCV finds the ruling lines of the table, detects merged cells,
  OCRs each cell (`nep+eng`) and translates Nepali with a dictionary + transliteration (`translate_ne.py`).
* **Claude vision** (optional): set `ANTHROPIC_API_KEY` in `.env` (see `.env.example`) and restart the
  service. Handles hand-written or grid-less tables and gives better translations. When configured, the
  "Auto" engine uses it and falls back to Tesseract on error.

## Public access (live)

* Front-end (GitHub Pages): **https://litigoner.github.io/image-to-xl/**
* Backend: this server, exposed over HTTPS by a Cloudflare quick tunnel (`imagetoexcel-tunnel` service,
  `tunnel_publish.py`). The tunnel address changes when the tunnel restarts; the service writes the current
  address to `docs/api.json` and pushes it, and the Pages front-end reads that file. Quick tunnels need no
  account but carry no uptime guarantee; for a permanent address use one of the options below.

## Deployment options (so anyone can use it)

GitHub Pages can only serve static files; the OCR needs a Python server. Choose one:

| Where | How | Notes |
|---|---|---|
| **This server (done)** | systemd service `imagetoexcel` on port 8080 | LAN: `http://192.168.0.100:8080`. For the public IP (`110.34.26.106`) forward TCP 8080 on the router/firewall to this machine. |
| **Hugging Face Spaces** (free) | New Space -> Docker -> point at this repo (or push the files) | Good free option for OCR apps; always reachable at `https://<user>-image-to-xl.hf.space`. |
| **Render** (free tier) | New + -> Blueprint -> select this repo (`render.yaml`) | HTTPS URL out of the box; free instances sleep when idle (first request is slow). |
| **Any Docker host** (Fly.io, Railway, Cloud Run, a VPS) | `docker build -t image-to-xl . && docker run -p 8080:8080 image-to-xl` | Set `PORT` if the platform requires a different port. |
| **GitHub Pages front-end** | `docs/index.html` (enable Pages: Settings -> Pages -> branch `main`, folder `/docs`) | Static page that calls one of the HTTPS API deployments above; the API URL is entered once in the page. A `xyz.github.io` root site needs a GitHub user/org named `xyz` with a repo `xyz.github.io`. |

Optional: set `ANTHROPIC_API_KEY` in the environment of any deployment to enable the Claude vision engine.

## Files

`app.py` Flask app · `pipeline.py` orchestration · `ocr_table.py` grid detection + OCR ·
`table_model.py` header/column interpretation · `translate_ne.py` Nepali→English ·
`excel_builder.py` workbook + charts · `claude_extract.py` optional Claude engine ·
`tests/eval_digits.py` / `tests/eval_digits_any.py` OCR accuracy checks on the samples · `tunnel_publish.py` public HTTPS tunnel.

System packages required: `tesseract-ocr tesseract-ocr-nep tesseract-ocr-eng libgl1`.
