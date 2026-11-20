"""Stimmen auf der Discord-Spur auseinanderhalten und wiedererkennen."""
import os

import numpy as np
import sherpa_onnx

from . import models
from .audio import SR

_THREADS = max(1, (os.cpu_count() or 4) // 2)


def diarize(audio, num_speakers=0, progress=None):
    """Gibt Liste [(start, end, sprecher_nr)] zurück."""
    models.ensure_diarization_models()
    cfg = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models.SEGMENTATION)),
            num_threads=_THREADS),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(models.EMBEDDING), num_threads=_THREADS),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=int(num_speakers) if num_speakers and num_speakers > 0 else -1,
            threshold=0.7),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    d = sherpa_onnx.OfflineSpeakerDiarization(cfg)

    def cb(done, total):
        if progress and total:
            progress(done / total)
        return 0

    res = d.process(audio, callback=cb).sort_by_start_time()
    return [(float(s.start), float(s.end), int(s.speaker)) for s in res]


def _overlap_intervals(base, margin=1.0):
    """Zeiten, in denen sherpa mehrere Sprecher gleichzeitig erkennt (Durcheinander)."""
    ev = []
    for s, e, _ in base:
        ev += [(s, 1), (e, -1)]
    ev.sort()
    out, active, start = [], 0, None
    for t, d in ev:
        prev = active
        active += d
        if prev < 2 <= active:
            start = t
        elif prev >= 2 > active and start is not None:
            if t - start > 0.2:
                out.append((max(0.0, start - margin), t + margin))   # Ränder großzügig mitnehmen
            start = None
    return out


def _hits(t0, t1, intervals, min_ov=0.25):
    for a, b in intervals:
        if min(t1, b) - max(t0, a) > min_ov:
            return True
        if a > t1:
            break
    return False


def diarize_fine(audio, n_groups, progress=None, win=1.5, step=0.75, max_link=6000, return_windows=False):
    """Genauere Stimmtrennung: kurze Fenster einzeln per Stimmabdruck gruppieren.

    1. sherpa liefert fein unterteilte Sprechabschnitte (Grenzen) und Stellen mit Durcheinander
    2. jeder Abschnitt wird in 1,5-s-Fenster zerlegt, jedes Fenster bekommt einen Stimmabdruck
    3. hierarchische Gruppierung nur aus "sauberen" Fenstern (niemand redet dazwischen)
    4. Gruppen, die nur aus Gemisch bestehen (in sich uneinheitlich), werden aufgelöst
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    prog = progress or (lambda f: None)
    base = diarize(audio, max(12, n_groups * 3), lambda f: prog(0.45 * f))
    overl = _overlap_intervals(base)
    ext = _get_extractor()
    wins, vecs = [], []
    for i, (s, e, _) in enumerate(base):
        t = s
        starts = []
        while t + win <= e + 1e-6:
            starts.append(t)
            t += step
        if not starts and e - s >= 0.6:
            starts = [s]
        for t0 in starts:
            t1 = min(e, t0 + win)
            x = audio[int(t0 * SR):int(t1 * SR)]
            st = ext.create_stream()
            st.accept_waveform(SR, x)
            st.input_finished()
            wins.append([i, t0, t1, _hits(t0, t1, overl)])
            vecs.append(_norm(ext.compute(st)))
        if i % 50 == 0:
            prog(0.45 + 0.4 * i / max(1, len(base)))
    if len(vecs) < n_groups:
        return (base, []) if return_windows else base
    X = np.stack(vecs)
    clean = np.array([not w[3] for w in wins])
    pool = np.where(clean)[0] if clean.sum() >= n_groups * 4 else np.arange(len(X))
    rng = np.random.default_rng(0)
    idx = pool if len(pool) <= max_link else np.sort(rng.choice(pool, max_link, replace=False))
    Z = linkage(X[idx], method="average", metric="cosine")
    lab_sub = fcluster(Z, n_groups, criterion="maxclust") - 1
    groups = [k for k in range(lab_sub.max() + 1) if (lab_sub == k).sum() > 0]
    cent = {k: _norm(X[idx][lab_sub == k].mean(axis=0)) for k in groups}
    # Gemisch-Gruppen auflösen: Fenster ähneln dem eigenen Mittelpunkt kaum
    coh = {k: float(np.mean(X[idx][lab_sub == k] @ cent[k])) for k in groups}
    keep = [k for k in groups if coh[k] >= 0.45 or len(groups) <= n_groups // 2]
    # zweite Prüfung über alle Fenster: Gruppen, deren Fenster meist im Durcheinander liegen
    if len(keep) > 2:
        sims0 = X @ np.stack([cent[k] for k in keep]).T
        lab0 = np.array(keep)[np.argmax(sims0, axis=1)]
        ovl = np.array([w[3] for w in wins])
        mix = [k for k in keep if (lab0 == k).sum() > 0 and ovl[lab0 == k].mean() >= 0.5]
        if mix and len(keep) - len(mix) >= 2:
            keep = [k for k in keep if k not in mix]
            try:
                from .debuglog import log
                log.info("Durcheinander-Gruppen aufgelöst: %s", mix)
            except Exception:
                pass
    if len(keep) < len(groups):
        log_dissolved = [k for k in groups if k not in keep]
        try:
            from .debuglog import log
            log.info("Gemisch-Gruppen aufgelöst: %s (Zusammenhalt %s)", log_dissolved,
                     {k: round(coh[k], 2) for k in log_dissolved})
        except Exception:
            pass
    C_ = np.stack([cent[k] for k in keep])
    sims = X @ C_.T
    labels = np.array(keep)[np.argmax(sims, axis=1)]
    best = sims.max(axis=1)
    for w, lab, sim in zip(wins, labels, best):
        w += [int(lab), float(sim)]          # [i, t0, t1, overlap, label, sim]
    cent_arr = C_
    prog(0.9)
    # Abschnitte entlang der Fensterbeschriftung neu zuschneiden
    by_seg = {}
    for w in wins:
        by_seg.setdefault(w[0], []).append((w[1], w[2], w[4]))
    out = []
    for i, (s, e, _) in enumerate(base):
        ws = by_seg.get(i)
        if not ws:
            out.append([s, e, None])
            continue
        cur_s, cur_l = s, ws[0][2]
        for j in range(1, len(ws)):
            if ws[j][2] != cur_l:
                cut = (ws[j - 1][1] + ws[j][0]) / 2 if ws[j][0] < ws[j - 1][1] else ws[j][0]
                out.append([cur_s, cut, cur_l])
                cur_s, cur_l = cut, ws[j][2]
        out.append([cur_s, e, cur_l])
    for n_, seg in enumerate(out):
        if seg[2] is None:
            x = audio[int(seg[0] * SR):int(seg[1] * SR)]
            if len(x) >= SR * 0.3:
                st = ext.create_stream()
                st.accept_waveform(SR, x)
                st.input_finished()
                seg[2] = int(keep[int(np.argmax(cent_arr @ _norm(ext.compute(st))))])
            else:
                seg[2] = out[n_ - 1][2] if n_ > 0 and out[n_ - 1][2] is not None else int(keep[0])
    merged = []
    for s, e, k in sorted(out):
        if merged and merged[-1][2] == k and s - merged[-1][1] < 0.3:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    prog(1.0)
    segs = [(float(s), float(e), int(k)) for s, e, k in merged]
    if return_windows:
        return segs, [(w[1], w[2], w[4], w[5], w[3]) for w in wins]   # (t0, t1, label, sim, overlap)
    return segs


_extractor = None


def _get_extractor():
    global _extractor
    if _extractor is None:
        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(models.EMBEDDING), num_threads=_THREADS))
    return _extractor


def _norm(v):
    v = np.asarray(v, np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def embed(audio, parts):
    """Stimmabdruck aus mehreren Stücken (Mittelwert)."""
    ext = _get_extractor()
    vecs = []
    for st, en in parts:
        x = audio[int(st * SR):int(en * SR)]
        if len(x) < SR * 0.8:
            continue
        s = ext.create_stream()
        s.accept_waveform(SR, x)
        s.input_finished()
        vecs.append(_norm(ext.compute(s)))
    if not vecs:
        return None
    return _norm(np.mean(vecs, axis=0))


def cluster_parts(segments, spk, n=20):
    """Die längsten Stücke eines Sprechers."""
    segs = sorted([(e - s, s, e) for s, e, k in segments if k == spk], reverse=True)
    return [(s, min(e, s + 15)) for _, s, e in segs[:n]]


def match(cluster_embs, profiles, schwelle):
    """Jede Gruppe bekommt die ähnlichste bekannte Stimme – mehrere Gruppen dürfen dieselbe Person sein."""
    names = list(profiles)
    out = {c: (None, 0.0) for c in cluster_embs}
    if not names:
        return out
    P = np.stack([_norm(profiles[n]["embedding"]) for n in names])
    for c, e in cluster_embs.items():
        if e is None:
            continue
        sims = P @ e
        i = int(np.argmax(sims))
        out[c] = (names[i] if sims[i] >= schwelle else None, float(sims[i]))
    return out


def update_profile(profiles, name, emb):
    emb = _norm(emb)
    p = profiles.get(name)
    if p is None:
        profiles[name] = {"embedding": emb.tolist(), "n": 1}
    else:
        n = min(p.get("n", 1), 9)
        new = _norm(np.asarray(p["embedding"]) * n + emb)
        profiles[name] = {"embedding": new.tolist(), "n": n + 1}
