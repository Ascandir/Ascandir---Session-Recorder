"""Einzelspuren über Vesktop: Vencord mit dem SessionRecorder-Plugin einrichten.

Vesktop ist ein Discord-Programm, in dem Sprache über WebRTC läuft – dort hat jede Person
einen eigenen Audiostrom. Unser Vencord-Plugin nimmt diese Ströme getrennt auf, solange der
SessionRecorder aufnimmt, und legt sie in den Aufnahmeordner.
"""
import json
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import config as C
from .debuglog import log

PLUGIN_TAG = "vencord-plugin"
ASSET = "Vencord-SessionRecorder.zip"
VENCORD_DIR = C.ROOT / "vencord"
PLUGIN_NAME = "AscandirSessionRecorder"
UA = {"User-Agent": "Ascandir-SessionRecorder"}


def _appdata():
    return Path(os.environ.get("APPDATA") or C.ORIG_ENV.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def data_dir():
    """Vesktop-Datenordner (normal oder portable)."""
    for name in ("vesktop", "Vesktop"):
        d = _appdata() / name
        if d.exists():
            return d
    return None


def vesktop_installed():
    if data_dir():
        return True
    local = Path(os.environ.get("LOCALAPPDATA") or C.ORIG_ENV.get("LOCALAPPDATA") or "")
    return bool(local) and (local / "vesktop").exists()


def vesktop_running():
    try:
        import psutil
        return any((p.info["name"] or "").lower() == "vesktop.exe" for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def installed_version():
    try:
        return (VENCORD_DIR / "version.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def configured():
    """Zeigt Vesktop auf unseren Vencord-Ordner?"""
    d = data_dir()
    if not d:
        return False
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        return Path(st.get("vencordDir", "")).resolve() == VENCORD_DIR.resolve()
    except Exception:
        return False


def _release():
    repo = C.settings().get("update_repo") or "Ascandir/Ascandir---Session-Recorder"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/tags/{PLUGIN_TAG}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def install(progress=lambda f, m: None):
    """Lädt das fertig gebaute Vencord (mit Plugin), entpackt es und stellt Vesktop darauf um."""
    if vesktop_running():
        raise RuntimeError("Bitte Vesktop zuerst ganz schließen (auch unten rechts im Infobereich), "
                           "dann noch einmal klicken.")
    progress(0.05, "Suche das neueste Plugin …")
    rel = _release()
    asset = next((a for a in rel.get("assets", []) if a["name"] == ASSET), None)
    if not asset:
        raise RuntimeError("Das Plugin-Paket wurde im Release nicht gefunden.")
    tmp = C.TEMP / ASSET
    progress(0.2, "Plugin wird geladen …")
    req = urllib.request.Request(asset["browser_download_url"], headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    progress(0.6, "Plugin wird entpackt …")
    new = C.TEMP / "vencord_neu"
    shutil.rmtree(new, ignore_errors=True)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(new)
    tmp.unlink(missing_ok=True)
    if not (new / "vencordDesktopMain.js").exists():
        raise RuntimeError("Das Plugin-Paket ist unvollständig.")
    (new / "version.txt").write_text(asset.get("updated_at", "") or rel.get("published_at", ""), encoding="utf-8")
    shutil.rmtree(VENCORD_DIR, ignore_errors=True)
    shutil.move(str(new), str(VENCORD_DIR))

    progress(0.85, "Vesktop wird eingestellt …")
    d = data_dir()
    if not d:
        d = _appdata() / "vesktop"
        d.mkdir(parents=True, exist_ok=True)
    sp = d / "state.json"
    try:
        st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:
        st = {}
    st["vencordDir"] = str(VENCORD_DIR)
    sp.write_text(json.dumps(st, indent=4), encoding="utf-8")
    # Plugin in den Vencord-Einstellungen einschalten
    vp = d / "settings" / "settings.json"
    try:
        vs = json.loads(vp.read_text(encoding="utf-8")) if vp.exists() else {}
        vs.setdefault("plugins", {}).setdefault(PLUGIN_NAME, {})["enabled"] = True
        vp.parent.mkdir(parents=True, exist_ok=True)
        vp.write_text(json.dumps(vs, indent=4), encoding="utf-8")
    except Exception as e:
        log.warning("Vencord-Einstellungen nicht angepasst: %s", e)
    log.info("Vesktop-Plugin installiert (%s) und Vesktop umgestellt: %s", installed_version(), VENCORD_DIR)
    progress(1.0, "Fertig.")


def uninstall():
    if vesktop_running():
        raise RuntimeError("Bitte Vesktop zuerst ganz schließen.")
    d = data_dir()
    if d and (d / "state.json").exists():
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        st.pop("vencordDir", None)
        (d / "state.json").write_text(json.dumps(st, indent=4), encoding="utf-8")
    shutil.rmtree(VENCORD_DIR, ignore_errors=True)


def status_text():
    from .recorder import plugin_connected
    if plugin_connected():
        return "✅ **Plugin verbunden** – Vesktop läuft und meldet sich beim SessionRecorder."
    if not vesktop_installed():
        return "Vesktop ist noch nicht installiert."
    if not configured():
        return "Vesktop gefunden, das Plugin ist aber noch nicht eingerichtet."
    if not vesktop_running():
        return f"Eingerichtet (Stand {installed_version()[:10] or '?'}). Starte Vesktop, dann verbindet es sich."
    return ("Eingerichtet, aber das Plugin meldet sich nicht. Prüfe in Vesktop unter Einstellungen → Vencord → "
            "Plugins, ob „AscandirSessionRecorder“ eingeschaltet ist, und starte Vesktop neu.")
