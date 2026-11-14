import os
import sys

# Nichts nach außen senden
os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chronist import config  # noqa: E402

# Ohne Konsolenfenster (pythonw) Ausgaben in eine Logdatei schreiben
if sys.stdout is None or sys.stderr is None:
    _log = open(config.DATA / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from chronist import debuglog  # noqa: E402
debuglog.setup()

if __name__ == "__main__":
    if "--modelle" in sys.argv:
        from chronist import models
        models.ensure_all(config.settings()["whisper_model"])
    else:
        print("Ascandir - SessionRecorder startet ... (Fenster offen lassen, solange du das Tool benutzt)")
        from chronist import ui
        ui.launch()
