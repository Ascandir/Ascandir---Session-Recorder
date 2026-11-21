"""Hauptfenster der Desktop-App (Qt) – läuft unter Windows und Linux."""
import os
import sys
import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMainWindow, QTabWidget, QVBoxLayout, QWidget

from .. import config as C
from .. import llm as L
from .. import updater as U
from ..debuglog import log
from ..recorder import REC
from .common import apply_theme, ask, button, error, label, resource, run_job
from .tab_aufnahme import AufnahmeTab
from .tab_auswerten import AuswertenTab
from .tab_einstellungen import EinstellungenTab
from .tab_gruppe import GruppeTab

APP_ID = "ascandir-sessionrecorder"


class MainWindow(QMainWindow):
    update_found = Signal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Ascandir · SessionRecorder")
        self.resize(1280, 900)
        self.setMinimumSize(980, 680)
        icon = resource("icon.png")
        if icon:
            self.setWindowIcon(QIcon(str(icon)))
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # Kopfzeile
        head = QFrame()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(24, 16, 24, 6)
        title = QVBoxLayout()
        title.addWidget(label(f"🎲 Ascandir · SessionRecorder  <span style='font-size:9pt;color:#a8998a'>v{U.VERSION}</span>",
                              "h1", md=False))
        title.addWidget(label("Aufnahme rein, Chronik raus – alles bleibt auf deinem PC.", "muted"))
        hl.addLayout(title, 1)
        hl.addWidget(button("⬆️ Updates", slot=self.check_update))
        lay.addWidget(head)

        # Update-Hinweis
        self.upd_bar = QFrame()
        self.upd_bar.setObjectName("status")
        ul = QHBoxLayout(self.upd_bar)
        ul.setContentsMargins(16, 10, 16, 10)
        self.upd_text = label("")
        self.upd_go = button("⬆️ Jetzt aktualisieren", "primary", slot=self.do_update)
        ul.addWidget(self.upd_text, 1)
        ul.addWidget(self.upd_go)
        ul.addWidget(button("✕", slot=self.upd_bar.hide))
        self.upd_bar.hide()
        wrap = QWidget()
        wl = QVBoxLayout(wrap)
        wl.setContentsMargins(24, 0, 24, 4)
        wl.addWidget(self.upd_bar)
        lay.addWidget(wrap)

        # Hinweis beim ersten Start: Modelle fehlen noch
        self.mod_bar = QFrame()
        self.mod_bar.setObjectName("status")
        ml = QHBoxLayout(self.mod_bar)
        ml.setContentsMargins(16, 10, 16, 10)
        ml.addWidget(label("📥 **Die KI-Modelle sind noch nicht geladen.** Sie kommen sonst beim ersten Auswerten bzw. "
                           "Comic von selbst – oder du lädst jetzt alles vorab."), 1)
        ml.addWidget(button("📥 Alle Modelle jetzt laden", "primary", slot=self._load_models_now))
        ml.addWidget(button("✕", slot=self.mod_bar.hide))
        self.mod_bar.hide()
        wl.addWidget(self.mod_bar)
        try:
            from .. import models
            self.mod_bar.setVisible(not models.all_present())
        except Exception:
            pass

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.t_rec = AufnahmeTab()
        self.t_eval = AuswertenTab()
        self.t_group = GruppeTab()
        self.t_set = EinstellungenTab()
        self.tabs.addTab(self.t_rec, "🎙️  Aufnahme")
        self.tabs.addTab(self.t_eval, "📜  Session auswerten")
        self.tabs.addTab(self.t_group, "👥  Gruppe")
        self.tabs.addTab(self.t_set, "⚙️  Einstellungen")
        lay.addWidget(self.tabs, 1)

        self.t_rec.recording_saved.connect(self._recording_saved)
        for t in (self.t_rec, self.t_eval):
            t.group_changed.connect(self.refresh_all)
        self.t_group.changed.connect(self.refresh_all)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.update_found.connect(self._show_update)

        if C.settings().get("update_beim_start", True):
            threading.Thread(target=lambda: self.update_found.emit(U.check()), daemon=True).start()

    # ---------- Zusammenspiel der Tabs ----------
    def refresh_all(self):
        self.t_rec.refresh()
        self.t_eval.refresh()
        self.t_group.refresh(self.t_group._sel())
        self.t_set.refresh()

    def _tab_changed(self, i):
        w = self.tabs.widget(i)
        if w is self.t_set:
            self.t_set.refresh()

    def _recording_saved(self, path):
        self.t_eval.select_recording(path)
        self.tabs.setCurrentWidget(self.t_eval)

    def _load_models_now(self):
        self.mod_bar.hide()
        self.tabs.setCurrentWidget(self.t_set)
        self.t_set.ensureWidgetVisible(self.t_set.models_btn)
        self.t_set.load_models()

    # ---------- Updates ----------
    def _show_update(self, info, manual=False):
        if info.get("neu"):
            self.upd_text.setText(f"**⬆️ Neue Version {info['version']} verfügbar** (du hast {U.VERSION})")
            self.upd_go.show()
            self.upd_bar.show()
        elif manual:
            self.upd_text.setText(f"Update-Prüfung: {info['fehler']}" if "fehler" in info else
                                  f"✅ Du hast die neueste Version ({U.VERSION}).")
            self.upd_go.hide()
            self.upd_bar.show()

    def check_update(self):
        run_job(lambda progress: U.check(force=True), on_done=lambda i: self._show_update(i, True),
                exclusive=False, name="Update-Prüfung")

    def do_update(self):
        if REC.running:
            error(self, "Erst die Aufnahme beenden – dann aktualisieren.")
            return
        self.upd_go.setEnabled(False)

        def done(v):
            self.upd_text.setText(f"⬆️ Version {v} wird installiert – der SessionRecorder startet danach neu.")
            QTimer.singleShot(1200, lambda: self.close_for_update())

        def fail(m):
            self.upd_go.setEnabled(True)
            error(self, f"Update fehlgeschlagen: {m}")
        run_job(lambda progress: U.install(progress=progress),
                on_progress=lambda f, m: self.upd_text.setText(f"⏳ {m}"), on_done=done, on_error=fail,
                exclusive=False, name="Update")

    def close_for_update(self):
        self._force = True
        self.close()

    # ---------- Beenden ----------
    def closeEvent(self, e):
        if REC.running and not getattr(self, "_force", False):
            if not ask(self, "Die Aufnahme läuft noch.\n\nAufnahme beenden, speichern und das Programm schließen?",
                       "Aufnahme läuft"):
                e.ignore()
                return
        e.accept()
        shutdown()


def shutdown():
    """Beendet wirklich alles: Aufnahme (wird gespeichert), gestartete KI, Bildgenerator, Unterprozesse."""
    try:
        if REC.running:
            REC.stop()
    except Exception:
        log.exception("Aufnahme beim Beenden")
    try:
        from .. import comic as CO
        CO.kill()
    except Exception:
        pass
    try:
        L.stop_all()
    except Exception:
        pass
    try:
        import psutil
        kids = psutil.Process().children(recursive=True)
        for k in kids:
            try:
                k.terminate()
            except Exception:
                pass
        psutil.wait_procs(kids, timeout=3)
        for k in kids:
            try:
                k.kill()
            except Exception:
                pass
    except Exception:
        pass
    log.info("Programm beendet")
    os._exit(0)


def _startup_chores():
    from .. import storage
    storage.clean_temp()
    try:
        from .. import comic as CO
        CO.kill_stale()
        st = C.settings()
        if not st.get("comic_fix_263"):
            C.save("einstellungen", {**st, "comic_9b_haengt": False, "comic_fix_263": True})
    except Exception:
        pass
    from ..recorder import start_bridge
    start_bridge()


def launch():
    QApplication.setApplicationName("Ascandir SessionRecorder")
    QApplication.setDesktopFileName(APP_ID)          # Linux: passendes Icon in der Taskleiste
    app = QApplication(sys.argv)
    # Nur ein Fenster: läuft schon eins, holen wir es nach vorne
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        print("Der SessionRecorder läuft schon.")
        return
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)
    apply_theme(app)
    _startup_chores()
    win = MainWindow()

    def raise_window():
        conn = server.nextPendingConnection()
        if conn:
            conn.close()
        win.showNormal()
        win.raise_()
        win.activateWindow()
    server.newConnection.connect(raise_window)
    win.show()
    log.info("Fenster geöffnet (Qt %s, %s)", __import__("PySide6").__version__, sys.platform)
    app.exec()
    shutdown()
