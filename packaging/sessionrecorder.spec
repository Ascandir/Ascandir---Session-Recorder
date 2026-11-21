# PyInstaller-Bauplan: macht aus dem Python-Code ein eigenständiges Programm (Windows und Linux).
# Aufruf aus dem Repo-Hauptordner:  pyinstaller packaging/sessionrecorder.spec --noconfirm
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent
WIN = sys.platform == "win32"
NAME = "SessionRecorder" if WIN else "ascandir-sessionrecorder"

datas = [(str(ROOT / "app" / "VERSION"), "."), (str(ROOT / "assets"), "assets"),
         (str(ROOT / "ANLEITUNG.html"), ".")]
binaries = []
hidden = collect_submodules("chronist")
for pkg in ("faster_whisper", "imageio_ffmpeg", "sherpa_onnx", "ctranslate2", "onnxruntime", "tokenizers"):
    datas += collect_data_files(pkg, include_py_files=False)
    binaries += collect_dynamic_libs(pkg)
datas += collect_data_files("imageio_ffmpeg", subdir="binaries")
if WIN:
    hidden += collect_submodules("proctap")
    binaries += collect_dynamic_libs("proctap")
binaries += collect_dynamic_libs("sounddevice") + collect_dynamic_libs("_sounddevice_data")
datas += collect_data_files("_sounddevice_data")

# Große Qt-Teile, die wir nicht brauchen
excludes = ["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
            "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DExtras", "PySide6.QtQuick3D",
            "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs", "PySide6.QtPdf",
            "PySide6.QtPdfWidgets", "PySide6.QtDesigner", "PySide6.QtBluetooth", "PySide6.QtNfc",
            "PySide6.QtSensors", "PySide6.QtSerialPort", "PySide6.QtLocation", "PySide6.QtPositioning",
            "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtSpatialAudio", "PySide6.QtTextToSpeech",
            "PySide6.QtHttpServer", "PySide6.QtWebSockets", "PySide6.QtWebChannel", "PySide6.QtWebView",
            "tkinter", "gradio", "webview", "matplotlib", "IPython", "pytest", "pandas"]

a = Analysis([str(ROOT / "app" / "main.py")], pathex=[str(ROOT / "app")], binaries=binaries, datas=datas,
             hiddenimports=hidden, excludes=excludes, noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, disable_windowed_traceback=True,
          icon=str(ROOT / "assets" / ("icon.ico" if WIN else "icon.png")), upx=False,
          version=str(ROOT / "packaging" / "version_info.txt") if WIN and (ROOT / "packaging" / "version_info.txt").exists() else None)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
