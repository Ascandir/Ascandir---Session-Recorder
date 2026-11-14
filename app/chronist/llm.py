"""Lokale KI für die Zusammenfassung: Strata starten/beenden oder eine eingebaute KI installieren.

Ablauf in der Auswertung:
    with KI(progress) as url:
        ... Zusammenfassung über url ...
Beim Verlassen wird die KI wieder beendet – aber nur, wenn das Tool sie selbst gestartet hat.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from . import config as C

WIN = sys.platform == "win32"
NOWIN = 0x08000000 if WIN else 0

BUILTIN_PORT = 8091
BUILTIN_URL = f"http://127.0.0.1:{BUILTIN_PORT}/v1"
BUILTIN_DIR = C.MODELS / "lokale-ki"
MODEL_NAME = "Qwen3-14B-Q4_K_M.gguf"
MODEL_URL = f"https://huggingface.co/Qwen/Qwen3-14B-GGUF/resolve/main/{MODEL_NAME}"
LLAMA_RELEASE_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest"

MODI = ["Automatisch", "Strata", "Eingebaute KI", "Eigene Adresse"]
LOG = C.DATA / "ki-log.txt"


# ---------- Hilfen ----------
def api_up(url, timeout=3):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models", headers={"Authorization": "Bearer lokal"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _port(url):
    try:
        return int(url.split("://", 1)[1].split("/", 1)[0].rsplit(":", 1)[1])
    except Exception:
        return 80


def _kill_tree(pid):
    import psutil
    try:
        p = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return
    procs = p.children(recursive=True) + [p]
    for q in procs:
        try:
            q.terminate()
        except Exception:
            pass
    _, alive = psutil.wait_procs(procs, timeout=8)
    for q in alive:
        try:
            q.kill()
        except Exception:
            pass


def _listener_pids(port):
    import psutil
    pids = set()
    try:
        for c in psutil.net_connections(kind="tcp"):
            if c.laddr and c.laddr.port == port and c.status == psutil.CONN_LISTEN and c.pid:
                pids.add(c.pid)
    except Exception:
        pass
    return pids


# ---------- Strata finden ----------
def find_strata():
    """Gibt (art, pfad) zurück: art = 'app' | 'cli' | 'bat' – oder None."""
    exe = shutil.which("strata")
    if exe:
        return ("cli", exe)
    la = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    home = str(Path.home())
    apps = [Path(la, "Programs", "Strata", "Strata.exe"), Path(la, "Programs", "strata", "Strata.exe"),
            Path(la, "Strata", "Strata.exe"), Path(pf, "Strata", "Strata.exe"),
            Path(pf86, "Strata", "Strata.exe") if pf86 else None]
    for a in apps:
        if a and a.exists():
            return ("app", str(a))
    clis = [Path(la, "Programs", "Strata", "strata.exe"), Path(home, ".strata", "bin", "strata.exe"),
            Path(la, "strata", "bin", "strata.exe"), Path(home, ".local", "bin", "strata.exe")]
    for c in clis:
        if c.exists():
            return ("cli", str(c))
    for base in [home, Path(home, "Desktop"), Path(home, "Downloads"), Path(home, "Documents"), "C:\\", "D:\\"]:
        for name in ["Strata", "Strata-main", "strata"]:
            b = Path(base, name, "START-HERE.bat")
            if b.exists():
                return ("bat", str(b))
    return None


def strata_description():
    st = C.settings()
    if st.get("strata_befehl", "").strip():
        return f"Eigener Startbefehl: `{st['strata_befehl']}`"
    f = find_strata()
    if not f:
        return "⚠️ Strata wurde nicht gefunden. Trag unten den Startbefehl oder den Pfad zu Strata ein."
    art = {"app": "Strata-App", "cli": "Strata-Befehl (strata launch)", "bat": "Strata-Ordner (START-HERE.bat)"}[f[0]]
    return f"✅ Gefunden: {art} – `{f[1]}`"


# ---------- Eingebaute KI ----------
def builtin_server():
    for p in BUILTIN_DIR.glob("**/llama-server.exe" if WIN else "**/llama-server"):
        return p
    return None


def builtin_installed():
    return builtin_server() is not None and (BUILTIN_DIR / MODEL_NAME).exists()


def _download(url, dest, progress, label):
    tmp = Path(str(dest) + ".part")
    start = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={"User-Agent": "Ascandir-SessionRecorder",
                                               **({"Range": f"bytes={start}-"} if start else {})})
    with urllib.request.urlopen(req, timeout=60) as r:
        if start and r.status != 206:
            start = 0
        total = int(r.headers.get("Content-Length") or 0) + start
        done = start
        with open(tmp, "ab" if start else "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(done / total, f"{label} … {done / 1e9:.1f} von {total / 1e9:.1f} GB")
    tmp.replace(dest)


def install_builtin(progress=lambda f, m: None):
    """Lädt llama.cpp (Vulkan, läuft auf AMD/NVIDIA/Intel) und ein deutschsprachiges Modell."""
    BUILTIN_DIR.mkdir(parents=True, exist_ok=True)
    if builtin_server() is None:
        progress(0.0, "Suche die neueste KI-Engine …")
        req = urllib.request.Request(LLAMA_RELEASE_API, headers={"User-Agent": "Ascandir-SessionRecorder"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rel = json.load(r)
        want = "bin-win-vulkan-x64.zip" if WIN else "bin-ubuntu-vulkan-x64.zip"
        asset = next((a for a in rel["assets"] if a["name"].endswith(want)), None)
        if not asset:
            raise RuntimeError("Keine passende KI-Engine im llama.cpp-Release gefunden.")
        z = BUILTIN_DIR / "engine.zip"
        _download(asset["browser_download_url"], z, lambda f, m: progress(f * 0.1, m), "KI-Engine wird geladen")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(BUILTIN_DIR / "engine")
        z.unlink()
    model = BUILTIN_DIR / MODEL_NAME
    if not model.exists():
        _download(MODEL_URL, model, lambda f, m: progress(0.1 + f * 0.9, m), "KI-Modell wird geladen")
    progress(1.0, "Eingebaute KI ist installiert.")


def resolved_mode():
    """'Automatisch' → Strata, wenn vorhanden (oder schon laufend), sonst die eingebaute KI."""
    st = C.settings()
    m = st.get("ki_modus") or "Automatisch"
    if m != "Automatisch":
        return m
    if st.get("strata_befehl", "").strip() or find_strata() or api_up(st["llm_url"], timeout=1):
        return "Strata"
    return "Eingebaute KI"


# ---------- Start / Stopp ----------
class KI:
    """Kontextmanager: stellt eine laufende KI bereit und räumt danach auf."""

    def __init__(self, progress=lambda f, m: None):
        self.progress = progress
        self.proc = None
        self.started = False
        self.url = None
        self.mode = resolved_mode()

    # -- Startbefehle
    def _strata_cmd(self):
        st = C.settings()
        custom = st.get("strata_befehl", "").strip()
        if custom:
            p = custom.strip('"')
            if p.lower().endswith(".exe") and Path(p).exists():
                return [p], "exe"
            if p.lower().endswith((".bat", ".cmd")) and Path(p).exists():
                return ["cmd", "/c", p], "cli"
            return custom, "shell"
        f = find_strata()
        if not f:
            raise RuntimeError("Strata wurde nicht gefunden. Bitte unter ⚙️ Einstellungen → Lokale KI den "
                               "Startbefehl eintragen (z. B. den Pfad zu Strata.exe).")
        art, path = f
        if art == "app":
            return [path], "exe"
        if art == "cli":
            return [path, "launch"], "cli"
        return ["cmd", "/c", path], "cli"

    def _start(self, cmd, kind, cwd=None):
        log = open(LOG, "a", encoding="utf-8", errors="replace")
        log.write(f"\n=== {time.ctime()} Start: {cmd}\n")
        log.flush()
        kw = dict(stdout=log, stderr=log, cwd=cwd)
        if kind in ("cli", "shell", "server"):
            kw["creationflags"] = NOWIN
            kw["stdin"] = subprocess.PIPE
        self.proc = subprocess.Popen(cmd, shell=(kind == "shell"), **kw)
        self.started = True
        if kw.get("stdin"):
            try:  # Rückfragen beim Start mit der Empfehlung (Enter) beantworten
                self.proc.stdin.write(b"\r\n" * 40)
                self.proc.stdin.flush()
            except Exception:
                pass

    def _wait(self, url, timeout, what):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if api_up(url):
                return
            if self.proc is not None and self.proc.poll() is not None and self.proc.returncode not in (0, None) \
                    and time.time() - t0 > 10 and not api_up(url):
                raise RuntimeError(f"{what} hat sich beendet, bevor sie bereit war. Details: {LOG}")
            sec = int(time.time() - t0)
            self.progress(min(0.95, sec / timeout), f"{what} startet … ({sec // 60}:{sec % 60:02d}) – "
                                                    "beim ersten Laden kann das ein paar Minuten dauern")
            time.sleep(3)
        raise RuntimeError(f"{what} hat nach {timeout // 60} Minuten nicht geantwortet. Details: {LOG}")

    def __enter__(self):
        st = C.settings()
        if self.mode == "Eigene Adresse":
            self.url = st["llm_url"]
            if not api_up(self.url):
                raise RuntimeError(f"Unter {self.url} antwortet keine KI.")
            return self.url
        if self.mode == "Eingebaute KI":
            self.url = BUILTIN_URL
            if api_up(self.url):
                return self.url
            if not builtin_installed():
                install_builtin(self.progress)
            srv = builtin_server()
            self._start([str(srv), "-m", str(BUILTIN_DIR / MODEL_NAME), "--host", "127.0.0.1",
                         "--port", str(BUILTIN_PORT), "-ngl", "99", "-c", "32768", "--jinja"],
                        "server", cwd=str(srv.parent))
            self._wait(self.url, 600, "Die eingebaute KI")
            return self.url
        # Strata
        self.url = st["llm_url"]
        if api_up(self.url):
            return self.url          # läuft schon – nicht anfassen
        cmd, kind = self._strata_cmd()
        self.progress(0.01, "Strata wird gestartet …")
        self._start(cmd, kind)
        self._wait(self.url, int(st.get("strata_wartezeit", 900)), "Strata")
        return self.url

    def stop(self):
        if not self.started:
            return
        st = C.settings()
        if self.mode == "Strata" and not st.get("strata_beenden", True):
            return
        self.progress(0.99, "KI wird beendet …")
        if self.proc is not None:
            _kill_tree(self.proc.pid)
        # Hat die App die Engine abgekoppelt gestartet? Dann den Prozess am Port beenden.
        if self.url and WIN:
            time.sleep(2)
            if api_up(self.url, timeout=2):
                for pid in _listener_pids(_port(self.url)):
                    _kill_tree(pid)
        self.started = False

    def __exit__(self, *exc):
        try:
            self.stop()
        except Exception:
            pass
        return False


def test(progress=lambda f, m: None):
    """Startet die KI, stellt eine Frage, beendet sie wieder."""
    from . import summary as S
    with KI(progress) as url:
        answer = S.ask(url, "Antworte nur mit: Bereit.", 20)
    return answer
