"""Zusammenfassung und Comic-Vorlage über das lokale Sprachmodell (z. B. Strata)."""
import re
from pathlib import Path

from openai import OpenAI

from . import config as C

SYSTEM = ("Du bist der Chronist einer Dungeons-&-Dragons-Gruppe. Du schreibst auf Deutsch, "
          "lebendig, aber sachlich korrekt. Du erfindest nichts dazu, was nicht in der Vorlage steht.")


def _client(st):
    return OpenAI(base_url=st["llm_url"].rstrip("/"), api_key=st.get("llm_key") or "lokal",
                  timeout=900)


def _model(cl, st):
    if st.get("llm_model"):
        return st["llm_model"]
    ms = cl.models.list().data
    if not ms:
        raise RuntimeError("Das Sprachmodell meldet kein Modell. Läuft Strata?")
    return ms[0].id


def _chat(cl, model, prompt, max_tokens=2500):
    r = cl.chat.completions.create(
        model=model, temperature=0.4, max_tokens=max_tokens,
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}])
    txt = r.choices[0].message.content or ""
    txt = re.sub(r"<think>.*?</think>", "", txt, flags=re.S)
    return txt.strip()


def test_connection():
    st = C.settings()
    cl = _client(st)
    m = _model(cl, st)
    a = _chat(cl, m, "Antworte nur mit: Bereit.", 20)
    return f"Verbunden mit Modell '{m}'. Antwort: {a}"


def _gruppe_text(grp):
    ich = grp.get("ich_name", "Ich")
    if C.is_dm(C.ICH_ID, grp):
        rows = [f"- {ich} ist der Spielleiter (DM): erzählt die Welt und spricht alle NPCs"]
    else:
        rows = [f"- Spieler {ich} spielt {grp.get('ich_rolle') or '?'}"]
    for s in grp.get("spieler", []):
        if not s.get("name"):
            continue
        if C.is_dm(s["name"], grp):
            rows.append(f"- {s['name']} ist der Spielleiter (DM): erzählt die Welt und spricht alle NPCs")
            continue
        if s.get("name"):
            z = f"- Spieler {s['name']} spielt {s.get('charakter') or '?'}"
            if s.get("aussehen"):
                z += f" (Aussehen: {s['aussehen']})"
            if C.image_path(s["name"], grp):
                z += " [Referenzbild vorhanden]"
            rows.append(z)
    t = "Die Gruppe:\n" + "\n".join(rows)
    if grp.get("kontext"):
        t += "\n\nHintergrund zur Kampagne:\n" + grp["kontext"]
    return t


def _chunks(text, size):
    out, cur, n = [], [], 0
    for line in text.splitlines():
        if n + len(line) > size and cur:
            out.append("\n".join(cur)); cur, n = [], 0
        cur.append(line); n += len(line) + 1
    if cur:
        out.append("\n".join(cur))
    return out


NOTIZ = """{gruppe}

Unten steht Teil {i} von {n} der automatischen Abschrift einer Spielsitzung.
Achtung: Die Abschrift kann Hörfehler enthalten, und gelegentlich ist ein Satz dem falschen Sprecher zugeordnet.
Format der Zeilen: [Zeit] Spieler (Charakter): Gesagtes. Zeilen des DM sind Erzählung, Beschreibungen oder NPC-Dialog.

Schreibe knappe Stichpunkt-Notizen zu diesem Teil:
- Was in der Spielwelt passiert (chronologisch, mit Charakternamen statt Spielernamen)
- Wichtige Entscheidungen und Würfe mit Folgen
- NPCs, Orte, Gegenstände, Beute, Gold
- Hinweise, Rätsel, offene Fragen
- Witzige oder dramatische Momente mit kurzem wörtlichem Zitat und Sprecher
Gespräche außerhalb des Spiels (Technik, Pausen, Essen, Regelfragen) lässt du weg, außer sie sind wirklich lustig – dann markiere sie mit [OOC].

Abschrift:
{text}"""

VERDICHTEN = """Fasse diese Notizen mehrerer Abschnitte einer Spielsitzung zu einer einzigen, chronologischen Stichpunktliste zusammen. Nichts Wichtiges weglassen, Zitate behalten.

{text}"""

FINAL = """{gruppe}

Hier sind die Notizen zur Spielsitzung „{titel}“:

{text}

Schreibe daraus die Sitzungs-Chronik in Markdown mit genau diesen Abschnitten:
# (ein stimmungsvoller Titel für die Sitzung)
## Kurzfassung
(3–5 Sätze)
## Was geschah
(Erzählung in der Vergangenheitsform, wie ein Kapitel einer Chronik, mehrere Absätze)
## Die Helden
(je Spieler-Charakter 1–3 Sätze, was er in dieser Sitzung getan hat – der DM ist kein Held)
## Begegnungen, Orte und Fundstücke
(Stichpunkte: NPCs, Orte, Beute, Gold)
## Offene Fäden
(Stichpunkte)
## Zitate der Sitzung
(die besten 3–6 Zitate mit Sprecher)"""

COMIC = """{gruppe}

Hier ist die Chronik einer Spielsitzung:

{text}

Erstelle daraus eine Comic-Vorlage mit 8 bis 12 Panels, die die Sitzung als kurze Comic-Geschichte erzählt (die spannendsten und lustigsten Momente).
Für jedes Panel in Markdown:
### Panel N
- **Szene:** Ort, Licht, Stimmung
- **Im Bild:** welche Charaktere, was sie tun (Aussehen beachten, soweit bekannt)
- **Sprechblasen:** kurz, höchstens zwei pro Panel, mit Sprecher
- **Bild-Prompt (Englisch):** eine Beschreibung für einen Bildgenerator, beginnend mit „comic book panel, fantasy,“"""


def summarize(state, progress=lambda f, m: None):
    st = C.settings()
    grp = C.group()
    folder = Path(state["ordner"])
    text = (folder / "transkript.txt").read_text(encoding="utf-8")
    if not text.strip():
        raise RuntimeError("Die Abschrift ist leer.")
    cl = _client(st)
    progress(0.02, "Verbinde mit dem Sprachmodell ...")
    model = _model(cl, st)
    g = _gruppe_text(grp)
    size = int(st["chunk_zeichen"])
    parts = _chunks(text, size)
    notes = []
    for i, p in enumerate(parts, 1):
        progress(0.05 + 0.75 * (i - 1) / len(parts), f"Notizen zu Teil {i} von {len(parts)} ...")
        notes.append(_chat(cl, model, NOTIZ.format(gruppe=g, i=i, n=len(parts), text=p)))
    # Bei sehr langen Sitzungen Notizen schrittweise verdichten
    while len(notes) > 1 and sum(len(x) for x in notes) > size * 1.5:
        progress(0.82, "Notizen werden verdichtet ...")
        merged, cur = [], ""
        for nt in notes:
            if len(cur) + len(nt) > size and cur:
                merged.append(_chat(cl, model, VERDICHTEN.format(text=cur))); cur = ""
            cur += nt + "\n\n"
        if cur:
            merged.append(_chat(cl, model, VERDICHTEN.format(text=cur)) if merged else cur)
        if len(merged) >= len(notes):
            break
        notes = merged
    (folder / "notizen.md").write_text("\n\n---\n\n".join(notes), encoding="utf-8")
    progress(0.88, "Chronik wird geschrieben ...")
    final = _chat(cl, model, FINAL.format(gruppe=g, titel=state["titel"], text="\n\n".join(notes)), 4000)
    (folder / "zusammenfassung.md").write_text(final, encoding="utf-8")
    progress(1.0, "Fertig.")
    return final


def comic(state, progress=lambda f, m: None):
    st = C.settings()
    folder = Path(state["ordner"])
    zf = folder / "zusammenfassung.md"
    if not zf.exists():
        raise RuntimeError("Bitte zuerst die Zusammenfassung erstellen.")
    cl = _client(st)
    progress(0.1, "Comic-Vorlage wird geschrieben ...")
    out = _chat(cl, _model(cl, st), COMIC.format(gruppe=_gruppe_text(C.group()),
                                                 text=zf.read_text(encoding="utf-8")), 4000)
    (folder / "comic_vorlage.md").write_text(out, encoding="utf-8")
    progress(1.0, "Fertig.")
    return out
