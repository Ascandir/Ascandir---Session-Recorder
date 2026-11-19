"""Pfade und Einstellungen. Alles liegt im Ordner des Tools – nichts in der Cloud."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # .../DnD-Chronist
DATA = ROOT / "daten"
MODELS = DATA / "modelle"
SESSIONS = ROOT / "Sessions"
RECORDINGS = ROOT / "Aufnahmen"
IMAGES = DATA / "bilder"
TEMP = DATA / "temp"                       # alle Zwischendateien
HF_CACHE = MODELS / "hf-cache"             # Download-Zwischenspeicher der Modellbibliothek
for p in (DATA, MODELS, SESSIONS, RECORDINGS, IMAGES, TEMP, HF_CACHE):
    p.mkdir(parents=True, exist_ok=True)


ORIG_ENV = dict(os.environ)     # für fremde Programme wie Strata – die sollen ihren eigenen Temp nutzen


def pin_storage_env():
    """Alle Bibliotheken zwingen, im Programmordner zu speichern – nichts im Benutzerordner oder %TEMP%."""
    env = {
        "HF_HOME": HF_CACHE, "HF_HUB_CACHE": HF_CACHE / "hub", "HF_XET_CACHE": HF_CACHE / "xet",
        "HUGGINGFACE_HUB_CACHE": HF_CACHE / "hub", "XDG_CACHE_HOME": DATA / "cache",
        "GRADIO_TEMP_DIR": TEMP / "gradio", "TEMP": TEMP, "TMP": TEMP, "TMPDIR": TEMP,
        "PIP_CACHE_DIR": DATA / "cache" / "pip", "UV_CACHE_DIR": DATA / "cache" / "uv",
    }
    for k, v in env.items():
        Path(v).mkdir(parents=True, exist_ok=True)
        os.environ[k] = str(v)
    import tempfile
    tempfile.tempdir = str(TEMP)

IGNORIEREN = "(ignorieren)"
UNBEKANNT = "Unbekannt"
ICH_ID = "__ich__"

DEFAULT_SETTINGS = {
    "llm_url": "http://127.0.0.1:8080/v1",   # Strata-Standard
    "llm_model": "",                          # leer = automatisch erstes Modell
    "llm_key": "lokal",
    "whisper_model": "large-v3-turbo",
    "aufnahme_ordner": str(RECORDINGS),
    "mikro": "",
    "design": "Dunkel",
    "update_repo": "Ascandir/Ascandir---Session-Recorder",
    "update_beim_start": True,
    "ki_modus": "Automatisch",   # Strata wenn gefunden, sonst eingebaute KI
    "strata_befehl": "",
    "strata_preset": "",         # leer = erstes gespeichertes Preset
    "strata_beenden": True,
    "strata_wartezeit": 900,
    "match_schwelle": 0.55,
    "chunk_zeichen": 12000,
}

DEFAULT_GROUP = {
    "ich_name": "Ich",
    "ich_rolle": "",        # dein Charakter
    "dm": ICH_ID,           # wer leitet: ICH_ID oder ein Spielername
    "spieler": [],          # [{"name":..., "charakter":..., "aussehen":...}]
    "kontext": "",
    "bilder": {},           # {"Spielername": "dateiname.png"}
}


def _path(name):
    return DATA / f"{name}.json"


def load(name, default):
    p = _path(name)
    data = dict(default) if isinstance(default, dict) else default
    if p.exists():
        try:
            with open(p, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(default, dict):
                data.update(stored)
            else:
                data = stored
        except Exception:
            pass
    return data


def save(name, data):
    p = _path(name)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def settings():
    st = load("einstellungen", DEFAULT_SETTINGS)
    if not st.get("ki_modus"):
        st["ki_modus"] = "Automatisch"
    return st


# ---------- Mehrere Gruppen ----------
GROUPS = DATA / "gruppen"
GROUPS.mkdir(parents=True, exist_ok=True)
DEFAULT_GROUP_NAME = "Meine Runde"


def safe_name(name):
    import re
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", (name or "").strip()).strip(" .")
    return n[:60] or "Gruppe"


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def list_groups():
    """Namen aller Gruppen (= Ordnernamen), alphabetisch."""
    _ensure_groups()
    return sorted([d.name for d in GROUPS.iterdir() if (d / "gruppe.json").exists()], key=str.lower)


def _ensure_groups():
    if any((d / "gruppe.json").exists() for d in GROUPS.iterdir()):
        return
    # Erste Gruppe aus der alten Einzel-Gruppe (Daten, Stimmen, Bilder) anlegen
    d = GROUPS / DEFAULT_GROUP_NAME
    (d / "bilder").mkdir(parents=True, exist_ok=True)
    old = _read(_path("gruppe")) if _path("gruppe").exists() else {}
    _write(d / "gruppe.json", old)
    if _path("stimmen").exists():
        _write(d / "stimmen.json", _read(_path("stimmen")))
        _path("stimmen").rename(_path("stimmen").with_suffix(".json.alt"))
    old_imgs = DATA / "bilder"
    if old_imgs.exists():
        for f in old_imgs.iterdir():
            if f.is_file():
                os.replace(f, d / "bilder" / f.name)
    if _path("gruppe").exists():
        _path("gruppe").rename(_path("gruppe").with_suffix(".json.alt"))


def active_group():
    names = list_groups()
    a = load("einstellungen", DEFAULT_SETTINGS).get("aktive_gruppe", "")
    return a if a in names else names[0]


def set_active_group(name):
    st = load("einstellungen", DEFAULT_SETTINGS)
    st["aktive_gruppe"] = name
    save("einstellungen", st)


def group_dir(name=None):
    return GROUPS / (name or active_group())


def group(name=None):
    raw = _read(group_dir(name) / "gruppe.json")
    g = dict(DEFAULT_GROUP)
    g.update(raw)
    if "dm" not in raw:                     # alte Einstellungen umstellen
        g["dm"] = ""
        if g.get("ich_rolle", "").strip().upper() == "DM":
            g["dm"], g["ich_rolle"] = ICH_ID, ""
        for sp in g.get("spieler", []):
            if sp.get("charakter", "").strip().upper() in ("DM", "SL", "SPIELLEITER"):
                g["dm"], sp["charakter"] = sp["name"], ""
        if not g["dm"]:
            g["dm"] = ICH_ID
        r = g.get("ich_rolle", "")
        if r.lower().startswith("spieler /"):
            g["ich_rolle"] = r.split("/", 1)[1].strip()
    return g


def save_group(g, name=None):
    _write(group_dir(name) / "gruppe.json", g)


def images_dir(name=None):
    d = group_dir(name) / "bilder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def recordings_dir(name=None):
    st = load("einstellungen", DEFAULT_SETTINGS)
    d = Path(st.get("aufnahme_ordner") or RECORDINGS) / (name or active_group())
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir(name=None):
    d = SESSIONS / (name or active_group())
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_group(name):
    n = safe_name(name)
    if (GROUPS / n / "gruppe.json").exists():
        raise ValueError(f"Die Gruppe „{n}“ gibt es schon.")
    _write(GROUPS / n / "gruppe.json", {"spieler": [], "kontext": "", "bilder": {}})
    (GROUPS / n / "bilder").mkdir(parents=True, exist_ok=True)
    return n


def _fix_session_paths(folder, old, new):
    """Pfade in analyse.json anpassen, wenn Ordner umbenannt/verschoben wurden."""
    for f in Path(folder).glob("*/analyse.json"):
        try:
            txt = f.read_text(encoding="utf-8")
            o, n = json.dumps(str(old))[1:-1], json.dumps(str(new))[1:-1]
            if o in txt:
                f.write_text(txt.replace(o, n), encoding="utf-8")
        except Exception:
            pass


def rename_group(old, new_name):
    new = safe_name(new_name)
    if new == old:
        return old
    if (GROUPS / new).exists():
        raise ValueError(f"Die Gruppe „{new}“ gibt es schon.")
    os.replace(GROUPS / old, GROUPS / new)
    rec_root = Path(load("einstellungen", DEFAULT_SETTINGS).get("aufnahme_ordner") or RECORDINGS)
    for root in (rec_root, SESSIONS):
        if (root / old).exists():
            os.replace(root / old, root / new)
    _fix_session_paths(SESSIONS / new, SESSIONS / old, SESSIONS / new)
    _fix_session_paths(SESSIONS / new, rec_root / old, rec_root / new)
    if active_group() == new or load("einstellungen", DEFAULT_SETTINGS).get("aktive_gruppe") == old:
        set_active_group(new)
    return new


def delete_group(name):
    """Löscht nur die Gruppen-Einstellungen (Mitspieler, Stimmen, Bilder) – Aufnahmen & Sessions bleiben."""
    import shutil
    if len(list_groups()) <= 1:
        raise ValueError("Die letzte Gruppe kann nicht gelöscht werden.")
    shutil.rmtree(GROUPS / name, ignore_errors=True)
    if load("einstellungen", DEFAULT_SETTINGS).get("aktive_gruppe") == name:
        set_active_group(list_groups()[0])


def is_dm(name, grp=None):
    grp = grp or group()
    dm = grp.get("dm", ICH_ID)
    if name in (ICH_ID, grp.get("ich_name")):
        return dm == ICH_ID
    return dm == name


def voices(name=None):
    return _read(group_dir(name) / "stimmen.json")


def save_voices(v, name=None):
    _write(group_dir(name) / "stimmen.json", v)


def player_label(name, grp=None):
    """'Lena' -> 'Lena (Elara)'."""
    grp = grp or group()
    if is_dm(name, grp):
        n = grp.get("ich_name", "Ich") if name == ICH_ID else name
        return f"{n} (DM)"
    if name == ICH_ID or name == grp.get("ich_name"):
        rolle = grp.get("ich_rolle", "").strip()
        n = grp.get("ich_name", "Ich")
        return f"{n} ({rolle})" if rolle else n
    for s in grp.get("spieler", []):
        if s.get("name") == name and s.get("charakter"):
            return f"{name} ({s['charakter']})"
    return name


def image_path(name, grp=None):
    """Pfad zum Charakterbild eines Spielers (oder None)."""
    grp = grp or group()
    f = (grp.get("bilder") or {}).get(name)
    if f and (images_dir() / f).exists():
        return str(images_dir() / f)
    return None
