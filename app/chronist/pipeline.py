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
from .debuglog import log


def _slug(s):
    s = re.sub(r"[^\w\- ]+", "", s, flags=re.U).strip().replace(" ", "_")
    return s[:60] or "Session"


def session_dir(title):
    d = C.sessions_dir() / f"{dt.date.today().isoformat()}_{_slug(title)}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prompt(grp):
    names = [c.get("name", "") for c in C.characters(grp)] + \
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


EXTRA_STIMMEN = 3          # ein paar Gruppen mehr als Mitspieler, damit niemand "verschluckt" wird
MIN_SEKUNDEN = 20          # kleinere Gruppen werden der ähnlichsten großen Stimme zugeschlagen


def _spread_parts(segs, k, n=4, max_len=5.0):
    """Hörprobe aus verschiedenen Zeitpunkten (Anfang, Mitte, Ende) statt aus einem Stück."""
    mine = [(s, e) for s, e, kk in segs if kk == k and e - s >= 1.2]
    if not mine:
        mine = [(s, e) for s, e, kk in segs if kk == k]
    if not mine:
        return []
    mine.sort()
    out = []
    for i in range(n):
        chunk = mine[i * len(mine) // n:(i + 1) * len(mine) // n] or mine[-1:]
        s, e = max(chunk, key=lambda x: x[1] - x[0])
        if (s, e) not in out:
            out.append((s, min(e, s + max_len)))
    return out


def _clean_parts(wins, k, n_bins=4, per_bin=2, max_total=14.0):
    """Hörprobe nur aus Stellen, die eindeutig nach dieser Person klingen und an denen niemand
    dazwischenredet – verteilt über Anfang, Mitte und Ende."""
    mine = [w for w in wins if w[2] == k and not w[4]]
    if len(mine) < 3:
        mine = [w for w in wins if w[2] == k]
    if not mine:
        return []
    cut = sorted(w[3] for w in mine)[int(len(mine) * 0.5)]        # obere Hälfte nach Eindeutigkeit
    good = sorted([w for w in mine if w[3] >= cut], key=lambda w: w[0])
    picks = []
    for b in range(n_bins):
        chunk = good[b * len(good) // n_bins:(b + 1) * len(good) // n_bins]
        picks += sorted(chunk, key=lambda w: -w[3])[:per_bin]
    picks.sort()
    parts, total = [], 0.0
    for t0, t1, *_ in picks:
        if parts and t0 <= parts[-1][1] + 0.05:
            parts[-1] = (parts[-1][0], max(parts[-1][1], t1))
        else:
            parts.append((t0, t1))
    out = []
    for s, e in parts:
        if total >= max_total:
            break
        e = min(e, s + (max_total - total))
        out.append((s, e))
        total += e - s
    return out


def _voices(dis, n_spk, out, progress):
    """Stimmen trennen, Kleinstgruppen zusammenlegen, Stimmabdrücke + Hörproben, Vorschläge."""
    st = C.settings()
    n_groups = int(n_spk) + EXTRA_STIMMEN if n_spk else 0
    if n_groups:
        segs, wins = D.diarize_fine(dis, n_groups, progress, return_windows=True)
    else:
        segs, wins = D.diarize(dis, 0, progress), []
    clusters = sorted({k for _, _, k in segs})
    log.info("Stimmtrennung: %s Gruppen (angefragt %s), %s Abschnitte", len(clusters), n_groups, len(segs))

    def parts_for(k):
        cp = _clean_parts(wins, k, n_bins=6, per_bin=3, max_total=30) if wins else []
        return cp or D.cluster_parts(segs, k)

    embs = {k: D.embed(dis, parts_for(k)) for k in clusters}
    dauer = {k: sum(e - s for s, e, kk in segs if kk == k) for k in clusters}

    # Kleinstgruppen der ähnlichsten großen Gruppe zuschlagen (Grenze wächst mit der Länge der Aufnahme)
    total = sum(dauer.values()) or 1
    grenze = max(1.5, min(MIN_SEKUNDEN, 0.015 * total))
    big = [k for k in clusters if dauer[k] >= grenze and embs[k] is not None]
    if big:
        remap = {}
        for k in clusters:
            if k in big:
                continue
            if embs[k] is None or dauer[k] < 0.8:
                remap[k] = max(big, key=lambda b: dauer[b]) if embs[k] is None else \
                    max(big, key=lambda b: float(embs[b] @ embs[k]))
                continue
            best = max(big, key=lambda b: float(embs[b] @ embs[k]))
            if float(embs[best] @ embs[k]) >= 0.45:
                remap[k] = best          # klingt nach jemandem → dazulegen
            # sonst: eigene Person, die nur wenig sagt → bleibt eine eigene Stimme
        if remap:
            log.info("Kleinstgruppen zusammengelegt: %s", remap)
            segs = [(s, e, remap.get(k, k)) for s, e, k in segs]
            wins = [(a, b, remap.get(k, k), sim, ov) for a, b, k, sim, ov in wins]
            clusters = [k for k in clusters if k not in remap]
            embs = {k: D.embed(dis, parts_for(k)) for k in clusters}
            dauer = {k: sum(e - s for s, e, kk in segs if kk == k) for k in clusters}

    # Nach Redeanteil sortieren (wer am meisten spricht, steht oben)
    clusters.sort(key=lambda k: -dauer[k])
    for old in out.glob("stimme_*.wav"):
        old.unlink(missing_ok=True)
    matches = D.match(embs, C.voices(), float(st["match_schwelle"]))
    stimmen = []
    for nr, k in enumerate(clusters, 1):
        name, sim = matches.get(k, (None, 0.0))
        parts = (_clean_parts(wins, k) if wins else []) or _spread_parts(segs, k)
        clip = A.write_clip(dis, parts, out / f"stimme_{nr}.wav", max_len=16.0)
        stimmen.append({
            "id": f"S{k}", "nr": nr, "vorschlag": name or "", "aehnlichkeit": round(sim, 2),
            "clip": clip, "dauer": round(dauer[k], 1), "clip_teile": parts,
            "embedding": embs[k].tolist() if embs[k] is not None else None,
        })
    return segs, stimmen


def _speech_parts(words, max_total=16.0, n=6):
    """Hörprobe aus den längsten zusammenhängenden Redestücken (über die Session verteilt)."""
    runs, cur = [], None
    for ws, we, *_ in words:
        if cur and ws - cur[1] <= 0.8:
            cur[1] = we
        else:
            if cur:
                runs.append(tuple(cur))
            cur = [ws, we]
    if cur:
        runs.append(tuple(cur))
    runs = [r for r in runs if r[1] - r[0] >= 1.2] or runs
    if not runs:
        return []
    k = max(1, len(runs) // n)
    pick = sorted(sorted(runs[i:i + k], key=lambda r: r[0] - r[1])[0] for i in range(0, len(runs), k))
    out, total = [], 0.0
    for a, b in sorted(pick, key=lambda r: r[0] - r[1]):
        if total >= max_total:
            break
        b = min(b, a + 5.0)
        out.append((a, b))
        total += b - a
    return sorted(out)


def _guess_player(sp, grp):
    """Vorschlag für eine Discord-Person: gespeicherte Zuordnung, sonst Namensvergleich."""
    known = (grp.get("discord") or {}).get(str(sp["id"]))
    if known:
        return known, 1.0
    names = [grp.get("ich_name", "")] + [x.get("name", "") for x in grp.get("spieler", [])]
    cand = {str(sp.get(k) or "").lower() for k in ("anzeige", "name")}
    for n in names:
        if n and any(n.lower() == c or (len(n) > 2 and n.lower() in c) for c in cand):
            return n, 0.9
    return "", 0.0


def analyse_user_tracks(path, spuren, title, spuren_vertauscht, progress):
    """Einzelspuren aus Vesktop: keine Stimmtrennung nötig – jede Spur ist genau eine Person."""
    st = C.settings()
    grp = C.group()
    out = session_dir(title or path.stem)
    work = out / "_arbeit"
    work.mkdir(exist_ok=True)
    prompt = _prompt(grp)
    for old in out.glob("stimme_*.wav"):
        old.unlink(missing_ok=True)
    n = A.audio_streams(path)
    mic = A.load_wav(A.extract(path, 1 if (spuren_vertauscht and n >= 2) else 0, work / "mikro.wav"))
    tracks = [(sp, A.load_wav(A.extract(sp["file"], 0, work / f"u_{i}.wav"))) for i, sp in enumerate(spuren)]
    total = len(mic) + sum(len(a) for _, a in tracks) or 1
    done = 0
    log.info("Analyse mit %s Einzelspuren: %s", len(spuren), [sp.get("anzeige") for sp in spuren])

    def part(n_samples, msg):
        a = 0.05 + 0.9 * done / total
        b = 0.05 + 0.9 * (done + n_samples) / total
        return lambda f: progress(a + (b - a) * f, msg)

    words = [[s, e, t, C.ICH_ID] for s, e, t in T.transcribe(mic, st["whisper_model"], prompt,
                                                            part(len(mic), "Dein Mikro wird abgeschrieben ..."))]
    done += len(mic)
    stimmen = []
    for sp, aud in tracks:
        label = sp.get("anzeige") or sp.get("name")
        w = T.transcribe(aud, st["whisper_model"], prompt, part(len(aud), f"Spur von {label} wird abgeschrieben ..."))
        done += len(aud)
        if not w:
            log.info("Spur %s: kein verständlicher Text – übersprungen", label)
            continue
        sid = f"U{sp['id']}"
        words += [[s, e, t, sid] for s, e, t in w]
        parts = _speech_parts(w)
        clip = A.write_clip(aud, parts, out / f"stimme_{len(stimmen) + 1}.wav", max_len=16.0)
        emb = D.embed(aud, parts) if parts else None
        name, sim = _guess_player(sp, grp)
        stimmen.append({
            "id": sid, "nr": len(stimmen) + 1, "vorschlag": name, "aehnlichkeit": sim, "clip": clip,
            "dauer": round(sum(e - s for s, e, _ in w), 1), "clip_teile": parts,
            "embedding": emb.tolist() if emb is not None else None,
            "discord": {"id": str(sp["id"]), "name": sp.get("name"), "anzeige": sp.get("anzeige")},
        })
    stimmen.sort(key=lambda v: -v["dauer"])
    for i, v in enumerate(stimmen, 1):          # Nummern/Hörproben zur Sortierung passend machen
        if v["nr"] != i and v["clip"]:
            Path(v["clip"]).rename(out / f"_tmp_{i}.wav")
            v["clip"] = str(out / f"_tmp_{i}.wav")
        v["nr"] = i
    for v in stimmen:
        if v["clip"] and Path(v["clip"]).name.startswith("_tmp_"):
            dst = out / f"stimme_{v['nr']}.wav"
            Path(v["clip"]).rename(dst)
            v["clip"] = str(dst)
    words.sort(key=lambda x: x[0])
    state = {
        "titel": title or path.stem, "quelle": str(path), "modus": "einzelspuren",
        "vertauscht": bool(spuren_vertauscht), "n_andere": len(stimmen),
        "ordner": str(out), "stimmen": stimmen, "woerter": words, "gruppe": C.active_group(),
        "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    with open(out / "analyse.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)
    log.info("Analyse (Einzelspuren) fertig: %s Wörter, Stimmen: %s", len(words),
             [(v["discord"]["anzeige"], v["vorschlag"], v["dauer"]) for v in stimmen])
    progress(1.0, "Fertig analysiert.")
    return state


def analyse(path, title, n_andere, spuren_vertauscht=False, progress=lambda f, m: None):
    """Der lange Teil. Speichert alles in analyse.json im Session-Ordner."""
    st = C.settings()
    grp = C.group()
    path = Path(str(path).strip().strip('"'))
    if not path.exists():
        raise FileNotFoundError(f"Datei nicht gefunden: {path}")
    from .recorder import user_tracks
    spuren = user_tracks(path)
    if spuren:
        return analyse_user_tracks(path, spuren, title, spuren_vertauscht, progress)
    out = session_dir(title or path.stem)
    work = out / "_arbeit"
    work.mkdir(exist_ok=True)

    progress(0.01, "Tonspuren werden gelesen ...")
    n = A.audio_streams(path)
    log.info("Analyse: %s (%s Tonspuren, %s weitere Personen)", path.name, n, n_andere)
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

    def sub(a, b, msg):
        return lambda f: progress(a + (b - a) * f, msg)

    progress(0.05, "Stimmen werden getrennt ...")
    segs, stimmen = _voices(dis, n_spk, out, sub(0.05, 0.25, "Stimmen werden getrennt ..."))

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

    state = {
        "titel": title or path.stem, "quelle": str(path), "modus": modus,
        "vertauscht": bool(spuren_vertauscht), "n_andere": int(n_andere or 0),
        "ordner": str(out), "stimmen": stimmen, "woerter": words, "gruppe": C.active_group(),
        "erstellt": dt.datetime.now().isoformat(timespec="seconds"),
    }
    with open(out / "analyse.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)
    log.info("Analyse fertig: %s Wörter, Stimmen: %s", len(words),
             [(v["id"], v["vorschlag"], v["aehnlichkeit"], v["dauer"]) for v in stimmen])
    progress(1.0, "Fertig analysiert.")
    return state


def resplit(state, n_andere, progress=lambda f, m: None):
    """Nur die Stimmen neu trennen – ohne neue Abschrift (geht auch bei alten Sessions)."""
    if state.get("modus") == "einzelspuren":
        raise RuntimeError("Diese Session hat Einzelspuren aus Vesktop – jede Spur ist schon genau eine Person, "
                           "neu trennen ist nicht nötig.")
    path = Path(state["quelle"])
    if not path.exists():
        raise FileNotFoundError(f"Die Original-Aufnahme fehlt: {path}\nOhne sie geht nur eine komplett neue Analyse.")
    out = Path(state["ordner"])
    work = out / "_arbeit"
    work.mkdir(exist_ok=True)
    progress(0.02, "Discord-Spur wird gelesen ...")
    if state.get("modus") == "gemischt":
        dis_i = 0
    else:
        dis_i = 0 if state.get("vertauscht") else 1
    dis = A.load_wav(A.extract(path, dis_i, work / "discord.wav"))
    n_spk = int(n_andere or 0) + (1 if state.get("modus") == "gemischt" and n_andere else 0)
    segs, stimmen = _voices(dis, n_spk, out, lambda f: progress(0.05 + 0.9 * f, "Stimmen werden neu getrennt ..."))
    mine = [w[:3] for w in state["woerter"] if w[3] != C.ICH_ID]
    ich = [w for w in state["woerter"] if w[3] == C.ICH_ID]
    words = ich + _assign(mine, segs, "S")
    words.sort(key=lambda w: w[0])
    state.update({"stimmen": stimmen, "woerter": words, "n_andere": int(n_andere or 0)})
    state.pop("zuordnung", None)
    with open(out / "analyse.json", "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    shutil.rmtree(work, ignore_errors=True)
    log.info("Stimmen neu getrennt: %s", [(v["id"], v["vorschlag"], v["aehnlichkeit"], v["dauer"]) for v in stimmen])
    progress(1.0, "Fertig.")
    return state


def load_state(folder):
    with open(Path(folder) / "analyse.json", encoding="utf-8") as f:
        return json.load(f)


def list_sessions():
    return sorted([p.parent.name for p in C.sessions_dir().glob("*/analyse.json")], reverse=True)


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
        C.save_voices(profiles)
    # Discord-Konten merken: nächstes Mal ist die Zuordnung schon vorausgefüllt
    dmap = {v["discord"]["id"]: mapping.get(v["id"]) for v in state["stimmen"] if v.get("discord")}
    dmap = {k: n for k, n in dmap.items() if n and n not in (C.IGNORIEREN, C.UNBEKANNT)}
    if dmap:
        g = C.group()
        g.setdefault("discord", {}).update(dmap)
        C.save_group(g)
        grp = g
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
