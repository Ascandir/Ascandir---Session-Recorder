"""Comic-Seiten lokal malen: Comic-Vorlage → Panels (FLUX.2-klein über stable-diffusion.cpp, Vulkan) → Seiten mit Sprechblasen.

Läuft komplett auf dem eigenen PC (Grafikkarte über Vulkan – AMD, NVIDIA, Intel).
Charakterbilder aus der Gruppe werden als Referenzbilder mitgegeben, damit die Figuren wiedererkennbar sind.
"""
import json
import os
import re
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
DIR = C.MODELS / "bilder-ki"
ENGINE_DIR = DIR / "engine"
SD_RELEASE_API = "https://api.github.com/repos/leejet/stable-diffusion.cpp/releases/latest"
UA = {"User-Agent": "Ascandir-SessionRecorder"}

# Bildmodelle: "schnell" (4B, Apache 2.0) und "qualitaet" (9B, FLUX Non-Commercial – privat ok)
MODELLE = {
    # Q5/Q4 statt Q8: passt mit Arbeitsspeicher fürs Rechnen in 16 GB Grafikspeicher (Q8 lief über)
    "qualitaet": {"name": "Hohe Qualität (FLUX.2-klein 9B)", "groesse": "ca. 12 GB", "rev": 2,
                  "diff": ["leejet/FLUX.2-klein-9B-GGUF", "unsloth/FLUX.2-klein-9B-GGUF"],
                  "diff_pref": ["Q5_K_M", "Q5_K_S", "Q5_0", "Q6_K", "Q4_K_M", "Q4_0"],
                  "llm": ["unsloth/Qwen3-8B-GGUF"], "llm_pref": ["Q4_K_M", "Q5_K_M", "Q4_K_S"],
                  "offload": True},
    "schnell": {"name": "Schnell (FLUX.2-klein 4B)", "groesse": "ca. 9 GB",
                "diff": ["unsloth/FLUX.2-klein-4B-GGUF", "leejet/FLUX.2-klein-4B-GGUF"],
                "diff_pref": ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M"],
                "llm": ["unsloth/Qwen3-4B-GGUF"], "llm_pref": ["Q8_0", "Q6_K", "Q4_K_M"],
                "offload": False},
}
MODELL_NAMEN = {v["name"]: k for k, v in MODELLE.items()}


def modell():
    st = C.settings()
    m = st.get("comic_modell") or "schnell"
    if m == "qualitaet" and st.get("comic_9b_haengt"):
        return "schnell"                 # auf diesem PC schon einmal hängengeblieben
    return m if m in MODELLE else "schnell"


FORMATE = {
    "manhwa": "Manhwa / Webtoon (zum Scrollen)",
    "manga": "Manga-Seiten",
    "klassisch": "Klassische Comic-Seiten",
}
FORMAT_NAMEN = {v: k for k, v in FORMATE.items()}
STILE = {
    "manhwa": ("full color korean manhwa webtoon art style, clean thin lineart, soft cel shading, cinematic "
               "lighting, expressive anime-style faces, detailed fantasy background"),
    "manga": ("black and white japanese manga art, clean ink lineart, screentone shading, high contrast, "
              "dramatic camera angles, expressive faces, detailed backgrounds"),
}


def comic_format():
    f = C.settings().get("comic_format") or "manhwa"
    return f if f in FORMATE else "manhwa"


def style_for(fmt):
    own = (C.settings().get("comic_stil") or "").strip()
    if own and own not in STILE.values() and own != STYLE_DEFAULT:
        return own
    return STILE.get(fmt, STYLE_DEFAULT)
VAE_URL = ("https://huggingface.co/Comfy-Org/vae-text-encorder-for-flux-klein-4b/resolve/main/"
           "split_files/vae/flux2-vae.safetensors")
VAE_FILE = DIR / "flux2-vae.safetensors"

STYLE_DEFAULT = ("colorful western comic book art, bold clean ink outlines, cel shading, dynamic composition, "
                 "fantasy adventure, high detail")


# ---------- Installation ----------
def _get_json(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _download(url, dest, progress, label, a=0.0, b=1.0):
    dest = Path(dest)
    tmp = Path(str(dest) + ".part")
    start = tmp.stat().st_size if tmp.exists() else 0
    req = urllib.request.Request(url, headers={**UA, **({"Range": f"bytes={start}-"} if start else {})})
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
                    progress(a + (b - a) * done / total, f"{label} … {done / 1e9:.1f} von {total / 1e9:.1f} GB")
    tmp.replace(dest)


def _hf_pick(repo, prefs, must=".gguf"):
    """Dateiname im HF-Repo nach Vorliebe (z. B. Q8_0) auswählen – robust gegen Umbenennungen."""
    files = [s["rfilename"] for s in _get_json(f"https://huggingface.co/api/models/{repo}").get("siblings", [])]
    files = [f for f in files if f.lower().endswith(must) and "mmproj" not in f.lower()]
    for p in prefs:
        for f in files:
            if p.lower() in f.lower():
                return f
    if not files:
        raise RuntimeError(f"Im Repo {repo} wurde keine passende Datei gefunden.")
    return files[0]


def _state():
    p = DIR / "modelle.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def sd_cli():
    name = "sd-cli.exe" if WIN else "sd-cli"
    for p in ENGINE_DIR.rglob(name):
        return p
    for p in ENGINE_DIR.rglob("sd.exe" if WIN else "sd"):    # ältere Versionen
        return p
    return None


def _mstate(st, m):
    if m == "schnell" and "schnell" not in st and st.get("diffusion"):      # alte Installation (2.3.0)
        return {"diffusion": st["diffusion"], "llm": st.get("llm")}
    return st.get(m) or {}


def installed(m=None):
    m = m or modell()
    ms = _mstate(_state(), m)
    if ms.get("rev", 1) != MODELLE[m].get("rev", 1):
        return False
    return bool(sd_cli() and VAE_FILE.exists() and ms.get("diffusion") and ms.get("llm")
                and (DIR / ms["diffusion"]).exists() and (DIR / ms["llm"]).exists())


def _fetch_model(repos, prefs, progress, label, a, b):
    last = None
    for repo in repos:
        try:
            f = _hf_pick(repo, prefs)
            _download(f"https://huggingface.co/{repo}/resolve/main/{f}", DIR / Path(f).name, progress, label, a, b)
            log.info("Modell geladen: %s/%s", repo, f)
            return Path(f).name
        except Exception as e:
            last = e
            log.info("Modell aus %s nicht ladbar: %s", repo, e)
    raise RuntimeError(f"{label} konnte nicht geladen werden: {last}")


def install(progress=lambda f, m: None, m=None):
    m = m or modell()
    spec = MODELLE[m]
    DIR.mkdir(parents=True, exist_ok=True)
    st = _state()
    if not sd_cli():
        progress(0.0, "Suche den neuesten Bildgenerator …")
        rel = _get_json(SD_RELEASE_API)
        want = (lambda n: "win" in n and "vulkan" in n and n.endswith(".zip")) if WIN else \
               (lambda n: "linux" in n.lower() and "vulkan" in n.lower() and n.endswith(".zip"))
        asset = next((a for a in rel.get("assets", []) if want(a["name"].lower())), None)
        if not asset:
            raise RuntimeError("Im stable-diffusion.cpp-Release gibt es kein Vulkan-Paket für Windows.")
        z = DIR / "engine.zip"
        _download(asset["browser_download_url"], z, progress, "Bildgenerator wird geladen", 0.0, 0.05)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(ENGINE_DIR)
        z.unlink()
        log.info("Bildgenerator installiert: %s", asset["name"])
    ms = dict(_mstate(st, m))
    if ms.get("rev", 1) != spec.get("rev", 1):          # neue, kleinere Modellvariante → alte Dateien weg
        for k in ("diffusion", "llm"):
            old = ms.pop(k, None)
            in_use = any(v.get(k) == old for kk, v in st.items() if kk != m and isinstance(v, dict))
            if old and not in_use:
                (DIR / old).unlink(missing_ok=True)
                log.info("Alte Modelldatei entfernt: %s", old)
        ms["rev"] = spec.get("rev", 1)
    if not ms.get("diffusion") or not (DIR / ms["diffusion"]).exists():
        ms["diffusion"] = _fetch_model(spec["diff"], spec["diff_pref"], progress,
                                       f"Bildmodell ({spec['name']}) wird geladen", 0.05, 0.55)
    if not ms.get("llm") or not (DIR / ms["llm"]).exists():
        ms["llm"] = _fetch_model(spec["llm"], spec["llm_pref"], progress,
                                 "Textverständnis (Qwen3) wird geladen", 0.55, 0.92)
    st[m] = ms
    st.pop("diffusion", None) if m == "schnell" else None
    st.pop("llm", None) if m == "schnell" else None
    if not VAE_FILE.exists():
        _download(VAE_URL, VAE_FILE, progress, "Bild-Decoder wird geladen", 0.9, 1.0)
    (DIR / "modelle.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
    progress(1.0, f"Bildgenerator ist bereit: {spec['name']}.")


# ---------- Comic-Vorlage lesen ----------
_KEYS = {"szene": "szene", "im bild": "im_bild", "sprechblasen": "blasen", "bild-prompt": "prompt",
         "bildprompt": "prompt", "prompt": "prompt"}


def parse_template(md):
    """Panels aus der Comic-Vorlage (Markdown) lesen – tolerant gegenüber Formatvarianten."""
    panels = []
    blocks = re.split(r"(?im)^\s*#{1,4}\s*panel\s*(\d+)[^\n]*$|^\s*\**panel\s*(\d+)\**\s*$", md)
    # re.split liefert [vorher, nr1, nr2, text, nr1, nr2, text, ...]
    i = 1
    while i < len(blocks):
        nr = blocks[i] or blocks[i + 1]
        text = blocks[i + 2] if i + 2 < len(blocks) else ""
        i += 3
        p = {"nr": int(nr), "szene": "", "im_bild": "", "blasen": [], "prompt": ""}
        cur = None
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            clean = re.sub(r"^[\-\*\u2022]+\s*", "", line).replace("**", "").strip()
            m = re.match(r"(?i)(szene|im bild|sprechblasen|bild-?prompt[^:]*|prompt)\s*:\s*(.*)", clean)
            if m:
                key = m.group(1).lower()
                key = "bild-prompt" if key.startswith("bild") else key
                cur = _KEYS.get(key, None)
                val = m.group(2).strip()
                if cur == "blasen":
                    if val:
                        p["blasen"].append(_bubble(val))
                elif cur:
                    p[cur] = val
                continue
            if cur == "blasen":
                b = _bubble(clean)
                if b:
                    p["blasen"].append(b)
            elif cur and cur in ("szene", "im_bild", "prompt"):
                p[cur] = (p[cur] + " " + clean).strip()
        p["blasen"] = [b for b in p["blasen"] if b][:3]
        panels.append(p)
    return panels


def _bubble(text):
    m = re.match(r"^([^:„\"]{1,40}):\s*(.+)$", text)
    if not m:
        return ("", text.strip(" „“\"")) if text.strip() else None
    who, said = m.group(1).strip(), m.group(2).strip()
    said = said.strip().strip("„“\"”").strip()
    return (who, said) if said else None


# ---------- Referenzbilder ----------
GROUP_WORDS = r"(?<!\w)(gruppe|party|heroes|helden|abenteurer|adventurers|spielercharaktere|player characters|" \
              r"alle|everyone|the group|companions|gefährten)(?!\w)"


def _people(grp):
    """Alle Charaktere mit Namen, Bildern und Aussehen (DM-Hauptcharaktere ausgenommen).

    Ein Charakter wird erkannt über seinen Namen, Namensteile (z. B. „Krätz“ aus „Krätz und Knabba“)
    und – bei Hauptcharakteren – über den Namen des Spielers, dem er gehört.
    """
    out = []
    me = grp.get("ich_name", "Ich")
    for c in C.characters(grp):
        owner = c.get("besitzer", "")
        main = c.get("art") == "Hauptcharakter"
        if owner and main and C.is_dm(C.ICH_ID if owner == me else owner, grp):
            continue
        imgs = [str(x) for x in C.char_images(c, grp) if Path(x).exists()]
        namen = [c["name"]] + ([owner] if main and owner else [])
        out.append({"namen": namen, "anzeige": c["name"], "bilder": imgs, "bild": imgs[0] if imgs else None,
                    "aussehen": c.get("aussehen", ""), "haupt": main, "besitzer": owner})
    return out


def _mentions(text, names):
    low = text.lower()
    for n in names:
        for part in [n] + [w for w in re.split(r"\s+und\s+|\s+and\s+|,|/|&", n) if len(w.strip()) > 2]:
            part = part.strip()
            if len(part) > 2 and re.search(r"(?<!\w)" + re.escape(part.lower()) + r"(?!\w)", low):
                return True
    return False


def refs_for(panel, grp, max_refs=4):
    """Welche Charaktere sind im Panel? Reihenfolge: Im-Bild-Zeile zuerst, dann Sprecher, dann Rest."""
    people = _people(grp)
    im_bild = panel.get("im_bild", "")
    rest = " ".join([panel.get("szene", ""), panel.get("prompt", "")])
    speakers = " ".join(w for w, _ in panel.get("blasen", []))
    hits = []
    for text in (im_bild, speakers, rest):
        for pp in people:
            if pp not in hits and _mentions(text, pp["namen"]):
                hits.append(pp)
    # „die Gruppe“, „adventurers“ … → alle Hauptcharaktere
    if re.search(GROUP_WORDS, (im_bild + " " + rest).lower()):
        for pp in people:
            if pp["haupt"] and pp not in hits:
                hits.append(pp)
    # Gefährten nur, wenn ihr Besitzer auch im Bild ist oder sie genannt wurden (schon oben)
    return hits[:max_refs]


def assign_refs(refs, max_images=4):
    """Referenzbilder verteilen: erst ein Bild pro Charakter, dann zweite Ansichten, solange Platz ist.
    Liefert [(charakter, [bildpfade])] – die Reihenfolge entspricht „image 1, image 2 …“ im Prompt."""
    with_img = [r for r in refs if r.get("bilder")]
    plan = [[r, []] for r in with_img]
    n = 0
    for round_ in range(2):
        for entry in plan:
            imgs = entry[0]["bilder"]
            if round_ < len(imgs) and n < max_images:
                entry[1].append(imgs[round_])
                n += 1
    return [(r, imgs) for r, imgs in plan if imgs]


def _ref_file(path, cache_dir):
    """Turnaround-Bögen (mehrere Ansichten nebeneinander) in Einzelansichten zerlegen und die
    erste (meist Vorderansicht) nehmen – sonst wird jede Figur beim Verkleinern winzig."""
    try:
        from PIL import Image, ImageChops
        im = Image.open(path).convert("RGB")
        w, h = im.size
        if w < h * 1.5:
            return path
        bg = Image.new("RGB", im.size, im.getpixel((2, 2)))
        diff = ImageChops.difference(im, bg).convert("L").point(lambda v: 255 if v > 28 else 0)
        cols = [sum(1 for y in range(0, h, 4) if diff.getpixel((x, y))) for x in range(w)]
        thr = max(1, h // 4 // 100)
        segs, start = [], None
        for x, c in enumerate(cols + [0]):
            if c > thr and start is None:
                start = x
            elif c <= thr and start is not None:
                if x - start > w * 0.06:
                    segs.append((start, x))
                start = None
        if len(segs) < 2:
            return path
        x0, x1 = segs[0]
        pad = int((x1 - x0) * 0.08)
        crop = im.crop((max(0, x0 - pad), 0, min(w, x1 + pad), h))
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / (Path(path).stem + "_ansicht1.png")
        crop.save(out)
        log.info("Turnaround erkannt (%s Ansichten): %s → erste Ansicht", len(segs), Path(path).name)
        return str(out)
    except Exception as e:
        log.info("Referenzbild unverändert (%s): %s", Path(path).name, e)
        return path


def _short(text, n=320):
    t = re.sub(r"[#*_>`]+", "", text or "")
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n].rsplit(" ", 1)[0] + "…" if len(t) > n else t


def build_prompt(panel, refs, style, plan=None):
    """Prompt in der Form, die FLUX.2 für Mehrfach-Referenzen erwartet: Figuren zuerst, mit „image N“."""
    plan = assign_refs(refs) if plan is None else plan
    base = panel.get("prompt") or f"{panel.get('szene', '')}. {panel.get('im_bild', '')}"
    base = re.sub(r"(?i)^comic book panel,?\s*(fantasy,?)?\s*", "", base).strip()
    cast, idx = [], 1
    for r, imgs in plan:
        nums = " and ".join(f"image {idx + k}" for k in range(len(imgs)))
        idx += len(imgs)
        d = (f"{r['anzeige']} is the character shown in {nums} – keep exactly the same face, hair, body, "
             f"outfit, colors and species")
        if r["aussehen"]:
            d += f" ({_short(r['aussehen'], 220).rstrip('.')})"
        cast.append(d)
    for r in refs:
        if not any(r is x for x, _ in plan) and r["aussehen"]:
            cast.append(f"{r['anzeige']}: {_short(r['aussehen']).rstrip('.')}")
    parts = [f"A single comic panel in this style: {style}."]
    if panel.get("shot"):
        parts.append(panel["shot"] + ".")
    if cast:
        parts.append("Characters in this panel: " + "; ".join(cast) + ".")
    if panel.get("im_bild"):
        parts.append("What happens: " + _short(panel["im_bild"], 300).rstrip(".") + ".")
    parts.append("Scene: " + base.rstrip(".") + ".")
    parts.append("No text, no letters, no speech bubbles, no character sheet, no multiple views.")
    return " ".join(parts)


# ---------- Malen ----------
def gen_size(box_w, box_h, mp=1.05):
    """Malgröße passend zum Seitenformat des Panels (~1 Megapixel, Vielfache von 64)."""
    a = max(0.4, min(2.5, box_w / box_h))
    w = (mp * 1e6 * a) ** 0.5
    h = w / a
    r = lambda v: max(512, min(1600, int(round(v / 64)) * 64))
    return r(w), r(h)


MEM_HINTS = ("memory", "capacity", "out of vram", "allocate", "OutOfDeviceMemory")


_HELP = {}


def _supports(exe, flag):
    """Kennt diese sd-cli-Version den Schalter? (ältere Versionen brechen bei unbekannten ab)"""
    if exe not in _HELP:
        try:
            r = subprocess.run([str(exe), "--help"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=NOWIN, cwd=str(exe.parent), env=C.ORIG_ENV, timeout=60)
            _HELP[exe] = r.stdout + r.stderr
        except Exception:
            _HELP[exe] = ""
    return flag in _HELP[exe]


CURRENT = {"proc": None, "abbruch": False}
HANG_SECONDS = 240          # so lange ohne neue Ausgabe = hängt


class Hang(RuntimeError):
    """Der Bildgenerator macht keinen Fortschritt mehr (z. B. Treiberproblem mit dem großen Modell)."""


class _Res:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def _run_sd(cmd, exe, logfile=None):
    """sd-cli starten; Ausgabe läuft live ins Comic-Protokoll; per Abbrechen beendbar."""
    if CURRENT["abbruch"]:
        raise RuntimeError("Abgebrochen.")
    logfile = Path(logfile or (C.TEMP / "bildgenerator.log"))
    start = logfile.stat().st_size if logfile.exists() else 0
    with open(logfile, "a", encoding="utf-8") as f:
        f.write("\n===== " + time.strftime("%H:%M:%S ") +
                " ".join(x if " " not in x else f'"{x[:60]}…"' for x in cmd[1:]) + "\n")
        f.flush()
        p = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT, creationflags=NOWIN,
                             cwd=str(exe.parent), env=C.ORIG_ENV)
        CURRENT["proc"] = p
        # Wächter: tut sich im Protokoll minutenlang nichts, hängt der Bildgenerator → beenden
        last_size, last_change, hung = -1, time.time(), False
        while p.poll() is None:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            size = logfile.stat().st_size
            if size != last_size:
                last_size, last_change = size, time.time()
            elif time.time() - last_change > HANG_SECONDS:
                hung = True
                log.warning("Bildgenerator hängt seit %s s ohne Fortschritt – wird beendet", HANG_SECONDS)
                p.kill()
                p.wait()
        CURRENT["proc"] = None
    with open(logfile, "rb") as f:
        f.seek(start)
        out = f.read().decode("utf-8", "replace")
    if CURRENT["abbruch"]:
        raise RuntimeError("Abgebrochen.")
    if hung:
        raise Hang(out)
    return _Res(p.returncode, out)


def kill_stale():
    """Übrig gebliebene Bildgenerator-Prozesse (z. B. nach Neustart/Update) beenden – die blockieren
    sonst Prozessor und Grafikspeicher."""
    n = 0
    try:
        import psutil
        eng = str(ENGINE_DIR.resolve()).lower()
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if (p.info["name"] or "").lower() in ("sd-cli.exe", "sd-cli", "sd.exe") and \
                        str(p.info["exe"] or "").lower().startswith(eng):
                    p.kill()
                    n += 1
            except Exception:
                pass
    except Exception:
        pass
    if n:
        log.info("%s alte(r) Bildgenerator-Prozess(e) beendet", n)
    return n


def kill():
    """Abbrechen-Knopf: laufendes Malen sofort beenden."""
    CURRENT["abbruch"] = True
    p = CURRENT["proc"]
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass


def generate_panel(prompt, refs, out_png, seed, w=1024, h=768, plan=None, m=None):
    m = m or modell()
    ms = _mstate(_state(), m)
    exe = sd_cli()
    plan = assign_refs(refs) if plan is None else plan
    ref_args = []
    for r, imgs in plan:
        for b in imgs:
            ref_args += ["-r", _ref_file(b, Path(out_png).parent / "_referenzen")]
    log.info("Panel malen: %s – Figuren: %s – %s Referenzbild(er)", Path(out_png).name,
             ", ".join(r["anzeige"] for r in refs) or "keine erkannt", len(ref_args) // 2)

    def build(gw, gh, refs_args, spar=False):
        cmd = [str(exe), "--diffusion-model", str(DIR / ms["diffusion"]), "--vae", str(VAE_FILE),
               "--llm", str(DIR / ms["llm"]), "-p", prompt, "--cfg-scale", "1.0", "--steps", "4",
               "-W", str(gw), "-H", str(gh), "-s", str(seed), "-o", str(out_png),
               "--diffusion-fa", "--vae-tiling"]
        if _supports(exe, "--backend"):
            cmd += ["--backend", "vulkan0"]            # Rechnen immer auf der Grafikkarte, nie auf dem Prozessor
        if spar and _supports(exe, "--max-vram"):
            cmd += ["--max-vram", "-1"]                # ~1 GB für Windows/Discord frei lassen
        if spar and _supports(exe, "--params-backend"):
            # Notlösung bei knappem Speicher: Gewichte im RAM, Textmodell nach dem Lesen wieder freigeben
            cmd += ["--params-backend", "all=cpu,te=disk"]
        elif spar:
            cmd += ["--offload-to-cpu"]
        return cmd + refs_args

    # Versuche: normal → kleiner gemalt → zusätzlich Textmodell von der Platte → mit weniger Vorlagen
    small = gen_size(w, h, mp=0.7)
    # 1. Versuch: sd.cpp verteilt selbst (alles auf die Grafikkarte, was passt) – 2./3.: sparsam
    tries = [(w, h, ref_args, False), (*small, ref_args, True), (*gen_size(w, h, mp=0.55), ref_args[:4], True)]
    sdlog = Path(out_png).parent / "bildgenerator.log"
    t0 = time.time()
    last = ""
    for i, (gw, gh, ra, disk) in enumerate(tries):
        Path(out_png).unlink(missing_ok=True)
        p = _run_sd(build(gw, gh, ra, disk), exe, sdlog)
        if p.returncode == 0 and Path(out_png).exists():
            if i:
                log.info("Panel mit Sparversuch %s gemalt (%sx%s)", i, gw, gh)
            dev = next((l.strip() for l in p.stdout.splitlines() if "ggml_vulkan: 0 =" in l), "")
            log.info("Panel fertig in %.0f s%s", time.time() - t0, f" ({dev.split('=', 1)[-1].split('|')[0].strip()})" if dev else "")
            return
        last = "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:])
        log.error("Bildgenerator-Fehler (Code %s, Versuch %s):\n%s", p.returncode, i + 1, last)
        if not any(k.lower() in last.lower() for k in MEM_HINTS):
            break                                  # kein Speicherproblem → Weiterprobieren bringt nichts
    if any(k.lower() in last.lower() for k in MEM_HINTS):
        raise RuntimeError(
            "Der Grafikspeicher reicht gerade nicht. Meist belegt ein anderes Programm viel davon – "
            "z. B. Strata/SillyTavern, ein Spiel, Foundry im Browser oder Vesktop mit Hardwarebeschleunigung. "
            "Schließ das kurz und versuch es noch einmal, oder stell unter ⚙️ Einstellungen das Bildmodell "
            "„Schnell“ ein.\n\nTechnische Details:\n" + last)
    raise RuntimeError(f"Der Bildgenerator hat kein Bild erzeugt.\n\n{last}")


# ---------- Seiten bauen ----------
def _font(size, bold=False, clean=False):
    from PIL import ImageFont
    if clean:      # Manga/Manhwa: klare, runde Schrift
        cands = (["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"] if bold else ["seguisb.ttf", "segoeui.ttf", "arial.ttf"]) \
            + ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    else:
        cands = (["comicbd.ttf", "comic.ttf"] if bold else ["comic.ttf", "comicbd.ttf"])
    cands = cands + \
            ["segoeprb.ttf", "segoepr.ttf", "arialbd.ttf" if bold else "arial.ttf",
             "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    dirs = [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts", Path("/usr/share/fonts/truetype/dejavu")]
    for d in dirs:
        for c in cands:
            if (d / c).exists():
                return ImageFont.truetype(str(d / c), size)
    return ImageFont.load_default()


def _wrap(draw, text, font, maxw):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _bubbles(img, blasen, size=30, shape="round"):
    """Sprechblasen ins Panel (oben, abwechselnd links/rechts).
    shape="oval" (Manga/Manhwa): ovale Blasen, Name klein darüber; "round": Kasten mit Namen."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    W, H = img.size
    oval = shape == "oval"
    f_name = _font(max(16, int(size * 0.62)), True, oval)
    f_text = _font(size, oval, oval)
    y = int(H * 0.04)
    for i, (who, said) in enumerate(blasen):
        maxw = int(W * (0.42 if W > 900 else 0.7))
        lines = _wrap(d, said, f_text, maxw - 30)[:5]
        lh = int(f_text.size * 1.18)
        tw = max(d.textlength(l, font=f_text) for l in lines) if lines else 0
        th = len(lines) * lh
        left = i % 2 == 0
        if oval:
            bw, bh = int(tw * 1.32 + size * 1.6), int(th * 1.45 + size * 1.2)
            name_h = f_name.size + 6 if who else 0
            x = int(W * 0.04) if left else int(max(W * 0.04, W - bw - W * 0.04))
            if y + name_h + bh > H * 0.66:
                break
            if who:
                nw = d.textlength(who, font=f_name)
                nx = x + (bw - nw) / 2
                d.rounded_rectangle([nx - 8, y - 2, nx + nw + 8, y + f_name.size + 4], radius=8, fill="white")
                d.text((nx, y), who, font=f_name, fill="#555555")
            by = y + name_h
            tx = x + bw * (0.35 if left else 0.65)
            d.polygon([(tx - size * 0.5, by + bh * 0.8), (tx + size * 0.5, by + bh * 0.8),
                       (tx + (-size if left else size), by + bh + size * 1.1)], fill="white", outline="black")
            d.ellipse([x, by, x + bw, by + bh], fill="white", outline="black", width=3)
            ty = by + (bh - th) / 2
            for l in lines:
                d.text((x + (bw - d.textlength(l, font=f_text)) / 2, ty), l, font=f_text, fill="black")
                ty += lh
            y = by + bh + size * 1.6
        else:
            bw = max(tw, d.textlength(who, font=f_name) if who else 0) + 40
            bh = th + (f_name.size + 8 if who else 0) + 26
            x = int(W * 0.03) if left else int(max(W * 0.03, W - bw - W * 0.03))
            if y + bh > H * 0.62:
                break
            d.rounded_rectangle([x, y, x + bw, y + bh], radius=26, fill="white", outline="black", width=4)
            tx = x + bw * (0.3 if left else 0.7)
            d.polygon([(tx - 14, y + bh - 3), (tx + 14, y + bh - 3), (tx + (-22 if left else 22), y + bh + 30)],
                      fill="white", outline="black")
            d.line([(tx - 12, y + bh - 3), (tx + 12, y + bh - 3)], fill="white", width=6)
            ty = y + 13
            if who:
                d.text((x + 20, ty), who, font=f_name, fill="#8a1c00")
                ty += f_name.size + 8
            for l in lines:
                d.text((x + 20, ty), l, font=f_text, fill="black")
                ty += lh
            y += bh + 40
    return img


# ---------- Layout: wo kommt welches Panel hin, wie groß wird es gemalt ----------
SHOTS = {
    "wide": "Wide cinematic establishing shot, horizontal composition",
    "tall": "Vertical composition, full body shot, dynamic low camera angle",
    "square": "Medium shot, characters from the waist up, expressive faces",
    "close": "Close-up on faces and emotions",
    "splash": "Dramatic full-page splash illustration, epic composition, characters in heroic poses",
}

# Seiten-Vorlagen (Anteile der Nutzfläche): x0, y0, x1, y1, Bildausschnitt
PAGE_TEMPLATES = {
    1: [[(0, 0, 1, 1, "splash")]],
    2: [[(0, 0, 1, .52, "wide"), (0, .54, 1, 1, "wide")]],
    3: [[(0, 0, 1, .40, "wide"), (0, .42, .49, 1, "tall"), (.51, .42, 1, 1, "tall")],
        [(0, 0, .56, 1, "tall"), (.58, 0, 1, .49, "square"), (.58, .51, 1, 1, "close")]],
    4: [[(0, 0, 1, .30, "wide"), (0, .32, .44, .68, "square"), (.46, .32, 1, .68, "square"), (0, .70, 1, 1, "wide")],
        [(0, 0, .60, .47, "square"), (.62, 0, 1, .47, "tall"), (0, .49, .38, 1, "tall"), (.40, .49, 1, 1, "square")]],
}
PAGE_RHYTHM = [3, 4, 3, 4, 3]          # Panels pro Seite (Manga/Comic) – nie gequetscht


def plan_pages(n, fmt):
    """Liefert pro Panel: Seite, Box in Pixeln, Bildausschnitt. Seite 1 ist ein Splash mit Kapiteltitel."""
    PW, PH, M = 1654, 2339, 90
    G = 34 if fmt == "manga" else 26
    counts, left, k = [], n, 0
    if fmt == "manga" and n >= 4:
        counts.append(1)                 # Kapitel-Auftakt als ganze Seite
        left -= 1
    while left > 0:
        c = PAGE_RHYTHM[k % len(PAGE_RHYTHM)]
        k += 1
        if left - c in (1, 2) and left <= 5:
            c = left if left <= 4 else left - 2
        c = max(1, min(c, left))
        counts.append(c)
        left -= c
    slots = []
    for pi, c in enumerate(counts):
        tpls = PAGE_TEMPLATES[c]
        tpl = tpls[pi % len(tpls)]
        top = M + (120 if pi == 0 and c > 1 else 0)
        aw, ah = PW - 2 * M, PH - top - M
        for (x0, y0, x1, y1, shot) in tpl:
            box = (int(M + x0 * aw + (G / 2 if x0 > 0 else 0)), int(top + y0 * ah + (G / 2 if y0 > 0 else 0)),
                   int(M + x1 * aw - (G / 2 if x1 < 1 else 0)), int(top + y1 * ah - (G / 2 if y1 < 1 else 0)))
            slots.append({"seite": pi, "box": box, "shot": shot})
    return slots, (PW, PH), len(counts)


def plan_webtoon(n):
    """Manhwa/Webtoon: ein langer Streifen, Panels in voller Breite oder versetzt, mit Luft dazwischen."""
    W = 1080
    pattern = [("wide", 1.0, 0.56, "c"), ("tall", 1.0, 1.35, "c"), ("square", .82, .82, "l"),
               ("close", .82, .70, "r"), ("wide", 1.0, .62, "c"), ("tall", .9, 1.25, "r"), ("square", .86, .9, "l")]
    slots, y = [], 160
    for i in range(n):
        shot, fw, ar, al = ("splash", 1.0, 1.4, "c") if i == n - 1 and n > 3 else pattern[i % len(pattern)]
        w = int(W * fw)
        h = int(W * ar)
        x = 0 if al == "l" or fw == 1.0 else (W - w if al == "r" else (W - w) // 2)
        slots.append({"seite": 0, "box": (x, y, x + w, y + h), "shot": shot})
        y += h + (220 if i % 3 == 2 else 130)
    return slots, (W, y + 200), 1


def _cover(im, bw, bh):
    from PIL import Image
    scale = max(bw / im.width, bh / im.height)
    im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1), Image.LANCZOS)
    l, t = (im.width - bw) // 2, (im.height - bh) // 2
    return im.crop((l, t, l + bw, t + bh))


def make_pages(panels, images, title, out_dir, fmt="manhwa", slots=None):
    """Panels nach Format zusammensetzen → PNG-Seiten bzw. Webtoon-Abschnitte + Comic.pdf."""
    from PIL import Image, ImageDraw
    out_dir = Path(out_dir)
    for old in list(out_dir.glob("seite_*.png")) + list(out_dir.glob("webtoon_*.png")):
        old.unlink()
    if slots is None:
        slots = plan_webtoon(len(panels))[0] if fmt == "manhwa" else plan_pages(len(panels), fmt)[0]
    if fmt == "manhwa":
        W = 1080
        H = max(s["box"][3] for s in slots) + 220
        strip = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(strip)
        if title:
            f = _font(58, True, True)
            d.text(((W - d.textlength(title, font=f)) / 2, 50), title, font=f, fill="#222222")
        for panel, path, sl in zip(panels, images, slots):
            x0, y0, x1, y1 = sl["box"]
            im = _cover(Image.open(path).convert("RGB"), x1 - x0, y1 - y0)
            im = _bubbles(im, panel.get("blasen", []), size=30, shape="oval")
            strip.paste(im, (x0, y0))
        # in Abschnitte schneiden – nur in den Lücken zwischen Panels
        cuts, last = [0], 0
        for sl in slots:
            if sl["box"][3] - last > 5000:
                cut = sl["box"][1] - 60
                if cut > last + 1500:
                    cuts.append(cut)
                    last = cut
        cuts.append(H)
        pages = [strip.crop((0, a, W, b)) for a, b in zip(cuts, cuts[1:])]
        prefix = "webtoon"
    else:
        manga = fmt == "manga"
        PW, PH = 1654, 2339
        n_pages = max(s["seite"] for s in slots) + 1
        bg = "white" if manga else "#f4ecd8"
        pages = [Image.new("RGB", (PW, PH), bg) for _ in range(n_pages)]
        for panel, path, sl in zip(panels, images, slots):
            pg = pages[sl["seite"]]
            x0, y0, x1, y1 = sl["box"]
            im = _cover(Image.open(path).convert("RGB"), x1 - x0, y1 - y0)
            im = _bubbles(im, panel.get("blasen", []), size=34 if sl["shot"] == "splash" else 30,
                          shape="oval" if manga else "round")
            pg.paste(im, (x0, y0))
            ImageDraw.Draw(pg).rectangle([x0, y0, x1, y1], outline="black", width=5 if manga else 6)
        if title:
            d = ImageDraw.Draw(pages[0])
            f = _font(74, True, manga)
            tw = d.textlength(title, font=f)
            if slots[0]["shot"] == "splash":          # Titel als Kasten auf dem Auftakt-Bild
                x0, y0, x1, _ = slots[0]["box"]
                bx, by = x0 + 40, y0 + 40
                d.rectangle([bx, by, bx + tw + 60, by + 120], fill="white", outline="black", width=5)
                d.text((bx + 30, by + 18), title, font=f, fill="black")
            else:
                d.text(((PW - tw) / 2, 80), title, font=f, fill="#111111" if manga else "#3b1f0b")
        prefix = "seite"
    out = []
    for i, pg in enumerate(pages, 1):
        p = out_dir / f"{prefix}_{i:02d}.png"
        pg.save(p)
        out.append(str(p))
    if pages:
        pages[0].save(out_dir / "Comic.pdf", save_all=True, append_images=pages[1:], resolution=200)
    (out_dir / "seiten.json").write_text(json.dumps([Path(x).name for x in out]), encoding="utf-8")
    return out


# ---------- Ablauf ----------
def run(state, progress=lambda f, m: None, style=None, fmt=None):
    folder = Path(state["ordner"])
    tpl = folder / "comic_vorlage.md"
    if not tpl.exists():
        raise RuntimeError("Es gibt noch keine Comic-Vorlage für diese Session – bitte zuerst erstellen.")
    panels = parse_template(tpl.read_text(encoding="utf-8"))
    if not panels:
        raise RuntimeError("In der Comic-Vorlage wurden keine Panels gefunden.")
    CURRENT["abbruch"] = False
    if kill_stale():
        time.sleep(2)                     # Grafikspeicher freigeben lassen
    m = modell()
    if not installed(m):
        install(lambda f, msg: progress(0.02 + 0.28 * f, msg), m)
    fmt = fmt or comic_format()
    slots = plan_webtoon(len(panels))[0] if fmt == "manhwa" else plan_pages(len(panels), fmt)[0]
    from .llm import api_up
    if api_up(C.settings()["llm_url"], timeout=1):
        log.info("Hinweis: Eine KI läuft noch und belegt Grafikspeicher – Malen kann langsamer sein")
    grp = C.group()
    style = style or style_for(fmt)
    log.info("Comic: %s Panels, Format %s, Modell %s", len(panels), fmt, m)
    out = folder / "comic"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    images, protokoll = [], []
    seed = abs(hash(state.get("titel", ""))) % 100000
    for i, p in enumerate(panels):
        progress(0.3 + 0.65 * i / len(panels), f"Panel {i + 1} von {len(panels)} wird gemalt …")
        sl = slots[i]
        p["shot"] = SHOTS[sl["shot"]]
        refs = refs_for(p, grp)
        plan = assign_refs(refs)
        prompt = build_prompt(p, refs, style, plan)
        png = out / f"panel_{p['nr']:02d}.png"
        protokoll.append(f"Panel {p['nr']}: Figuren: {', '.join(r['anzeige'] for r in refs) or '– keine erkannt –'}\n"
                         f"  Referenzbilder: {', '.join(Path(b).name for _, im in plan for b in im) or '–'}\n"
                         f"  Prompt: {prompt}\n")
        (out / "panels.txt").write_text("\n".join(protokoll), encoding="utf-8")
        bw, bh = sl["box"][2] - sl["box"][0], sl["box"][3] - sl["box"][1]
        gw, gh = gen_size(bw, bh)
        try:
            generate_panel(prompt, refs, png, seed + i, w=gw, h=gh, plan=plan, m=m)
        except Hang:
            if m != "qualitaet":
                raise RuntimeError("Der Bildgenerator hängt und macht keinen Fortschritt. Bitte schick den Debug-Log.")
            log.warning("Hohe Qualität hängt auf diesem PC – wechsle auf „Schnell“ und male weiter")
            C.save("einstellungen", {**C.settings(), "comic_9b_haengt": True})
            m = "schnell"
            if not installed(m):
                install(lambda f, msg: progress(0.3, msg), m)
            progress(0.3 + 0.67 * i / len(panels), "Hohe Qualität hing – male mit „Schnell“ weiter …")
            generate_panel(prompt, refs, png, seed + i, w=gw, h=gh, plan=plan, m=m)
        images.append(str(png))
    progress(0.97, "Seiten werden zusammengesetzt …")
    names = {}
    for who in [grp.get("ich_name", "Ich")] + [sp.get("name") for sp in grp.get("spieler", [])]:
        mc = C.main_character(who, grp)
        if who and mc:
            names[who.lower()] = mc["name"]
    for p in panels:
        p["blasen"] = [(names.get(w.lower(), w), t) for w, t in p["blasen"]]
    title = state.get("titel", "")
    zf = folder / "zusammenfassung.md"
    if zf.exists():
        first = next((l for l in zf.read_text(encoding="utf-8").splitlines() if l.startswith("# ")), "")
        title = re.sub(r"[*_`]", "", first.lstrip("# ")).strip() or title
    title = title[:60]
    pages = make_pages(panels, images, title, out, fmt=fmt, slots=slots)
    progress(1.0, "Comic fertig.")
    log.info("Comic fertig: %s Panels, %s Seiten", len(images), len(pages))
    return pages
