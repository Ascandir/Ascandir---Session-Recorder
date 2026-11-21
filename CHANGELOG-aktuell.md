**Version 3.0 – echtes Desktop-Programm, jetzt auch für CachyOS/Arch Linux**

- **🖥️ Neue Oberfläche:** Der SessionRecorder ist jetzt ein richtiges Desktop-Programm (Qt) statt einer Web-Oberfläche im Fenster. Schneller, mit echten Bedienelementen, Hörproben direkt im Programm und einem Comic-Betrachter zum Scrollen
- **🐧 Linux-Version für CachyOS/Arch:** als pacman-Paket (`sudo pacman -U ascandir-sessionrecorder-…pkg.tar.zst`) mit Eintrag im Anwendungsmenü. Mikrofon und Discord-Ton laufen über PipeWire (nur die Discord-/Vesktop-App wird mitgeschnitten), Comic und eingebaute KI über Vulkan, Vesktop-Einzelspuren inklusive (auch Vesktop als Flatpak)
- **📦 Eigenständig:** Kein mitinstalliertes Python mehr – das Setup lädt das fertige Programm. Updates unter Windows wie gewohnt über „Jetzt aktualisieren“, unter Linux über pacman (fragt einmal nach dem Passwort)
- **🎧 Vesktop-Plugin 3:** schickt die Einzelspuren über die lokale Verbindung an den SessionRecorder statt selbst zu schreiben – funktioniert damit auch in Sandboxen. Im Aufnahme-Tab einmal auf „Plugin jetzt aktualisieren“ klicken
- Deine Gruppen, Charaktere, gelernten Stimmen, Aufnahmen und Sessions bleiben beim Update erhalten
