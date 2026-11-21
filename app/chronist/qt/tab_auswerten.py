"""Tab „Session auswerten“: Aufnahme wählen → Analysieren → Stimmen zuordnen → Chronik, Comic, Abschrift."""
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QScrollArea, QSpinBox, QTabWidget, QTextBrowser, QVBoxLayout,
                               QWidget)

from .. import config as C
from .. import llm as L
from .. import pipeline as P
from .. import summary as S
from ..debuglog import log
from .common import (Card, StatusCard, ask, button, cancel_all, error, hbox, label, open_path, plain, run_job)

EXT = {".mka", ".mkv", ".mp4", ".mov", ".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac"}


def recordings():
    folder = C.recordings_dir()
    if not folder.exists():
        return []
    files = [p for p in folder.iterdir() if p.suffix.lower() in EXT and p.is_file()
             and not (p.suffix.lower() == ".mp3" and p.with_suffix(".mka").exists())]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files[:40]:
        t = time.strftime("%d.%m.%Y %H:%M", time.localtime(p.stat().st_mtime))
        out.append((f"{p.name}  ·  {t}  ·  {p.stat().st_size / 1e6:.0f} MB", str(p)))
    return out


def name_choices():
    g = C.group()
    names = [s["name"] for s in g.get("spieler", []) if s.get("name")]
    return names + [g.get("ich_name", "Ich"), C.UNBEKANNT, C.IGNORIEREN]


def n_spieler():
    return len([s for s in C.group().get("spieler", []) if s.get("name")])


class Player:
    """Hörproben abspielen (eine zur Zeit)."""

    def __init__(self):
        self.player = None
        self.btn = None

    def toggle(self, path, btn):
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        except Exception as e:
            error(btn, f"Wiedergabe nicht möglich: {e}")
            return
        if self.player is None:
            self.player = QMediaPlayer()
            self.out = QAudioOutput()
            self.player.setAudioOutput(self.out)
            self.player.playbackStateChanged.connect(self._state)
        if self.btn is btn and self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.stop()
            return
        if self.btn:
            self.btn.setText("▶  Anhören")
        self.btn = btn
        self.player.setSource(QUrl.fromLocalFile(str(path)))
        self.player.play()
        btn.setText("⏸  Stopp")

    def _state(self, st):
        from PySide6.QtMultimedia import QMediaPlayer
        if st != QMediaPlayer.PlayingState and self.btn:
            self.btn.setText("▶  Anhören")


class ComicViewer(QScrollArea):
    """Seiten bzw. Webtoon-Abschnitte untereinander zum Scrollen."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setMinimumHeight(420)
        self.inner = QWidget()
        self.inner.setStyleSheet("background:#0e0b09;")
        self.lay = QVBoxLayout(self.inner)
        self.lay.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
        self.setWidget(self.inner)
        self.pages = []

    def show_pages(self, pages):
        while self.lay.count():
            w = self.lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.pages = [p for p in pages if Path(p).exists()]
        self._labels = []
        for p in self.pages:
            lab = QLabel()
            lab.setAlignment(Qt.AlignCenter)
            lab._pix = QPixmap(str(p))
            self._labels.append(lab)
            self.lay.addWidget(lab)
        self._rescale()

    def _rescale(self):
        w = max(300, min(900, self.viewport().width() - 40))
        for lab in getattr(self, "_labels", []):
            if not lab._pix.isNull():
                lab.setPixmap(lab._pix.scaledToWidth(w, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()


class AuswertenTab(QScrollArea):
    group_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 18, 24, 24)
        lay.setSpacing(14)
        self.state = None
        self.player = Player()
        self.job = None

        # ① Aufnahme wählen
        c = Card("① Aufnahme wählen")
        self.group = QComboBox()
        self.group.activated.connect(self._group_picked)
        self.rec = QComboBox()
        self.rec.setEditable(False)
        self.rec.setMinimumWidth(420)
        c.add(label("Gruppe"))
        c.add(self.group)
        c.add(label("Aufnahme (neueste zuerst)"))
        c.add(hbox(self.rec, button("🔄", tip="Liste neu laden", slot=self.fill_recordings),
                   button("Datei wählen …", slot=self.pick_file)))
        self.title = QLineEdit()
        self.title.setPlaceholderText("z. B. Session 12 – Der Keller")
        self.n_andere = QSpinBox()
        self.n_andere.setRange(1, 30)
        g = QGridLayout()
        g.addWidget(label("Titel der Session"), 0, 0)
        g.addWidget(label("Leute außer dir", wrap=False), 0, 1)
        g.addWidget(self.title, 1, 0)
        g.addWidget(self.n_andere, 1, 1)
        g.setColumnStretch(0, 3)
        c.add(g)
        self.with_comic = QCheckBox("Comic-Vorlage mit erstellen")
        self.with_comic.setChecked(True)
        self.with_draw = QCheckBox("🎨 Comic-Seiten direkt malen (nach der Chronik, auf der Grafikkarte)")
        c.add(hbox(self.with_comic, self.with_draw, None))
        # Erweitert
        self.adv_btn = button("▸ Erweitert", slot=self._toggle_adv)
        self.adv = plain()
        al = QVBoxLayout(self.adv)
        al.setContentsMargins(0, 0, 0, 0)
        self.swapped = QCheckBox("Spuren vertauscht (Discord ist Spur 1, Mikro Spur 2)")
        self.saved = QComboBox()
        self.saved.setMinimumWidth(320)
        al.addWidget(self.swapped)
        al.addWidget(label("Gespeicherte Session öffnen"))
        al.addLayout(hbox(self.saved, button("Öffnen", slot=self.load_session), None))
        self.adv.hide()
        c.add(hbox(self.adv_btn, None))
        c.add(self.adv)
        lay.addWidget(c)

        # ② Auswerten
        c2 = Card("② Auswerten")
        self.go = button("▶  Analysieren", "primary", slot=self.analyse)
        self.go.setMinimumHeight(46)
        self.cancel = button("⏹  Abbrechen", "danger", slot=self.do_cancel)
        self.cancel.setMinimumHeight(46)
        c2.add(hbox(self.go, self.cancel))
        self.status = StatusCard("Klick auf **▶ Analysieren** – der Rest passiert von selbst: Abschrift, Stimmen "
                                 "erkennen, KI starten, Chronik schreiben, KI beenden. Bei 3 Stunden Aufnahme grob "
                                 "30–75 Minuten.")
        c2.add(self.status)
        lay.addWidget(c2)

        # Wer ist wer?
        self.voice_card = Card("Wer ist wer?", "Lieber eine Stimme zu viel als zu wenig – **mehrere Stimmen dürfen "
                               "dieselbe Person sein**. Hör rein und ordne jede zu. Musik-Bots o. Ä. auf "
                               "*(ignorieren)*. Fehlt jemand? Im Tab **Gruppe** eintragen.")
        self.voice_grid = QGridLayout()
        self.voice_grid.setColumnStretch(0, 3)
        self.voice_card.add(self.voice_grid)
        # Neu trennen
        self.resplit_n = QSpinBox()
        self.resplit_n.setRange(1, 30)
        self.resplit_btn = button("🔄 Stimmen neu trennen", slot=self.resplit)
        self.resplit_box = plain()
        rl = QVBoxLayout(self.resplit_box)
        rl.setContentsMargins(0, 6, 0, 0)
        rl.addWidget(label("Passt nicht? Fehlt eine Person oder sind zwei in einer Stimme gelandet? Neu trennen – "
                           "dauert ein paar Minuten, die Abschrift bleibt.", "muted"))
        rl.addLayout(hbox(label("Leute außer dir:", wrap=False), self.resplit_n, self.resplit_btn, None))
        self.voice_card.add(self.resplit_box)
        self.finish_btn = button("Weiter ▶", "primary", slot=self.finish)
        self.finish_btn.setMinimumHeight(44)
        self.voice_card.add(self.finish_btn)
        self.voice_card.hide()
        lay.addWidget(self.voice_card)
        self.voice_rows = []

        # ③ Ergebnis
        c3 = Card("③ Ergebnis")
        self.results = QTabWidget()
        self.results.setObjectName("inner")
        self.summary = QTextBrowser()
        self.summary.setOpenExternalLinks(True)
        self.summary.setMinimumHeight(420)
        self.summary.setMarkdown("*Noch keine Chronik – sie erscheint hier nach der Auswertung.*")
        self.results.addTab(self.summary, "📖 Chronik")
        # Comic
        comic = plain()
        cl = QVBoxLayout(comic)
        self.draw_btn = button("🎨  Comic-Seiten erstellen", "primary", slot=self.draw)
        self.tpl_btn = button("📝 Comic-Vorlage (neu) schreiben", slot=self.write_template)
        self.pdf_btn = button("📄 PDF öffnen", slot=self.open_pdf)
        self.pdf_btn.hide()
        cl.addLayout(hbox(self.draw_btn, self.tpl_btn, self.pdf_btn, None))
        self.draw_status = StatusCard("Malt aus der Comic-Vorlage jedes Panel auf deiner Grafikkarte und setzt es mit "
                                      "Sprechblasen zu Seiten zusammen. Charakterbilder dienen als Vorlage. Format und "
                                      "Bildmodell stellst du unter ⚙️ Einstellungen ein.")
        cl.addWidget(self.draw_status)
        self.viewer = ComicViewer()
        cl.addWidget(self.viewer, 1)
        self.tpl_toggle = button("▸ Comic-Vorlage (Text) – Panels von Hand korrigieren", slot=self._toggle_tpl)
        self.tpl = QPlainTextEdit()
        self.tpl.setPlaceholderText("Noch keine Comic-Vorlage.")
        self.tpl.setMinimumHeight(320)
        self.tpl.hide()
        cl.addLayout(hbox(self.tpl_toggle, None))
        cl.addWidget(self.tpl)
        self.results.addTab(comic, "🎨 Comic")
        # Abschrift
        tr = plain()
        tl = QVBoxLayout(tr)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText("Die komplette Abschrift erscheint hier.")
        self.transcript.setMinimumHeight(420)
        tl.addWidget(self.transcript)
        tl.addLayout(hbox(button("📋 Kopieren", slot=lambda: QGuiApplication.clipboard().setText(
            self.transcript.toPlainText())), None))
        self.results.addTab(tr, "📝 Abschrift")
        c3.add(self.results)
        c3.add(hbox(button("📂 Session-Ordner öffnen", slot=self.open_folder), None))
        lay.addWidget(c3)
        self.result_card = c3
        lay.addStretch(1)
        self.refresh()

    # ---------- Befüllen ----------
    def refresh(self):
        self.group.blockSignals(True)
        self.group.clear()
        self.group.addItems(C.list_groups())
        self.group.setCurrentText(C.active_group())
        self.group.blockSignals(False)
        self.fill_recordings()
        self.saved.clear()
        self.saved.addItems(P.list_sessions())
        n = n_spieler() or 4
        self.n_andere.setValue(n)
        self.resplit_n.setValue(n)
        if self.state:
            self._show_voices()

    def fill_recordings(self, select=None):
        cur = select or self.rec.currentData()
        self.rec.clear()
        for text, path in recordings():
            self.rec.addItem(text, path)
        if cur:
            i = self.rec.findData(str(cur))
            if i < 0 and Path(str(cur)).exists():
                self.rec.insertItem(0, Path(str(cur)).name, str(cur))
                i = 0
            if i >= 0:
                self.rec.setCurrentIndex(i)

    def select_recording(self, path):
        self.fill_recordings(select=path)
        try:
            from ..recorder import REC
            if getattr(REC, "session_name", ""):
                self.title.setText(REC.session_name)
        except Exception:
            pass

    def pick_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "Aufnahme wählen", str(C.recordings_dir()),
                                           "Audio/Video (*.mka *.mkv *.mp4 *.mov *.wav *.mp3 *.flac *.m4a *.ogg *.aac)")
        if f:
            self.fill_recordings(select=f)

    def _group_picked(self):
        C.set_active_group(self.group.currentText())
        log.info("Aktive Gruppe: %s", C.active_group())
        self.group_changed.emit()

    def _toggle_adv(self):
        vis = not self.adv.isVisible()
        self.adv.setVisible(vis)
        self.adv_btn.setText(("▾" if vis else "▸") + " Erweitert")
        if vis:
            self.saved.clear()
            self.saved.addItems(P.list_sessions())

    def _toggle_tpl(self):
        vis = not self.tpl.isVisible()
        self.tpl.setVisible(vis)
        self.tpl_toggle.setText(("▾" if vis else "▸") + " Comic-Vorlage (Text) – Panels von Hand korrigieren")

    # ---------- Fortschritt ----------
    def _busy(self, on, card=None):
        for b in (self.go, self.finish_btn, self.resplit_btn, self.draw_btn, self.tpl_btn):
            b.setEnabled(not on)
        self.cancel.setEnabled(True)

    def do_cancel(self):
        cancel_all()
        self._busy(False)
        for card in (self.status, self.draw_status):
            card.done()
        self.status.set("⏹ Abgebrochen. Eine gestartete KI und der Bildgenerator wurden beendet.")

    def _fail(self, card, prefix=""):
        def f(msg):
            self._busy(False)
            card.done(f"⚠️ {prefix}{msg}")
        return f

    # ---------- Analysieren ----------
    def analyse(self):
        rec = self.rec.currentData()
        if not rec:
            error(self, "Bitte zuerst eine Aufnahme auswählen.")
            return
        title, n = self.title.text().strip(), self.n_andere.value() or n_spieler()
        swapped, with_comic, with_draw = self.swapped.isChecked(), self.with_comic.isChecked(), self.with_draw.isChecked()
        self.voice_card.hide()
        self._busy(True)
        self.status.set("⏳ Läuft – du kannst nebenbei weitermachen.")

        def work(progress):
            state = P.analyse(rec, title, n, swapped, progress=lambda f, m: progress(f * 0.6, m))
            if any(not v["vorschlag"] for v in state["stimmen"]):
                return state, None
            names = [v["vorschlag"] for v in state["stimmen"]]
            return state, self._finish_work(state, with_comic, names, lambda f, m: progress(0.6 + f * 0.4, m),
                                            with_draw)

        def done(res):
            state, fin = res
            self.state = state
            self._busy(False)
            if fin is None:
                unb = sum(1 for v in state["stimmen"] if not v["vorschlag"])
                self.status.done(f"⏸️ Fast geschafft – **{unb} Stimme(n) kenne ich noch nicht.** Hör kurz rein, wähl "
                                 "den Namen und klick auf **Weiter**. Ab dem nächsten Mal erkenne ich sie von selbst.")
                self._show_voices()
                QTimer.singleShot(100, lambda: self.ensureWidgetVisible(self.voice_card))
                return
            self._show_result(fin)
            self.status.done("✅ Fertig! Alle Stimmen wiedererkannt – hier ist eure Chronik." if fin["ok"] else
                             "⚠️ Abschrift fertig, aber die KI hatte ein Problem – Details im Ergebnis.")
        self.job = run_job(work, on_done=done, on_error=self._fail(self.status),
                           on_progress=self.status.progress, name="Analyse")
        if not self.job:
            self._busy(False)

    def _finish_work(self, state, with_comic, names, pr, with_draw=False):
        """Läuft im Hintergrund: Abschrift bauen, KI starten, Chronik/Comic schreiben, KI beenden."""
        mapping = {v["id"]: (n or C.UNBEKANNT) for v, n in zip(state["stimmen"], names)}
        text = P.build_transcript(state, mapping)
        comic, pages = "", []
        try:
            with L.KI(pr) as url:
                summ = S.summarize(state, url, progress=pr)
                if with_comic:
                    comic = S.comic(state, url, progress=pr)
            if with_draw and comic:
                from .. import comic as CO
                pages = CO.run(state, progress=pr)
            ok = True
        except Exception as e:
            from .common import Cancelled
            if isinstance(e, Cancelled):
                raise
            log.exception("Zusammenfassung fehlgeschlagen")
            summ = (f"⚠️ Die Abschrift ist fertig, aber die Zusammenfassung ging schief:\n\n`{e}`\n\nÖffne die Session "
                    "unter „Erweitert → Gespeicherte Session öffnen“ und klick auf **Weiter** – die Abschrift muss "
                    "nicht wiederholt werden.")
            ok = False
        return {"text": text, "summary": summ, "comic": comic, "pages": pages, "ok": ok}

    def _show_voices(self):
        while self.voice_grid.count():
            w = self.voice_grid.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.voice_rows = []
        st = self.state or {}
        zu = st.get("zuordnung", {})
        choices = name_choices()
        for r, v in enumerate(st.get("stimmen", [])):
            min_ = v["dauer"] / 60
            erkannt = (f"erkannt als **{v['vorschlag']}** ({int(v['aehnlichkeit'] * 100)} % sicher)"
                       if v["vorschlag"] else "noch unbekannt – bitte zuordnen")
            head = (f"**{v['discord']['anzeige']}** (Discord: {v['discord']['name']})" if v.get("discord")
                    else f"**Stimme {v['nr']}**")
            info = label(f"{head} · {min_:.1f} Min. · {erkannt}")
            play = button("▶  Anhören")
            clip = v.get("clip")
            play.setEnabled(bool(clip and Path(clip).exists()))
            play.clicked.connect(lambda _=False, p=clip, b=play: self.player.toggle(p, b))
            combo = QComboBox()
            combo.addItem("– bitte wählen –", None)
            for n in choices:
                combo.addItem(n, n)
            val = zu.get(v["id"]) or v["vorschlag"]
            if val in choices:
                combo.setCurrentIndex(choices.index(val) + 1)
            combo.setMinimumWidth(200)
            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet("color:#3a3027;")
            self.voice_grid.addWidget(info, r * 2, 0)
            self.voice_grid.addWidget(play, r * 2, 1)
            self.voice_grid.addWidget(combo, r * 2, 2)
            self.voice_grid.addWidget(sep, r * 2 + 1, 0, 1, 3)
            self.voice_rows.append(combo)
        self.resplit_box.setVisible(st.get("modus") != "einzelspuren")
        self.voice_card.setVisible(bool(st.get("stimmen")))

    def finish(self):
        if not self.state:
            error(self, "Bitte zuerst analysieren oder eine Session laden.")
            return
        names = [c.currentData() for c in self.voice_rows]
        state, with_comic, with_draw = self.state, self.with_comic.isChecked(), self.with_draw.isChecked()
        self._busy(True)
        self.status.set("⏳ Die KI schreibt die Chronik …")

        def done(fin):
            self._busy(False)
            self.voice_card.setVisible(not fin["ok"])
            self._show_result(fin)
            self.status.done("✅ Fertig! Hier ist eure Chronik." if fin["ok"] else
                             "⚠️ Die KI hatte ein Problem – Details im Ergebnis.")
        self.job = run_job(lambda progress: self._finish_work(state, with_comic, names, progress, with_draw),
                           on_done=done, on_error=self._fail(self.status), on_progress=self.status.progress,
                           name="Chronik")
        if not self.job:
            self._busy(False)

    def resplit(self):
        if not self.state:
            return
        state, n = self.state, self.resplit_n.value()
        self._busy(True)

        def done(st):
            self.state = st
            self._busy(False)
            self.status.done(f"🔄 Neu getrennt: **{len(st['stimmen'])} Stimmen** zum Zuordnen. Mehrere Stimmen dürfen "
                             "dieselbe Person sein – ordne jede zu und klick auf **Weiter**.")
            self._show_voices()
        self.job = run_job(lambda progress: P.resplit(state, n, progress=progress), on_done=done,
                           on_error=self._fail(self.status), on_progress=self.status.progress, name="Neu trennen")
        if not self.job:
            self._busy(False)

    def _show_result(self, fin):
        self.summary.setMarkdown(fin["summary"] or "")
        self.transcript.setPlainText(fin["text"] or "")
        if fin.get("comic"):
            self.tpl.setPlainText(fin["comic"])
        self.show_pages()
        self.results.setCurrentIndex(0)
        QTimer.singleShot(100, lambda: self.ensureWidgetVisible(self.result_card))

    # ---------- Session öffnen ----------
    def load_session(self):
        name = self.saved.currentText()
        if not name:
            error(self, "Keine Session gewählt.")
            return
        try:
            self.state = P.load_state(C.sessions_dir() / name)
        except Exception as e:
            error(self, f"Session konnte nicht geöffnet werden: {e}")
            return
        folder = Path(self.state["ordner"])
        rd = lambda n: (folder / n).read_text(encoding="utf-8") if (folder / n).exists() else ""
        self.summary.setMarkdown(rd("zusammenfassung.md") or "*Noch keine Chronik.*")
        self.transcript.setPlainText(rd("transkript.txt"))
        self.tpl.setPlainText(rd("comic_vorlage.md"))
        self.title.setText(self.state.get("titel", ""))
        self._show_voices()
        self.show_pages()
        self.status.done(f"Session **{self.state['titel']}** geladen.")

    def _folder(self):
        if not self.state or not self.state.get("ordner"):
            return None
        p = Path(self.state["ordner"])
        if not p.exists():
            grp = self.state.get("gruppe")
            cand = (C.sessions_dir(grp) if grp in C.list_groups() else C.sessions_dir()) / p.name
            if cand.exists():
                self.state["ordner"] = str(cand)
                p = cand
        return p

    def open_folder(self):
        open_path(self._folder() or C.sessions_dir())

    # ---------- Comic ----------
    def show_pages(self):
        d = (self._folder() or Path("/nonexistent")) / "comic"
        pages = []
        if (d / "seiten.json").exists():
            try:
                pages = [str(d / n) for n in json.loads((d / "seiten.json").read_text(encoding="utf-8"))]
            except Exception:
                pages = []
        if not pages and d.exists():
            pages = sorted(str(p) for p in d.glob("seite_*.png"))
        self.viewer.show_pages(pages)
        self.pdf_btn.setVisible((d / "Comic.pdf").exists())

    def open_pdf(self):
        f = (self._folder() or Path(".")) / "comic" / "Comic.pdf"
        if f.exists():
            open_path(f)

    def write_template(self):
        if not self.state:
            error(self, "Keine Session geladen.")
            return
        state = self.state
        self._busy(True)

        def work(progress):
            with L.KI(progress) as url:
                return S.comic(state, url, progress=progress)

        def done(text):
            self._busy(False)
            self.tpl.setPlainText(text)
            self.tpl.show()
            self.draw_status.done("📝 Neue Comic-Vorlage geschrieben.")
        self.job = run_job(work, on_done=done, on_error=self._fail(self.draw_status),
                           on_progress=self.draw_status.progress, name="Comic-Vorlage")
        if not self.job:
            self._busy(False)

    def draw(self):
        if not self.state:
            error(self, "Bitte zuerst eine Session auswerten oder öffnen.")
            return
        from .. import comic as CO
        state = self.state
        edited = self.tpl.toPlainText()
        tplf = Path(state["ordner"]) / "comic_vorlage.md"
        if edited.strip() and "panel" in edited.lower():
            old = tplf.read_text(encoding="utf-8") if tplf.exists() else ""
            if edited.strip() != old.strip():
                tplf.write_text(edited.strip() + "\n", encoding="utf-8")
                log.info("Comic-Vorlage von Hand geändert und gespeichert")
        self._busy(True)
        self.draw_status.set("⏳ Comic wird gemalt …")

        def work(progress):
            if not tplf.exists():
                with L.KI(progress) as url:
                    S.comic(state, url, progress=progress)
            return CO.run(state, progress=progress)

        def done(pages):
            self._busy(False)
            self.tpl.setPlainText(tplf.read_text(encoding="utf-8") if tplf.exists() else "")
            self.show_pages()
            self.draw_status.done(f"✅ Fertig: **{len(pages)} Seite(n)** – liegen auch im Session-Ordner unter `comic`.")
        self.job = run_job(work, on_done=done, on_error=self._fail(self.draw_status, "Comic konnte nicht gemalt werden: "),
                           on_progress=self.draw_status.progress, name="Comic malen")
        if not self.job:
            self._busy(False)
