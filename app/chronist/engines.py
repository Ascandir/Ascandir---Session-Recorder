"""Hilfen für mitgelieferte Programme (Bildgenerator, eingebaute KI) – Windows und Linux."""
import os
import stat
import sys
import tarfile
import zipfile
from pathlib import Path

from . import config as C

WIN = sys.platform == "win32"


def extract(archive, dest):
    """zip oder tar.gz entpacken; unter Linux die Programme ausführbar machen."""
    archive, dest = Path(archive), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")
    if not WIN:
        for p in dest.rglob("*"):
            if p.is_file() and (p.suffix in ("", ".so") or ".so." in p.name):
                p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def env_for(exe):
    """Umgebung für ein Programm: unter Linux die mitgelieferten Bibliotheken daneben finden."""
    env = dict(C.ORIG_ENV)
    if not WIN:
        dirs = {str(Path(exe).parent)} | {str(p.parent) for p in Path(exe).parent.parent.rglob("*.so*")}
        env["LD_LIBRARY_PATH"] = os.pathsep.join(sorted(dirs) + [env.get("LD_LIBRARY_PATH", "")]).strip(os.pathsep)
    return env


def linux_asset(name):
    """Passt ein Release-Paket zu Linux mit Vulkan?"""
    n = name.lower()
    is_linux = "linux" in n or "ubuntu" in n
    is_x64 = "x64" in n or "x86_64" in n or "amd64" in n
    return is_linux and is_x64 and "vulkan" in n and (n.endswith(".zip") or n.endswith(".tar.gz"))
