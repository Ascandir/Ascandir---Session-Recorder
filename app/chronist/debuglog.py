"""Protokoll aller wichtigen Schritte + Debug-Bericht zum Verschicken."""
import datetime as dt
import json
import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

from . import config as C

LOGFILE = C.DATA / "app.log"
log = logging.getLogger("recorder")
_set_up = False


def setup():
    global _set_up
    if _set_up:
        return
    _set_up = True
    h = logging.handlers.RotatingFileHandler(LOGFILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)

    def hook(t, v, tb):
        log.error("Unbehandelter Fehler:\n%s", "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    threading.excepthook = lambda a: hook(a.exc_type, a.exc_value, a.exc_traceback)
    log.info("==== Start, Version %s ====", _version())


def _version():
    p = Path(__file__).resolve().parents[1] / "VERSION"
    return p.read_text().strip() if p.exists() else "?"


def _tail(path, n):
    try:
        return "\n".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-n:])
    except Exception as e:
        return f"(nicht lesbar: {e})"


def _safe(fn):
    try:
        return fn()
    except Exception as e:
        return f"(Fehler: {e})"


def _sd_log():
    from . import config as C
    logs = sorted(C.SESSIONS.glob("*/*/comic/bildgenerator.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return "(keins)"
    lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    keep = [l for l in lines if l.startswith("=====") or "vulkan" in l.lower() or "backend" in l.lower()
            or "[E]" in l or "[W]" in l or "taking" in l or "completed" in l]
    return "\n".join(l[:400] for l in keep[-60:])


def _plugin_log():
    from . import config as C
    logs = sorted(C.recordings_dir().glob("*_spuren/plugin.log"), key=lambda p: p.stat().st_mtime)
    if not logs:
        return "(keins)"
    return "".join(logs[-1].read_text(encoding="utf-8", errors="replace").splitlines(True)[-40:])


def report():
    from . import llm as L
    from .recorder import find_discord, list_mics
    st = dict(C.settings())
    st.pop("llm_key", None)
    lines = [
        "===== Ascandir SessionRecorder – Debug-Bericht =====",
        f"Erstellt: {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Version: {_version()}",
        f"Windows: {platform.platform()}",
        f"Python: {sys.version.split()[0]}  ({sys.executable})",
        f"Programmordner: {C.ROOT}",
        "",
        "----- KI -----",
        f"Modus eingestellt: {st.get('ki_modus')}  →  tatsächlich: {_safe(L.resolved_mode)}",
        f"Strata gefunden: {_safe(lambda: L.find_strata(force=True))}",
        f"Strata-Startbefehl (Einstellung): {st.get('strata_befehl') or '(leer)'}",
        f"Strata im PATH: {_safe(lambda: __import__('shutil').which('strata'))}",
        f"KI-Adresse: {st.get('llm_url')}  →  antwortet: {_safe(lambda: L.api_up(st['llm_url']))}",
        f"Eingebaute KI installiert: {_safe(L.builtin_installed)}",
        f"Prozesse am KI-Port: {_safe(lambda: _port_procs(L, st['llm_url']))}",
        f"strata-Befehl: {_safe(L.strata_cli)}",
        f"Strata-Preset (Einstellung): {st.get('strata_preset') or '(automatisch)'}  →  benutzt: {_safe(L.strata_preset)}",
        "strata launch --list-presets:",
        _safe(lambda: L.list_presets()[1]),
        "strata launch --status:",
        _safe(lambda: L.strata_status_urls()[1]),
        f"Laufende Strata-Prozesse: {_safe(_strata_procs)}",
        f"Vesktop/Plugin: {_safe(lambda: __import__('chronist.vesktop', fromlist=['x']).status_text())}",
        f"Vesktop-Plugin-Protokoll (letzte Aufnahme):\n{_safe(_plugin_log)}",
        f"Bildgenerator-Protokoll (letzter Comic):\n{_safe(_sd_log)}",
        "",
        "----- Aufnahme -----",
        f"Discord-Prozess: {_safe(find_discord)}",
        f"Mikrofone: {_safe(list_mics)}",
        "",
        "----- Einstellungen -----",
        json.dumps(st, ensure_ascii=False, indent=2),
        "",
        f"----- Gruppe (aktiv: {C.active_group()}, alle: {C.list_groups()}) -----",
        json.dumps({k: v for k, v in C.group().items() if k != "kontext"}, ensure_ascii=False, indent=2),
        "",
        "----- Programm-Protokoll (letzte 300 Zeilen) -----",
        _tail(LOGFILE, 300),
        "",
        "----- KI-Protokoll (letzte 150 Zeilen) -----",
        _tail(C.DATA / "ki-log.txt", 150),
        "",
        "----- Start-Protokoll (letzte 80 Zeilen) -----",
        _tail(C.DATA / "log.txt", 80),
    ]
    return "\n".join(str(x) for x in lines)


def _port_procs(L, url):
    import psutil
    out = []
    for pid in L._listener_pids(L._port(url)):
        try:
            p = psutil.Process(pid)
            out.append(f"{pid} {p.name()} {p.exe()}")
        except Exception:
            out.append(str(pid))
    return out or "keine"


def _strata_procs():
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline"]):
        try:
            text = " ".join([p.info["name"] or "", p.info["exe"] or "", " ".join(p.info["cmdline"] or [])])
            if "strata" in text.lower() and "sessionrecorder" not in text.lower():
                out.append(f"{p.info['pid']} {p.info['name']} {p.info['exe']}")
        except Exception:
            pass
    return out or "keine"


def save_report():
    import re
    text = report()
    # API-Schlüssel nie mitschicken
    text = re.sub(r"(?i)(key[^:=\n]*[:=]\s*\"?)([A-Za-z0-9_\-\.]{4})[A-Za-z0-9_\-\.]{4,}", r"\1\2…", text)
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M")
    folder = C.DATA / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"SessionRecorder-Debug-{stamp}.txt"
    p.write_text(text, encoding="utf-8")
    return str(p), text
