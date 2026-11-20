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

# Modelldateien (alle frei verfügbar, Apache 2.0)
DIFF_REPO = "unsloth/FLUX.2-klein-4B-GGUF"
DIFF_PREF = ["Q8_0", "Q6_K", "Q5_K_M", "Q4_K_M"]
LLM_REPO = "unsloth/Qwen3-4B-GGUF"
LLM_PREF = ["Q8_0", "Q6_K", "Q4_K_M"]
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


def installed():
    st = _state()
    return bool(sd_cli() and VAE_FILE.exists() and st.get("diffusion") and st.get("llm")
                and (DIR / st["diffusion"]).exists() and (DIR / st["llm"]).exists())


def install(progress=lambda f, m: None):
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
    if not st.get("diffusion") or not (DIR / st["diffusion"]).exists():
        f = _hf_pick(DIFF_REPO, DIFF_PREF)
        _download(f"https://huggingface.co/{DIFF_REPO}/resolve/main/{f}", DIR / Path(f).name, progress,
                  "Bildmodell (FLUX.2-klein) wird geladen", 0.05, 0.5)
        st["diffusion"] = Path(f).name
    if not st.get("llm") or not (DIR / st["llm"]).exists():
        f = _hf_pick(LLM_REPO, LLM_PREF)
        _download(f"https://huggingface.co/{LLM_REPO}/resolve/main/{f}", DIR / Path(f).name, progress,
                  "Textverständnis (Qwen3-4B) wird geladen", 0.5, 0.9)
        st["llm"] = Path(f).name
    if not VAE_FILE.exists():
        _download(VAE_URL, VAE_FILE, progress, "Bild-Decoder wird geladen", 0.9, 1.0)
    (DIR / "modelle.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
    progress(1.0, "Bildgenerator ist bereit.")


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
    parts = [f"A single comic book panel in this style: {style}."]
    if cast:
        parts.append("Characters in this panel: " + "; ".join(cast) + ".")
    if panel.get("im_bild"):
        parts.append("What happens: " + _short(panel["im_bild"], 300).rstrip(".") + ".")
    parts.append("Scene: " + base.rstrip(".") + ".")
    parts.append("No text, no letters, no speech bubbles, no character sheet, no multiple views.")
    return " ".join(parts)


# ---------- Malen ----------
def generate_panel(prompt, refs, out_png, seed, w=1024, h=768, plan=None):
    st = _state()
    exe = sd_cli()
    cmd = [str(exe), "--diffusion-model", str(DIR / st["diffusion"]), "--vae", str(VAE_FILE),
           "--llm", str(DIR / st["llm"]), "-p", prompt, "--cfg-scale", "1.0", "--steps", "4",
           "-W", str(w), "-H", str(h), "-s", str(seed), "-o", str(out_png),
           "--diffusion-fa", "--vae-tiling"]
    plan = assign_refs(refs) if plan is None else plan
    n = 0
    for r, imgs in plan:
        for b in imgs:
            cmd += ["-r", _ref_file(b, Path(out_png).parent / "_referenzen")]
            n += 1
    log.info("Panel malen: %s – Figuren: %s – %s Referenzbild(er)", Path(out_png).name,
             ", ".join(r["anzeige"] for r in refs) or "keine erkannt", n)
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=NOWIN, cwd=str(exe.parent), env=C.ORIG_ENV)
    if p.returncode != 0 or not Path(out_png).exists():
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-12:])
        log.error("Bildgenerator-Fehler (Code %s):\n%s", p.returncode, tail)
        raise RuntimeError(f"Der Bildgenerator hat kein Bild erzeugt (Code {p.returncode}).\n\n{tail}")
    log.info("Panel fertig in %.0f s", time.time() - t0)


# ---------- Seiten bauen ----------
def _font(size, bold=False):
    from PIL import ImageFont
    cands = (["comicbd.ttf", "comic.ttf"] if bold else ["comic.ttf", "comicbd.ttf"]) + \
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


def _bubbles(img, blasen, size=30):
    """Sprechblasen auf ein Panel zeichnen (oben, abwechselnd links/rechts) – gleiche Schriftgröße überall."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    W, H = img.size
    f_name, f_text = _font(size - 2, True), _font(size)
    y = int(H * 0.03)
    for i, (who, said) in enumerate(blasen):
        maxw = int(W * (0.46 if W > 1000 else 0.8))
        lines = _wrap(d, said, f_text, maxw - 36)[:5]
        lh = f_text.size + 6
        bw = max([d.textlength(l, font=f_text) for l in lines] + [d.textlength(who, font=f_name)]) + 40
        bh = len(lines) * lh + (f_name.size + 8 if who else 0) + 26
        x = int(W * 0.03) if i % 2 == 0 else int(max(W * 0.03, W - bw - W * 0.03))
        if y + bh > H * 0.62:
            break
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=26, fill="white", outline="black", width=4)
        tx = x + bw * (0.3 if i % 2 == 0 else 0.7)       # Schwänzchen
        d.polygon([(tx - 14, y + bh - 3), (tx + 14, y + bh - 3), (tx + (-22 if i % 2 == 0 else 22), y + bh + 30)],
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


def make_pages(panels, images, title, out_dir):
    """Panels zu A4-Seiten zusammensetzen (oben breit, Mitte zwei, unten breit) + PDF."""
    from PIL import Image, ImageDraw
    PW, PH, M, G = 1654, 2339, 70, 26
    layouts = [["wide"], ["half", "half"], ["wide"]]
    pages, page, slots = [], None, []

    def new_page(first):
        pg = Image.new("RGB", (PW, PH), "#f4ecd8")
        top = M
        if first and title:
            d = ImageDraw.Draw(pg)
            f = _font(74, True)
            tw = d.textlength(title, font=f)
            d.text(((PW - tw) / 2, M - 10), title, font=f, fill="#3b1f0b")
            top = M + 110
        rows_h = (PH - top - M - G * 2) // 3
        s = []
        y = top
        for row in layouts:
            if row == ["wide"]:
                s.append((M, y, PW - M, y + rows_h))
            else:
                half = (PW - 2 * M - G) // 2
                s.append((M, y, M + half, y + rows_h))
                s.append((M + half + G, y, PW - M, y + rows_h))
            y += rows_h + G
        return pg, s

    for n, (panel, path) in enumerate(zip(panels, images)):
        if not slots:
            if page is not None:
                pages.append(page)
            page, slots = new_page(first=not pages)
        x0, y0, x1, y1 = slots.pop(0)
        im = Image.open(path).convert("RGB")
        bw, bh = x1 - x0, y1 - y0
        scale = max(bw / im.width, bh / im.height)                   # füllen und mittig zuschneiden
        im = im.resize((int(im.width * scale) + 1, int(im.height * scale) + 1))
        l, t = (im.width - bw) // 2, (im.height - bh) // 2
        im = im.crop((l, t, l + bw, t + bh))
        im = _bubbles(im, panel.get("blasen", []))
        page.paste(im, (x0, y0))
        ImageDraw.Draw(page).rectangle([x0, y0, x1, y1], outline="black", width=6)
    if page is not None:
        pages.append(page)
    out = []
    for i, pg in enumerate(pages, 1):
        p = Path(out_dir) / f"seite_{i:02d}.png"
        pg.save(p)
        out.append(str(p))
    if pages:
        pages[0].save(Path(out_dir) / "Comic.pdf", save_all=True, append_images=pages[1:], resolution=200)
    return out


# ---------- Ablauf ----------
def run(state, progress=lambda f, m: None, style=None):
    folder = Path(state["ordner"])
    tpl = folder / "comic_vorlage.md"
    if not tpl.exists():
        raise RuntimeError("Es gibt noch keine Comic-Vorlage für diese Session – bitte zuerst erstellen.")
    panels = parse_template(tpl.read_text(encoding="utf-8"))
    if not panels:
        raise RuntimeError("In der Comic-Vorlage wurden keine Panels gefunden.")
    if not installed():
        install(lambda f, m: progress(0.02 + 0.28 * f, m))
    from .llm import api_up
    if api_up(C.settings()["llm_url"], timeout=1):
        log.info("Hinweis: Eine KI läuft noch und belegt Grafikspeicher – Malen kann langsamer sein")
    grp = C.group()
    style = style or C.settings().get("comic_stil") or STYLE_DEFAULT
    out = folder / "comic"
    out.mkdir(exist_ok=True)
    for old in out.glob("*.png"):
        old.unlink()
    images, protokoll = [], []
    seed = abs(hash(state.get("titel", ""))) % 100000
    for i, p in enumerate(panels):
        progress(0.3 + 0.65 * i / len(panels), f"Panel {i + 1} von {len(panels)} wird gemalt …")
        refs = refs_for(p, grp)
        plan = assign_refs(refs)
        prompt = build_prompt(p, refs, style, plan)
        png = out / f"panel_{p['nr']:02d}.png"
        protokoll.append(f"Panel {p['nr']}: Figuren: {', '.join(r['anzeige'] for r in refs) or '– keine erkannt –'}\n"
                         f"  Referenzbilder: {', '.join(Path(b).name for _, im in plan for b in im) or '–'}\n"
                         f"  Prompt: {prompt}\n")
        (out / "panels.txt").write_text("\n".join(protokoll), encoding="utf-8")
        generate_panel(prompt, refs, png, seed + i, plan=plan)
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
    pages = make_pages(panels, images, title, out)
    progress(1.0, "Comic fertig.")
    log.info("Comic fertig: %s Panels, %s Seiten", len(images), len(pages))
    return pages
