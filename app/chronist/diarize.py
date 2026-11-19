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


def diarize_fine(audio, n_groups, progress=None, win=1.5, step=0.75, max_link=6000):
    """Genauere Stimmtrennung: kurze Fenster einzeln per Stimmabdruck gruppieren.

    1. sherpa liefert fein unterteilte Sprechabschnitte (Grenzen)
    2. jeder Abschnitt wird in 1,5-s-Fenster zerlegt, jedes Fenster bekommt einen Stimmabdruck
    3. hierarchische Gruppierung (Kosinus, mittlere Verknüpfung) in n_groups Gruppen
    """
    from scipy.cluster.hierarchy import fcluster, linkage
    prog = progress or (lambda f: None)
    base = diarize(audio, max(12, n_groups * 3), lambda f: prog(0.45 * f))
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
            wins.append((i, t0, t1))
            vecs.append(_norm(ext.compute(st)))
        if i % 50 == 0:
            prog(0.45 + 0.4 * i / max(1, len(base)))
    if len(vecs) < n_groups:
        return base
    X = np.stack(vecs)
    rng = np.random.default_rng(0)
    idx = np.arange(len(X)) if len(X) <= max_link else np.sort(rng.choice(len(X), max_link, replace=False))
    Z = linkage(X[idx], method="average", metric="cosine")
    lab_sub = fcluster(Z, n_groups, criterion="maxclust") - 1
    cent = np.stack([_norm(X[idx][lab_sub == k].mean(axis=0)) for k in range(lab_sub.max() + 1)])
    labels = np.argmax(X @ cent.T, axis=1)
    prog(0.9)
    # Abschnitte entlang der Fensterbeschriftung neu zuschneiden
    by_seg = {}
    for (i, t0, t1), lab in zip(wins, labels):
        by_seg.setdefault(i, []).append((t0, t1, int(lab)))
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
    # sehr kurze Abschnitte ohne Fenster: Stimmabdruck des Abschnitts → nächste Gruppe
    for seg in out:
        if seg[2] is None:
            x = audio[int(seg[0] * SR):int(seg[1] * SR)]
            if len(x) >= SR * 0.3:
                st = ext.create_stream()
                st.accept_waveform(SR, x)
                st.input_finished()
                seg[2] = int(np.argmax(cent @ _norm(ext.compute(st))))
            else:
                seg[2] = out[out.index(seg) - 1][2] if out.index(seg) > 0 and out[out.index(seg) - 1][2] is not None else 0
    # benachbarte Stücke derselben Gruppe verbinden
    merged = []
    for s, e, k in out:
        if merged and merged[-1][2] == k and s - merged[-1][1] < 0.3:
            merged[-1][1] = e
        else:
            merged.append([s, e, k])
    prog(1.0)
    return [(float(s), float(e), int(k)) for s, e, k in merged]


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
