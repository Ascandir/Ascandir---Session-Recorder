# 🎲 Ascandir · SessionRecorder

Nimmt D&D-Sessions aus Discord auf (nur Ton: eigenes Mikro + Discord, getrennte Spuren), erkennt wer spricht,
schreibt alles ab und erstellt daraus eine Chronik – auf Wunsch mit fertig gemalten Comic-Seiten. **Alles läuft lokal.**
Für **Windows** und **CachyOS/Arch Linux**.

## Installation
Unter [Releases](../../releases/latest):
- **Windows:** `Ascandir-SessionRecorder-Setup.exe` herunterladen und starten.
- **CachyOS / Arch:** `ascandir-sessionrecorder-…-x86_64.pkg.tar.zst` herunterladen, dann
  `sudo pacman -U ascandir-sessionrecorder-*.pkg.tar.zst`.

Updates gibt es direkt im Programm über **⬆️ Updates**.

## Funktionen
- Tonaufnahme: Mikrofon + nur die Discord-/Vesktop-App (Windows: Prozess-Loopback, Linux: PipeWire)
- Optional Einzelspuren pro Person über Vesktop mit eigenem Vencord-Plugin (inkl. Stummschalten einzelner Personen)
- Sprechertrennung & Wiedererkennung der Stimmen (sherpa-onnx), Abschrift mit Whisper (faster-whisper) – lokal
- Chronik & Comic-Vorlage über ein lokales Sprachmodell (Strata oder eingebautes llama.cpp)
- Comic-Seiten mit FLUX.2-klein (stable-diffusion.cpp, Vulkan) – Manhwa, Manga oder klassisch
- Mehrere Gruppen, Charaktere mit Aussehen und Bildern

## Aufbau
| Ordner | Inhalt |
|---|---|
| `app/` | Programm (Python): `chronist/qt/` Oberfläche (PySide6), sonst Aufnahme, Auswertung, KI, Comic. Version in `app/VERSION` |
| `packaging/` | PyInstaller-Bauplan, Arch-PKGBUILD und Desktop-Eintrag |
| `installer/setup/` | Windows-Setup-Assistent (Go, lxn/walk) – lädt das fertige Programm aus dem Release |
| `vencord-plugin/` | Vencord-Plugin für Einzelspuren in Vesktop |
| `assets/` | Icon |

## Bauen
Alles baut GitHub Actions (`.github/workflows/release.yml`): eigenständiges Programm für Windows und Linux
(PyInstaller), Arch-Paket, Windows-Setup. Neue Version: `app/VERSION` erhöhen und den Workflow starten.

Lokal starten (Entwicklung): `pip install -r app/requirements.txt && python app/main.py`
