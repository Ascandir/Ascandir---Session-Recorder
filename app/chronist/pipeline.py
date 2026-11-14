"""Ablauf: Aufnahme -> Stimmen trennen -> Text -> Abschrift."""
import bisect
import datetime as dt
import json
import re
import shutil
from pathlib import Path

import numpy as np

from . import audio as A
from . import config as C
from . import diarize as D
from . import transcribe as T


def _slug(s):
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.U).strip().replace(" ", "_")
    return s[:60] or "Session"


def session_dir(title):
    d = C.SESSIONS / f"{dt.date.today().isoformat()}_{_slug(title)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prompt(grp):
    names = [s.get("charakter", "") for s in grp.get("spieler", [])] + \
            [s.get("name", "") for s in grp.get("spieler", [])]
    names = ", ".join(n for n in names if n)
    p = "Eine Dungeons-and-Dragons-Runde auf Deutsch."
    if names:
        p += f" Namen: {names}."
    if grp.get("kontext"):
        p += " " + grp["kontext"][:600]
    return p


def _assign(words, segs, spk_prefix):
    """Ordnet jedem Wort den Sprecher zu, dessen Abschnitt am besten passt."""
    starts = [s for s, e, k in segs]
    out = []
    for ws, we, txt in words:
        mid = (ws + we) / 2
        i = bisect.bisect_right(starts, mid) - 1
        best, dist = None, 1e9
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(segs):
                s, e, k = segs[j]
                d = 0 if s <= mid <= e else min(abs(mid - s), abs(mid - e))
                if d < dist:
                    best, dist = k, d
        spk = f"{spk_prefix}{best}" if best is not None and dist <= 1.5 else "?"
        out.append([ws, we, txt, spk])
    return out


def analyse(path, title, n_andere, spuren_vertauscht=False, progress=lambda f, m: None):
    """Der lange Teil. Speichert alles in analyse.json im Session-Ordner."""
    st = C.settings()
    grp = C.group()
    path = Path(str(path).strip().strip('"'))
    if not path.exists():
        raise FileNotFoundError(f"Datei nicht gefunden: {path}")
    out = session_dir(title or path.stem)
    work = out / "_arbeit"
    work.mkdir(exist_ok=True)

    progress(0.01, "Tonspuren werden gelesen ...")
    n = A.audio_streams(path)
    if n == 0:
        raise RuntimeError("In der Datei ist keine Tonspur.")
    if n >= 2:
        mic_i, dis_i = (1, 0) if spuren_vertauscht else (0, 1)
        mic = A.load_wav(A.extract(path, mic_i, work / "mikro.wav"))
        dis = A.load_wav(A.extract(path, dis_i, work / "discord.wav"))
        modus = "getrennt"
    else:
        mic = None
        dis = A.load_wav(A.extract(path, 0, work / "alles.wav"))
        modus = "gemischt"

    n_spk = int(n_andere or 0) + (1 if modus == "gemischt" and n_andere else 0)

    # Anteil am Fortschritt grob nach Rechenzeit gewichten
    def sub(a, b, msg):
        return lambda f: progress(a + (b - a) * f, msg)

    progress(0.05, "Stimmen werden getrennt ...")
    segs = D.diarize(dis, n_spk, sub(0.05, 0.25, "Stimmen werden getrennt ..."))
    clusters = sorted({k for _, _, k in segs})

    progress(0.25, "Stimmen werden wiedererkannt ...")
    profiles = C.voices()
    embs, info = {}, {}
    for k in clusters:
        parts = D.cluster_parts(segs, k)
        embs[k] = D.embed(dis, parts)
        clip = A.write_clip(dis, parts[:4], out / f"stimme_{k + 1}.wav")
        dauer = sum(e - s for s, e, kk in segs if kk == k)
        info[k] = {"clip": clip, "dauer": round(dauer, 1)}
    matches = D.match(embs, profiles, float(st["match_schwelle"]))

    prompt = _prompt(grp)
    words = []
    if mic is not None:
        mic_w = T.transcribe(mic, st["whisper_model"], prompt,
                             sub(0.27, 0.27 + 0.68 * len(mic) / (len(mic) + len(dis)),
                                 "Dein Mikro wird abgeschrieben ..."))
        words += [[s, e, t, C.ICH_ID] for s, e, t in mic_w]
        a0 = 0.27 + 0.68 * len(mic) / (len(mic) + len(dis))
    else:
        a0 = 0.27
    dis_w = T.transcribe(dis, st["whisper_model"], prompt,
                         sub(a0, 0.97, "Discord-Spur wird abgeschrieben ..."))
    words += _assign(dis_w, segs, "S")
    words.sort(key=lambda w: w[0])

    stimmen = []
    for k in clusters:
        name, sim = matches.get(k, (None, 0.0))
        stimmen.append({
            "id": f"S{k}", "nr": k + 1, "vorschlag": name or "",
            "aehnlichkeit": round(sim, 2), "clip": info[k]["clip"],
            "dauer": info[k]["dauer"],
            "embedding": embs[k].tolist() if embs[k] is not None else None,
        })
    state = {
        "titel": title or path.stem, "quelle": str(path), "modus": modus,
        "ordner": str(out), "stimmen": stimmen, "woerter": words,
        "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    with open(out / "analyse.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)
    progress(1.0, "Fertig analysiert.")
    return state


def load_state(folder):
    with open(Path(folder) / "analyse.json", encoding="utf-8") as f:
        return json.load(f)


def list_sessions():
    return sorted([p.parent.name for p in C.SESSIONS.glob("*/analyse.json")], reverse=True)


def _fmt(t):
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def build_transcript(state, mapping, lern=True):
    """mapping: {"S0": "Lena", ...}. Schreibt transkript.txt und lernt Stimmen."""
    grp = C.group()
    if lern:
        profiles = C.voices()
        for v in state["stimmen"]:
            name = mapping.get(v["id"], "")
            if name and name not in (C.IGNORIEREN, C.UNBEKANNT) and v.get("embedding"):
                D.update_profile(profiles, name, np.asarray(v["embedding"]))
        C.save("stimmen", profiles)
    state["zuordnung"] = mapping

    def label(spk):
        if spk == C.ICH_ID:
            return C.player_label(C.ICH_ID, grp)
        name = mapping.get(spk) or C.UNBEKANNT
        return None if name == C.IGNORIEREN else C.player_label(name, grp)

    lines, cur, cur_lab, last_end, start = [], [], None, -9, 0
    for ws, we, txt, spk in state["woerter"]:
        lab = label(spk)
        if lab is None:
            continue
        if lab != cur_lab or ws - last_end > 2.0 or ws - start > 90:
            if cur:
                lines.append(f"[{_fmt(start)}] {cur_lab}: {' '.join(cur)}")
            cur, cur_lab, start = [], lab, ws
        cur.append(txt)
        last_end = we
    if cur:
        lines.append(f"[{_fmt(start)}] {cur_lab}: {' '.join(cur)}")
    text = "\n".join(lines)
    out = Path(state["ordner"])
    (out / "transkript.txt").write_text(text, encoding="utf-8")
    with open(out / "analyse.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    return text
