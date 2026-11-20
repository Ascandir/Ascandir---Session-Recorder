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
from .debuglog import log
from .audio import FFMPEG, _NOWIN

DISCORD_NAMES = {"discord.exe", "discordptb.exe", "discordcanary.exe", "vesktop.exe"}
BRIDGE_PORT = 7863          # hier fragt das Vencord-Plugin, ob gerade aufgenommen wird


# ---------- Geräte & Discord finden ----------
def list_mics():
    """[(Anzeigename, Geräte-Index)] – Standardmikro zuerst."""
    try:
        import sounddevice as sd
        apis = sd.query_hostapis()
        pick = next((i for i, h in enumerate(apis) if "WASAPI" in h["name"]), None)
        if pick is None:
            pick = next((i for i, h in enumerate(apis) if "MME" in h["name"]), None)
        default_in = apis[pick]["default_input_device"] if pick is not None else sd.default.device[0]
        out, seen = [], set()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] < 1:
                continue
            if pick is not None and d["hostapi"] != pick:
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

    def add(self, mono, now, fill_pauses=False):
        """fill_pauses: nur für Discord – liefert in Stille gar keine Daten. Dann (und nur dann)
        wird die Pause mit Stille aufgefüllt, damit die Spuren zeitlich zusammenpassen."""
        if self.t0 is None or len(mono) == 0:
            return
        if fill_pauses and (self.last_data == 0.0 or now - self.last_data > 0.6):
            expected_end = int((now - self.t0) * self.sr)
            gap = expected_end - (self.written + len(mono))
            if gap > self.sr * 0.3:
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

    def decay(self):
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
        self.stopping = False
        self.spuren_dir = None
        self.plugin_seen = 0.0          # letzter Kontakt des Vencord-Plugins (time.time())

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
        folder = C.recordings_dir()
        self.group = C.active_group()
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
        mic_ch = max(1, min(2, int(dev["max_input_channels"])))
        self.mic = _Track(tmp / "mikro.flac", mic_sr)
        self.dis = _Track(tmp / "discord.flac", 48000)
        self._q = queue.Queue()
        self._stop.clear()

        def mic_cb(indata, frames, t, status):
            if status and status.input_overflow:
                self.overflows += 1
            self._q.put(("m", indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0].copy(),
                         time.monotonic()))

        self.overflows = 0
        self._stream = sd.InputStream(device=mic_index, channels=mic_ch, samplerate=mic_sr,
                                      dtype="float32", blocksize=0, latency="high",
                                      callback=mic_cb)
        t0 = time.monotonic()
        self.mic.start(t0)
        self.dis.start(t0)
        self.start_time = t0
        self.start_epoch = time.time()
        self.spuren_dir = self.out_path.with_name(self.out_path.stem + "_spuren")
        self.stopping = False
        self._stream.start()
        log.info("Aufnahme gestartet: %s | Mikro %s (%s Hz, %s Kanäle)", self.out_path.name,
                 dev.get("name"), mic_sr, mic_ch)
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
                self.mic.decay(); self.dis.decay()
                continue
            try:
                if kind == "m":
                    self.mic.add(data, t)
                    if time.monotonic() - max(self.dis.last_data, self.dis.t0 or 0) > 0.6:
                        self.dis.decay()
                else:
                    self.dis.add(data, t, fill_pauses=True)
            except Exception as e:  # pragma: no cover
                self.error = f"Schreibfehler: {e}"

    def _discord_loop(self):
        """Hängt sich an Discord. Wird Discord neu gestartet, verbindet es sich neu."""
        cap = None
        last_check = time.monotonic()
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
                    log.info("Discord-Aufnahme verbunden (PID %s)", pid)
                except Exception as e:
                    self.discord_status = f"⚠️ Discord-Ton nicht erreichbar: {e}"
                    log.warning("Discord-Ton nicht erreichbar: %s", e)
                    cap = None
                    self._stop.wait(3)
                    continue
            try:
                data = cap.read(timeout=0.2)
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
            if cap is not None and time.monotonic() - last_check > 5:
                last_check = time.monotonic()
                if find_discord() is None:
                    try:
                        cap.close()
                    except Exception:
                        pass
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
        self.stopping = True            # Plugin hört auf und schreibt den Rest weg
        try:
            self._stream.stop(); self._stream.close()
        except Exception:
            pass
        self._stop.set()
        for th in self._threads:
            th.join(timeout=5)
        end = time.monotonic()
        self.dis.pad_to(end)
        # gleiche Länge (Mikro ist die Referenz)
        mic_len = self.mic.written / self.mic.sr
        dis_len = self.dis.written / self.dis.sr
        if dis_len < mic_len:
            self.dis._silence(int((mic_len - dis_len) * self.dis.sr))
        self.mic.close(); self.dis.close()
        self.running = False
        log.info("Aufnahme beendet: Mikro %.0f s, Discord %.0f s, Mikro-Stocker: %s",
                 self.mic.written / self.mic.sr, self.dis.written / self.dis.sr, getattr(self, "overflows", 0))
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                            "-i", str(self.mic.path), "-i", str(self.dis.path),
                            "-map", "0:a", "-map", "1:a", "-c", "copy",
                            "-metadata:s:a:0", "title=Mikro", "-metadata:s:a:1", "title=Discord",
                            str(self.out_path)], capture_output=True, creationflags=_NOWIN)
        if r.returncode != 0:
            self.error = r.stderr.decode("utf-8", "replace")[-500:]
            return None
        # Zusätzlich eine normale MP3 zum Anhören (beide Spuren gemischt, lautstärke-angeglichen)
        mp3 = self.out_path.with_suffix(".mp3")
        subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(self.mic.path), "-i", str(self.dis.path),
                        "-filter_complex", "[0:a]dynaudnorm=f=200:g=15[a];[1:a]dynaudnorm=f=200:g=15[b];"
                                           "[a][b]amix=inputs=2:normalize=0,alimiter=limit=0.95",
                        "-ac", "1", "-ar", "44100", "-b:a", "96k", str(mp3)],
                       capture_output=True, creationflags=_NOWIN)
        self.mic.path.unlink(missing_ok=True)
        self.dis.path.unlink(missing_ok=True)
        try:
            n = finish_user_tracks(self.spuren_dir, self.start_epoch, mic_len)
            if n:
                log.info("Einzelspuren aus Vesktop gespeichert: %s", n)
        except Exception:
            log.exception("Einzelspuren konnten nicht verarbeitet werden")
        self.stopping = False
        return str(self.out_path)


# ---------- Einzelspuren aus dem Vencord-Plugin (Vesktop) ----------
def _safe(n):
    import re
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", str(n)).strip(" .")[:40] or "user"


def finish_user_tracks(folder, rec_start_epoch, length_s, wait=6.0):
    """Wartet kurz auf das Plugin, wandelt die rohen Spuren in FLAC (auf die Aufnahme ausgerichtet)."""
    import json
    if not folder:
        return 0
    folder = Path(folder)
    meta_p = folder / "spuren.json"
    t_end = time.time() + wait
    last = None
    while time.time() < t_end:                     # bis das Plugin fertig geschrieben hat
        if meta_p.exists():
            cur = (meta_p.stat().st_mtime, sum(p.stat().st_size for p in folder.glob("*.pcm")))
            if cur == last:
                break
            last = cur
        time.sleep(1.5)
    if not meta_p.exists():
        return 0
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    sr = int(meta.get("sample_rate") or 48000)
    offset = meta.get("start_epoch_ms", rec_start_epoch * 1000) / 1000 - rec_start_epoch
    out = []
    for sp in meta.get("spuren", []):
        pcm = folder / sp.get("file", "")
        if not pcm.exists() or pcm.stat().st_size < sr:       # unter ~0,5 s Ton: weglassen
            continue
        flac = folder / f"{_safe(sp.get('anzeige') or sp.get('name'))}__{sp['id']}.flac"
        af = (f"adelay={int(offset * 1000)}:all=1," if offset > 0 else f"atrim=start={-offset:.3f},asetpts=PTS-STARTPTS,") \
            + f"apad,atrim=0:{length_s:.3f}"
        r = subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-f", "s16le", "-ar", str(sr),
                            "-ac", "1", "-i", str(pcm), "-af", af, "-c:a", "flac", str(flac)],
                           capture_output=True, creationflags=_NOWIN)
        if r.returncode == 0:
            pcm.unlink(missing_ok=True)
            out.append({**sp, "file": flac.name})
        else:
            log.warning("Spur %s nicht umgewandelt: %s", sp.get("anzeige"), r.stderr.decode("utf-8", "replace")[-300:])
    meta["spuren"] = out
    meta["ausgerichtet_auf_aufnahme"] = True
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(out)


def user_tracks(rec_path):
    """Einzelspuren zu einer Aufnahme: [{id, name, anzeige, file(Path)}] oder []."""
    import json
    folder = Path(rec_path).with_name(Path(rec_path).stem + "_spuren")
    try:
        meta = json.loads((folder / "spuren.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    if not meta.get("ausgerichtet_auf_aufnahme"):
        return []
    return [{**sp, "file": folder / sp["file"]} for sp in meta.get("spuren", []) if (folder / sp["file"]).exists()]


REC = Recorder()


def start_bridge():
    """Kleiner lokaler Dienst (nur 127.0.0.1): das Vencord-Plugin fragt hier, ob und wohin es aufnehmen soll."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] != "/status":
                self.send_error(404)
                return
            REC.plugin_seen = time.time()
            on = REC.running and not REC.stopping and REC.spuren_dir is not None
            body = json.dumps({"app": "Ascandir SessionRecorder", "recording": on,
                               "dir": str(REC.spuren_dir) if on else None,
                               "session": getattr(REC, "session_name", "") if on else ""}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), H)
    except OSError as e:
        log.warning("Plugin-Brücke nicht gestartet (Port %s belegt?): %s", BRIDGE_PORT, e)
        return None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("Plugin-Brücke läuft auf 127.0.0.1:%s", BRIDGE_PORT)
    return srv


def plugin_connected():
    return time.time() - REC.plugin_seen < 5
