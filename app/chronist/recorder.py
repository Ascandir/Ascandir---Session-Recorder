"""Eingebaute Tonaufnahme: dein Mikro + nur der Ton von Discord, auf zwei getrennten Spuren.

Kein Bild, keine Kamera. Läuft im Hintergrund und braucht kaum Leistung.
"""
import datetime as dt
import queue
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config as C
from .audio import FFMPEG, _NOWIN

DISCORD_NAMES = {"discord.exe", "discordptb.exe", "discordcanary.exe"}


# ---------- Geräte & Discord finden ----------
def list_mics():
    """[(Anzeigename, Geräte-Index)] – Standardmikro zuerst."""
    try:
        import sounddevice as sd
        default_in = sd.default.device[0]
        hostapi_mme = next((i for i, h in enumerate(sd.query_hostapis()) if "MME" in h["name"]), None)
        out, seen = [], set()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            if hostapi_mme is not None and d["hostapi"] != hostapi_mme:
                continue        # jedes Gerät nur einmal zeigen
            name = d["name"]
            if name in seen or "Zuordnung" in name or "Mapper" in name:
                continue
            seen.add(name)
            out.append((("⭐ " if i == default_in else "") + name, i))
        out.sort(key=lambda x: not x[0].startswith("⭐"))
        return out
    except Exception:
        return []


def find_discord():
    """PID des Haupt-Discord-Prozesses (inkl. Unterprozesse wird mit aufgenommen)."""
    import psutil
    procs = []
    for p in psutil.process_iter(["pid", "name", "ppid"]):
        try:
            if (p.info["name"] or "").lower() in DISCORD_NAMES:
                procs.append(p)
        except Exception:
            pass
    for p in procs:
        try:
            parent = psutil.Process(p.info["ppid"])
            if (parent.name() or "").lower() not in DISCORD_NAMES:
                return p.info["pid"]
        except Exception:
            return p.info["pid"]
    return procs[0].info["pid"] if procs else None


# ---------- Eine Spur ----------
class _Track:
    """Schreibt eine Spur als FLAC und füllt Lücken (Stille) zeitrichtig auf."""

    def __init__(self, path, sr):
        self.path = Path(path)
        self.sr = sr
        self.file = sf.SoundFile(str(path), "w", samplerate=sr, channels=1,
                                 format="FLAC", subtype="PCM_16")
        self.written = 0
        self.t0 = None
        self.level = 0.0
        self.last_data = 0.0

    def start(self, t0):
        self.t0 = t0

    def add(self, mono, now):
        if self.t0 is None or len(mono) == 0:
            return
        # Wo müssten wir laut Uhr am Ende dieses Stücks sein?
        expected_end = int((now - self.t0) * self.sr)
        gap = expected_end - (self.written + len(mono))
        if gap > self.sr * 0.15:
            self._silence(gap)
        self.file.write(np.clip(mono, -1, 1).astype(np.float32))
        self.written += len(mono)
        rms = float(np.sqrt(np.mean(mono ** 2)) + 1e-9)
        self.level = max(self.level * 0.6, min(1.0, max(0.0, (20 * np.log10(rms) + 50) / 45)))
        self.last_data = now

    def pad_to(self, now):
        expected = int((now - self.t0) * self.sr)
        if expected - self.written > self.sr * 0.5:
            self._silence(expected - self.written)
        self.level *= 0.5

    def _silence(self, n):
        chunk = np.zeros(min(n, self.sr * 10), np.float32)
        while n > 0:
            k = min(n, len(chunk))
            self.file.write(chunk[:k])
            self.written += k
            n -= k

    def close(self):
        self.file.close()


# ---------- Aufnahme ----------
class Recorder:
    def __init__(self):
        self.running = False
        self.error = ""
        self.start_time = None
        self.discord_status = ""
        self._threads = []
        self._stop = threading.Event()

    # Öffentliche Infos für die Oberfläche
    def elapsed(self):
        return time.monotonic() - self.start_time if self.running and self.start_time else 0

    def levels(self):
        return (self.mic.level if self.running else 0.0,
                self.dis.level if self.running else 0.0)

    def start(self, mic_index, name=""):
        if self.running:
            return
        import sounddevice as sd
        self.error = ""
        st = C.settings()
        folder = Path(st["aufnahme_ordner"])
        folder.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H-%M")
        import re
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", (name or "").strip()).strip(" .")[:80]
        self.session_name = safe
        base = f"{stamp} {safe}" if safe else f"{stamp} Session"
        self.out_path = folder / f"{base}.mka"
        n = 2
        while self.out_path.exists():
            self.out_path = folder / f"{base} ({n}).mka"
            n += 1
        tmp = C.DATA / "aufnahme_tmp"
        tmp.mkdir(exist_ok=True)

        dev = sd.query_devices(mic_index) if mic_index is not None else sd.query_devices(kind="input")
        mic_sr = int(dev["default_samplerate"]) or 48000
        self.mic = _Track(tmp / "mikro.flac", mic_sr)
        self.dis = _Track(tmp / "discord.flac", 48000)
        self._q = queue.Queue()
        self._stop.clear()

        def mic_cb(indata, frames, t, status):
            self._q.put(("m", indata[:, 0].copy(), time.monotonic()))

        self._stream = sd.InputStream(device=mic_index, channels=1, samplerate=mic_sr,
                                      dtype="float32", blocksize=int(mic_sr * 0.1),
                                      callback=mic_cb)
        t0 = time.monotonic()
        self.mic.start(t0)
        self.dis.start(t0)
        self.start_time = t0
        self._stream.start()
        self.running = True
        self._threads = [threading.Thread(target=self._writer, daemon=True),
                         threading.Thread(target=self._discord_loop, daemon=True)]
        for th in self._threads:
            th.start()

    def _writer(self):
        while not self._stop.is_set() or not self._q.empty():
            try:
                kind, data, t = self._q.get(timeout=0.5)
            except queue.Empty:
                now = time.monotonic()
                self.mic.pad_to(now)
                self.dis.pad_to(now)
                continue
            try:
                (self.mic if kind == "m" else self.dis).add(data, t)
                if kind == "m":
                    self.dis.pad_to(t)   # Discord liefert in Stille nichts – auffüllen
            except Exception as e:  # pragma: no cover
                self.error = f"Schreibfehler: {e}"

    def _discord_loop(self):
        """Hängt sich an Discord. Wird Discord neu gestartet, verbindet es sich neu."""
        cap = None
        while not self._stop.is_set():
            if cap is None:
                pid = find_discord()
                if pid is None:
                    self.discord_status = "⚠️ Discord läuft nicht – bitte starten."
                    self._stop.wait(3)
                    continue
                try:
                    from proctap import ProcessAudioCapture
                    cap = ProcessAudioCapture(pid)
                    cap.start()
                    self.discord_status = "✅ Discord wird aufgenommen."
                except Exception as e:
                    self.discord_status = f"⚠️ Discord-Ton nicht erreichbar: {e}"
                    cap = None
                    self._stop.wait(3)
                    continue
            try:
                data = cap.read(timeout=0.5)
            except Exception:
                data = None
                try:
                    cap.close()
                except Exception:
                    pass
                cap = None
                continue
            if data:
                x = np.frombuffer(data, dtype=np.float32)
                if len(x) >= 2:
                    mono = x[: len(x) // 2 * 2].reshape(-1, 2).mean(axis=1)
                    self._q.put(("d", mono, time.monotonic()))
            if cap is not None and find_discord() is None:
                cap = None
        if cap is not None:
            try:
                cap.stop(); cap.close()
            except Exception:
                pass

    def stop(self):
        """Beendet die Aufnahme und baut daraus eine Datei mit 2 Spuren (Mikro, Discord)."""
        if not self.running:
            return None
        try:
            self._stream.stop(); self._stream.close()
        except Exception:
            pass
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5)
        end = time.monotonic()
        self.mic.pad_to(end); self.dis.pad_to(end)
        self.mic.close(); self.dis.close()
        self.running = False
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(self.mic.path), "-i", str(self.dis.path),
                            "-map", "0:a", "-map", "1:a", "-c", "copy",
                            "-metadata:s:a:0", "title=Mikro", "-metadata:s:a:1", "title=Discord",
                            str(self.out_path)], capture_output=True, creationflags=_NOWIN)
        if r.returncode != 0:
            self.error = r.stderr.decode("utf-8", "replace")[-500:]
            return None
        self.mic.path.unlink(missing_ok=True)
        self.dis.path.unlink(missing_ok=True)
        return str(self.out_path)


REC = Recorder()
