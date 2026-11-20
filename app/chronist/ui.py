"""Bedienoberfläche im Browser (läuft nur auf deinem PC: http://127.0.0.1:7860)."""
import os
import sys
import threading
import time
from pathlib import Path

import gradio as gr

from . import config as C
from . import pipeline as P
from . import summary as S
from .recorder import REC, find_discord, list_mics
from . import updater as U
from . import llm as L
from .debuglog import log, save_report

MAX_STIMMEN = 12
SCROLL_RESULT = "() => { const e = document.getElementById('ergebnis'); if (e) e.scrollIntoView({behavior: 'smooth'}); }"
SCROLL_IF_DONE = ("() => { const box = [...document.querySelectorAll('.status-card')][0]; "
                  "if (box && box.innerText.includes('Fertig!')) { const e = document.getElementById('ergebnis'); "
                  "if (e) e.scrollIntoView({behavior: 'smooth'}); } }")
CSS = """
footer {display: none !important;}
.status-card {border-left: 4px solid #f2a93b; background: rgba(242,169,59,.08);
              padding: 14px 18px !important; border-radius: 8px; min-height: 64px;}
.result-card {padding: 6px 4px; min-height: 120px;}
.status-small {min-height: 0 !important;}
h4 {margin-top: 18px !important; color: #f2a93b;}
"""
MAX_BILDER = 24
EXT = {".mka", ".mkv", ".mp4", ".mov", ".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}


# ---------- Hilfen ----------
def recordings():
    folder = C.recordings_dir()
    if not folder.exists():
        return []
    files = [p for p in folder.iterdir() if p.suffix.lower() in EXT and p.is_file()
             and not (p.suffix.lower() == ".mp3" and p.with_suffix(".mka").exists())]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:30]:
        t = time.strftime("%d.%m.%Y %H:%M", time.localtime(p.stat().st_mtime))
        out.append((f"{p.name}  ·  {t}  ·  {p.stat().st_size / 1e6:.0f} MB", str(p)))
    return out


def name_choices():
    g = C.group()
    names = [s["name"] for s in g.get("spieler", []) if s.get("name")]
    return names + [g.get("ich_name", "Ich"), C.UNBEKANNT, C.IGNORIEREN]


def n_spieler():
    return len([s for s in C.group().get("spieler", []) if s.get("name")])


def _open_path(path):
    """Ordner im Windows-Explorer öffnen (mit Ausweichweg)."""
    if sys.platform != "win32":
        return
    try:
        os.startfile(str(path))
    except Exception:
        import subprocess
        subprocess.Popen(["explorer", str(path)])


def open_folder(state):
    target, hint = None, ""
    if state and state.get("ordner"):
        p = Path(state["ordner"])
        if p.exists():
            target = p
        else:   # z. B. durch den Umzug in Gruppenordner verschoben
            cand = C.sessions_dir(state.get("gruppe")) / p.name if state.get("gruppe") in C.list_groups() \
                else C.sessions_dir() / p.name
            if cand.exists():
                target = cand
                state["ordner"] = str(cand)
    if target is None:
        target = C.sessions_dir()
        hint = " (gerade ist keine Session geöffnet – das ist der Session-Ordner der Gruppe)"
    try:
        _open_path(target)
    except Exception as e:
        log.exception("Ordner öffnen fehlgeschlagen")
        raise gr.Error(f"Ordner konnte nicht geöffnet werden: {e}")
    log.info("Ordner geöffnet: %s", target)
    return f"📂 Geöffnet: `{target}`{hint}"


def _voice_updates(state):
    ups = []
    stimmen = (state or {}).get("stimmen", [])
    zu = (state or {}).get("zuordnung", {})
    for i in range(MAX_STIMMEN):
        if i < len(stimmen):
            v = stimmen[i]
            min_ = v["dauer"] / 60
            erkannt = (f"erkannt als **{v['vorschlag']}** ({int(v['aehnlichkeit'] * 100)} % sicher)"
                       if v["vorschlag"] else "noch unbekannt – bitte zuordnen")
            val = zu.get(v["id"]) or v["vorschlag"] or None
            ups += [gr.update(visible=True),
                    gr.update(value=f"**Stimme {v['nr']}** · {min_:.1f} Min. gesprochen · {erkannt}"),
                    gr.update(value=v["clip"]),
                    gr.update(choices=name_choices(), value=val if val in name_choices() else None)]
        else:
            ups += [gr.update(visible=False), gr.update(), gr.update(value=None), gr.update(value=None)]
    return ups


# ---------- Aktionen ----------
BUSY = threading.Lock()


def _busy_guard(fn):
    """Verhindert, dass ein zweiter Klick unbemerkt hinter einem hängenden Durchlauf wartet."""
    import functools

    @functools.wraps(fn)
    def wrap(*a, **k):
        if not BUSY.acquire(blocking=False):
            raise gr.Error("Es läuft schon eine Auswertung oder ein KI-Test. Warte kurz oder klick auf "
                           "⏹ Abbrechen.")
        try:
            log.info("Start: %s", fn.__name__)
            return fn(*a, **k)
        finally:
            BUSY.release()
            log.info("Ende: %s", fn.__name__)
    return wrap


def do_cancel():
    log.info("Abbrechen gedrückt")
    try:
        L.stop_all()
    except Exception:
        log.exception("Fehler beim Beenden der KI")
    if BUSY.locked():
        try:
            BUSY.release()
        except RuntimeError:
            pass
    return "⏹ Abgebrochen. Eine gestartete KI wurde beendet."


@_busy_guard
def do_analyse(rec, title, n_andere, vertauscht, with_comic, progress=gr.Progress()):
    """Ein Klick: Abschrift → Stimmen → (nur bei Unbekannten anhalten) → KI an → Chronik → KI aus."""
    if not rec:
        raise gr.Error("Bitte zuerst eine Aufnahme auswählen.")
    if not n_andere or int(n_andere) < 1:
        n_andere = n_spieler() or 0
    try:
        state = P.analyse(rec, title, n_andere, vertauscht,
                          progress=lambda f, m: progress(f * 0.6, desc=m))
    except Exception as e:
        log.exception("Analyse fehlgeschlagen")
        raise gr.Error(str(e))
    unbekannt = sum(1 for v in state["stimmen"] if not v["vorschlag"])
    if unbekannt:
        msg = (f"⏸️ Fast geschafft – **{unbekannt} Stimme(n) kenne ich noch nicht.** Hör kurz rein, "
               "wähl den Namen und klick auf **Weiter**. Ab dem nächsten Mal erkenne ich sie von selbst.")
        return [msg, state, gr.update(visible=True)] + _voice_updates(state) + [gr.update(), gr.update(), gr.update()]
    names = [v["vorschlag"] for v in state["stimmen"]]
    state, text, summ, comic = _finish(state, with_comic, names, lambda f, m: progress(0.6 + f * 0.4, desc=m))
    msg = ("✅ Fertig! Alle Stimmen wiedererkannt – hier ist eure Chronik." if not summ.startswith("⚠️")
           else "⚠️ Abschrift fertig, aber die KI hatte ein Problem – Details unten.")
    return [msg, state, gr.update(visible=False)] + _voice_updates(state) + [text, summ, comic]


def do_load(name):
    if not name:
        raise gr.Error("Keine Session gewählt.")
    state = P.load_state(C.sessions_dir() / name)
    folder = Path(state["ordner"])
    zf = folder / "zusammenfassung.md"
    tr = folder / "transkript.txt"
    cf = folder / "comic_vorlage.md"
    return ([f"Session **{state['titel']}** geladen.", state, gr.update(visible=True)]
            + _voice_updates(state)
            + [zf.read_text(encoding="utf-8") if zf.exists() else "",
               tr.read_text(encoding="utf-8") if tr.exists() else "",
               cf.read_text(encoding="utf-8") if cf.exists() else ""])


@_busy_guard
def do_resplit(state, n, progress=gr.Progress()):
    if not state:
        raise gr.Error("Bitte zuerst analysieren oder eine Session öffnen.")
    try:
        state = P.resplit(state, n or n_spieler(), progress=lambda f, m: progress(f, desc=m))
    except Exception as e:
        log.exception("Neu trennen fehlgeschlagen")
        raise gr.Error(str(e))
    msg = (f"🔄 Neu getrennt: **{len(state['stimmen'])} Stimmen** zum Zuordnen. Mehrere Stimmen dürfen "
           "dieselbe Person sein – ordne einfach jede zu und klick auf **Weiter**.")
    return [msg, state, gr.update(visible=True)] + _voice_updates(state) + [msg]


@_busy_guard
def do_finish(state, with_comic, *names, progress=gr.Progress()):
    if not state:
        raise gr.Error("Bitte zuerst analysieren oder eine Session laden.")
    state, text, summ, comic = _finish(state, with_comic, names, lambda f, m: progress(f, desc=m))
    ok = not summ.startswith("⚠️")
    msg = "✅ Fertig! Hier ist eure Chronik." if ok else "⚠️ Die KI hatte ein Problem – Details im Ergebnis-Bereich."
    return state, text, summ, comic, msg, gr.update(visible=not ok), msg


def _finish(state, with_comic, names, pr):
    mapping = {}
    for v, n in zip(state["stimmen"], names):
        mapping[v["id"]] = n or C.UNBEKANNT
    text = P.build_transcript(state, mapping)
    comic = ""
    try:
        with L.KI(pr) as url:          # startet die KI bei Bedarf und beendet sie danach
            summ = S.summarize(state, url, progress=pr)
            if with_comic:
                comic = S.comic(state, url, progress=pr)
    except Exception as e:
        log.exception("Zusammenfassung fehlgeschlagen")
        return (state, text, f"⚠️ Die Abschrift ist fertig, aber die Zusammenfassung ging schief:\n\n"
                f"`{e}`\n\nÖffne die Session unter „Erweitert → Gespeicherte Session öffnen“ und klick "
                "auf **Weiter** – die Abschrift muss nicht wiederholt werden.", "")
    return state, text, summ, comic


@_busy_guard
def do_comic(state, progress=gr.Progress()):
    if not state:
        raise gr.Error("Keine Session geladen.")
    pr = lambda f, m: progress(f, desc=m)
    try:
        with L.KI(pr) as url:
            return S.comic(state, url, progress=pr)
    except Exception as e:
        raise gr.Error(str(e))


def dm_choices(g=None):
    g = g or C.group()
    return ([(f"Ich ({g.get('ich_name', 'Ich')})", C.ICH_ID)]
            + [(sp["name"], sp["name"]) for sp in g.get("spieler", []) if sp.get("name")])


FIG = "fig:"          # Schlüssel-Präfix für Gefährten & weitere Figuren


def owner_choices(g=None):
    g = g or C.group()
    return [g.get("ich_name", "Ich")] + [sp["name"] for sp in g.get("spieler", []) if sp.get("name")]


def fig_label(f):
    art = f.get("art") or "Figur"
    return f"{f['name']} ({art} von {f.get('besitzer') or '?'})"


def save_group(ich_name, ich_rolle, dm, table, kontext, fig_table=None):
    old = C.group()
    looks = {sp.get("name"): sp.get("aussehen", "") for sp in old.get("spieler", [])}
    rows = []
    for r in (table or []):
        r = list(r) + ["", ""]
        if str(r[0] or "").strip():
            n = str(r[0]).strip()
            rows.append({"name": n, "charakter": str(r[1] or "").strip(), "aussehen": looks.get(n, "")})
    names = {r["name"] for r in rows}
    if dm != C.ICH_ID and dm not in names:
        dm = C.ICH_ID
    # Gefährten & weitere Figuren (Aussehen bleibt beim Namen erhalten)
    old_figs = {f.get("name"): f for f in old.get("figuren", [])}
    owners = [ich_name.strip() or "Ich"] + [r["name"] for r in rows]
    figs, warn = [], []
    for r in (fig_table if fig_table is not None else
              [[f.get("besitzer"), f.get("name"), f.get("art")] for f in old.get("figuren", [])]):
        r = list(r) + ["", "", ""]
        name = str(r[1] or "").strip()
        if not name:
            continue
        owner = str(r[0] or "").strip()
        if owner and owner not in owners:
            warn.append(f"„{owner}“ (bei {name}) ist kein Mitglied der Gruppe")
        figs.append({"besitzer": owner, "name": name, "art": str(r[2] or "").strip(),
                     "aussehen": old_figs.get(name, {}).get("aussehen", "")})
    g = {"ich_name": ich_name.strip() or "Ich", "ich_rolle": ich_rolle.strip(), "dm": dm,
         "spieler": rows, "kontext": kontext, "bilder": old.get("bilder", {}),
         "ich_aussehen": old.get("ich_aussehen", ""), "figuren": figs}
    C.save_group(g)
    wer = "Du bist der DM." if dm == C.ICH_ID else f"{dm} ist der DM."
    msg = f"Gespeichert: {len(rows)} Spieler, {len(figs)} weitere Figuren. {wer}"
    if warn:
        msg += "  \n⚠️ " + "; ".join(warn)
    return ([msg, gr.update(value=len(rows)), gr.update(choices=dm_choices(g), value=dm)] + image_updates())


# ---------- Gruppen ----------
def group_choices():
    return C.list_groups()


def do_create_group(name):
    if not (name or "").strip():
        raise gr.Error("Bitte einen Namen für die neue Gruppe eingeben.")
    try:
        n = C.create_group(name)
    except ValueError as e:
        raise gr.Error(str(e))
    C.set_active_group(n)
    log.info("Gruppe angelegt: %s", n)
    return f"➕ Gruppe **{n}** angelegt – trag jetzt die Mitspieler ein.", ""


def do_rename_group(new_name):
    old = C.active_group()
    if REC.running:
        raise gr.Error("Während einer Aufnahme kann die Gruppe nicht umbenannt werden.")
    try:
        n = C.rename_group(old, new_name)
    except ValueError as e:
        raise gr.Error(str(e))
    log.info("Gruppe umbenannt: %s → %s", old, n)
    return f"✏️ **{old}** heißt jetzt **{n}** – auch die Ordner für Aufnahmen und Sessions wurden umbenannt.", ""


def do_delete_group(confirmed):
    if not confirmed:
        return "Abgebrochen.", gr.update()
    name = C.active_group()
    try:
        C.delete_group(name)
    except ValueError as e:
        raise gr.Error(str(e))
    log.info("Gruppe gelöscht: %s", name)
    return (f"🗑️ Gruppe **{name}** gelöscht. Ihre Aufnahmen und Sessions liegen weiter in den Ordnern "
            f"`Aufnahmen\\{name}` und `Sessions\\{name}`."), gr.update()


def do_switch_group(name):
    if name and name in C.list_groups():
        if REC.running:
            raise gr.Error("Während einer Aufnahme kann die Gruppe nicht gewechselt werden.")
        C.set_active_group(name)
        log.info("Aktive Gruppe: %s", name)


def group_refresh(*radio_vals):
    """Alles, was von der aktiven Gruppe abhängt, neu befüllen."""
    a = C.active_group()
    g = C.group()
    rows = [[x["name"], x.get("charakter", "")] for x in g.get("spieler", [])] or [["", ""]] * 5
    out = [gr.update(choices=group_choices(), value=a) for _ in range(3)] + [
           gr.update(choices=recordings(), value=(recordings()[0][1] if recordings() else None)),
           gr.update(choices=P.list_sessions(), value=None),
           gr.update(value=n_spieler() or 4), gr.update(value=n_spieler() or 4),
           g["ich_name"], g["ich_rolle"], gr.update(choices=dm_choices(g), value=g.get("dm", C.ICH_ID)),
           rows, [[f.get("besitzer", ""), f.get("name", ""), f.get("art", "")] for f in g.get("figuren", [])]
           or [["", "", ""]] * 3, g.get("kontext", ""), gr.update(choices=list(C.voices()), value=None),
           f"Gruppe: **{a}** · Aufnahmen landen in `{C.recordings_dir()}`"]
    names = name_choices()
    out += [gr.update(choices=names, value=None) for _ in range(MAX_STIMMEN)]
    return out + image_updates()


# ---------- Charakterbilder ----------
def persons():
    """[(Spielername, Beschriftung)] – du selbst zuerst."""
    g = C.group()
    out = [(g.get("ich_name", "Ich"), C.player_label(C.ICH_ID, g))]
    for sp in g.get("spieler", []):
        if sp.get("name"):
            out.append((sp["name"], C.player_label(sp["name"], g)))
    for f in g.get("figuren", []):
        if f.get("name"):
            out.append((FIG + f["name"], "🐾 " + fig_label(f)))
    return out[:MAX_BILDER]


def appearance(name, g=None):
    g = g or C.group()
    if name.startswith(FIG):
        return next((f.get("aussehen", "") for f in g.get("figuren", []) if f.get("name") == name[len(FIG):]), "")
    if name == g.get("ich_name"):
        return g.get("ich_aussehen", "")
    return next((sp.get("aussehen", "") for sp in g.get("spieler", []) if sp.get("name") == name), "")


def save_look(i, text):
    ps = persons()
    if i >= len(ps):
        return ""
    name, lab = ps[i]
    g = C.group()
    if name.startswith(FIG):
        for f in g.get("figuren", []):
            if f.get("name") == name[len(FIG):]:
                f["aussehen"] = (text or "").strip()
    elif name == g.get("ich_name"):
        g["ich_aussehen"] = (text or "").strip()
    else:
        for sp in g.get("spieler", []):
            if sp.get("name") == name:
                sp["aussehen"] = (text or "").strip()
    C.save_group(g)
    return f"Aussehen von {lab} gespeichert."


def image_updates():
    """Updates für alle Charakter-Karten: erst die Bilder, dann die Aussehen-Felder."""
    ps = persons()
    g = C.group()
    imgs, looks = [], []
    for i in range(MAX_BILDER):
        if i < len(ps):
            name, lab = ps[i]
            imgs.append(gr.update(visible=True, label=lab, value=C.image_path(name)))
            looks.append(gr.update(visible=True, label=f"Aussehen – {lab}", value=appearance(name, g)))
        else:
            imgs.append(gr.update(visible=False, value=None))
            looks.append(gr.update(visible=False, value=""))
    return imgs + looks


def save_image(i, path):
    ps = persons()
    if i >= len(ps):
        return ""
    name, lab = ps[i]
    g = C.group()
    bilder = g.setdefault("bilder", {})
    if not path:
        old = bilder.pop(name, None)
        if old:
            (C.images_dir() / old).unlink(missing_ok=True)
        C.save_group(g)
        return f"Bild von {lab} entfernt."
    from PIL import Image
    safe = "".join(c if c.isalnum() else "_" for c in name) or "spieler"
    fname = f"{safe}.png"
    img = Image.open(path)
    img.thumbnail((1024, 1024))
    img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(C.images_dir() / fname)
    bilder[name] = fname
    C.save_group(g)
    return f"Bild von {lab} gespeichert."


def save_settings(url, model, wm, ordner, schwelle, design, upd_start,
                  ki_mode, strata_cmd, strata_stop, strata_wait):
    st = C.settings()
    st.update({"ki_modus": ki_mode, "strata_befehl": strata_cmd.strip(),
               "strata_beenden": bool(strata_stop), "strata_wartezeit": int(strata_wait) * 60})
    st.update({"llm_url": url.strip(), "llm_model": model.strip(), "whisper_model": wm,
               "aufnahme_ordner": ordner.strip(), "match_schwelle": float(schwelle),
               "design": design, "update_beim_start": bool(upd_start)})
    C.save("einstellungen", st)
    return "Gespeichert.", ki_status()


@_busy_guard
def test_llm(progress=gr.Progress()):
    try:
        log.info("KI-Test gestartet")
        ans = L.test(lambda f, m: progress(f, desc=m))
        log.info("KI-Test ok: %s", ans)
        return f"✅ {ans}\n\nDie KI wurde danach wieder beendet (falls das Tool sie gestartet hat)."
    except Exception as e:
        log.exception("KI-Test fehlgeschlagen")
        return f"❌ {e}\n\nKlick auf **🐞 Debug-Log erstellen** und schick mir die Datei."


def ki_status():
    st = C.settings()
    mode = L.resolved_mode()
    pre = f"**Automatisch gewählt: {mode}.** " if st.get("ki_modus", "Automatisch") == "Automatisch" else ""
    return pre + _ki_status(mode, st)


def _ki_status(mode, st):
    if mode == "Strata":
        d = L.strata_description()
        if L.strata_cli():
            p = st.get("strata_preset") or "(erstes gespeichertes)"
            d += f"  \nPreset: **{p}** – Start ohne Fenster, Stopp über `strata launch --stop`."
        return d
    if mode == "Eingebaute KI":
        return ("✅ Eingebaute KI ist installiert." if L.builtin_installed() else
                "Die eingebaute KI wird beim ersten Bedarf automatisch geladen (ca. 9 GB) – "
                "oder jetzt mit dem Knopf unten.")
    return f"Es wird die KI unter `{st['llm_url']}` genutzt. Sie muss selbst gestartet sein."


def do_storage_refresh():
    from . import storage
    return storage.overview()


def do_clear_temp():
    from . import storage
    storage.clear_temp()
    return storage.overview(), "🧹 Temporäre Dateien und Download-Zwischenspeicher geleert."


def do_remove_whisper():
    from . import storage
    freed = storage.remove_unused_whisper()
    return storage.overview(), (f"🗑️ {storage._fmt(freed)} frei geworden." if freed
                                else "Es gibt keine ungenutzten Spracherkennungs-Modelle.")


def do_remove_builtin():
    from . import storage
    if L.resolved_mode() == "Eingebaute KI":
        raise gr.Error("Die eingebaute KI ist gerade ausgewählt. Stell unter „Welche KI?“ erst auf Strata um.")
    freed = storage.remove_builtin_ki()
    return storage.overview(), (f"🗑️ Eingebaute KI gelöscht – {storage._fmt(freed)} frei." if freed
                                else "Die eingebaute KI war nicht installiert.")


def do_open_root():
    _open_path(C.ROOT)
    return f"📂 `{C.ROOT}`"


def do_debug():
    try:
        path, text = save_report()
    except Exception as e:
        raise gr.Error(f"Debug-Log konnte nicht erstellt werden: {e}")
    return (f"✅ Erstellt – lade die Datei unten herunter oder kopier den Text. (Liegt auch im Programmordner unter `daten\\debug`.)",
            gr.update(value=text, visible=True), gr.update(value=path, visible=True))


def pick_strata():
    if WINDOW is None:
        raise gr.Error("Die Dateiauswahl gibt es nur im Programmfenster – trag den Pfad bitte ins Feld ein.")
    import webview
    res = WINDOW.create_file_dialog(webview.FileDialog.OPEN if hasattr(webview, "FileDialog") else webview.OPEN_DIALOG,
                                    file_types=("Programme (*.exe;*.bat;*.cmd)", "Alle Dateien (*.*)"))
    if not res:
        return gr.update(), ki_status()
    path = res[0] if isinstance(res, (list, tuple)) else res
    st = C.settings(); st["strata_befehl"] = path; C.save("einstellungen", st)
    return gr.update(value=path), ki_status()


def do_find_strata():
    L.find_strata(force=True)
    names, raw = L.list_presets()
    cur = C.settings().get("strata_preset", "")
    return (ki_status(), gr.update(choices=["(automatisch)"] + names,
                                   value=cur if cur in names else "(automatisch)"),
            f"```\n{raw or 'keine Ausgabe'}\n```")


def save_preset(name):
    st = C.settings()
    st["strata_preset"] = "" if name in (None, "(automatisch)") else name
    C.save("einstellungen", st)
    return ki_status()


def do_install_builtin(progress=gr.Progress()):
    try:
        L.install_builtin(lambda f, m: progress(f, desc=m))
    except Exception as e:
        raise gr.Error(f"Installation fehlgeschlagen: {e}")
    return ki_status()


def delete_voice(name):
    v = C.voices()
    if name in v:
        del v[name]
        C.save_voices(v)
    return gr.update(choices=list(v), value=None), f"Stimmprofil '{name}' gelöscht." if name else ""


# ---------- Aufnahme ----------
def _fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _bar(level):
    n = max(0, min(20, int(level * 20)))
    return "▰" * n + "▱" * (20 - n)


def rec_status():
    if not REC.running:
        pid = find_discord()
        d = "✅ Discord läuft." if pid else "⚠️ Discord läuft gerade nicht – vor der Aufnahme starten."
        return f"### ⏺ Bereit\n{d}"
    m, dsc = REC.levels()
    txt = (f"### 🔴 Aufnahme läuft · {_fmt_time(REC.elapsed())}\n"
           f"`Mikro   {_bar(m)}`  \n`Discord {_bar(dsc)}`\n\n{REC.discord_status}")
    if REC.error:
        txt += f"\n\n⚠️ {REC.error}"
    return txt


def mic_choices():
    return list_mics()


def do_rec_start(mic, name):
    if REC.running:
        return rec_status(), gr.update(), gr.update(), gr.update(), gr.update()
    if not (name or "").strip():
        raise gr.Error("Bitte zuerst einen Namen für die Session eingeben.")
    try:
        idx = int(mic) if mic not in (None, "") else None
        REC.start(idx, name)
        st = C.settings(); st["mikro"] = mic if mic is not None else ""; C.save("einstellungen", st)
    except Exception as e:
        raise gr.Error(f"Aufnahme konnte nicht starten: {e}")
    return (rec_status(), gr.update(interactive=False), gr.update(interactive=True),
            gr.Timer(active=True), gr.update(interactive=False))


def do_rec_stop():
    path = REC.stop()
    if not path:
        raise gr.Error("Beim Speichern ist etwas schiefgegangen: " + (REC.error or "unbekannt"))
    warn = f"\n\n⚠️ Das Mikro hat {REC.overflows}× kurz gestockt." if getattr(REC, "overflows", 0) > 5 else ""
    msg = (f"### ✅ Aufnahme gespeichert\n`{Path(path).name}` – zum Anhören liegt daneben "
           f"`{Path(path).with_suffix('.mp3').name}`.{warn}\n\n"
           "Wenn du so weit bist: Tab **Session auswerten** – die Aufnahme ist schon ausgewählt.")
    return (msg, gr.update(interactive=True), gr.update(interactive=False), gr.Timer(active=False),
            gr.update(choices=recordings(), value=path),
            gr.update(value=getattr(REC, "session_name", "")),
            gr.update(interactive=True, value=""))


def update_text(info):
    if "fehler" in info:
        return f"Du hast Version **{U.VERSION}**. Update-Prüfung: {info['fehler']}", gr.update(visible=False)
    if info.get("neu"):
        txt = f"### ⬆️ Neue Version {info['version']} verfügbar (du hast {U.VERSION})"
        if info.get("text"):
            txt += "\n\n" + info["text"]
        return txt, gr.update(visible=True)
    return f"✅ Du hast die neueste Version (**{U.VERSION}**).", gr.update(visible=False)


def do_check_update():
    return update_text(U.check(force=True))


def do_update(progress=gr.Progress()):
    if REC.running:
        raise gr.Error("Erst die Aufnahme beenden – dann aktualisieren.")
    try:
        v = U.install(progress=lambda f, m: progress(f, desc=m))
    except Exception as e:
        raise gr.Error(f"Update fehlgeschlagen: {e}")
    return (f"⬆️ Version {v} wird installiert. Der SessionRecorder schließt sich jetzt und startet "
            "nach dem Update von selbst neu. Du kannst diesen Tab schließen."), gr.update(visible=False)


WINDOW = None


def shutdown():
    """Beendet wirklich alles: Aufnahme (wird gespeichert), gestartete KI, Unterprozesse, Python."""
    try:
        if REC.running:
            REC.stop()
    except Exception:
        pass
    try:
        L.stop_all()
    except Exception:
        pass
    try:
        import psutil
        me = psutil.Process()
        kids = me.children(recursive=True)
        for k in kids:
            try:
                k.terminate()
            except Exception:
                pass
        psutil.wait_procs(kids, timeout=3)
        for k in kids:
            try:
                k.kill()
            except Exception:
                pass
    except Exception:
        pass
    os._exit(0)


def close_app():
    if WINDOW is not None:
        try:
            WINDOW.destroy()      # webview.start() kehrt zurück → shutdown()
            return
        except Exception:
            pass
    shutdown()


def do_quit():
    if REC.running:
        raise gr.Error("Erst die Aufnahme beenden.")
    threading.Timer(0.5, close_app).start()
    return "👋 Der SessionRecorder wird beendet …"


# ---------- Oberfläche ----------
def build():
    st, grp = C.settings(), C.group()
    theme = gr.themes.Soft(primary_hue="amber", neutral_hue="stone",
                           font=["Segoe UI", "system-ui", "sans-serif"],
                           font_mono=["Consolas", "monospace"]).set(
        body_background_fill_dark="#15110e",
        block_background_fill_dark="#211b16",
        input_background_fill_dark="#2a231d",
        border_color_primary_dark="#3a3027",
    )
    with gr.Blocks(title="Ascandir - SessionRecorder") as app:
        with gr.Row():
            gr.Markdown(f"# 🎲 Ascandir · SessionRecorder <sup style='font-size:.45em;opacity:.6'>v{U.VERSION}</sup>\n"
                        "Aufnahme rein, Chronik raus – alles bleibt auf deinem PC.")
            upd_btn = gr.Button("⬆️ Updates", scale=0, min_width=110, size="sm")
            quit_btn = gr.Button("⏻ Beenden", scale=0, min_width=110, size="sm")
        quit_md = gr.Markdown()
        with gr.Group(visible=True):
            upd_md = gr.Markdown()
            upd_go = gr.Button("⬆️ Jetzt aktualisieren", variant="primary", visible=False)
        state = gr.State(None)

        with gr.Tabs() as tabs:
          with gr.Tab("🎙️ Aufnahme", id="aufnahme"):
            gr.Markdown("Nimmt **nur Ton** auf: dein Mikro und den Ton von Discord – auf getrennten Spuren. "
                        "Keine Kamera, kein Bildschirm, keine Foundry-Musik. Braucht kaum Leistung.")
            with gr.Row():
                rec_group = gr.Dropdown(group_choices(), value=C.active_group(), label="Gruppe",
                                        info="Die Aufnahme landet im Ordner dieser Gruppe", scale=5,
                                        interactive=not REC.running)
            rec_group_md = gr.Markdown(f"Gruppe: **{C.active_group()}** · Aufnahmen landen in "
                                       f"`{C.recordings_dir()}`")
            mics = mic_choices()
            saved = st.get("mikro", "")
            mic_val = next((v for _, v in mics if str(v) == str(saved)), mics[0][1] if mics else None)
            with gr.Row():
                mic = gr.Dropdown(choices=mics, value=mic_val, label="Dein Mikrofon", scale=5)
                mic_ref = gr.Button("🔄", scale=0, min_width=50)
            sess_name = gr.Textbox(label="Name der Session",
                                   placeholder="z. B. Session 12 – Der Keller unter dem Grauen Eber",
                                   info="So heißt später die Aufnahme-Datei (mit Datum davor)",
                                   interactive=not REC.running)
            rec_md = gr.Markdown(rec_status())
            with gr.Row():
                rec_start = gr.Button("⏺ Aufnahme starten", variant="primary", size="lg",
                                      interactive=not REC.running)
                rec_stop = gr.Button("⏹ Aufnahme beenden", variant="stop", size="lg",
                                     interactive=REC.running)
            gr.Markdown("Du kannst den Browser-Tab während der Aufnahme schließen – aufgenommen wird "
                        "trotzdem weiter. Zum Beenden einfach den SessionRecorder wieder öffnen.")
            timer = gr.Timer(1.0, active=REC.running)

          with gr.Tab("📜 Session auswerten", id="auswerten"):
            gr.Markdown("#### ① Aufnahme wählen")
            with gr.Group():
                ev_group = gr.Dropdown(group_choices(), value=C.active_group(), label="Gruppe")
                with gr.Row():
                    rec = gr.Dropdown(choices=recordings(), label="Aufnahme (neueste zuerst)",
                                      allow_custom_value=True, scale=5)
                    refresh = gr.Button("🔄", scale=0, min_width=50)
                with gr.Row():
                    title = gr.Textbox(label="Titel der Session", placeholder="z. B. Session 12 – Der Keller")
                    n_andere = gr.Number(label="Leute außer dir im Discord",
                                         value=n_spieler() or 4, precision=0, minimum=1,
                                         info="Standard: alle aus deiner Gruppe")
                with_comic = gr.Checkbox(label="Comic-Vorlage mit erstellen", value=True)
            with gr.Accordion("Erweitert", open=False):
                vertauscht = gr.Checkbox(label="Spuren vertauscht (Discord ist Spur 1, Mikro Spur 2)")
                with gr.Row():
                    alte = gr.Dropdown(choices=P.list_sessions(), label="Gespeicherte Session öffnen")
                    load_btn = gr.Button("Öffnen", scale=0)

            gr.Markdown("#### ② Auswerten")
            with gr.Row():
                go = gr.Button("▶ Analysieren", variant="primary", size="lg", scale=5)
                cancel_btn = gr.Button("⏹ Abbrechen", variant="stop", size="lg", scale=1)
            status = gr.Markdown("Klick auf **▶ Analysieren** – der Rest passiert von selbst: Abschrift, "
                                 "Stimmen erkennen, KI starten, Chronik schreiben, KI beenden. "
                                 "Bei 3 Stunden Aufnahme grob 30–75 Minuten.",
                                 elem_classes=["status-card"])

            with gr.Column(visible=False) as voice_box:
                gr.Markdown("#### Wer ist wer?\nDas Tool bietet lieber eine Stimme zu viel an als eine zu wenig "
                            "– **mehrere Stimmen dürfen dieselbe Person sein** (z. B. wenn jemand mal "
                            "laut, mal leise spricht). Hör rein und ordne jede zu. Musik-Bots o. Ä. auf "
                            "*(ignorieren)*. Fehlt jemand? Im Tab **Gruppe** eintragen und speichern.")
                with gr.Accordion("Passt nicht? Stimmen neu trennen (ohne neue Abschrift)", open=False):
                    gr.Markdown("Fehlt eine Person ganz oder sind zwei Leute in einer Stimme gelandet? "
                                "Dann neu trennen – das dauert nur ein paar Minuten, die Abschrift bleibt.")
                    with gr.Row():
                        resplit_n = gr.Number(label="Leute außer dir im Discord", value=n_spieler() or 4,
                                              precision=0, minimum=1, scale=2)
                        resplit_btn = gr.Button("🔄 Stimmen neu trennen", scale=1)
                    resplit_status = gr.Markdown(elem_classes=["status-card", "status-small"])
                rows = []
                for i in range(MAX_STIMMEN):
                    with gr.Group(visible=False) as r:
                        with gr.Row():
                            md = gr.Markdown()
                            au = gr.Audio(type="filepath", interactive=False, show_label=False)
                        dd = gr.Radio(choices=name_choices(), label="Das ist …")
                    rows.append((r, md, au, dd))
                finish = gr.Button("Weiter ▶", variant="primary", size="lg")
                finish_status = gr.Markdown("Nach **Weiter** startet das Tool die KI und schreibt die Chronik – "
                                            "der Fortschritt erscheint hier.", elem_classes=["status-card"])

            gr.Markdown("#### ③ Ergebnis", elem_id="ergebnis")
            with gr.Tabs():
                with gr.Tab("📖 Chronik"):
                    summary = gr.Markdown("*Noch keine Chronik – sie erscheint hier nach der Auswertung.*",
                                          elem_classes=["result-card"])
                with gr.Tab("🖼️ Comic-Vorlage"):
                    comic_md = gr.Markdown("*Noch keine Comic-Vorlage.*", elem_classes=["result-card"])
                    comic_btn = gr.Button("🖼️ Comic-Vorlage (neu) erstellen")
                with gr.Tab("📝 Abschrift"):
                    transcript = gr.Textbox(lines=20, show_label=False, buttons=["copy"],
                                            placeholder="Die komplette Abschrift erscheint hier.")
            with gr.Row():
                folder_btn = gr.Button("📂 Session-Ordner öffnen", scale=0, min_width=280)
                folder_md = gr.Markdown()

          with gr.Tab("👥 Gruppe", id="gruppe"):
            with gr.Group():
                with gr.Row():
                    grp_sel = gr.Dropdown(group_choices(), value=C.active_group(), label="Gruppe", scale=3)
                    new_name = gr.Textbox(label="Neue Gruppe", placeholder="Name, z. B. Montagsrunde", scale=2)
                    new_btn = gr.Button("➕ Anlegen", scale=1)
                with gr.Row():
                    ren_name = gr.Textbox(label="Gruppe umbenennen", placeholder="neuer Name", scale=3)
                    ren_btn = gr.Button("✏️ Umbenennen", scale=1)
                    del_btn = gr.Button("🗑️ Gruppe löschen", variant="stop", scale=1)
            grp_status = gr.Markdown()
            gr.Markdown("Trag hier die Runde ein. Die Charakternamen helfen auch der "
                        "Spracherkennung bei der Schreibweise. Jede Gruppe hat eigene Mitspieler, "
                        "eigene gelernte Stimmen und eigene Ordner.")
            with gr.Row():
                ich_name = gr.Textbox(label="Dein Name", value=grp["ich_name"])
                ich_rolle = gr.Textbox(label="Dein Charakter", value=grp["ich_rolle"],
                                       info="Leer lassen, wenn du der DM bist")
                dm_dd = gr.Dropdown(choices=dm_choices(grp), value=grp.get("dm", C.ICH_ID),
                                    label="Wer ist der Spielleiter (DM)?",
                                    info="Seine Zeilen werden als Erzählung & NPCs behandelt")
            table = gr.Dataframe(
                headers=["Spieler", "Charakter (beim DM leer lassen)"],
                datatype=["str", "str"], type="array", interactive=True,
                value=[[s["name"], s.get("charakter", "")] for s in grp["spieler"]] or [["", ""]] * 5,
                column_count=(2, "fixed"), row_count=(5, "dynamic"))
            gr.Markdown("**🐾 Gefährten & weitere Figuren** – tierische Gefährten, Reittiere, Vertraute, "
                        "Begleiter … Unter „Gehört zu“ den Spielernamen (oder deinen) eintragen.")
            fig_table = gr.Dataframe(
                headers=["Gehört zu", "Name der Figur", "Art (z. B. Wolf-Gefährte, Reittier, Vertrauter)"],
                datatype=["str", "str", "str"], type="array", interactive=True,
                value=[[f.get("besitzer", ""), f.get("name", ""), f.get("art", "")] for f in grp.get("figuren", [])]
                      or [["", "", ""]] * 3,
                column_count=(3, "fixed"), row_count=(3, "dynamic"))
            kontext = gr.Textbox(label="Kampagne & wichtige Namen (NPCs, Orte, Götter …)",
                                 lines=4, value=grp["kontext"])
            save_g = gr.Button("Speichern", variant="primary")
            g_status = gr.Markdown()
            gr.Markdown("### 🖼️ Charaktere & Gefährten: Bild & Aussehen\nFür den Comic. Bild hineinziehen, anklicken oder mit "
                        "Strg+V einfügen; das Aussehen einfach ins Feld schreiben oder hineinkopieren. Beides wird "
                        "sofort gespeichert. Neue Spieler erscheinen hier nach dem Speichern oben.")
            img_slots, look_boxes = [], []
            ps = persons()
            for i in range(MAX_BILDER):
                vis = i < len(ps)
                with gr.Row(equal_height=True):
                    img_slots.append(gr.Image(
                        type="filepath", sources=["upload", "clipboard"], height=240, scale=1, min_width=200,
                        label=ps[i][1] if vis else "", visible=vis,
                        value=C.image_path(ps[i][0]) if vis else None))
                    look_boxes.append(gr.Textbox(
                        label=f"Aussehen – {ps[i][1]}" if vis else "Aussehen", lines=9, max_lines=30, scale=3,
                        visible=vis, value=appearance(ps[i][0]) if vis else "",
                        placeholder="z. B. Größe, Volk, Haare, Augen, Kleidung, Ausrüstung, Besonderheiten …"))
            img_status = gr.Markdown()

          with gr.Tab("⚙️ Einstellungen", id="einstellungen"):
            with gr.Row():
                dbg_btn = gr.Button("🐞 Debug-Log erstellen", variant="secondary", scale=0, min_width=220)
                dbg_md = gr.Markdown("Sammelt alle Infos zur Fehlersuche in einer Datei zum Verschicken.")
            dbg_file = gr.File(label="Debug-Log", visible=False, interactive=False)
            dbg_text = gr.Textbox(label="Inhalt", lines=12, visible=False, buttons=["copy"])
            gr.Markdown("### 🧠 Lokale KI (für Zusammenfassung & Comic)")
            ki_mode = gr.Radio(L.MODI, value=st.get("ki_modus", "Automatisch"), label="Welche KI?",
                               info="Automatisch (empfohlen): nimmt Strata, wenn es da ist – sonst die eingebaute KI. "
                                    "Beide werden nur für die Auswertung gestartet und danach wieder beendet.")
            ki_md = gr.Markdown(ki_status())
            with gr.Accordion("Strata-Einstellungen", open=False):
                strata_cmd = gr.Textbox(label="Strata-Startbefehl oder Pfad (leer = automatisch suchen)",
                                        value=st.get("strata_befehl", ""),
                                        placeholder=r"z. B. C:\Users\Tim\AppData\Local\Programs\Strata\Strata.exe  oder  strata launch")
                with gr.Row():
                    strata_preset_dd = gr.Dropdown(["(automatisch)"], value="(automatisch)",
                                                   label="Strata-Preset", scale=4,
                                                   info="Welches gespeicherte Strata-Preset soll gestartet werden?")
                    strata_find = gr.Button("🔍 Strata & Presets suchen", scale=1)
                presets_raw = gr.Markdown()
                strata_pick = gr.Button("📂 Strata-Programm selbst auswählen …")
                strata_stop = gr.Checkbox(value=st.get("strata_beenden", True),
                                          label="Strata nach der Zusammenfassung wieder beenden (gibt die Grafikkarte frei)")
                strata_wait = gr.Slider(2, 30, value=int(st.get("strata_wartezeit", 900)) // 60, step=1,
                                        label="So lange auf Strata warten (Minuten)")
            with gr.Accordion("Eingebaute KI", open=False):
                gr.Markdown("Läuft über die Grafikkarte (llama.cpp mit Vulkan) mit dem Modell Qwen3-14B. "
                            "Einmalig ca. 9 GB Download.")
                inst_ki = gr.Button("Eingebaute KI jetzt installieren")
            with gr.Accordion("Eigene Adresse / Experten", open=False):
                url = gr.Textbox(label="Adresse der KI", value=st["llm_url"],
                                 info="Strata: http://127.0.0.1:8080/v1")
                model = gr.Textbox(label="Modellname (leer = automatisch)", value=st["llm_model"])
            gr.Markdown("### 💾 Speicher")
            gr.Markdown("Alle Modelle, Downloads und Zwischendateien liegen im Programmordner – nichts im "
                        "Benutzerordner oder im Windows-Temp.")
            from . import storage as _storage
            sto_md = gr.Markdown(_storage.overview())
            with gr.Row():
                sto_refresh = gr.Button("🔄 Neu berechnen")
                sto_temp = gr.Button("🧹 Temporäre Dateien leeren")
                sto_whisper = gr.Button("🗑️ Ungenutzte Spracherkennung löschen")
                sto_builtin = gr.Button("🗑️ Eingebaute KI löschen")
                sto_open = gr.Button("📂 Programmordner öffnen")
            sto_status = gr.Markdown()
            gr.Markdown("### Weitere Einstellungen")
            wm = gr.Dropdown(["large-v3-turbo", "large-v3", "medium", "small"],
                             value=st["whisper_model"], label="Spracherkennung",
                             info="large-v3-turbo: schnell und gut. large-v3: etwas genauer, deutlich langsamer.")
            ordner = gr.Textbox(label="Ordner für Aufnahmen", value=st["aufnahme_ordner"])
            design = gr.Radio(["Dunkel", "Hell"], value=st.get("design", "Dunkel"),
                              label="Design (wirkt nach Neuladen der Seite)")
            upd_start = gr.Checkbox(value=st.get("update_beim_start", True),
                                    label="Beim Start nach Updates suchen (fragt nur die Versionsnummer bei GitHub ab)")
            schwelle = gr.Slider(0.3, 0.9, value=st["match_schwelle"], step=0.05,
                                 label="Wie sicher muss die Stimmerkennung sein?")
            with gr.Row():
                save_s = gr.Button("Speichern", variant="primary")
                test = gr.Button("KI testen (starten → fragen → beenden)")
                test_stop = gr.Button("⏹ Test abbrechen", variant="stop")
            s_status = gr.Markdown()
            gr.Markdown("### Gespeicherte Stimmen")
            with gr.Row():
                vlist = gr.Dropdown(choices=list(C.voices()), label="Stimmprofil")
                vdel = gr.Button("Löschen", variant="stop", scale=0)
            v_status = gr.Markdown()

        # ---------- Verdrahtung ----------
        mic_ref.click(lambda: gr.update(choices=mic_choices()), None, mic)
        rec_start.click(do_rec_start, [mic, sess_name], [rec_md, rec_start, rec_stop, timer, sess_name]).then(
            lambda: gr.update(interactive=not REC.running), None, rec_group)
        rec_stop.click(do_rec_stop, None, [rec_md, rec_start, rec_stop, timer, rec, title, sess_name]).then(
            lambda: gr.update(interactive=True), None, rec_group).then(
            lambda: gr.Tabs(selected="auswerten"), None, tabs)
        timer.tick(rec_status, None, rec_md)
        quit_btn.click(do_quit, None, quit_md)

        G_OUTS = [rec_group, ev_group, grp_sel, rec, alte, n_andere, resplit_n, ich_name, ich_rolle, dm_dd,
                  table, fig_table, kontext, vlist, rec_group_md] + [r[3] for r in rows] + img_slots + look_boxes
        for sel in (rec_group, ev_group, grp_sel):
            sel.input(do_switch_group, sel, None).then(group_refresh, None, G_OUTS)
        new_btn.click(do_create_group, new_name, [grp_status, new_name]).then(group_refresh, None, G_OUTS)
        ren_btn.click(do_rename_group, ren_name, [grp_status, ren_name]).then(group_refresh, None, G_OUTS)
        del_btn.click(do_delete_group, del_btn, [grp_status, del_btn],
                      js="(x) => confirm('Diese Gruppe wirklich löschen? Mitspieler, gelernte Stimmen und Bilder "
                         "der Gruppe werden entfernt – Aufnahmen und Sessions bleiben erhalten.')"
                      ).then(group_refresh, None, G_OUTS)
        upd_btn.click(do_check_update, None, [upd_md, upd_go])
        upd_go.click(do_update, None, [upd_md, upd_go])

        def startup_update():
            if not C.settings().get("update_beim_start", True):
                return "", gr.update(visible=False)
            info = U.check()
            if info.get("neu"):
                return update_text(info)
            return "", gr.update(visible=False)
        app.load(startup_update, None, [upd_md, upd_go])

        def on_load():
            g = C.group()
            rows = [[x["name"], x.get("charakter", "")] for x in g.get("spieler", [])] or [["", ""]] * 5
            return ([g["ich_name"], g["ich_rolle"],
                     gr.update(choices=dm_choices(g), value=g.get("dm", C.ICH_ID)),
                     rows, g.get("kontext", ""),
                     gr.update(choices=recordings()), rec_status(),
                     gr.update(interactive=not REC.running), gr.update(interactive=REC.running),
                     gr.Timer(active=REC.running),
                     gr.update(value=getattr(REC, "session_name", "") if REC.running else "",
                               interactive=not REC.running)] + image_updates())
        app.load(on_load, None, [ich_name, ich_rolle, dm_dd, table, kontext, rec, rec_md,
                                 rec_start, rec_stop, timer, sess_name] + img_slots + look_boxes).then(
            group_refresh, None, G_OUTS).then(lambda: gr.update(interactive=not REC.running), None, rec_group)
        voice_outs = [x for r in rows for x in r]
        refresh.click(lambda: gr.update(choices=recordings()), None, rec)
        ev_go = go.click(do_analyse, [rec, title, n_andere, vertauscht, with_comic],
                         [status, state, voice_box] + voice_outs + [transcript, summary, comic_md],
                         concurrency_limit=None, show_progress_on=[status])
        ev_go.then(None, None, None, js=SCROLL_IF_DONE)
        load_btn.click(do_load, alte, [status, state, voice_box] + voice_outs
                       + [summary, transcript, comic_md])
        alte.focus(lambda: gr.update(choices=P.list_sessions()), None, alte)
        ev_fin = finish.click(do_finish, [state, with_comic] + [r[3] for r in rows],
                              [state, transcript, summary, comic_md, status, voice_box, finish_status],
                              concurrency_limit=None, show_progress_on=[finish_status])
        ev_fin.then(None, None, None, js=SCROLL_RESULT)
        ev_comic = comic_btn.click(do_comic, state, comic_md, concurrency_limit=None)
        ev_resplit = resplit_btn.click(do_resplit, [state, resplit_n],
                                       [status, state, voice_box] + voice_outs + [resplit_status],
                                       concurrency_limit=None, show_progress_on=[resplit_status])
        cancel_btn.click(do_cancel, None, status, cancels=[ev_go, ev_fin, ev_comic, ev_resplit])
        folder_btn.click(open_folder, state, folder_md)
        save_g.click(save_group, [ich_name, ich_rolle, dm_dd, table, kontext, fig_table],
                     [g_status, n_andere, dm_dd] + img_slots + look_boxes).then(
            lambda *vals: [gr.update(choices=name_choices(), value=v if v in name_choices() else None)
                           for v in vals], [r[3] for r in rows], [r[3] for r in rows])
        for i, slot in enumerate(img_slots):
            slot.upload(lambda p, i=i: save_image(i, p), slot, img_status)
            slot.clear(lambda i=i: save_image(i, None), None, img_status)
        for i, box in enumerate(look_boxes):
            box.blur(lambda t, i=i: save_look(i, t), box, img_status)
        save_s.click(save_settings, [url, model, wm, ordner, schwelle, design, upd_start,
                                    ki_mode, strata_cmd, strata_stop, strata_wait], [s_status, ki_md])
        ki_mode.change(lambda m: (C.save("einstellungen", {**C.settings(), "ki_modus": m}), ki_status())[1],
                       ki_mode, ki_md)
        inst_ki.click(do_install_builtin, None, ki_md)
        strata_pick.click(pick_strata, None, [strata_cmd, ki_md])
        strata_find.click(do_find_strata, None, [ki_md, strata_preset_dd, presets_raw])
        strata_preset_dd.input(save_preset, strata_preset_dd, ki_md)
        dbg_btn.click(do_debug, None, [dbg_md, dbg_text, dbg_file])
        sto_refresh.click(do_storage_refresh, None, sto_md)
        sto_temp.click(do_clear_temp, None, [sto_md, sto_status])
        sto_whisper.click(do_remove_whisper, None, [sto_md, sto_status])
        sto_builtin.click(do_remove_builtin, None, [sto_md, sto_status])
        sto_open.click(do_open_root, None, sto_status)
        ev_test = test.click(test_llm, None, s_status, concurrency_limit=None)
        test_stop.click(do_cancel, None, s_status, cancels=[ev_test])
        vdel.click(delete_voice, vlist, [vlist, v_status])
        vlist.focus(lambda: gr.update(choices=list(C.voices())), None, vlist)
    return app, theme


def _open_window(url, own_server):
    """Eigenes Programmfenster (WebView2). Schließen = alles beenden."""
    global WINDOW
    import webview
    WINDOW = webview.create_window("Ascandir - SessionRecorder", url, width=1320, height=920,
                                   min_size=(980, 680), background_color="#15110e", text_select=True)

    def on_closing():
        if own_server and REC.running:
            ok = WINDOW.create_confirmation_dialog(
                "Aufnahme läuft", "Die Aufnahme läuft noch.\nAufnahme beenden, speichern und das Programm schließen?")
            if not ok:
                return False
        return True
    WINDOW.events.closing += on_closing
    webview.start(private_mode=False, storage_path=str(C.DATA / "fenster"))


def launch():
    import socket
    import webbrowser
    dark = C.settings().get("design", "Dunkel") == "Dunkel"
    url = "http://127.0.0.1:7860/" + ("?__theme=dark" if dark else "?__theme=light")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", 7860)) == 0:
            print("Der SessionRecorder läuft schon – öffne ein Fenster dazu.")
            try:
                _open_window(url, own_server=False)
            except Exception:
                webbrowser.open(url)
            return
    from . import storage
    storage.clean_temp()          # nur hier: wir sind der einzige laufende Server
    app, theme = build()
    js = ("() => { document.body.classList.add('dark'); }" if dark
          else "() => { document.body.classList.remove('dark'); }")
    app.queue().launch(server_name="127.0.0.1", server_port=7860, inbrowser=False,
                       prevent_thread_lock=True,
                       theme=theme, js=js, show_error=True, quiet=True,
                       css=CSS,
                       allowed_paths=[str(C.SESSIONS), str(C.RECORDINGS), str(C.DATA),
                                      str(Path(C.settings().get("aufnahme_ordner") or C.RECORDINGS))])
    try:
        _open_window(url, own_server=True)
    except Exception as e:      # kein WebView2 o. Ä. → Browser als Notlösung
        print("Fenster nicht möglich, öffne Browser:", e)
        webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    shutdown()
