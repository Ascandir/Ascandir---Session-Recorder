"""Updates von GitHub-Releases holen. Es wird nur die Versionsnummer abgefragt – keine Daten von dir gesendet."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

from . import config as C

VERSION = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip() \
    if (Path(__file__).resolve().parents[1] / "VERSION").exists() else "0.0.0"

_cache = {}


def _ver(v):
    out = []
    for p in v.strip().lstrip("vV").split("."):
        num = "".join(ch for ch in p if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out + [0] * (3 - len(out)))


def check(force=False):
    """{'neu': bool, 'version': str, 'text': str, 'url': str} oder {'fehler': str}."""
    if _cache and not force:
        return _cache
    repo = C.settings().get("update_repo", "").strip()
    if not repo:
        return {"fehler": "Keine Update-Quelle eingestellt."}
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                     headers={"User-Agent": "Ascandir-SessionRecorder", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"fehler": "Noch keine Version veröffentlicht."}
        return {"fehler": f"GitHub antwortet nicht (HTTP {e.code})."}
    except Exception:
        return {"fehler": "Keine Internetverbindung zu GitHub."}
    asset = next((a for a in d.get("assets", []) if a["name"].lower().endswith("setup.exe")), None)
    neu = _ver(d.get("tag_name", "0")) > _ver(VERSION) and asset is not None
    _cache.clear()
    _cache.update({"neu": neu, "version": d.get("tag_name", "").lstrip("vV"),
                   "text": (d.get("body") or "").strip(), "url": asset["browser_download_url"] if asset else ""})
    return dict(_cache)


def check_in_background():
    threading.Thread(target=check, daemon=True).start()


def install(progress=lambda f, m: None):
    """Lädt das neue Setup und startet es. Das Tool beendet sich danach selbst."""
    info = check()
    if not info.get("neu"):
        raise RuntimeError("Kein Update verfügbar.")
    dest = C.TEMP / f"Ascandir-SessionRecorder-Setup-{info['version']}.exe"
    req = urllib.request.Request(info["url"], headers={"User-Agent": "Ascandir-SessionRecorder"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if total:
                progress(done / total, f"Update wird geladen … {done / 1e6:.0f} von {total / 1e6:.0f} MB")
    if sys.platform == "win32":
        subprocess.Popen([str(dest), "/update"], close_fds=True,
                         creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
    threading.Timer(1.5, lambda: os._exit(0)).start()
    return info["version"]
