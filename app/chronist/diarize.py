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
    """Ordnet Cluster bekannten Stimmen zu. Rückgabe {cluster: (name, aehnlichkeit)}."""
    names = list(profiles)
    out = {c: (None, 0.0) for c in cluster_embs}
    if not names:
        return out
    P = np.stack([_norm(profiles[n]["embedding"]) for n in names])
    pairs = []
    for c, e in cluster_embs.items():
        if e is None:
            continue
        for i, sim in enumerate(P @ e):
            pairs.append((float(sim), c, names[i]))
    pairs.sort(reverse=True)
    used_c, used_n = set(), set()
    for sim, c, n in pairs:
        if c in used_c or n in used_n:
            continue
        used_c.add(c); used_n.add(n)
        out[c] = (n if sim >= schwelle else None, sim)
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
