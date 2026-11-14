# 🎲 Ascandir · SessionRecorder

Nimmt D&D-Sessions aus Discord auf (nur Ton: eigenes Mikro + Discord, getrennte Spuren), erkennt wer spricht,
schreibt alles ab und erstellt daraus eine Chronik – auf Wunsch mit Comic-Vorlage. **Alles läuft lokal.**

## Installation
Neueste `Ascandir-SessionRecorder-Setup.exe` unter [Releases](../../releases/latest) herunterladen und starten.
Updates gibt es direkt im Tool über den Knopf **⬆️ Updates**.

## Funktionen
- Eingebaute Tonaufnahme: Mikrofon + nur die Discord-App (WASAPI-Prozess-Loopback), keine Kamera/Bildschirm
- Sprechertrennung & Wiedererkennung der Stimmen (sherpa-onnx, lokal)
- Abschrift mit Whisper (faster-whisper, lokal auf der CPU)
- Chronik & Comic-Vorlage über ein lokales Sprachmodell mit OpenAI-kompatibler API (z. B. Strata)
- Gruppe mit DM-Auswahl, Charakteren und Charakterbildern

## Aufbau
| Ordner | Inhalt |
|---|---|
| `app/` | Python-App (Gradio-Oberfläche, Aufnahme, Auswertung). Version in `app/VERSION` |
| `installer/setup/` | Setup-Assistent (Go, lxn/walk) – installiert Python (uv), Pakete, Modelle |
| `installer/launcher/` | `SessionRecorder.exe` – startet die App ohne Konsolenfenster |
| `assets/` | Icon |

## Bauen
```bash
go install github.com/tc-hib/go-winres@latest
./build.sh   # erzeugt Ascandir-SessionRecorder-Setup.exe
```
Neue Version: `app/VERSION` erhöhen, bauen, als GitHub-Release mit Tag `vX.Y.Z` und der Setup-Datei veröffentlichen.
