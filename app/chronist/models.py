"""Einmaliger Download der KI-Modelle in den Tool-Ordner. Danach läuft alles offline."""
import tarfile
import urllib.request
from pathlib import Path

from .config import MODELS

_B = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
VOICE_DIR = MODELS / "stimmerkennung"
SEGMENTATION = VOICE_DIR / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING = VOICE_DIR / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
WHISPER_DIR = MODELS / "whisper"


def _download(url, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  lade {url.rsplit('/', 1)[-1]} ...", flush=True)
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def ensure_diarization_models():
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if not SEGMENTATION.exists():
        arc = VOICE_DIR / "seg.tar.bz2"
        _download(f"{_B}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", arc)
        with tarfile.open(arc) as t:
            t.extractall(VOICE_DIR)
        arc.unlink()
    if not EMBEDDING.exists():
        _download(f"{_B}/speaker-recongition-models/{EMBEDDING.name}", EMBEDDING)


def whisper_marker(name):
    return WHISPER_DIR / f".fertig-{name}"


def ensure_whisper(name):
    from faster_whisper import WhisperModel
    if whisper_marker(name).exists():
        return
    print(f"  lade Spracherkennung '{name}' (einmalig, ca. 0,8-3 GB) ...", flush=True)
    WHISPER_DIR.mkdir(parents=True, exist_ok=True)
    WhisperModel(name, device="cpu", compute_type="int8", download_root=str(WHISPER_DIR))
    whisper_marker(name).touch()


def ensure_all(whisper_name):
    print("Modelle werden geprueft/heruntergeladen ...")
    ensure_diarization_models()
    ensure_whisper(whisper_name)
    print("Alle Modelle sind da. Ab jetzt laeuft alles offline.")


# ---------- Alles auf einmal (Knopf „Alle Modelle jetzt laden“) ----------
def _size(p):
    p = Path(p)
    if not p.exists():
        return 0
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size


def status():
    """[(Name, fertig?, Hinweis)] – was für eine Auswertung/einen Comic schon da ist."""
    from . import comic as CO
    from . import config as C
    from . import llm as L
    wm = C.settings().get("whisper_model", "large-v3-turbo")
    mode = L.resolved_mode()
    out = [
        ("Stimmerkennung", SEGMENTATION.exists() and EMBEDDING.exists(), "ca. 50 MB"),
        (f"Spracherkennung ({wm})", whisper_marker(wm).exists(), "ca. 1,6–3 GB"),
        (f"Bildgenerator: {CO.MODELLE[CO.modell()]['name']}", CO.installed(), CO.MODELLE[CO.modell()]["groesse"]),
    ]
    if mode == "Eingebaute KI":
        out.append(("Eingebaute KI (Qwen3-14B)", L.builtin_installed(), "ca. 9 GB"))
    else:
        out.append((f"Text-KI: {mode}", True, "wird nicht vom SessionRecorder geladen"))
    return out


def all_present():
    return all(ok for _, ok, _ in status())


def install_all(progress=lambda f, m: None):
    """Lädt alles, was noch fehlt. Abbrechen ist zwischen den Schritten und während großer Downloads möglich."""
    from . import comic as CO
    from . import config as C
    from . import llm as L
    from .debuglog import log
    wm = C.settings().get("whisper_model", "large-v3-turbo")
    progress(0.01, "Stimmerkennung wird geprüft …")
    ensure_diarization_models()
    progress(0.05, f"Spracherkennung „{wm}“ wird geladen (ca. 1,6–3 GB, ohne Fortschrittsanzeige) …")
    ensure_whisper(wm)
    builtin = L.resolved_mode() == "Eingebaute KI"
    end_img = 0.7 if builtin else 1.0
    if not CO.installed():
        CO.install(lambda f, m: progress(0.25 + (end_img - 0.25) * f, m))
    if builtin and not L.builtin_installed():
        L.install_builtin(lambda f, m: progress(0.7 + 0.3 * f, m))
    log.info("Alle Modelle geladen")
    progress(1.0, "Alle Modelle sind da – ab jetzt läuft alles offline.")
    return status()
