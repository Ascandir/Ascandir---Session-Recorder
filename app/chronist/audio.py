"""Ton aus der Aufnahme holen (ffmpeg wird mitgeliefert)."""
import re
import subprocess
import sys

import imageio_ffmpeg
import numpy as np
import soundfile as sf

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
SR = 16000
_NOWIN = 0x08000000 if sys.platform == "win32" else 0   # kein schwarzes Fenster


def _run(args):
    return subprocess.run(args, capture_output=True, creationflags=_NOWIN)


def audio_streams(path):
    """Anzahl der Tonspuren in der Datei."""
    r = _run([FFMPEG, "-hide_banner", "-i", str(path)])
    txt = r.stderr.decode("utf-8", errors="replace")
    return len(re.findall(r"Stream #\d+:\d+.*?: Audio", txt))


def extract(path, index, out_wav):
    r = _run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", str(path),
              "-map", f"0:a:{index}", "-ac", "1", "-ar", str(SR),
              "-af", "highpass=f=70,dynaudnorm=f=200:g=15:p=0.9",
              "-c:a", "pcm_s16le", str(out_wav)])
    if r.returncode != 0:
        raise RuntimeError("Tonspur konnte nicht gelesen werden:\n"
                           + r.stderr.decode("utf-8", errors="replace")[-800:])
    return out_wav


def load_wav(path):
    a, sr = sf.read(str(path), dtype="float32")
    if a.ndim > 1:
        a = a.mean(axis=1)
    assert sr == SR
    return a


def write_clip(audio, parts, out_wav, max_len=12.0):
    """Kurzes Hörbeispiel aus mehreren Stücken [(start,end),...]."""
    pieces, total = [], 0.0
    gap = np.zeros(int(0.4 * SR), np.float32)
    for st, en in parts:
        en = min(en, st + (max_len - total))
        if en - st < 0.5:
            continue
        pieces += [audio[int(st * SR):int(en * SR)], gap]
        total += en - st
        if total >= max_len:
            break
    if pieces:
        sf.write(str(out_wav), np.concatenate(pieces), SR)
        return str(out_wav)
    return None
