"""Speicherplatz im Blick: was liegt wo, wie groß – und aufräumen."""
import shutil
from pathlib import Path

from . import config as C
from .debuglog import log


def _size(p):
    p = Path(p)
    if not p.exists():
        return 0
    if p.is_file():
        return p.stat().st_size
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file() and not f.is_symlink():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _fmt(b):
    for unit, div in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if b >= div:
            return f"{b / div:.1f} {unit}".replace(".", ",")
    return f"{b} B"


def migrate_old_layout():
    """Ältere Versionen haben die Stimmerkennung direkt in daten/modelle gelegt → verschieben."""
    from . import models
    old_seg = C.MODELS / "sherpa-onnx-pyannote-segmentation-3-0"
    old_emb = C.MODELS / models.EMBEDDING.name
    try:
        models.VOICE_DIR.mkdir(parents=True, exist_ok=True)
        if old_seg.exists() and not models.SEGMENTATION.parent.exists():
            shutil.move(str(old_seg), str(models.SEGMENTATION.parent))
            log.info("Stimmerkennung (Segmentierung) verschoben nach %s", models.VOICE_DIR)
        if old_emb.exists() and not models.EMBEDDING.exists():
            shutil.move(str(old_emb), str(models.EMBEDDING))
            log.info("Stimmerkennung (Stimmabdruck) verschoben nach %s", models.VOICE_DIR)
    except Exception:
        log.exception("Verschieben der alten Modelle fehlgeschlagen")


def migrate_to_groups():
    """Aufnahmen/Sessions aus der Zeit vor den Gruppen in den Ordner der ersten Gruppe verschieben."""
    first = C.list_groups()[0]
    rec_root = Path(C.settings().get("aufnahme_ordner") or C.RECORDINGS)
    moved = 0
    if rec_root.exists():
        target = rec_root / first
        for f in list(rec_root.iterdir()):
            if f.is_file() and f.suffix.lower() in (".mka", ".mkv", ".mp3", ".wav", ".flac", ".m4a"):
                target.mkdir(parents=True, exist_ok=True)
                shutil.move(str(f), str(target / f.name))
                moved += 1
    starget = C.SESSIONS / first
    for d in list(C.SESSIONS.iterdir()):
        if d.is_dir() and (d / "analyse.json").exists():
            starget.mkdir(parents=True, exist_ok=True)
            shutil.move(str(d), str(starget / d.name))
            moved += 1
    if moved:
        C._fix_session_paths(starget, C.SESSIONS, starget)
        # Pfad zur Original-Aufnahme anpassen (lag vorher direkt im Aufnahmeordner)
        for f in starget.glob("*/analyse.json"):
            try:
                import json
                stt = json.loads(f.read_text(encoding="utf-8"))
                q = Path(stt.get("quelle", ""))
                if q.parent == rec_root and (rec_root / first / q.name).exists():
                    stt["quelle"] = str(rec_root / first / q.name)
                    stt.setdefault("gruppe", first)
                    f.write_text(json.dumps(stt, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
        log.info("%s alte Aufnahmen/Sessions in die Gruppe „%s“ verschoben", moved, first)


def clean_temp():
    """Zwischendateien vom letzten Mal löschen (beim Start)."""
    n = 0
    for f in C.TEMP.iterdir() if C.TEMP.exists() else []:
        try:
            if f.is_dir():
                shutil.rmtree(f, ignore_errors=True)
            else:
                f.unlink()
            n += 1
        except OSError:
            pass
    for d in ("gradio",):
        (C.TEMP / d).mkdir(parents=True, exist_ok=True)
    if n:
        log.info("Temp aufgeräumt: %s Einträge", n)


def _whisper_repo(name):
    try:
        from faster_whisper.utils import _MODELS
        return _MODELS.get(name, name)
    except Exception:
        return name


def whisper_models():
    """[(name, ordner, größe)] der heruntergeladenen Spracherkennungs-Modelle."""
    from . import models
    out = []
    if not models.WHISPER_DIR.exists():
        return out
    for marker in models.WHISPER_DIR.glob(".fertig-*"):
        name = marker.name[len(".fertig-"):]
        folder = models.WHISPER_DIR / ("models--" + _whisper_repo(name).replace("/", "--"))
        out.append((name, folder, _size(folder)))
    return out


def overview():
    from . import llm as L
    from . import models
    root = C.ROOT
    wm = C.settings().get("whisper_model")
    rows = []
    for name, folder, size in whisper_models():
        rows.append((f"Spracherkennung **{name}**" + (" *(in Benutzung)*" if name == wm else ""), size))
    rows += [
        ("Stimmerkennung", _size(models.VOICE_DIR)),
        ("Eingebaute KI (Engine + Modell)", _size(L.BUILTIN_DIR)),
        ("Download-Zwischenspeicher", _size(C.HF_CACHE) + _size(C.DATA / "cache")),
        ("Python & Programmteile", _size(root / "venv") + _size(root / "python") + _size(root / "tools")),
        ("Aufnahmen (alle Gruppen)", _size(Path(C.settings().get("aufnahme_ordner") or C.RECORDINGS))),
        ("Sessions (alle Gruppen)", _size(C.SESSIONS)),
        ("Temporäre Dateien", _size(C.TEMP)),
        ("Protokolle & Debug", _size(C.DATA / "debug") + sum(_size(p) for p in C.DATA.glob("*.log*"))
         + _size(C.DATA / "log.txt") + _size(C.DATA / "ki-log.txt")),
    ]
    total = _size(root)
    lines = ["| Bereich | Größe |", "|---|---:|"]
    lines += [f"| {n} | {_fmt(s)} |" for n, s in rows if s > 0 or "Aufnahmen" in n]
    lines.append(f"| **Gesamt** (`{root}`) | **{_fmt(total)}** |")
    note = ("\n\nAlles oben liegt im Programmordner. **Strata** verwaltet seine Modelle selbst in seinem "
            "eigenen Ordner – die zählen hier nicht mit.")
    return "\n".join(lines) + note


def clear_temp():
    clean_temp()
    for d in (C.HF_CACHE / "xet", C.DATA / "cache"):
        shutil.rmtree(d, ignore_errors=True)
    log.info("Temp und Download-Zwischenspeicher geleert")


def remove_unused_whisper():
    from . import models
    wm = C.settings().get("whisper_model")
    freed = 0
    for name, folder, size in whisper_models():
        if name == wm:
            continue
        shutil.rmtree(folder, ignore_errors=True)
        (models.WHISPER_DIR / f".fertig-{name}").unlink(missing_ok=True)
        freed += size
        log.info("Spracherkennung %s gelöscht (%s)", name, _fmt(size))
    return freed


def remove_builtin_ki():
    from . import llm as L
    size = _size(L.BUILTIN_DIR)
    shutil.rmtree(L.BUILTIN_DIR, ignore_errors=True)
    log.info("Eingebaute KI gelöscht (%s)", _fmt(size))
    return size
