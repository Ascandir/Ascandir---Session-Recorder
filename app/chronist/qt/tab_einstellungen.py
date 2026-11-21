"""Tab „Einstellungen“: Debug-Log, KI, Bildgenerator, Einzelspuren (Vesktop), Speicher, Allgemeines, Stimmen."""
import sys
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QLineEdit,
                               QPlainTextEdit, QRadioButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

from .. import config as C
from .. import llm as L
from ..debuglog import log, save_report
from .common import Card, StatusCard, ask, button, cancel_all, error, hbox, label, open_path, run_job


class EinstellungenTab(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(14)
        st = C.settings()

        # Debug
        d = Card("🐞 Fehlersuche")
        d.add(hbox(button("🐞 Debug-Log erstellen", slot=self.debug),
                   label("Sammelt alle Infos zur Fehlersuche in einer Datei zum Verschicken.", "muted")))
        lay.addWidget(d)

        # KI
        k = Card("🧠 Lokale KI (für Zusammenfassung & Comic-Vorlage)",
                 "Automatisch (empfohlen): nimmt Strata, wenn es da ist – sonst die eingebaute KI. Beide werden nur für "
                 "die Auswertung gestartet und danach wieder beendet.")
        self.ki_group = QButtonGroup(self)
        row = []
        for m in L.MODI:
            rb = QRadioButton(m)
            rb.setChecked(m == st.get("ki_modus", "Automatisch"))
            self.ki_group.addButton(rb)
            row.append(rb)
        self.ki_group.buttonClicked.connect(self._ki_mode)
        k.add(hbox(*row, None))
        self.ki_md = StatusCard()
        k.add(self.ki_md)
        k.add(label("**Strata**"))
        self.strata_cmd = QLineEdit(st.get("strata_befehl", ""))
        self.strata_cmd.setPlaceholderText("Startbefehl oder Pfad – leer = automatisch suchen")
        k.add(hbox(self.strata_cmd, button("📂 Auswählen …", slot=self.pick_strata)))
        self.preset = QComboBox()
        self.preset.addItem("(automatisch)")
        self.preset.activated.connect(self._save_preset)
        k.add(hbox(label("Preset:", wrap=False), self.preset, button("🔍 Strata & Presets suchen", slot=self.find_strata), None))
        self.strata_stop = QCheckBox("Strata nach der Zusammenfassung wieder beenden (gibt die Grafikkarte frei)")
        self.strata_stop.setChecked(st.get("strata_beenden", True))
        self.strata_wait = QSpinBox()
        self.strata_wait.setRange(2, 30)
        self.strata_wait.setSuffix(" Min.")
        self.strata_wait.setValue(int(st.get("strata_wartezeit", 900)) // 60)
        k.add(self.strata_stop)
        k.add(hbox(label("So lange auf Strata warten:", wrap=False), self.strata_wait, None))
        k.add(label("**Eingebaute KI** – llama.cpp (Vulkan) mit Qwen3-14B, einmalig ca. 9 GB"))
        k.add(hbox(button("Eingebaute KI jetzt installieren", slot=self.install_builtin), None))
        k.add(label("**Eigene Adresse / Experten**"))
        self.url = QLineEdit(st["llm_url"])
        self.model = QLineEdit(st["llm_model"])
        self.model.setPlaceholderText("leer = automatisch")
        gk = QGridLayout()
        gk.addWidget(label("Adresse der KI"), 0, 0)
        gk.addWidget(label("Modellname"), 0, 1)
        gk.addWidget(self.url, 1, 0)
        gk.addWidget(self.model, 1, 1)
        k.add(gk)
        self.test_btn = button("KI testen (starten → fragen → beenden)", slot=self.test_ki)
        k.add(hbox(self.test_btn, button("⏹ Test abbrechen", slot=cancel_all), None))
        self.test_md = label("")
        k.add(self.test_md)
        lay.addWidget(k)

        # Bildgenerator
        from .. import comic as CO
        b = Card("🎨 Bildgenerator (für die Comic-Seiten)")
        self.draw_md = label("")
        b.add(self.draw_md)
        self.c_model = QComboBox()
        self.c_model.addItems([v["name"] for v in CO.MODELLE.values()])
        self.c_model.setCurrentText(CO.MODELLE[CO.modell()]["name"])
        self.c_fmt = QComboBox()
        self.c_fmt.addItems(list(CO.FORMATE.values()))
        self.c_fmt.setCurrentText(CO.FORMATE[CO.comic_format()])
        gb = QGridLayout()
        gb.addWidget(label("Bildmodell"), 0, 0)
        gb.addWidget(label("Comic-Format"), 0, 1)
        gb.addWidget(self.c_model, 1, 0)
        gb.addWidget(self.c_fmt, 1, 1)
        b.add(gb)
        b.add(label("Hohe Qualität (9B) ist experimentell: langsamer, hält die Charaktere besser. Hängt es, wechselt "
                    "das Tool automatisch auf Schnell.", "muted"))
        own = (st.get("comic_stil") or "").strip()
        self.c_style = QLineEdit("" if own in list(CO.STILE.values()) + [CO.STYLE_DEFAULT] else own)
        self.c_style.setPlaceholderText("Eigener Zeichenstil (optional, Englisch) – leer = passend zum Format")
        b.add(self.c_style)
        b.add(hbox(button("💾 Comic-Einstellungen speichern", slot=self.save_comic),
                   button("Gewähltes Bildmodell jetzt installieren", slot=self.install_draw), None))
        lay.addWidget(b)

        # Einzelspuren
        v = Card("🎧 Einzelspuren pro Person (Vesktop)",
                 "Mit **Vesktop** (ein Discord-Programm, gleiches Konto, gleiche Server) und unserem Plugin bekommt jede "
                 "Person im Sprachkanal eine eigene Tonspur – keine Stimmtrennung mehr nötig. Das Plugin nimmt nur auf, "
                 "solange hier die Aufnahme läuft.\n\n1. Vesktop installieren und einmal anmelden · 2. Vesktop schließen "
                 "· 3. **Plugin einrichten** · 4. Vesktop starten und darüber in den Sprachkanal gehen")
        self.vesk_md = label("")
        v.add(self.vesk_md)
        v.add(hbox(button("Vesktop herunterladen", slot=self.vesk_download),
                   button("Plugin einrichten / aktualisieren", "primary", slot=self.vesk_install),
                   button("Status prüfen", slot=self.vesk_status), button("Plugin entfernen", slot=self.vesk_remove), None))
        lay.addWidget(v)

        # Speicher
        s = Card("💾 Speicher", "Alle Modelle, Downloads und Zwischendateien liegen im Datenordner des Programms.")
        self.sto_md = label("")
        s.add(self.sto_md)
        s.add(hbox(button("🔄 Neu berechnen", slot=self.storage_refresh),
                   button("🧹 Temporäre Dateien leeren", slot=self.clear_temp),
                   button("🗑️ Ungenutzte Spracherkennung löschen", slot=self.remove_whisper),
                   button("🗑️ Eingebaute KI löschen", slot=self.remove_builtin),
                   button("📂 Datenordner öffnen", slot=lambda: open_path(C.ROOT)), None))
        self.sto_status = label("")
        s.add(self.sto_status)
        lay.addWidget(s)

        # Allgemein
        a = Card("Weitere Einstellungen")
        self.wm = QComboBox()
        self.wm.addItems(["large-v3-turbo", "large-v3", "medium", "small"])
        self.wm.setCurrentText(st["whisper_model"])
        self.folder = QLineEdit(st["aufnahme_ordner"])
        self.design = QComboBox()
        self.design.addItems(["Dunkel", "Hell"])
        self.design.setCurrentText(st.get("design", "Dunkel"))
        self.upd_start = QCheckBox("Beim Start nach Updates suchen (fragt nur die Versionsnummer bei GitHub ab)")
        self.upd_start.setChecked(st.get("update_beim_start", True))
        self.schwelle = QSlider(Qt.Horizontal)
        self.schwelle.setRange(30, 90)
        self.schwelle.setValue(int(float(st["match_schwelle"]) * 100))
        self.schwelle_lab = label("")
        self.schwelle.valueChanged.connect(lambda val: self.schwelle_lab.setText(f"{val} %"))
        self.schwelle_lab.setText(f"{self.schwelle.value()} %")
        ga = QGridLayout()
        ga.addWidget(label("Spracherkennung"), 0, 0)
        ga.addWidget(self.wm, 0, 1)
        ga.addWidget(label("large-v3-turbo: schnell und gut. large-v3: etwas genauer, deutlich langsamer.", "muted"), 0, 2)
        ga.addWidget(label("Ordner für Aufnahmen"), 1, 0)
        ga.addLayout(hbox(self.folder, button("📂", slot=self.pick_folder)), 1, 1, 1, 2)
        ga.addWidget(label("Design"), 2, 0)
        ga.addWidget(self.design, 2, 1)
        ga.addWidget(label("Stimmerkennung muss so sicher sein"), 3, 0)
        ga.addLayout(hbox(self.schwelle, self.schwelle_lab), 3, 1, 1, 2)
        ga.setColumnStretch(2, 1)
        a.add(ga)
        a.add(self.upd_start)
        self.s_status = label("")
        a.add(hbox(button("💾 Speichern", "primary", slot=self.save), self.s_status, None))
        lay.addWidget(a)

        # Stimmen
        vs = Card("Gespeicherte Stimmen der aktiven Gruppe")
        self.vlist = QComboBox()
        self.vlist.setMinimumWidth(260)
        vs.add(hbox(self.vlist, button("Löschen", "danger", slot=self.delete_voice), None))
        lay.addWidget(vs)
        lay.addStretch(1)
        self.refresh()

    # ---------- Befüllen ----------
    def refresh(self):
        self.ki_md.set(self._ki_status())
        self.draw_md.setText(self._draw_status())
        from .. import vesktop as V
        self.vesk_md.setText(V.status_text())
        from .. import storage
        self.sto_md.setText(storage.overview())
        self.vlist.clear()
        self.vlist.addItems(list(C.voices()))

    def _ki_status(self):
        st = C.settings()
        mode = L.resolved_mode()
        pre = f"**Automatisch gewählt: {mode}.** " if st.get("ki_modus", "Automatisch") == "Automatisch" else ""
        if mode == "Strata":
            d = L.strata_description()
            if L.strata_cli():
                d += f"  \nPreset: **{st.get('strata_preset') or '(erstes gespeichertes)'}** – Start ohne Fenster."
            return pre + d
        if mode == "Eingebaute KI":
            return pre + ("✅ Eingebaute KI ist installiert." if L.builtin_installed() else
                          "Die eingebaute KI wird beim ersten Bedarf automatisch geladen (ca. 9 GB).")
        return pre + f"Es wird die KI unter `{st['llm_url']}` genutzt. Sie muss selbst gestartet sein."

    def _draw_status(self):
        from .. import comic as CO
        lines = []
        for key, v in CO.MODELLE.items():
            mark = "✅ installiert" if CO.installed(key) else f"nicht installiert ({v['groesse']}, wird beim ersten Comic geladen)"
            akt = " ← **aktiv**" if key == CO.modell() else ""
            if key == "qualitaet" and C.settings().get("comic_9b_haengt"):
                akt += " ⚠️ ist auf deinem PC hängengeblieben – „Schnell“ wird benutzt"
            lines.append(f"- {v['name']}: {mark}{akt}")
        return "\n".join(lines) + f"\n\nFormat: **{CO.FORMATE[CO.comic_format()]}**"

    # ---------- Aktionen ----------
    def debug(self):
        try:
            path, text = save_report()
        except Exception as e:
            error(self, f"Debug-Log konnte nicht erstellt werden: {e}")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("Debug-Log")
        dlg.resize(900, 640)
        lay = QVBoxLayout(dlg)
        lay.addWidget(label(f"Gespeichert unter `{path}` – schick mir die Datei oder kopier den Text."))
        te = QPlainTextEdit(text)
        te.setReadOnly(True)
        lay.addWidget(te)
        lay.addLayout(hbox(button("📋 Text kopieren", slot=lambda: QGuiApplication.clipboard().setText(text)),
                           button("📂 Ordner öffnen", slot=lambda: open_path(Path(path).parent)),
                           None, button("Schließen", slot=dlg.accept)))
        dlg.exec()

    def _ki_mode(self, btn):
        C.save("einstellungen", {**C.settings(), "ki_modus": btn.text()})
        self.ki_md.set(self._ki_status())

    def pick_strata(self):
        flt = "Programme (*.exe *.bat *.cmd);;Alle Dateien (*)" if sys.platform == "win32" else "Alle Dateien (*)"
        f, _ = QFileDialog.getOpenFileName(self, "Strata-Programm wählen", "", flt)
        if f:
            self.strata_cmd.setText(f)
            C.save("einstellungen", {**C.settings(), "strata_befehl": f})
            self.ki_md.set(self._ki_status())

    def find_strata(self):
        self.ki_md.set("🔍 Suche Strata …")

        def work(progress):
            L.find_strata(force=True)
            return L.list_presets()

        def done(res):
            names, raw = res
            cur = C.settings().get("strata_preset", "")
            self.preset.clear()
            self.preset.addItems(["(automatisch)"] + names)
            self.preset.setCurrentText(cur if cur in names else "(automatisch)")
            self.ki_md.set(self._ki_status() + (f"\n\n```\n{raw}\n```" if raw else ""))
        run_job(work, on_done=done, on_error=lambda m: self.ki_md.set(f"⚠️ {m}"), exclusive=False, name="Strata suchen")

    def _save_preset(self):
        name = self.preset.currentText()
        C.save("einstellungen", {**C.settings(), "strata_preset": "" if name == "(automatisch)" else name})
        self.ki_md.set(self._ki_status())

    def install_builtin(self):
        run_job(lambda progress: L.install_builtin(progress), on_progress=self.ki_md.progress,
                on_done=lambda _: self.ki_md.done(self._ki_status()),
                on_error=lambda m: self.ki_md.done(f"⚠️ Installation fehlgeschlagen: {m}"), name="KI installieren")

    def test_ki(self):
        self.test_md.setText("⏳ KI wird gestartet und gefragt …")
        run_job(lambda progress: L.test(progress),
                on_progress=lambda f, m: self.test_md.setText(f"⏳ {m}"),
                on_done=lambda ans: self.test_md.setText(f"✅ {ans}\n\nDie KI wurde danach wieder beendet."),
                on_error=lambda m: self.test_md.setText(f"❌ {m}\n\nKlick auf **🐞 Debug-Log erstellen** und schick "
                                                        "mir die Datei."), name="KI-Test")

    def save_comic(self):
        from .. import comic as CO
        C.save("einstellungen", {**C.settings(), "comic_modell": CO.MODELL_NAMEN.get(self.c_model.currentText(), "schnell"),
                                 "comic_9b_haengt": False,
                                 "comic_format": CO.FORMAT_NAMEN.get(self.c_fmt.currentText(), "manhwa"),
                                 "comic_stil": self.c_style.text().strip()})
        self.draw_md.setText("💾 Gespeichert.\n\n" + self._draw_status())

    def install_draw(self):
        from .. import comic as CO
        m = CO.MODELL_NAMEN.get(self.c_model.currentText()) or CO.modell()
        self.draw_md.setText("⏳ Bildmodell wird geladen …")
        run_job(lambda progress: CO.install(progress, m),
                on_progress=lambda f, msg: self.draw_md.setText(f"⏳ {msg} ({int(f * 100)} %)"),
                on_done=lambda _: self.draw_md.setText(self._draw_status()),
                on_error=lambda e: self.draw_md.setText(f"⚠️ Installation fehlgeschlagen: {e}"), name="Bildmodell")

    def vesk_download(self):
        url = ("https://vesktop.dev/install/windows/" if sys.platform == "win32"
               else "https://vesktop.dev/install/linux/")
        webbrowser.open(url)

    def vesk_install(self):
        from .. import vesktop as V
        self.vesk_md.setText("⏳ Plugin wird eingerichtet …")

        def done(_):
            self.vesk_md.setText("✅ **Eingerichtet.** Starte jetzt Vesktop und geh darüber in den Sprachkanal. "
                                 "Im Aufnahme-Tab steht dann „Plugin verbunden“.")
        run_job(lambda progress: V.install(progress, restart=True), on_done=done,
                on_error=lambda m: self.vesk_md.setText(f"⚠️ Einrichten fehlgeschlagen: {m}"),
                on_progress=lambda f, m: self.vesk_md.setText(f"⏳ {m}"), exclusive=False, name="Vesktop-Plugin")

    def vesk_status(self):
        from .. import vesktop as V
        self.vesk_md.setText(V.status_text())

    def vesk_remove(self):
        from .. import vesktop as V
        try:
            V.uninstall()
        except Exception as e:
            error(self, str(e))
            return
        self.vesk_md.setText("Plugin entfernt – Vesktop nutzt beim nächsten Start wieder sein normales Vencord.")

    def storage_refresh(self):
        from .. import storage
        self.sto_md.setText(storage.overview())

    def clear_temp(self):
        from .. import storage
        storage.clear_temp()
        self.storage_refresh()
        self.sto_status.setText("🧹 Temporäre Dateien geleert.")

    def remove_whisper(self):
        from .. import storage
        freed = storage.remove_unused_whisper()
        self.storage_refresh()
        self.sto_status.setText(f"🗑️ {storage._fmt(freed)} frei geworden." if freed else
                                "Es gibt keine ungenutzten Spracherkennungs-Modelle.")

    def remove_builtin(self):
        from .. import storage
        if L.resolved_mode() == "Eingebaute KI":
            error(self, "Die eingebaute KI ist gerade ausgewählt. Stell oben erst auf Strata um.")
            return
        if not ask(self, "Eingebaute KI wirklich löschen?"):
            return
        freed = storage.remove_builtin_ki()
        self.storage_refresh()
        self.sto_status.setText(f"🗑️ Eingebaute KI gelöscht – {storage._fmt(freed)} frei." if freed else
                                "Die eingebaute KI war nicht installiert.")

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Ordner für Aufnahmen", self.folder.text())
        if d:
            self.folder.setText(d)

    def save(self):
        st = C.settings()
        old_design = st.get("design")
        st.update({"strata_befehl": self.strata_cmd.text().strip(), "strata_beenden": self.strata_stop.isChecked(),
                   "strata_wartezeit": self.strata_wait.value() * 60, "llm_url": self.url.text().strip(),
                   "llm_model": self.model.text().strip(), "whisper_model": self.wm.currentText(),
                   "aufnahme_ordner": self.folder.text().strip(), "match_schwelle": self.schwelle.value() / 100,
                   "design": self.design.currentText(), "update_beim_start": self.upd_start.isChecked()})
        C.save("einstellungen", st)
        self.s_status.setText("✅ Gespeichert.")
        self.ki_md.set(self._ki_status())
        if old_design != st["design"]:
            from PySide6.QtWidgets import QApplication
            from .common import apply_theme
            apply_theme(QApplication.instance())

    def delete_voice(self):
        name = self.vlist.currentText()
        if not name or not ask(self, f"Stimmprofil „{name}“ löschen?"):
            return
        v = C.voices()
        v.pop(name, None)
        C.save_voices(v)
        self.vlist.clear()
        self.vlist.addItems(list(v))
