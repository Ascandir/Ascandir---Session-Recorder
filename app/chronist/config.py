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
for p in (DATA, MODELS, SESSIONS, RECORDINGS, IMAGES):
    p.mkdir(parents=True, exist_ok=True)

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


def group():
    g = load("gruppe", DEFAULT_GROUP)
    raw = {}
    if _path("gruppe").exists():
        try:
            with open(_path("gruppe"), encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            pass
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


def is_dm(name, grp=None):
    grp = grp or group()
    dm = grp.get("dm", ICH_ID)
    if name in (ICH_ID, grp.get("ich_name")):
        return dm == ICH_ID
    return dm == name


def voices():
    return load("stimmen", {})


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
    if f and (IMAGES / f).exists():
        return str(IMAGES / f)
    return None
