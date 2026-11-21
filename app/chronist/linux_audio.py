"""Linux-Ton über PipeWire: Mikrofone auflisten und nur den Ton von Discord/Vesktop mitschneiden.

Gegenstück zu proc-tap unter Windows. Braucht die üblichen PipeWire-Werkzeuge (pw-dump, pw-record, pactl),
die auf CachyOS/Arch mit pipewire und pipewire-pulse dabei sind.
"""
import json
import queue
import shutil
import subprocess
import threading

from .debuglog import log

APP_HINTS = ("discord", "vesktop", "webcord", "legcord")


def available():
    return bool(shutil.which("pw-record") and shutil.which("pw-dump"))


def _run_json(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
        return json.loads(out or "[]")
    except Exception as e:
        log.info("%s nicht lesbar: %s", cmd[0], e)
        return []


def list_sources():
    """[(Anzeigename, 'pulse:<name>')] aller Mikrofone, Standardgerät zuerst (ohne Monitor-Quellen)."""
    if not shutil.which("pactl"):
        return []
    try:
        default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True,
                                 timeout=3).stdout.strip()
    except Exception:
        default = ""
    out = []
    for s in _run_json(["pactl", "-f", "json", "list", "sources"]):
        name = s.get("name", "")
        if not name or name.endswith(".monitor") or (s.get("monitor_of_sink") not in (None, "", "n/a")):
            continue
        desc = s.get("description") or name
        out.append((("⭐ " if name == default else "") + desc, "pulse:" + name))
    out.sort(key=lambda x: not x[0].startswith("⭐"))
    return out


def find_app_stream():
    """object.serial des Ausgabe-Stroms von Discord/Vesktop – oder None (z. B. solange niemand spricht)."""
    best = None
    for o in _run_json(["pw-dump"]):
        if o.get("type") != "PipeWire:Interface:Node":
            continue
        p = (o.get("info") or {}).get("props") or {}
        if p.get("media.class") != "Stream/Output/Audio":
            continue
        hay = " ".join(str(p.get(k, "")) for k in ("application.name", "application.process.binary",
                                                    "node.name", "application.id", "media.name")).lower()
        if any(h in hay for h in APP_HINTS):
            serial = p.get("object.serial") or o.get("id")
            # Sprach-Strom bevorzugen (WebRTC/VoiceEngine) vor Benachrichtigungstönen
            score = 2 if any(k in hay for k in ("webrtc", "voice", "playback")) else 1
            if best is None or score > best[0]:
                best = (score, serial, p.get("application.name") or p.get("node.name"))
    if best:
        return best[1], best[2]
    return None


class PwCapture:
    """Gleiche Schnittstelle wie proc-tap: start(), read(timeout), stop(), close(). Liefert float32-Stereo."""

    def __init__(self, target):
        self.target = str(target)
        self.proc = None
        self.q = queue.Queue(maxsize=400)

    def start(self):
        self.proc = subprocess.Popen(
            ["pw-record", "--target", self.target, "--rate", "48000", "--channels", "2", "--format", "f32",
             "-P", "{ node.name = ascandir-discord-mitschnitt node.dont-reconnect = true }", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while self.proc and self.proc.poll() is None:
            data = self.proc.stdout.read(9600)       # 50 ms
            if not data:
                break
            try:
                self.q.put(data, timeout=1)
            except queue.Full:
                pass
        self.q.put(None)

    def read(self, timeout=0.2):
        try:
            d = self.q.get(timeout=timeout)
        except queue.Empty:
            return b""
        if d is None:
            raise RuntimeError("Mitschnitt beendet (Discord-Strom weg)")
        return d

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(2)
            except Exception:
                self.proc.kill()

    def close(self):
        self.stop()
