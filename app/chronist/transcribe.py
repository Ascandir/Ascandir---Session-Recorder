"""Sprache -> Text mit Whisper, lokal auf dem Prozessor."""
import os
import re

from . import models
from .audio import SR

_model = None
_model_name = None

# Typische Whisper-"Halluzinationen" in Stille (deutsch)
_HALLU = re.compile(
    r"(untertitel|vielen dank f(ü|ue)rs zuschauen|danke f(ü|ue)rs zuschauen|"
    r"copyright|swr \d|ard text|zdf|abonnier|bis zum n(ä|ae)chsten mal)",
    re.I)


def _get(name):
    global _model, _model_name
    if _model is None or _model_name != name:
        from faster_whisper import WhisperModel
        models.ensure_whisper(name)
        threads = max(1, (os.cpu_count() or 8) // 2)
        _model = WhisperModel(name, device="cpu", compute_type="int8",
                              cpu_threads=threads,
                              download_root=str(models.WHISPER_DIR),
                              local_files_only=True)
        _model_name = name
    return _model


def transcribe(audio, model_name, prompt="", progress=None):
    """Gibt Wörter zurück: [(start, end, text)]."""
    m = _get(model_name)
    dur = max(len(audio) / SR, 1.0)
    segs, _info = m.transcribe(
        audio, language="de", beam_size=5, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
        word_timestamps=True, condition_on_previous_text=False,
        initial_prompt=prompt or None)
    words = []
    for s in segs:
        if progress:
            progress(min(s.end / dur, 1.0))
        if s.no_speech_prob > 0.8 and s.avg_logprob < -1.0:
            continue
        if _HALLU.search(s.text) and len(s.text) < 90:
            continue
        for w in (s.words or []):
            t = w.word.strip()
            if t:
                words.append((float(w.start), float(w.end), t))
    return words
