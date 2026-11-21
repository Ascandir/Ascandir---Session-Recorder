"""Gemeinsame Bausteine der Desktop-Oberfläche: Design, Hintergrund-Arbeit, Karten, Hilfsfunktionen."""
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMessageBox, QProgressBar,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from .. import config as C
from ..debuglog import log

AMBER = "#f2a93b"

DARK = {
    "bg": "#15110e", "panel": "#211b16", "input": "#2a231d", "border": "#3a3027", "text": "#efe6dc",
    "muted": "#a8998a", "accent": AMBER, "accent_text": "#1b140d", "danger": "#c4483c", "ok": "#6fbf73",
}
LIGHT = {
    "bg": "#f6f1ea", "panel": "#ffffff", "input": "#fbf8f4", "border": "#d9cfc3", "text": "#2a221b",
    "muted": "#7a6d60", "accent": "#d58a1c", "accent_text": "#ffffff", "danger": "#b23a2f", "ok": "#3c8d40",
}


def colors():
    return LIGHT if C.settings().get("design", "Dunkel") == "Hell" else DARK


def stylesheet(c=None):
    c = c or colors()
    return f"""
    QWidget {{ background: {c['bg']}; color: {c['text']}; font-size: 10.5pt; }}
    QMainWindow, QDialog {{ background: {c['bg']}; }}
    QToolTip {{ background: {c['panel']}; color: {c['text']}; border: 1px solid {c['border']}; padding: 6px; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar::tab {{ background: transparent; color: {c['muted']}; padding: 10px 18px; margin-right: 4px;
                    border-bottom: 3px solid transparent; font-size: 11pt; }}
    QTabBar::tab:selected {{ color: {c['text']}; border-bottom: 3px solid {c['accent']}; }}
    QTabBar::tab:hover {{ color: {c['text']}; }}
    QFrame#card {{ background: {c['panel']}; border: 1px solid {c['border']}; border-radius: 10px; }}
    QFrame#card QLabel, QFrame#card QCheckBox, QFrame#card QRadioButton {{ background: transparent; }}
    QFrame#status {{ background: {c['panel']}; border: 1px solid {c['border']}; border-left: 4px solid {c['accent']};
                     border-radius: 8px; }}
    QFrame#status QLabel {{ background: transparent; }}
    QLabel#h1 {{ font-size: 18pt; font-weight: 600; background: transparent; }}
    QLabel#h2 {{ font-size: 12.5pt; font-weight: 600; color: {c['accent']}; background: transparent; }}
    QLabel#muted {{ color: {c['muted']}; background: transparent; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QTextBrowser, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget,
    QTableWidget {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; padding: 5px;
                    selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{ border: 1px solid {c['accent']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox QAbstractItemView {{ background: {c['input']}; border: 1px solid {c['border']};
                                   selection-background-color: {c['accent']}; selection-color: {c['accent_text']}; }}
    QHeaderView::section {{ background: {c['panel']}; color: {c['muted']}; border: none; padding: 6px; }}
    QPushButton {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 7px; padding: 8px 16px; }}
    QPushButton:hover {{ border-color: {c['accent']}; }}
    QPushButton:disabled {{ color: {c['muted']}; border-color: {c['border']}; }}
    QPushButton#primary {{ background: {c['accent']}; color: {c['accent_text']}; border: none; font-weight: 600; }}
    QPushButton#primary:hover {{ background: #f7bb5c; }}
    QPushButton#primary:disabled {{ background: {c['border']}; color: {c['muted']}; }}
    QPushButton#danger {{ background: {c['danger']}; color: white; border: none; font-weight: 600; }}
    QPushButton#danger:disabled {{ background: {c['border']}; color: {c['muted']}; }}
    QPushButton#big {{ padding: 12px 22px; font-size: 11.5pt; }}
    QProgressBar {{ background: {c['input']}; border: 1px solid {c['border']}; border-radius: 6px; height: 14px;
                    text-align: center; color: {c['text']}; }}
    QProgressBar::chunk {{ background: {c['accent']}; border-radius: 5px; }}
    QScrollArea {{ border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 5px; min-height: 30px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QCheckBox, QRadioButton {{ background: transparent; spacing: 8px; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; border: 2px solid {c['muted']};
                                                     background: {c['input']}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {c['accent']};
                                                                     border-color: {c['accent']}; }}
    QListWidget::indicator {{ width: 16px; height: 16px; }}
    QWidget#plain {{ background: transparent; }}
    QTabWidget#inner::pane {{ border: 1px solid {c['border']}; border-radius: 8px; top: -1px; }}
    QListWidget::item {{ padding: 5px; }}
    QSlider::groove:horizontal {{ height: 6px; background: {c['input']}; border-radius: 3px; }}
    QSlider::handle:horizontal {{ background: {c['accent']}; width: 16px; margin: -6px 0; border-radius: 8px; }}
    """


def apply_theme(app: QApplication):
    c = colors()
    app.setStyle("Fusion")
    pal = QPalette()
    for role, key in [(QPalette.Window, "bg"), (QPalette.Base, "input"), (QPalette.AlternateBase, "panel"),
                      (QPalette.Text, "text"), (QPalette.WindowText, "text"), (QPalette.Button, "input"),
                      (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "accent_text"), (QPalette.ToolTipBase, "panel"),
                      (QPalette.ToolTipText, "text"), (QPalette.PlaceholderText, "muted")]:
        pal.setColor(role, QColor(c[key]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(c))


# ---------- Bausteine ----------
def label(text="", kind=None, md=True, wrap=True):
    lab = QLabel(text)
    if md:
        lab.setTextFormat(Qt.MarkdownText)
    lab.setWordWrap(wrap)
    lab.setOpenExternalLinks(True)
    lab.setTextInteractionFlags(Qt.TextBrowserInteraction)
    if kind:
        lab.setObjectName(kind)
    return lab


def plain(w=None):
    """Leerer Container ohne eigenen Hintergrund (für Bereiche in Karten)."""
    w = w or QWidget()
    w.setObjectName("plain")
    return w


def button(text, kind=None, tip=None, slot=None):
    b = QPushButton(text.replace("&", "&&"))
    if kind:
        b.setObjectName(kind)
    if tip:
        b.setToolTip(tip)
    if slot:
        b.clicked.connect(slot)
    b.setCursor(Qt.PointingHandCursor)
    return b


class Card(QFrame):
    """Abgesetzter Kasten mit optionaler Überschrift."""

    def __init__(self, title=None, hint=None, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 14, 18, 16)
        self.lay.setSpacing(10)
        if title:
            self.lay.addWidget(label(title, "h2"))
        if hint:
            self.lay.addWidget(label(hint, "muted"))

    def add(self, w, stretch=0):
        if isinstance(w, QWidget):
            self.lay.addWidget(w, stretch)
        else:
            self.lay.addLayout(w, stretch)
        return w


class StatusCard(QFrame):
    """Status-Kasten mit Text und Fortschrittsbalken (für lange Abläufe)."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self.setObjectName("status")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.text = label(text)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.hide()
        self.step = label("", "muted")
        self.step.hide()
        lay.addWidget(self.text)
        lay.addWidget(self.bar)
        lay.addWidget(self.step)

    def set(self, text):
        self.text.setText(text)

    def progress(self, frac, msg=""):
        self.bar.show()
        self.step.show()
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.step.setText(msg or "")

    def done(self, text=None):
        self.bar.hide()
        self.step.hide()
        if text is not None:
            self.set(text)


def hbox(*widgets, stretch_last=False, spacing=8):
    """Waagrechte Reihe: Eingabefelder und Texte dehnen sich, Knöpfe bleiben so breit wie nötig."""
    from PySide6.QtWidgets import QComboBox, QLineEdit, QSlider
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    has_spacer = any(w is None for w in widgets)
    for w in widgets:
        if w is None:
            lay.addStretch(1)
        elif isinstance(w, (QLineEdit, QComboBox, QProgressBar, QSlider)):
            lay.addWidget(w, 1)
        elif isinstance(w, QLabel) and w.wordWrap() and not has_spacer:
            lay.addWidget(w, 1)
        elif isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    if stretch_last:
        lay.addStretch(1)
    return lay


def grow(w):
    w.setSizePolicy(QSizePolicy.Expanding, w.sizePolicy().verticalPolicy())
    return w


def open_path(path):
    """Ordner/Datei im Dateimanager bzw. mit dem Standardprogramm öffnen (Windows und Linux)."""
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
    log.info("Geöffnet: %s", path)


def error(parent, text, title="Hoppla"):
    QMessageBox.warning(parent, title, text)


def ask(parent, text, title="Bitte bestätigen"):
    return QMessageBox.question(parent, title, text, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.No) == QMessageBox.Yes


# ---------- Hintergrund-Arbeit ----------
class Cancelled(Exception):
    pass


BUSY = threading.Lock()


class Job(QObject):
    """Führt eine lange Aufgabe in einem eigenen Thread aus; Fortschritt/Ergebnis kommen als Signale."""
    progress = Signal(float, str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, exclusive=True, name="", **kwargs):
        super().__init__()
        self.fn, self.args, self.kwargs = fn, args, kwargs
        self.exclusive = exclusive
        self.name = name or getattr(fn, "__name__", "Aufgabe")
        self.cancelled = False
        self._thread = None

    def report(self, frac, msg=""):
        """An die Backend-Funktionen als progress(f, m) übergeben – bricht beim Abbrechen ab."""
        if self.cancelled:
            raise Cancelled()
        self.progress.emit(float(frac), str(msg))

    def start(self):
        if self.exclusive and not BUSY.acquire(blocking=False):
            self.failed.emit("Es läuft schon eine Auswertung, ein Comic oder ein KI-Test. "
                             "Warte kurz oder klick auf ⏹ Abbrechen.")
            return False
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.name)
        self._thread.start()
        return True

    def _run(self):
        log.info("Start: %s", self.name)
        try:
            res = self.fn(*self.args, progress=self.report, **self.kwargs)
            if not self.cancelled:
                self.finished.emit(res)
        except Cancelled:
            log.info("Abgebrochen: %s", self.name)
        except Exception as e:
            if self.cancelled:
                log.info("Abgebrochen: %s (%s)", self.name, e)
            else:
                log.exception("Fehler in %s", self.name)
                self.failed.emit(str(e) or e.__class__.__name__)
        finally:
            if self.exclusive:
                try:
                    BUSY.release()
                except RuntimeError:
                    pass
            log.info("Ende: %s", self.name)

    def cancel(self):
        self.cancelled = True


JOBS = []          # laufende Jobs festhalten (sonst räumt Python die Signale weg)


def run_job(fn, *args, on_done=None, on_error=None, on_progress=None, exclusive=True, name="", **kwargs):
    job = Job(fn, *args, exclusive=exclusive, name=name, **kwargs)
    if on_progress:
        job.progress.connect(on_progress, Qt.QueuedConnection)
    if on_done:
        job.finished.connect(on_done, Qt.QueuedConnection)
    if on_error:
        job.failed.connect(on_error, Qt.QueuedConnection)
    job.finished.connect(lambda *_: JOBS.remove(job) if job in JOBS else None, Qt.QueuedConnection)
    job.failed.connect(lambda *_: JOBS.remove(job) if job in JOBS else None, Qt.QueuedConnection)
    JOBS.append(job)
    if not job.start():
        return None
    return job


def cancel_all():
    """Abbrechen: laufende Jobs markieren, Bildgenerator und gestartete KI beenden."""
    from .. import llm as L
    for j in list(JOBS):
        j.cancel()
    try:
        from .. import comic as CO
        CO.kill()
    except Exception:
        log.exception("Fehler beim Beenden des Bildgenerators")
    try:
        L.stop_all()
    except Exception:
        log.exception("Fehler beim Beenden der KI")
    if BUSY.locked():
        try:
            BUSY.release()
        except RuntimeError:
            pass
    log.info("Abbrechen gedrückt")


def fmt_time(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def resource(name):
    """Datei aus assets (im Paket oder im Quellordner)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    for cand in (base / "assets" / name, Path(__file__).resolve().parents[3] / "assets" / name):
        if cand.exists():
            return cand
    return None
