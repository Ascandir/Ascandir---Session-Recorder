"""Tab „Gruppe“: Gruppen verwalten, Mitspieler, DM, Kampagne, Charaktere mit Aussehen und Bildern."""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QGridLayout, QHBoxLayout, QInputDialog,
                               QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QScrollArea, QSplitter,
                               QVBoxLayout, QWidget)

from .. import config as C
from ..debuglog import log
from ..recorder import REC
from .common import Card, ask, button, error, hbox, label, plain

NEU = "➕ Neuer Charakter"


def owner_choices(g=None):
    g = g or C.group()
    return [g.get("ich_name", "Ich")] + [sp["name"] for sp in g.get("spieler", []) if sp.get("name")]


class GruppeTab(QScrollArea):
    changed = Signal()            # Gruppe/Mitspieler geändert → andere Tabs neu befüllen

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(14)

        # Gruppen
        c = Card("Gruppe")
        self.group = QComboBox()
        self.group.setMinimumWidth(260)
        self.group.activated.connect(self._switch)
        c.add(hbox(self.group, button("➕ Neue Gruppe", slot=self.create), button("✏️ Umbenennen", slot=self.rename),
                   button("🗑️ Löschen", slot=self.delete), None))
        c.add(label("Jede Gruppe hat eigene Mitspieler, eigene gelernte Stimmen, eigene Charaktere und eigene Ordner.",
                    "muted"))
        lay.addWidget(c)

        # Runde
        r = Card("Die Runde")
        self.ich = QLineEdit()
        self.dm = QComboBox()
        g = QGridLayout()
        g.addWidget(label("Dein Name"), 0, 0)
        g.addWidget(label("Wer ist der Spielleiter (DM)? – seine Zeilen werden als Erzählung & NPCs behandelt"), 0, 1)
        g.addWidget(self.ich, 1, 0)
        g.addWidget(self.dm, 1, 1)
        r.add(g)
        r.add(label("Mitspieler (ohne dich)"))
        self.players = QListWidget()
        self.players.setMaximumHeight(200)
        self.players.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed
                                     | QAbstractItemView.SelectedClicked)
        r.add(self.players)
        r.add(hbox(button("➕ Mitspieler", slot=self.add_player), button("➖ Entfernen", slot=self.remove_player),
                   label("Doppelklick zum Umbenennen", "muted"), None))
        r.add(label("Kampagne & wichtige Namen (NPCs, Orte, Götter …) – hilft der Spracherkennung bei der Schreibweise"))
        self.kontext = QPlainTextEdit()
        self.kontext.setMaximumHeight(120)
        r.add(self.kontext)
        self.save_btn = button("💾 Speichern", "primary", slot=self.save)
        self.status = label("")
        r.add(hbox(self.save_btn, self.status, None))
        lay.addWidget(r)

        # Charaktere
        ch = Card("🧙 Charaktere", "Leg beliebig viele Charaktere an – Hauptcharaktere, Gefährten, Reittiere, Vertraute … "
                  "– und weise sie einem Spieler zu. Jeder Charakter kann ein Aussehen und **mehrere Bilder** haben "
                  "(z. B. Turnaround). Die Bilder nutzt der Comic als Vorlage.")
        split = QSplitter(Qt.Horizontal)
        left = plain()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.chars = QListWidget()
        self.chars.currentItemChanged.connect(lambda *_: self.load_char())
        ll.addWidget(self.chars)
        right = plain()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(12, 0, 0, 0)
        self.c_name = QLineEdit()
        self.c_name.setPlaceholderText("z. B. Gnabber")
        self.c_art = QComboBox()
        self.c_art.setEditable(True)
        self.c_art.addItems(C.ARTEN)
        self.c_owner = QComboBox()
        gg = QGridLayout()
        gg.addWidget(label("Name"), 0, 0)
        gg.addWidget(label("Art"), 0, 1)
        gg.addWidget(label("Gehört zu"), 0, 2)
        gg.addWidget(self.c_name, 1, 0)
        gg.addWidget(self.c_art, 1, 1)
        gg.addWidget(self.c_owner, 1, 2)
        gg.setColumnStretch(0, 2)
        rl.addLayout(gg)
        rl.addWidget(label("Aussehen"))
        self.c_look = QPlainTextEdit()
        self.c_look.setPlaceholderText("z. B. Volk, Größe, Haare, Augen, Kleidung, Ausrüstung, Besonderheiten …")
        self.c_look.setMinimumHeight(160)
        rl.addWidget(self.c_look)
        rl.addWidget(label("Bilder (z. B. Turnaround)"))
        self.c_imgs = QListWidget()
        self.c_imgs.setViewMode(QListWidget.IconMode)
        self.c_imgs.setIconSize(QSize(150, 150))
        self.c_imgs.setResizeMode(QListWidget.Adjust)
        self.c_imgs.setMinimumHeight(190)
        self.c_imgs.setSpacing(6)
        rl.addWidget(self.c_imgs)
        rl.addLayout(hbox(button("🖼️ Bilder hinzufügen …", slot=self.add_images),
                          button("🗑️ Ausgewähltes Bild entfernen", slot=self.del_image), None))
        rl.addLayout(hbox(button("💾 Charakter speichern", "primary", slot=self.save_char),
                          button("🗑️ Charakter löschen", "danger", slot=self.del_char), None))
        self.c_status = label("")
        rl.addWidget(self.c_status)
        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([260, 700])
        ch.add(split)
        lay.addWidget(ch)
        lay.addStretch(1)
        self.refresh()

    # ---------- Befüllen ----------
    def refresh(self, keep_char=None):
        g = C.group()
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItems(C.list_groups())
        self.group.setCurrentText(C.active_group())
        self.group.blockSignals(False)
        self.ich.setText(g.get("ich_name", "Ich"))
        self.players.clear()
        for sp in g.get("spieler", []):
            if sp.get("name"):
                self._player_item(sp["name"])
        self._fill_dm(g)
        self.kontext.setPlainText(g.get("kontext", ""))
        self._fill_chars(keep_char)

    def _player_item(self, name):
        it = QListWidgetItem(name)
        it.setFlags(it.flags() | Qt.ItemIsEditable)
        self.players.addItem(it)
        return it

    def _fill_dm(self, g):
        self.dm.clear()
        self.dm.addItem(f"Ich ({g.get('ich_name', 'Ich')})", C.ICH_ID)
        for sp in g.get("spieler", []):
            if sp.get("name"):
                self.dm.addItem(sp["name"], sp["name"])
        i = self.dm.findData(g.get("dm", C.ICH_ID))
        self.dm.setCurrentIndex(max(0, i))

    def _fill_chars(self, keep=None):
        g = C.group()
        self.chars.blockSignals(True)
        self.chars.clear()
        self.chars.addItem(NEU)
        sel = 0
        for c in C.characters(g):
            n = len(c.get("bilder") or [])
            it = QListWidgetItem(f"{c['name']}\n{c.get('art', '')} · {c.get('besitzer', '?')} · {n} Bild(er)")
            it.setData(Qt.UserRole, c["name"])
            imgs = C.char_images(c, g)
            if imgs:
                it.setIcon(QIcon(QPixmap(str(imgs[0])).scaled(48, 48, Qt.KeepAspectRatio, Qt.SmoothTransformation)))
            self.chars.addItem(it)
            if keep and c["name"] == keep:
                sel = self.chars.count() - 1
        self.chars.blockSignals(False)
        self.chars.setCurrentRow(sel)
        self.load_char()

    def _sel(self):
        it = self.chars.currentItem()
        return it.data(Qt.UserRole) if it and it.data(Qt.UserRole) else None

    def _find(self, g, name):
        return next((c for c in g.get("charaktere", []) if c.get("name") == name), None)

    def load_char(self):
        g = C.group()
        c = self._find(g, self._sel()) if self._sel() else None
        owners = owner_choices(g)
        self.c_owner.clear()
        self.c_owner.addItems(owners)
        self.c_name.setText(c["name"] if c else "")
        self.c_art.setCurrentText((c or {}).get("art", "Hauptcharakter"))
        if c and c.get("besitzer") in owners:
            self.c_owner.setCurrentText(c["besitzer"])
        self.c_look.setPlainText((c or {}).get("aussehen", ""))
        self.c_imgs.clear()
        for p in (C.char_images(c, g) if c else []):
            it = QListWidgetItem(QIcon(QPixmap(str(p)).scaled(150, 150, Qt.KeepAspectRatio, Qt.SmoothTransformation)), "")
            it.setToolTip(str(p))
            self.c_imgs.addItem(it)
        self.c_status.setText("")

    # ---------- Gruppen ----------
    def _switch(self):
        if REC.running:
            error(self, "Während einer Aufnahme kann die Gruppe nicht gewechselt werden.")
            self.group.setCurrentText(C.active_group())
            return
        C.set_active_group(self.group.currentText())
        log.info("Aktive Gruppe: %s", C.active_group())
        self.refresh()
        self.changed.emit()

    def create(self):
        name, ok = QInputDialog.getText(self, "Neue Gruppe", "Name der neuen Gruppe, z. B. Montagsrunde:")
        if not ok or not name.strip():
            return
        try:
            n = C.create_group(name)
        except ValueError as e:
            error(self, str(e))
            return
        C.set_active_group(n)
        log.info("Gruppe angelegt: %s", n)
        self.refresh()
        self.changed.emit()
        self.status.setText(f"➕ Gruppe **{n}** angelegt – trag jetzt die Mitspieler ein.")

    def rename(self):
        if REC.running:
            error(self, "Während einer Aufnahme kann die Gruppe nicht umbenannt werden.")
            return
        old = C.active_group()
        name, ok = QInputDialog.getText(self, "Gruppe umbenennen", "Neuer Name:", text=old)
        if not ok or not name.strip() or name.strip() == old:
            return
        try:
            n = C.rename_group(old, name)
        except ValueError as e:
            error(self, str(e))
            return
        log.info("Gruppe umbenannt: %s → %s", old, n)
        self.refresh()
        self.changed.emit()

    def delete(self):
        name = C.active_group()
        if not ask(self, f"Gruppe „{name}“ wirklich löschen?\n\nMitspieler, gelernte Stimmen und Bilder der Gruppe "
                         "werden entfernt – Aufnahmen und Sessions bleiben erhalten."):
            return
        try:
            C.delete_group(name)
        except ValueError as e:
            error(self, str(e))
            return
        log.info("Gruppe gelöscht: %s", name)
        self.refresh()
        self.changed.emit()

    # ---------- Runde ----------
    def add_player(self):
        it = self._player_item("Neuer Mitspieler")
        self.players.setCurrentItem(it)
        self.players.editItem(it)

    def remove_player(self):
        for it in self.players.selectedItems():
            self.players.takeItem(self.players.row(it))

    def save(self):
        old = C.group()
        names = []
        for k in range(self.players.count()):
            n = self.players.item(k).text().strip()
            if n and n not in names:
                names.append(n)
        dm = self.dm.currentData()
        if dm != C.ICH_ID and dm not in names:
            dm = C.ICH_ID
        new_me = self.ich.text().strip() or "Ich"
        chars = old.get("charaktere", [])
        for c in chars:
            if c.get("besitzer") == old.get("ich_name") and new_me != old.get("ich_name"):
                c["besitzer"] = new_me
        g = dict(old)
        g.update({"ich_name": new_me, "dm": dm, "spieler": [{"name": n} for n in names],
                  "kontext": self.kontext.toPlainText(), "charaktere": chars})
        C.save_group(g)
        wer = "Du bist der DM." if dm == C.ICH_ID else f"{dm} ist der DM."
        msg = f"✅ Gespeichert: {len(names)} Mitspieler. {wer}"
        lost = [c["name"] for c in chars if c.get("besitzer") and c["besitzer"] not in owner_choices(g)]
        if lost:
            msg += "  ⚠️ Charaktere ohne gültigen Besitzer: " + ", ".join(lost)
        self.status.setText(msg)
        self._fill_dm(g)
        self._fill_chars(self._sel())
        self.changed.emit()

    # ---------- Charaktere ----------
    def save_char(self):
        name = self.c_name.text().strip()
        if not name:
            error(self, "Bitte einen Namen für den Charakter eingeben.")
            return
        g = C.group()
        g.setdefault("charaktere", [])
        sel = self._sel()
        c = self._find(g, sel) if sel else None
        other = self._find(g, name)
        if other and other is not c:
            error(self, f"Einen Charakter „{name}“ gibt es schon.")
            return
        if c is None:
            c = {"name": name, "bilder": []}
            g["charaktere"].append(c)
        c.update({"name": name, "art": self.c_art.currentText().strip() or "Hauptcharakter",
                  "besitzer": self.c_owner.currentText(), "aussehen": self.c_look.toPlainText().strip()})
        C.save_group(g)
        log.info("Charakter gespeichert: %s (%s, %s)", name, c["art"], c["besitzer"])
        self._fill_chars(name)
        self.c_status.setText(f"💾 **{name}** gespeichert.")
        self.changed.emit()

    def add_images(self):
        sel = self._sel()
        if not sel:
            error(self, "Bitte den Charakter zuerst speichern, dann Bilder hinzufügen.")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Bilder wählen", "", "Bilder (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not files:
            return
        from PIL import Image
        g = C.group()
        c = self._find(g, sel)
        safe = "".join(ch if ch.isalnum() else "_" for ch in sel) or "charakter"
        n = 0
        for path in files:
            k = len(c.setdefault("bilder", [])) + 1
            while (C.images_dir() / f"char_{safe}_{k}.png").exists():
                k += 1
            try:
                img = Image.open(path)
                img.thumbnail((1536, 1536))
                img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB").save(C.images_dir() / f"char_{safe}_{k}.png")
            except Exception as e:
                error(self, f"{path} konnte nicht gelesen werden: {e}")
                continue
            c["bilder"].append(f"char_{safe}_{k}.png")
            n += 1
        C.save_group(g)
        self._fill_chars(sel)
        self.c_status.setText(f"🖼️ {n} Bild(er) zu **{sel}** hinzugefügt.")

    def del_image(self):
        sel = self._sel()
        row = self.c_imgs.currentRow()
        g = C.group()
        c = self._find(g, sel) if sel else None
        if not c or row < 0 or row >= len(c.get("bilder") or []):
            error(self, "Bitte zuerst ein Bild anklicken.")
            return
        f = c["bilder"].pop(row)
        (C.images_dir() / f).unlink(missing_ok=True)
        C.save_group(g)
        self._fill_chars(sel)
        self.c_status.setText("Bild entfernt.")

    def del_char(self):
        sel = self._sel()
        if not sel:
            return
        if not ask(self, f"Charakter „{sel}“ wirklich löschen? Seine Bilder werden ebenfalls entfernt."):
            return
        g = C.group()
        c = self._find(g, sel)
        if c:
            for f in c.get("bilder") or []:
                (C.images_dir() / f).unlink(missing_ok=True)
            g["charaktere"].remove(c)
            C.save_group(g)
        self._fill_chars()
        self.c_status.setText(f"🗑️ **{sel}** gelöscht.")
        self.changed.emit()
