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
from .debuglog import log

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
KEYS = {}          # url -> API-Schlüssel (von Strata)


def api_up(url, timeout=3):
    try:
        req = urllib.request.Request(url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {KEYS.get(url, 'lokal')}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)      # läuft, will nur einen Schlüssel
    except Exception:
        return False


# ---------- Strata-Befehle (offizielle Terminal-Befehle) ----------
def strata_cli():
    """Pfad zum strata-Befehl oder None."""
    custom = C.settings().get("strata_befehl", "").strip().strip('"')
    if custom and Path(custom).name.lower().startswith("strata") and Path(custom).suffix.lower() in (".cmd", ".exe", ".bat", "") \
            and Path(custom).exists() and "launch" not in custom:
        return custom
    f = find_strata()
    if f and f[0] == "cli":
        return f[1]
    return shutil.which("strata")


def self_is_strata(exe):
    return "strata" in str(exe).lower()


def run_strata(args, timeout=60):
    exe = strata_cli()
    if not exe:
        return 127, "strata-Befehl nicht gefunden"
    try:
        r = subprocess.run([exe] + args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=NOWIN, stdin=subprocess.DEVNULL,
                           env=C.ORIG_ENV if self_is_strata(exe) else None)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "Zeitüberschreitung"
    except Exception as e:
        return 1, str(e)


def list_presets():
    """Gespeicherte Strata-Presets (Namen) + Rohausgabe."""
    import re
    rc, out = run_strata(["launch", "--list-presets"], timeout=30)
    names = []
    for line in out.splitlines():
        t = line.strip()
        if not t or rc not in (0,):
            continue
        low = t.lower()
        if low.startswith(("saved preset", "presets", "no preset", "keine", "name ", "usage", "strata:")) or set(t) <= set("-=_ "):
            continue
        t = re.sub(r"^[\-\*\u2022\d\.\)\s]+", "", t)          # Aufzählungszeichen
        m = re.match(r'^"([^"]+)"', t) or re.match(r"^'([^']+)'", t)
        name = m.group(1) if m else re.split(r"\s{2,}|\t| – | - |: | \(", t)[0].strip()
        if name and name not in names and len(name) < 80:
            names.append(name)
    return names, out.strip()


def strata_preset():
    st = C.settings()
    if st.get("strata_preset"):
        return st["strata_preset"]
    names, _ = list_presets()
    return names[0] if names else ""


def _redact(text, key):
    return text.replace(key, key[:4] + "…") if key and len(key) > 6 else text


def strata_key(preset):
    import re
    args = ["launch", "--show-key"] + (["--preset", preset] if preset else [])
    rc, out = run_strata(args, timeout=30)
    m = re.search(r"(?i)key[^:=\n]*[:=]\s*\"?([A-Za-z0-9_\-\.]{8,})", out)
    if m:
        return m.group(1)
    toks = re.findall(r"[A-Za-z0-9_\-]{20,}", out)
    return toks[-1] if toks else ""


def strata_status_urls():
    import re
    rc, out = run_strata(["launch", "--status"], timeout=20)
    urls = []
    for m in re.finditer(r"https?://[0-9a-zA-Z\.\-]+:\d+(?:/v1)?", out):
        u = m.group(0).replace("0.0.0.0", "127.0.0.1")
        u = u if u.endswith("/v1") else u + "/v1"
        if u not in urls:
            urls.append(u)
    return urls, out


def _log_tail(n=12):
    try:
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception:
        return ""


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
def _from_registry():
    """Installierte Programme (Windows-Programmliste) nach Strata durchsuchen."""
    if not WIN:
        return None
    import winreg
    roots = [(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")]
    for root, path in roots:
        try:
            key = winreg.OpenKey(root, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
            except OSError:
                continue
            if "strata" not in name.lower():
                continue
            cands = []
            for val in ("DisplayIcon", "InstallLocation"):
                try:
                    cands.append(str(winreg.QueryValueEx(sub, val)[0]).split(",")[0].strip('"'))
                except OSError:
                    pass
            for c in cands:
                pth = Path(c)
                if pth.is_file() and pth.suffix.lower() == ".exe" and "unins" not in pth.name.lower():
                    return ("app", str(pth))
                if pth.is_dir():
                    for exe in list(pth.glob("Strata*.exe")) + list(pth.glob("strata*.exe")):
                        if "unins" not in exe.name.lower():
                            return ("app", str(exe))
    return None


def _from_shortcuts():
    """Startmenü- und Desktop-Verknüpfungen namens 'Strata' auflösen."""
    if not WIN:
        return None
    dirs = [Path(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
            Path(os.environ.get("ProgramData", r"C:\ProgramData"), "Microsoft", "Windows", "Start Menu", "Programs"),
            Path.home() / "Desktop", Path.home() / "OneDrive" / "Desktop"]
    links = []
    for d in dirs:
        if d.exists():
            links += [p for p in d.rglob("*.lnk") if "strata" in p.stem.lower()]
    for lnk in links:
        try:
            ps = f"(New-Object -ComObject WScript.Shell).CreateShortcut('{str(lnk).replace(chr(39), chr(39)*2)}').TargetPath"
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                                 text=True, timeout=10, creationflags=NOWIN).stdout.strip()
            if out and Path(out).exists():
                kind = "bat" if out.lower().endswith((".bat", ".cmd")) else "app"
                return (kind, out)
        except Exception:
            pass
    return None


_found_cache = {}


def find_strata(force=False):
    """Gibt (art, pfad) zurück: art = 'app' | 'cli' | 'bat' – oder None."""
    if _found_cache and not force:
        return _found_cache.get("r")
    r = _find_strata()
    _found_cache["r"] = r
    return r


def _find_strata():
    for f in (_from_registry, _from_shortcuts):
        try:
            r = f()
            if r:
                return r
        except Exception:
            pass
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
ACTIVE = set()


def stop_all():
    """Beim Beenden des Programms: alle vom Tool gestarteten KIs stoppen."""
    for k in list(ACTIVE):
        try:
            k.stop()
        except Exception:
            pass


class KI:
    """Kontextmanager: stellt eine laufende KI bereit und räumt danach auf."""

    def __init__(self, progress=lambda f, m: None):
        self.progress = progress
        self.proc = None
        self.started = False
        self.url = None
        self.mode = resolved_mode()
        self.preset = ""

    # -- Startbefehle
    def _strata_cmd(self):
        st = C.settings()
        custom = st.get("strata_befehl", "").strip()
        if custom and strata_cli() == custom.strip('"'):
            custom = ""          # eigener Pfad zum strata-Befehl → wie gefundener Befehl behandeln
        if custom:
            p = custom.strip('"')
            if p.lower().endswith(".exe") and Path(p).exists():
                return [p], "exe"
            if p.lower().endswith((".bat", ".cmd")) and Path(p).exists():
                return ["cmd", "/c", p], "cli"
            return custom, "shell"
        cli = strata_cli()
        f = ("cli", cli) if cli else find_strata()
        if not f:
            raise RuntimeError("Strata wurde nicht gefunden. Bitte unter ⚙️ Einstellungen → Lokale KI den "
                               "Startbefehl eintragen (z. B. den Pfad zu Strata.exe).")
        art, path = f
        if art == "app":
            return [path], "exe"
        if art == "cli":
            preset = strata_preset()
            if not preset:
                raise RuntimeError("In Strata ist noch kein Preset gespeichert. Starte Strata einmal normal, "
                                   "wähl dein Modell und nimm „Save as preset“ – oder wähl unter ⚙️ Einstellungen → "
                                   "Lokale KI → Strata-Einstellungen ein Preset aus.")
            self.preset = preset
            return [path, "launch", "--preset", preset, "--no-tui", "--yes"], "cli"
        return ["cmd", "/c", path], "cli"

    def _start(self, cmd, kind, cwd=None):
        log.info("KI-Start (%s): %s", kind, cmd)
        logf = open(LOG, "a", encoding="utf-8", errors="replace")
        logf.write(f"\n=== {time.ctime()} Start: {cmd}\n")
        logf.flush()
        kw = dict(stdout=logf, stderr=logf, cwd=cwd)
        if self.mode == "Strata":
            kw["env"] = C.ORIG_ENV           # Strata nutzt seine eigenen Ordner
        if kind in ("cli", "shell", "server"):
            kw["creationflags"] = NOWIN
            kw["stdin"] = subprocess.PIPE
        self.proc = subprocess.Popen(cmd, shell=(kind == "shell"), **kw)
        log.info("KI-Prozess gestartet, PID %s", self.proc.pid)
        self.started = True
        ACTIVE.add(self)
        if kw.get("stdin"):
            try:  # Rückfragen beim Start mit der Empfehlung (Enter) beantworten
                self.proc.stdin.write(b"\r\n" * 40)
                self.proc.stdin.flush()
            except Exception:
                pass

    def _wait(self, url, timeout, what):
        t0 = time.time()
        log.info("Warte auf %s unter %s (max. %s s)", what, url, timeout)
        last_log = 0
        while time.time() - t0 < timeout:
            if api_up(url):
                log.info("%s antwortet nach %.0f s", what, time.time() - t0)
                return
            if time.time() - last_log > 30:
                last_log = time.time()
                rc = self.proc.poll() if self.proc else None
                log.info("… noch keine Antwort nach %.0f s (Prozess %s)", time.time() - t0,
                         "läuft" if rc is None else f"beendet mit Code {rc}")
            if self.proc is not None and self.proc.poll() is not None and self.proc.returncode not in (0, None) \
                    and time.time() - t0 > 5 and not api_up(url):
                raise RuntimeError(f"{what} hat sich beendet, bevor sie bereit war.\n\nLetzte Meldungen:\n{_log_tail()}")
            if self.mode == "Strata" and strata_cli() and int(time.time() - t0) % 15 < 3 and time.time() - t0 > 5:
                urls, _ = strata_status_urls()
                for u in urls:
                    if u != url and api_up(u):
                        log.info("Strata meldet sich unter %s (statt %s)", u, url)
                        self.url = u
                        return
            sec = int(time.time() - t0)
            self.progress(min(0.95, sec / timeout), f"{what} startet … ({sec // 60}:{sec % 60:02d}) – "
                                                    "beim ersten Laden kann das ein paar Minuten dauern")
            time.sleep(3)
        raise RuntimeError(f"{what} hat nach {timeout // 60} Minuten nicht unter {url} geantwortet.\n\n"
                           f"Letzte Meldungen:\n{_log_tail()}")

    def __enter__(self):
        st = C.settings()
        log.info("KI wird gebraucht – Modus: %s (eingestellt: %s)", self.mode, st.get("ki_modus"))
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
            log.info("Strata läuft schon unter %s – wird nicht angefasst", self.url)
            self._fetch_key()
            return self.url          # läuft schon – nicht anfassen
        log.info("Strata-Suche: %s", find_strata(force=True))
        cmd, kind = self._strata_cmd()
        self.progress(0.01, f"Strata wird gestartet (Preset „{self.preset}“) …" if self.preset else "Strata wird gestartet …")
        self._start(cmd, kind)
        self._wait(self.url, int(st.get("strata_wartezeit", 900)), "Strata")
        self._fetch_key()
        return self.url

    def _fetch_key(self):
        if strata_cli():
            key = strata_key(self.preset or strata_preset())
            if key:
                KEYS[self.url] = key
                log.info("Strata-Schlüssel geholt (%s…)", key[:4])

    def stop(self):
        if not self.started:
            return
        log.info("KI wird beendet (Modus %s)", self.mode)
        st = C.settings()
        if self.mode == "Strata" and not st.get("strata_beenden", True):
            return
        self.progress(0.99, "KI wird beendet …")
        if self.mode == "Strata" and strata_cli():
            args = ["launch", "--stop"] + (["--preset", self.preset] if self.preset else [])
            rc, out = run_strata(args, timeout=90)
            log.info("strata %s → Code %s: %s", " ".join(args), rc, out.strip()[:300])
        if self.proc is not None:
            _kill_tree(self.proc.pid)
        # Hat die App die Engine abgekoppelt gestartet? Dann den Prozess am Port beenden.
        if self.url and WIN:
            time.sleep(2)
            if api_up(self.url, timeout=2):
                for pid in _listener_pids(_port(self.url)):
                    _kill_tree(pid)
        self.started = False
        ACTIVE.discard(self)

    def __exit__(self, *exc):
        if exc and exc[0]:
            log.error("Fehler während die KI lief: %s", "".join(__import__("traceback").format_exception(*exc)))
        try:
            self.stop()
        except Exception:
            log.exception("Fehler beim Beenden der KI")
        return False


def test(progress=lambda f, m: None):
    """Startet die KI, stellt eine Frage, beendet sie wieder."""
    from . import summary as S
    with KI(progress) as url:
        answer = S.ask(url, "Antworte nur mit: Bereit.", 20)
    return answer
