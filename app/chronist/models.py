"""Einmaliger Download der KI-Modelle in den Tool-Ordner. Danach läuft alles offline."""
import tarfile
import urllib.request
from pathlib import Path

from .config import MODELS

_B = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION = MODELS / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
EMBEDDING = MODELS / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
WHISPER_DIR = MODELS / "whisper"


def _download(url, dest: Path):
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  lade {url.rsplit('/', 1)[-1]} ...", flush=True)
    urllib.request.urlretrieve(url, tmp)
    tmp.replace(dest)


def ensure_diarization_models():
    if not SEGMENTATION.exists():
        arc = MODELS / "seg.tar.bz2"
        _download(f"{_B}/speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2", arc)
        with tarfile.open(arc) as t:
            t.extractall(MODELS)
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
