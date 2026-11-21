"""Tab „Aufnahme“: Gruppe, Mikro, Session-Name, Pegel, Einzelspuren (Vesktop), Start/Stopp."""
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
                               QScrollArea, QVBoxLayout, QWidget)

from .. import config as C
from ..debuglog import log
from ..recorder import REC, channel_people, find_discord, list_mics, plugin_state
from .common import Card, StatusCard, button, error, fmt_time, hbox, label, plain, run_job


class AufnahmeTab(QScrollArea):
    recording_saved = Signal(str)        # Pfad der neuen Aufnahme
    group_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(14)
        lay.addWidget(label("Nimmt **nur Ton** auf: dein Mikro und den Ton von Discord, auf getrennten Spuren. "
                            "Keine Kamera, kein Bildschirm. Braucht kaum Leistung.", "muted"))

        c = Card("Vorbereitung")
        self.group = QComboBox()
        self.group.activated.connect(self._group_picked)
        self.group_hint = label("", "muted")
        self.mic = QComboBox()
        self.mic.setMinimumWidth(380)
        mic_ref = button("🔄", tip="Mikrofone neu suchen", slot=self.fill_mics)
        self.name = QLineEdit()
        self.name.setPlaceholderText("z. B. Session 12 – Der Keller unter dem Grauen Eber")
        c.add(label("Gruppe"))
        c.add(self.group)
        c.add(self.group_hint)
        c.add(label("Dein Mikrofon"))
        c.add(hbox(self.mic, mic_ref))
        c.add(label("Name der Session – so heißt später die Aufnahme-Datei (mit Datum davor)", "muted"))
        c.add(self.name)
        lay.addWidget(c)

        # Status + Pegel
        self.status = StatusCard()
        self.lvl_mic = QProgressBar()
        self.lvl_dis = QProgressBar()
        for b in (self.lvl_mic, self.lvl_dis):
            b.setRange(0, 100)
            b.setTextVisible(False)
            b.setFixedHeight(12)
        self.lvl_box = plain()
        lv = QVBoxLayout(self.lvl_box)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addLayout(hbox(label("Mikro", wrap=False), self.lvl_mic))
        lv.addLayout(hbox(label("Discord", wrap=False), self.lvl_dis))
        self.status.layout().addWidget(self.lvl_box)
        self.lvl_box.hide()
        lay.addWidget(self.status)

        # Einzelspuren
        self.plug = Card("🎧 Einzelspuren (Vesktop)")
        self.plug_hint = label("")
        self.plug_upd = button("🔄 Plugin jetzt aktualisieren (Vesktop startet kurz neu)", slot=self.update_plugin)
        self.plug_upd.hide()
        self.people = QListWidget()
        self.people.setMaximumHeight(220)
        self.people.itemChanged.connect(self._people_changed)
        self.people_hint = label("✔ = wird aufgenommen · Haken weg = 🔇 stumm: von dieser Person wird nichts "
                                 "aufgezeichnet. Wirkt sofort, auch mitten in der Aufnahme. Wer beim Beenden noch "
                                 "stumm ist, wird komplett verworfen. Du hörst alle in Discord normal weiter.", "muted")
        self.plug.add(self.plug_hint)
        self.plug.add(self.plug_upd)
        self.plug.add(self.people)
        self.plug.add(self.people_hint)
        lay.addWidget(self.plug)

        self.start_btn = button("⏺  Aufnahme starten", "primary", slot=self.start)
        self.stop_btn = button("⏹  Aufnahme beenden", "danger", slot=self.stop)
        for b in (self.start_btn, self.stop_btn):
            b.setProperty("class", "big")
            b.setMinimumHeight(48)
        lay.addLayout(hbox(self.start_btn, self.stop_btn))
        lay.addStretch(1)

        self._people_sig = None
        self._updating_people = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.refresh()

    # ---------- Befüllen ----------
    def refresh(self):
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItems(C.list_groups())
        self.group.setCurrentText(C.active_group())
        self.group.blockSignals(False)
        self.group_hint.setText(f"Aufnahmen landen in `{C.recordings_dir()}`")
        if self.mic.count() == 0:
            self.fill_mics()
        self._people_sig = None
        self.tick()

    def fill_mics(self):
        saved = str(C.settings().get("mikro", ""))
        self.mic.clear()
        for name, idx in list_mics():
            self.mic.addItem(name, idx)
        for i in range(self.mic.count()):
            if str(self.mic.itemData(i)) == saved:
                self.mic.setCurrentIndex(i)
                break

    def _group_picked(self):
        name = self.group.currentText()
        if REC.running:
            error(self, "Während einer Aufnahme kann die Gruppe nicht gewechselt werden.")
            self.group.setCurrentText(C.active_group())
            return
        C.set_active_group(name)
        log.info("Aktive Gruppe: %s", name)
        self.group_changed.emit()

    # ---------- Laufender Status ----------
    def tick(self):
        running = REC.running
        self.start_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.group.setEnabled(not running)
        self.name.setEnabled(not running)
        self.mic.setEnabled(not running)
        plug = plugin_state()
        if running:
            m, d = REC.levels()
            self.lvl_box.show()
            self.lvl_mic.setValue(int(m * 100))
            self.lvl_dis.setValue(int(d * 100))
            txt = f"### 🔴 Aufnahme läuft · {fmt_time(REC.elapsed())}\n\n{REC.discord_status}"
            if plug != "aus" and REC.spuren_dir and REC.spuren_dir.exists():
                n = len(list(REC.spuren_dir.glob("*.pcm")))
                txt += f"\n\n🎧 Einzelspuren: **{n} Person(en)** bisher aufgenommen"
            if REC.error:
                txt += f"\n\n⚠️ {REC.error}"
            self.status.set(txt)
        elif not getattr(self, "_saved_msg", None):
            self.lvl_box.hide()
            d = "✅ Discord läuft." if find_discord() else "⚠️ Discord läuft gerade nicht – vor der Aufnahme starten."
            self.status.set(f"### ⏺ Bereit\n\n{d}")
        self._plugin_tick(plug)

    def _plugin_tick(self, stt):
        hints = {
            "aus": "Vesktop-Plugin nicht verbunden – ohne Plugin wird Discord gesamt aufgenommen. "
                   "(Einrichten unter ⚙️ Einstellungen → 🎧 Einzelspuren)",
            "alt": "Das Plugin in Vesktop ist veraltet – für die Personen-Liste bitte aktualisieren.",
            "kein_kanal": "Plugin verbunden ✅ – sobald du in Vesktop in einem Sprachkanal bist, erscheinen hier "
                          "alle Personen zum An- und Abwählen.",
            "ok": "Plugin verbunden ✅",
        }
        self.plug_hint.setText(hints[stt])
        self.plug_upd.setVisible(stt == "alt")
        ppl = channel_people() if stt == "ok" else []
        grp = C.group(getattr(REC, "group", None) if REC.running else None)
        skip = set(grp.get("spur_aus", []))
        sig = (tuple(ppl), tuple(sorted(skip)))
        self.people.setVisible(bool(ppl))
        self.people_hint.setVisible(bool(ppl))
        if sig == self._people_sig:
            return
        self._people_sig = sig
        known = grp.get("discord") or {}
        self._updating_people = True
        self.people.clear()
        for i, an, nm in ppl:
            text = an + (f" – {known[i]}" if known.get(i) and known[i].lower() not in an.lower() else "") \
                + (f"  ({nm})" if nm and nm.lower() != an.lower() else "")
            it = QListWidgetItem(text)
            it.setData(Qt.UserRole, i)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked if i in skip else Qt.Checked)
            self.people.addItem(it)
        self._updating_people = False

    def _people_changed(self, _item):
        if self._updating_people:
            return
        name = getattr(REC, "group", None) if REC.running else None
        grp = C.group(name)
        here, sel = set(), set()
        for k in range(self.people.count()):
            it = self.people.item(k)
            here.add(it.data(Qt.UserRole))
            if it.checkState() == Qt.Checked:
                sel.add(it.data(Qt.UserRole))
        skip = {i for i in grp.get("spur_aus", []) if i not in here} | (here - sel)
        grp["spur_aus"] = sorted(skip)
        C.save_group(grp, name)
        self._people_sig = None
        log.info("Einzelspuren: %s Person(en) stumm", len(skip))

    def update_plugin(self):
        from .. import vesktop as V
        self.plug_upd.setEnabled(False)
        self.plug_hint.setText("Plugin wird aktualisiert …")

        def done(_):
            self.plug_upd.setEnabled(True)
            self.plug_hint.setText("Plugin aktualisiert – Vesktop startet neu. Gleich erscheint die Personen-Liste.")

        def fail(msg):
            self.plug_upd.setEnabled(True)
            error(self, f"Aktualisieren fehlgeschlagen: {msg}")
        run_job(lambda progress: V.install(progress, restart=True), on_done=done, on_error=fail,
                exclusive=False, name="Plugin-Update")

    # ---------- Start / Stopp ----------
    def start(self):
        if REC.running:
            return
        name = self.name.text().strip()
        if not name:
            error(self, "Bitte zuerst einen Namen für die Session eingeben.")
            self.name.setFocus()
            return
        idx = self.mic.currentData()
        try:
            REC.start(idx, name)
            st = C.settings()
            st["mikro"] = idx if idx is not None else ""
            C.save("einstellungen", st)
        except Exception as e:
            log.exception("Aufnahme-Start")
            error(self, f"Aufnahme konnte nicht starten:\n\n{e}")
            return
        self._saved_msg = None
        self.tick()

    def stop(self):
        self.stop_btn.setEnabled(False)
        self.status.set("### ⏳ Aufnahme wird gespeichert …\n\nEinen Moment – die Spuren werden zusammengeführt.")

        def work(progress):
            return REC.stop()

        def done(path):
            if not path:
                error(self, "Beim Speichern ist etwas schiefgegangen: " + (REC.error or "unbekannt"))
                self.tick()
                return
            from ..recorder import user_tracks
            warn = f"\n\n⚠️ Das Mikro hat {REC.overflows}× kurz gestockt." if getattr(REC, "overflows", 0) > 5 else ""
            ut = user_tracks(path)
            if ut:
                warn += ("\n\n🎧 **Einzelspuren:** " + ", ".join(sp.get("anzeige") or sp.get("name") for sp in ut)
                         + " – die Stimmtrennung entfällt.")
            self._saved_msg = (f"### ✅ Aufnahme gespeichert\n\n`{Path(path).name}` – zum Anhören liegt daneben "
                               f"`{Path(path).with_suffix('.mp3').name}`.{warn}\n\nWenn du so weit bist: Tab "
                               "**Session auswerten** – die Aufnahme ist schon ausgewählt.")
            self.status.set(self._saved_msg)
            self.name.clear()
            self.lvl_box.hide()
            self.recording_saved.emit(path)
            QTimer.singleShot(60000, lambda: setattr(self, "_saved_msg", None))
        run_job(work, on_done=done, on_error=lambda m: error(self, m), exclusive=False, name="Aufnahme speichern")
