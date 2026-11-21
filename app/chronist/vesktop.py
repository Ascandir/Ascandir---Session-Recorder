"""Einzelspuren über Vesktop: Vencord mit dem SessionRecorder-Plugin einrichten.

Vesktop ist ein Discord-Programm, in dem Sprache über WebRTC läuft – dort hat jede Person
einen eigenen Audiostrom. Unser Vencord-Plugin nimmt diese Ströme getrennt auf, solange der
SessionRecorder aufnimmt, und legt sie in den Aufnahmeordner.
"""
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

from . import config as C
from .debuglog import log

PLUGIN_TAG = "vencord-plugin"
ASSET = "Vencord-SessionRecorder.zip"
LINUX = sys.platform.startswith("linux")
FLATPAK_ID = "dev.vencord.Vesktop"
PLUGIN_NAME = "AscandirSessionRecorder"
UA = {"User-Agent": "Ascandir-SessionRecorder"}


def _appdata():
    return Path(os.environ.get("APPDATA") or C.ORIG_ENV.get("APPDATA") or Path.home() / "AppData" / "Roaming")


def _candidates():
    if LINUX:
        cfg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        return [cfg / "vesktop", cfg / "Vesktop", Path.home() / ".var" / "app" / FLATPAK_ID / "config" / "vesktop"]
    return [_appdata() / "vesktop", _appdata() / "Vesktop"]


def data_dir():
    """Vesktop-Datenordner (Windows: %APPDATA%\vesktop · Linux: ~/.config/vesktop oder Flatpak)."""
    return next((d for d in _candidates() if d.exists()), None)


def is_flatpak():
    d = data_dir()
    return bool(d and FLATPAK_ID in str(d))


def vencord_dir():
    """Wo unser Vencord liegt. Linux: im Vesktop-Datenordner, damit auch die Flatpak-Sandbox es lesen kann."""
    if LINUX and data_dir():
        return data_dir() / "ascandir-vencord"
    return C.ROOT / "vencord"


def vesktop_installed():
    if data_dir():
        return True
    if LINUX:
        return bool(shutil.which("vesktop"))
    local = Path(os.environ.get("LOCALAPPDATA") or C.ORIG_ENV.get("LOCALAPPDATA") or "")
    return bool(local) and (local / "vesktop").exists()


def _is_vesktop(p):
    n = (p.info.get("name") or "").lower()
    if n in ("vesktop.exe", "vesktop", ".vesktop-wrapped"):
        return True
    if LINUX and n.startswith("electron"):
        try:
            return any("vesktop" in a.lower() for a in (p.cmdline() or [])[:3])
        except Exception:
            return False
    return False


def vesktop_running():
    try:
        import psutil
        return any(_is_vesktop(p) for p in psutil.process_iter(["name"]))
    except Exception:
        return False


def installed_version():
    try:
        return (vencord_dir() / "version.txt").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def configured():
    """Zeigt Vesktop auf unseren Vencord-Ordner?"""
    d = data_dir()
    if not d:
        return False
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        return Path(st.get("vencordDir", "")).resolve() == vencord_dir().resolve()
    except Exception:
        return False


def _release():
    repo = C.settings().get("update_repo") or "Ascandir/Ascandir---Session-Recorder"
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/tags/{PLUGIN_TAG}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _vesktop_procs():
    import psutil
    out = []
    for p in psutil.process_iter(["name", "exe", "ppid", "pid"]):
        try:
            if _is_vesktop(p):
                out.append(p)
        except Exception:
            pass
    return out


def install(progress=lambda f, m: None, restart=False):
    """Lädt das fertig gebaute Vencord (mit Plugin), entpackt es und stellt Vesktop darauf um.
    restart=True: ein laufendes Vesktop wird dafür kurz beendet und danach wieder gestartet."""
    exe = None
    if vesktop_running():
        if not restart:
            raise RuntimeError("Bitte Vesktop zuerst ganz schließen (auch unten rechts im Infobereich), "
                               "dann noch einmal klicken.")
        import time
        procs = _vesktop_procs()
        pids = {p.info["pid"] for p in procs}
        main = next((p for p in procs if p.info.get("ppid") not in pids), procs[0] if procs else None)
        if is_flatpak():
            exe = ["flatpak", "run", FLATPAK_ID]
        elif main is not None:
            try:
                exe = main.cmdline() if LINUX else [main.info["exe"]]
            except Exception:
                exe = [main.info["exe"]] if main.info.get("exe") else None
        progress(0.02, "Vesktop wird kurz beendet …")
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        for _ in range(20):
            if not vesktop_running():
                break
            time.sleep(0.5)
        for p in _vesktop_procs():
            try:
                p.kill()
            except Exception:
                pass
        time.sleep(1)
    if not data_dir():
        _candidates()[0].mkdir(parents=True, exist_ok=True)
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
    shutil.rmtree(vencord_dir(), ignore_errors=True)
    shutil.move(str(new), str(vencord_dir()))

    progress(0.85, "Vesktop wird eingestellt …")
    d = data_dir()
    if not d:
        d = _candidates()[0]
        d.mkdir(parents=True, exist_ok=True)
    sp = d / "state.json"
    try:
        st = json.loads(sp.read_text(encoding="utf-8")) if sp.exists() else {}
    except Exception:
        st = {}
    st["vencordDir"] = str(vencord_dir())
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
    log.info("Vesktop-Plugin installiert (%s) und Vesktop umgestellt: %s", installed_version(), vencord_dir())
    if exe:
        import subprocess
        progress(0.95, "Vesktop wird wieder gestartet …")
        try:
            subprocess.Popen(exe, env=C.ORIG_ENV, close_fds=True, start_new_session=LINUX,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except Exception as e:
            log.warning("Vesktop konnte nicht neu gestartet werden: %s", e)
    progress(1.0, "Fertig.")


def uninstall():
    if vesktop_running():
        raise RuntimeError("Bitte Vesktop zuerst ganz schließen.")
    d = data_dir()
    if d and (d / "state.json").exists():
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
        st.pop("vencordDir", None)
        (d / "state.json").write_text(json.dumps(st, indent=4), encoding="utf-8")
    shutil.rmtree(vencord_dir(), ignore_errors=True)


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
