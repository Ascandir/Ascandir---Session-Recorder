import os
import sys

# Nichts nach außen senden
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("DO_NOT_TRACK", "1")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chronist import config  # noqa: E402
config.pin_storage_env()        # vor allen anderen Bibliotheken!

# Ohne Konsolenfenster (pythonw) Ausgaben in eine Logdatei schreiben
if sys.stdout is None or sys.stderr is None:
    _log = open(config.DATA / "log.txt", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from chronist import debuglog  # noqa: E402
debuglog.setup()

from chronist import storage  # noqa: E402
storage.migrate_old_layout()
storage.migrate_to_groups()

if __name__ == "__main__":
    if "--modelle" in sys.argv:
        from chronist import models
        models.ensure_all(config.settings()["whisper_model"])
    elif "--selbsttest" in sys.argv:
        # Für den Paketbau: lädt alle Bausteine einmal, ohne Fenster. Ergebnis auch in daten/selbsttest.txt
        import importlib
        import traceback
        lines, ok = [], True
        mods = ["faster_whisper", "ctranslate2", "sherpa_onnx", "soundfile", "sounddevice", "scipy.cluster",
                "openai", "PIL", "psutil", "imageio_ffmpeg", "av", "PySide6.QtWidgets", "PySide6.QtMultimedia",
                "chronist.pipeline", "chronist.recorder", "chronist.comic", "chronist.qt.app"]
        if sys.platform == "win32":
            mods.append("proctap")
        for m in mods:
            try:
                importlib.import_module(m)
                lines.append(f"ok {m}")
            except Exception:
                ok = False
                lines.append(f"FEHLER {m}:\n{traceback.format_exc()}")
        try:
            import imageio_ffmpeg
            lines.append("ffmpeg: " + imageio_ffmpeg.get_ffmpeg_exe())
        except Exception as e:
            ok = False
            lines.append(f"FEHLER ffmpeg: {e}")
        lines.append("SELBSTTEST OK" if ok else "SELBSTTEST FEHLGESCHLAGEN")
        text = "\n".join(lines)
        (config.DATA / "selbsttest.txt").write_text(text, encoding="utf-8")
        print(text)
        sys.exit(0 if ok else 1)
    else:
        from chronist.qt.app import launch
        launch()
