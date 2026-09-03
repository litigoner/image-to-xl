"""Run a Cloudflare quick tunnel to the local app and publish its HTTPS URL.

The tunnel gives the app a public https://*.trycloudflare.com address without any
account. The address changes whenever the tunnel restarts, so this script writes it
to docs/api.json and pushes that file to GitHub; the GitHub Pages front-end reads
the file to find the current server.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
API_JSON = BASE / "docs" / "api.json"
TARGET = os.environ.get("TUNNEL_TARGET", "http://127.0.0.1:8080")
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def publish(url: str) -> None:
    current = None
    if API_JSON.exists():
        try:
            current = json.loads(API_JSON.read_text()).get("api")
        except ValueError:
            pass
    if current == url:
        print(f"[tunnel] URL unchanged: {url}", flush=True)
        return
    API_JSON.write_text(json.dumps({"api": url, "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}) + "\n")
    print(f"[tunnel] published {url}", flush=True)
    try:
        subprocess.run(["git", "-C", str(BASE), "add", "docs/api.json"], check=True)
        subprocess.run(["git", "-C", str(BASE), "-c", "user.name=image-to-xl tunnel",
                        "-c", "user.email=tunnel@image-to-xl.local", "commit", "-q", "-m",
                        f"Update public API address ({url})"], check=True)
        subprocess.run(["git", "-C", str(BASE), "push", "-q", "origin", "HEAD:main"], check=True, timeout=120)
        print("[tunnel] pushed docs/api.json to GitHub", flush=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"[tunnel] git push failed: {exc}", flush=True)


def main() -> int:
    proc = subprocess.Popen(["cloudflared", "tunnel", "--url", TARGET, "--no-autoupdate"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        m = URL_RE.search(line)
        if m:
            publish(m.group(0))
    return proc.wait()


if __name__ == "__main__":
    sys.exit(main())
