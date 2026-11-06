"""
CHD Manager - convert game disc images between formats (CHD, PBP, CSO/ZSO, ECM, RVZ, WBFS and more)

Put this file next to chdman.exe (or point it at chdman.exe in Settings, Output page).
Needs only Python 3 (tkinter is included with it). Double-click to run.

What it does
  * Queue of disc images (.cue .gdi .iso .toc, lone .bin) -> .chd, and .chd -> extract / verify
  * Detects CD vs DVD automatically (and the platform: PS1, PS2, PSP, Dreamcast, Saturn, Sega CD)
  * Compression presets, threads, hunk size, overwrite / skip / rename, output folder
  * Verify after create; optional "send sources to Recycle Bin" ONLY after a verified CHD
  * Live progress, log, summary of space saved
"""
import bisect
import bz2
import ctypes
import hashlib
import json
import lzma
import os
import platform
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zlib
import webbrowser
import tkinter as tk
from ctypes import wintypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_NAME = "CHD Manager"
APP_VER = "2.0.1"
FROZEN = getattr(sys, "frozen", False)          # True when running as the packaged .exe
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "chd_manager_settings.json"
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

CD_MAX_BYTES = 737_280_000          # 80-minute CD: 360,000 sectors * 2048 bytes
SRC_EXTS = {".cue", ".gdi", ".toc", ".iso", ".bin", ".pbp", ".cdi", ".vcd", ".zar", ".mds", ".mdf", ".nrg", ".ccd", ".img", ".sub", ".m3u", ".cso", ".zso", ".ecm", ".pkg", ".nsp", ".xci", ".nup", ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud"}
ARCHIVE_EXTS = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz"}
CHDMAN = None                        # path to chdman.exe, set at startup

THEMES = {
    "dark": dict(bg="#12141a", panel="#1a1d26", panel2="#1f2330", field="#242a3b", btn="#2a3042", btn_hover="#37405c",
                 border="#2d3348", head="#222737", fg="#e8eaf2", muted="#8d93a8", sel="#2f3f78", trough="#242a3b",
                 accent="#7c5cff", accent2="#9b82ff", g1="#6d4dff", g2="#1fc8e8",
                 info="#e8eaf2", cmd="#6ea8ff", ok="#3ddc97", err="#ff6b6b", warn="#ffb454", work="#6ea8ff", skip="#8d93a8"),
    "light": dict(bg="#f3f4f8", panel="#ffffff", panel2="#f5f6fb", field="#ffffff", btn="#e3e6ef", btn_hover="#d3d9ea",
                  border="#cfd4e3", head="#eceff7", fg="#1b1e2b", muted="#6b7185", sel="#cfd9ff", trough="#dfe3ef",
                  accent="#6a4cf0", accent2="#8068ff", g1="#6a4cf0", g2="#0fb5d6",
                  info="#1b1e2b", cmd="#0b57d0", ok="#1a7f37", err="#c62828", warn="#b26a00", work="#0b57d0", skip="#777777"),
}


class _Tip:
    """A small hover hint for a widget; text_fn() is called each time so it can reflect the current state."""

    def __init__(self, widget, text_fn):
        self.w, self.fn, self.tw, self.job = widget, text_fn, None, None
        widget.bind("<Enter>", self._enter, add="+")
        widget.bind("<Leave>", self._leave, add="+")
        widget.bind("<ButtonPress>", self._leave, add="+")

    def _enter(self, e=None):
        self._leave()
        self.job = self.w.after(500, self._show)

    def _leave(self, e=None):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None
        if self.tw:
            self.tw.destroy()
            self.tw = None

    def _show(self):
        txt = self.fn()
        if not txt:
            return
        tw = tk.Toplevel(self.w)
        tw.wm_overrideredirect(True)
        tw.geometry(f"+{self.w.winfo_rootx()}+{self.w.winfo_rooty() - 32}")
        tk.Label(tw, text=txt, bg="#2a3042", fg="#ffffff", padx=9, pady=4, font=("Segoe UI", 9)).pack()
        self.tw = tw


def lerp_hex(c1, c2, t):
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --------------------------------------------------------------------------- helpers
def open_folder(path, select=False):
    """Show a folder (or, with select, the file inside it) in the system's file manager."""
    path = str(path)
    if os.name == "nt":
        if select:
            subprocess.Popen(["explorer", "/select,", path])
        else:
            os.startfile(path)
        return
    target = str(Path(path).parent) if select else path
    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", target], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m {sec % 60:02d}s"
    return f"{sec // 3600}h {(sec % 3600) // 60:02d}m"


def find_chdman(saved=""):
    for cand in (saved, str(HERE / "chdman.exe"), str(HERE / "chdman")):
        if cand and Path(cand).is_file():
            return str(Path(cand))
    from shutil import which
    return which("chdman")


def restore_bundled_chdman():
    """Packaged exe only: if chdman.exe is missing, copy the spare one baked into the exe next to it."""
    src = Path(getattr(sys, "_MEIPASS", "")) / "chdman.exe"
    dst = HERE / "chdman.exe"
    if FROZEN and src.is_file() and not dst.exists():
        try:
            shutil.copy2(src, dst)
            return str(dst)
        except OSError:
            return None
    return None


def run_capture(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout + r.stderr).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return -1, str(e)


def chdman_info():
    """-> (version string, supports createdvd)"""
    if not CHDMAN:
        return "", False
    _, text = run_capture([CHDMAN])
    m = re.search(r"manager ([\d.]+) \((.*?)\)", text)
    return (m.group(1) if m else "?"), ("createdvd" in text)


# ----------------------------------------------------------- chdman updates (GitHub)
RELEASE_API = "https://api.github.com/repos/mamedev/mame/releases/latest"


def ver_to_int(s):
    """'0.289' or 'mame0289' -> 289"""
    digits = re.sub(r"\D", "", s or "")
    return int(digits) if digits else 0


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": f"{APP_NAME}/{APP_VER}"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_latest():
    """Latest official MAME release -> dict(tag, num, exe=(name,url,size), sums_url)."""
    with http_get(RELEASE_API) as r:
        data = json.loads(r.read().decode("utf-8"))
    arch = "arm64" if "ARM" in platform.machine().upper() else "(?:x64|64bit)"   # MAME renamed 64bit -> x64 in 0.281
    exe, sums = None, None
    for a in data.get("assets", []):
        if re.fullmatch(rf"mame\d+b_{arch}\.exe", a["name"]):
            exe = (a["name"], a["browser_download_url"], a["size"])
        elif a["name"] == "SHA256SUMS":
            sums = a["browser_download_url"]
    if not exe or not sums:
        raise RuntimeError("Could not find the Windows download or checksum file in the latest release "
                           "(MAME may have renamed it). Get it by hand from https://github.com/mamedev/mame/releases/latest "
                           "and use the Browse button in Settings, Output page.")
    for url in (exe[1], sums):
        if not url.startswith("https://github.com/mamedev/mame/"):
            raise RuntimeError("Unexpected download address: " + url)
    return {"tag": data["tag_name"], "num": ver_to_int(data["tag_name"]), "exe": exe, "sums": sums}


def find_7z():
    for c in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if Path(c).is_file():
            return c
    return shutil.which("7z") or shutil.which("7zz") or shutil.which("7za")


def install_7zip():
    """Install 7-Zip from the official winget source. Windows shows its own permission (UAC) prompt.
    -> (ok, message)"""
    if not shutil.which("winget"):
        return False, "no-winget"
    rc, text = run_capture(["winget", "install", "-e", "--id", "7zip.7zip", "--silent",
                            "--accept-package-agreements", "--accept-source-agreements"], timeout=900)
    if find_7z():
        return True, ""
    return False, text.strip()[-300:] or f"winget exited with code {rc}"


def chd_kind(path):
    return chd_info(path)[0]


def chd_info(path):
    """(disc kind, track count) read from 'chdman info -v'."""
    rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(path)])
    if rc != 0:
        return "?", 0
    tracks = len(re.findall(r"^\s*TRACK:", text, re.M))
    return _kind_from_tags(text), tracks


def chd_probe(path):
    """(disc kind, [track type per track, e.g. 'MODE2_RAW' or 'AUDIO']) read from 'chdman info -v'."""
    rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(path)])
    if rc != 0:
        return "?", []
    return _kind_from_tags(text), re.findall(r"^\s*TRACK:\s*\d+\s+TYPE:(\S+)", text, re.M)


def _kind_from_tags(text):
    tags = set(re.findall(r"Tag='(.{4})'", text))
    if tags & {"CHGT", "CHGD"}:
        return "GD-ROM"
    if tags & {"CHT2", "CHTR", "CHCD"}:
        return "CD"
    if "DVD " in tags:
        return "DVD"
    if "GDDD" in tags:
        return "HD"
    if tags & {"AVAU", "AVLD"}:
        return "LD"
    return "Raw"


def ref_path(folder, name):
    """folder/name. Windows ignores upper/lower case in file names and Linux does not, so when the exact name isn't there
    but one that differs only in case is, that one is used (a CUE saying game.bin next to GAME.BIN)."""
    f = folder / name
    if os.name != "nt" and not f.exists():
        try:
            low = name.lower()
            for c in folder.iterdir():
                if c.name.lower() == low:
                    return c
        except OSError:
            pass
    return f


def case_fixed_sheet(path):
    """Linux: chdman wants the file names in a .cue/.gdi/.toc spelled exactly as they are on disk. When some differ only in
    upper/lower case, write a temporary copy with the real names and return it (the caller deletes it); None when nothing needs fixing."""
    path = Path(path)
    pat = {".cue": r'(\s*FILE\s+)(?:"([^"]+)"|(\S+))(.*)', ".gdi": r'(\s*\d+\s+\d+\s+\d+\s+\d+\s+)(?:"([^"]+)"|(\S+))(.*)',
           ".toc": r'(.*?(?:DATA)?FILE\s+)(?:"([^"]+)")()(.*)'}.get(path.suffix.lower())
    if os.name == "nt" or not pat:
        return None
    try:
        lines = path.read_text("utf-8", "replace").splitlines()
    except OSError:
        return None
    changed = False
    for i, line in enumerate(lines):
        if path.suffix.lower() == ".gdi" and i == 0:
            continue
        m = re.match(pat, line, re.I)
        if not m:
            continue
        name = m.group(2) or m.group(3)
        real = ref_path(path.parent, name).name
        if real != name and (path.parent / real).exists():
            lines[i] = f'{m.group(1)}"{real}"{m.group(4)}'
            changed = True
    if not changed:
        return None
    out = path.with_name(f".{path.stem}.__chdm_case__{path.suffix}")
    out.write_text("\n".join(lines) + "\n", "utf-8")
    return out


def sheet_refs(files):
    """{lower-case path of a file a .cue/.gdi/.toc points to: that sheet's name}, for the sheets among files."""
    out = {}
    for f in files:
        f = Path(f)
        e = f.suffix.lower()
        if e in (".cue", ".gdi", ".toc") and f.is_file():
            for r in (parse_cue(f) if e == ".cue" else parse_gdi(f) if e == ".gdi" else parse_toc(f)):
                out.setdefault(str(r).lower(), f.name)
    return out


def parse_cue(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            m = re.match(r'\s*FILE\s+(?:"([^"]+)"|(\S+))', line, re.I)
            if m:
                refs.append(ref_path(p.parent, m.group(1) or m.group(2)))
    except OSError:
        pass
    return refs


def parse_gdi(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines()[1:]:
            m = re.match(r'\s*\d+\s+\d+\s+\d+\s+\d+\s+(?:"([^"]+)"|(\S+))', line)
            if m:
                refs.append(ref_path(p.parent, m.group(1) or m.group(2)))
    except OSError:
        pass
    return refs


def parse_toc(p):
    refs = []
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            m = re.search(r'(?:DATA)?FILE\s+"([^"]+)"', line, re.I)
            if m:
                refs.append(ref_path(p.parent, m.group(1)))
    except OSError:
        pass
    return refs


# ----------------------------------------------------------- disc sniffing (platform)
def cue_data_start(p):
    """(file, byte offset) of the first non-audio track in a .cue, so discs that begin with audio tracks
    (common for Dreamcast self-boot discs) can still be recognized."""
    sizes = {"MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048, "MODE2/2336": 2336}
    cur_file, cur_type = None, None
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            t = line.strip()
            m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
            if m:
                cur_file = ref_path(p.parent, m.group(1) or m.group(2))
                continue
            m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
            if m:
                cur_type = m.group(1).upper()
                continue
            m = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", t, re.I)
            if m and cur_file and cur_type and cur_type != "AUDIO":
                frames = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
                return cur_file, frames * sizes.get(cur_type, 2352)
    except OSError:
        pass
    return None


class DiscReader:
    def __init__(self, path, sector_size, data_offset, base=0):
        self.path, self.ss, self.off, self.base = path, sector_size, data_offset, base

    def sector(self, lba):
        with open(self.path, "rb") as f:
            f.seek(self.base + lba * self.ss + self.off)
            return f.read(2048)


def _iso_platform(r, pvd):
    rec = pvd[156:190]
    extent = struct.unpack("<I", rec[2:6])[0]
    size = struct.unpack("<I", rec[10:14])[0]
    data = b"".join(r.sector(extent + i) for i in range(min(8, (size + 2047) // 2048)))
    pos, names = 0, []
    while pos < len(data):
        ln = data[pos]
        if ln == 0:
            pos = (pos // 2048 + 1) * 2048
            continue
        nl = data[pos + 32]
        name = data[pos + 33:pos + 33 + nl].decode("latin1").upper()
        names.append((name, struct.unpack("<I", data[pos + 2:pos + 6])[0],
                      struct.unpack("<I", data[pos + 10:pos + 14])[0]))
        pos += ln
    for name, ext, sz in names:
        if name.startswith("PSP_GAME") or name.startswith("UMD_DATA"):
            return "PSP"
    for name, ext, sz in names:
        if name.startswith("SYSTEM.CNF"):
            txt = r.sector(ext)[:max(1, min(sz, 2048))].decode("latin1", "replace").upper()
            if "BOOT2" in txt:
                return "PS2"
            if "BOOT" in txt:
                return "PS1"
    return None


def sniff_platform(path, base=0):
    try:
        for ss, off in ((2048, 0), (2352, 16), (2352, 24)):
            r = DiscReader(path, ss, off, base)
            s0, s16 = r.sector(0), r.sector(16)
            if s0.startswith(b"SEGA SEGAKATANA"):
                return "Dreamcast"
            if len(s16) >= 6 and s16[1:6] == b"CD001":
                plat = _iso_platform(r, s16)
                if plat:
                    return plat
            if s0.startswith(b"SEGA SEGASATURN"):
                return "Saturn"
            if s0.startswith(b"SEGADISCSYSTEM") or s0.startswith(b"SEGA MEGA DRIVE"):
                return "Sega CD"
    except Exception:  # noqa: BLE001
        pass
    return ""


def decide_type(ext, size, platform):
    """-> (CD|DVD, reason)"""
    if ext in (".cue", ".gdi", ".toc", ".bin"):
        return "CD", "CUE/GDI/TOC images are CD-based"
    if ext in (".pbp", ".cdi", ".vcd"):
        return "CD", "PBP, CDI and VCD images are CD-based"
    if ext == ".zar":
        return "DVD", "Xbox 360 games are on DVDs"
    if platform == "PSP":
        return "DVD", "PSP UMD image"
    if size <= CD_MAX_BYTES:
        return "CD", f"{fmt_size(size)} fits on a CD (limit {fmt_size(CD_MAX_BYTES)})"
    return "DVD", f"{fmt_size(size)} is larger than a CD ({fmt_size(CD_MAX_BYTES)})"


# ------------------------------------------------------------------ recycle bin
class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", ctypes.c_void_p),
                ("pTo", ctypes.c_void_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def _trash_posix(paths):
    """Send files to the desktop's trash (recoverable) with the tools a Linux desktop has, or macOS's Finder; False if none worked."""
    if sys.platform == "darwin":
        for p in paths:
            script = 'tell application "Finder" to delete POSIX file "%s"' % p.replace("\\", "\\\\").replace('"', '\\"')
            if subprocess.run(["osascript", "-e", script], capture_output=True).returncode != 0:
                return False
        return True
    for tool in (["gio", "trash"], ["trash-put"], ["kioclient5", "move"], ["kioclient", "move"]):
        if shutil.which(tool[0]):
            ok = True
            for p in paths:
                cmd = tool + [p] + (["trash:/"] if tool[0].startswith("kioclient") else [])
                ok = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL).returncode == 0 and ok
            return ok
    return False


def recycle(paths):
    """Send files to the Recycle Bin (recoverable). Returns True on success."""
    paths = [str(p) for p in paths if Path(p).exists()]
    if not paths:
        return True
    if os.name != "nt":
        return _trash_posix(paths)
    joined = "\0".join(paths) + "\0\0"
    buf = ctypes.create_unicode_buffer(joined, len(joined) + 1)
    op = _SHFILEOPSTRUCTW()
    op.wFunc = 3                       # FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p).value
    op.fFlags = 0x40 | 0x10 | 0x4 | 0x400   # ALLOWUNDO | NOCONFIRMATION | SILENT | NOERRORUI
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    return res == 0 and not op.fAnyOperationsAborted


def delete_forever(paths):
    """Delete files for good (not recoverable; frees the space at once). Returns True when every one is gone."""
    ok = True
    for p in paths:
        try:
            if Path(p).is_dir():
                shutil.rmtree(p)
            elif Path(p).exists():
                Path(p).unlink()
        except OSError:
            ok = False
    return ok


# ------------------------------------------------- CHD Manager's own updates (GitHub releases)
APP_REPO = "Mr-Shizzy/CHD-Manager"
APP_RELEASE_API = f"https://api.github.com/repos/{APP_REPO}/releases/latest"
APP_URL_PREFIX = f"https://github.com/{APP_REPO}/"
SWAP_BAT = """@echo off
setlocal
set "NEW={new}"
set "OLD={old}"
for /l %%i in (1,1,90) do (
  move /y "%NEW%" "%OLD%" >nul 2>&1 && goto swapped
  timeout /t 1 /nobreak >nul
)
echo Could not replace the program. The new version was saved as: %NEW%
pause
exit /b 1
:swapped
set "PYINSTALLER_RESET_ENVIRONMENT=1"
start "" "%OLD%"
(goto) 2>nul & del "%~f0"
"""


def ver_tuple(s):
    """'v1.2.0' / '1.2' -> (1, 2, 0)"""
    nums = [int(x) for x in re.findall(r"\d+", s or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))


def app_asset_kind():
    """Which download of a release fits this install: the Windows exe zip, or the program source (Linux, macOS, Windows-from-source)."""
    return "win64" if (FROZEN or os.name == "nt") else "source"


def parse_app_release(data, kind):
    """A GitHub 'latest release' answer -> dict(tag, name, url, size, sha, page, notes, kind). Only trusts github.com/<repo>/.
    win64: the first 64-hex text in the notes is the zip's SHA-256 (the convention every released updater already reads).
    source: the SHA-256 is on the line of the notes that names the source zip ('<hash>  CHD-Manager-x-source.zip').
    A release without a source zip still parses (url is empty): the update can be announced, just not installed by itself."""
    pat = r"CHD-Manager-.+-win64\.zip" if kind == "win64" else r"CHD-Manager-.+-source\.zip"
    asset = next((a for a in data.get("assets", []) if re.fullmatch(pat, a["name"])), None)
    body = data.get("body") or ""
    if kind == "win64":
        if not asset:
            raise RuntimeError("The latest release has no Windows zip.")
        m = re.search(r"\b([0-9a-fA-F]{64})\b", body)
        sha = m.group(1) if m else ""
    else:
        sha = ""
        if asset:
            for line in body.splitlines():
                if asset["name"] in line:
                    m = re.search(r"\b([0-9a-fA-F]{64})\b", line)
                    if m:
                        sha = m.group(1)
                        break
    if asset and not asset["browser_download_url"].startswith(APP_URL_PREFIX):
        raise RuntimeError("Unexpected download address: " + asset["browser_download_url"])
    return {"tag": data["tag_name"], "name": asset["name"] if asset else "", "url": asset["browser_download_url"] if asset else "",
            "size": asset["size"] if asset else 0, "sha": sha, "page": data.get("html_url", APP_URL_PREFIX + "releases"),
            "notes": body.strip(), "kind": kind}


def fetch_app_release():
    """Latest CHD Manager release -> dict(tag, name, url, size, sha, page, notes, kind)."""
    with http_get(APP_RELEASE_API) as r:
        data = json.loads(r.read().decode("utf-8"))
    return parse_app_release(data, app_asset_kind())


def stage_app_update(info, dest_dir, progress=None):
    """Download the release zip, verify its SHA-256, and save the new exe next to the current one as
    'CHD Manager.new.exe'. Returns that path. Refuses anything without a matching checksum."""
    import zipfile
    if not info["url"].startswith(APP_URL_PREFIX):
        raise RuntimeError("Unexpected download address.")
    if not info["sha"]:
        raise RuntimeError("This release has no published checksum, so it can't be verified. Download it by hand.")
    dest_dir = Path(dest_dir)
    tmp = Path(tempfile.mkdtemp(prefix="chdm_app_update_"))
    try:
        zpath, h, got = tmp / info["name"], hashlib.sha256(), 0
        with http_get(info["url"], 60) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if progress:
                    progress(100 * got / max(info["size"], 1), f"Downloading {info['name']}: {fmt_size(got)} / {fmt_size(info['size'])}")
        if h.hexdigest().lower() != info["sha"].lower():
            raise RuntimeError("Checksum mismatch - the download was discarded.")
        with zipfile.ZipFile(zpath) as z:
            names = {Path(n).name: n for n in z.namelist() if not n.endswith("/")}
            if "CHD Manager.exe" not in names:
                raise RuntimeError("The zip does not contain CHD Manager.exe.")
            exe_bytes = z.read(names["CHD Manager.exe"])
            extras = {n: z.read(names[n]) for n in ("LICENSE", "THIRD_PARTY_NOTICES.txt") if n in names}
        if exe_bytes[:2] != b"MZ":
            raise RuntimeError("CHD Manager.exe in the zip is not a Windows program.")
        new = dest_dir / "CHD Manager.new.exe"
        new.write_bytes(exe_bytes)
        for n, b in extras.items():
            (dest_dir / n).write_bytes(b)
        return new
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---- updating the program from source (Linux, macOS): replace the program files, keep the old ones, restart
SOURCE_FILES = ("CHD_Manager.pyw", "chd-manager.sh", "LICENSE", "THIRD_PARTY_NOTICES.txt", "README.md")   # the only names an update may write
SOURCE_BACKUP = "previous-version"
SOURCE_MAX = 20 << 20


def can_self_update_source():
    """True for a Linux/macOS copy that runs from its own folder (not the packaged exe, not Windows)."""
    return (not FROZEN) and os.name != "nt" and (HERE / "CHD_Manager.pyw").is_file()


def fetch_source_update(info, progress=None, opener=None):
    """Download the source zip, check its size and SHA-256, and return {file name: bytes} for the known file names only.
    Refuses an address outside github.com/<repo>/, a release without a published checksum, a hash mismatch, a duplicate
    name and anything bigger than expected. Nothing is written to the program folder here."""
    import zipfile
    opener = opener or http_get
    if not info.get("url", "").startswith(APP_URL_PREFIX):
        raise RuntimeError("Unexpected download address.")
    if not info.get("sha"):
        raise RuntimeError("This release has no published checksum, so it can't be verified. Download it by hand.")
    tmp = Path(tempfile.mkdtemp(prefix="chdm_src_update_"))
    try:
        zpath, h, got = tmp / "update.zip", hashlib.sha256(), 0
        cap = min(SOURCE_MAX, max(int(info.get("size") or 0), 1) + (1 << 20))
        with opener(info["url"], 60) as r, open(zpath, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
                if got > cap:
                    raise RuntimeError("The download is bigger than the release says, so it was discarded.")
                f.write(chunk)
                h.update(chunk)
                if progress:
                    progress(100 * got / max(info.get("size") or 1, 1), f"Downloading {info['name']}: {fmt_size(got)} / {fmt_size(info.get('size') or 0)}")
        if h.hexdigest().lower() != info["sha"].lower():
            raise RuntimeError("Checksum mismatch - the download was discarded.")
        with zipfile.ZipFile(zpath) as z:
            chosen = {}
            for zi in z.infolist():
                base = Path(zi.filename).name
                if zi.is_dir() or base not in SOURCE_FILES:
                    continue                                    # anything else in the zip is ignored, never written
                if base in chosen:
                    raise RuntimeError(f"The zip holds {base} twice, so it was not trusted.")
                if zi.file_size > SOURCE_MAX:
                    raise RuntimeError(f"{base} in the zip is unexpectedly large.")
                chosen[base] = zi
            if "CHD_Manager.pyw" not in chosen:
                raise RuntimeError("The zip does not contain CHD_Manager.pyw.")
            return {n: z.read(zi) for n, zi in chosen.items()}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_source_files(files, want_version):
    """The new program must be Python that compiles, say it is the version the release claims, and its launcher must be a script."""
    try:
        src = files["CHD_Manager.pyw"].decode("utf-8")
        compile(src, "CHD_Manager.pyw", "exec")
    except (UnicodeDecodeError, SyntaxError, ValueError) as e:
        raise RuntimeError(f"The downloaded program isn't valid ({type(e).__name__}).") from None
    m = re.search(r'^APP_VER\s*=\s*"([^"]+)"', src, re.M)
    if not m or ver_tuple(m.group(1)) != ver_tuple(want_version):
        raise RuntimeError(f"The downloaded program says it is version {m.group(1) if m else '?'}, not {want_version}, so it was not installed.")
    sh = files.get("chd-manager.sh")
    if sh is not None and not sh.startswith(b"#!"):
        raise RuntimeError("The downloaded launcher script isn't a script.")


def install_source_update(files, dest_dir):
    """Put the new files into dest_dir. The old copies are kept in dest_dir/previous-version, and if anything goes wrong half way
    every file is put back as it was. Settings, keys, chdman and anything else in the folder are not touched."""
    dest = Path(dest_dir)
    if not os.access(dest, os.W_OK):
        raise RuntimeError("CHD Manager's folder can't be written to (it may belong to another user or a package), so it can't update itself.")
    backup = dest / SOURCE_BACKUP
    order = [n for n in files if n != "CHD_Manager.pyw"] + ["CHD_Manager.pyw"]     # the program itself goes last
    staged, done = {}, []
    try:
        backup.mkdir(exist_ok=True)
        for n in order:
            old = dest / n
            if old.is_file():
                shutil.copy2(old, backup / n)
        for n in order:
            old, tmp = dest / n, dest / f".{n}.chdm_new"
            tmp.write_bytes(files[n])
            mode = (old.stat().st_mode & 0o777) if old.is_file() else 0o644
            os.chmod(tmp, (mode | 0o755) if n.endswith(".sh") else mode)
            staged[n] = tmp
        for n in order:
            os.replace(staged[n], dest / n)
            done.append(n)
    except Exception:
        for n in done:
            try:
                if (backup / n).is_file():
                    shutil.copy2(backup / n, dest / n)
                else:
                    (dest / n).unlink()                       # a file that did not exist before
            except OSError:
                pass
        for tmp in staged.values():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def spawn_swap(script):
    """Run the swap script detached. The environment is cleaned first: a PyInstaller exe that is relaunched from
    inside itself inherits its parent's bookkeeping variables and, when the new exe has the same path as the old
    one, tries to reuse the old run's temp folder, which no longer exists ('Failed to load Python DLL')."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=CREATE_NO_WINDOW | 0x00000008, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, env=env)


def write_swap_script(new_exe, old_exe):
    """A tiny batch file that waits for the running program to exit, swaps in the new exe, and starts it."""
    bat = Path(tempfile.gettempdir()) / f"chdm_swap_{os.getpid()}.bat"
    text = SWAP_BAT.format(new=str(new_exe), old=str(old_exe)).replace("\n", "\r\n")
    bat.write_bytes(text.encode("utf-8"))              # bytes: text mode would turn \r\n into \r\r\n
    return bat


# ------------------------------------------------------------------ smart suggestions
def total_ram_gb():
    if os.name != "nt":
        try:
            if sys.platform == "darwin":
                return int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 1024 ** 3
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024 ** 2
        except (OSError, ValueError):
            pass
        return None

    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    try:
        m = MEMSTAT()
        m.dwLength = ctypes.sizeof(MEMSTAT)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullTotalPhys / 1024 ** 3
    except Exception:  # noqa: BLE001
        return None


# chdman's own defaults (measured with chdman 0.289): CD hunk 19,584 bytes, DVD hunk 4,096 bytes
def build_suggestions(jobs, out_hint=None):
    """Settings that suit this PC and this queue. -> dict(values, rows, warnings, notes, summary)"""
    cores = os.cpu_count() or 4
    ram = total_ram_gb()
    conv = [j for j in jobs if j.kind == "convert" and not j.missing]
    n_cd = sum(1 for j in conv if j.type == "CD")
    n_dvd = len(conv) - n_cd
    total = sum(j.size for j in conv)

    # threads: leave headroom for Windows. (Measured: chdman needs only ~30-50 MB even at 24 threads, so RAM
    # is not a limit; CPU contention is what makes a weak PC feel frozen.)
    if cores <= 2:
        threads, why_t = 1, f"Detected {cores} thread(s). Using 1 leaves the rest for Windows."
    elif cores <= 11:
        threads, why_t = cores - 1, f"Detected {cores} threads. Using {cores - 1} leaves 1 free so Windows stays responsive."
    else:
        threads, why_t = cores - 2, f"Detected {cores} threads. Using {cores - 2} leaves 2 free so Windows stays responsive."
    if ram and ram < 2:
        threads = min(threads, 2)
        why_t += f" Only {ram:.1f} GB RAM, so capped at 2."
    shown_t = f"{threads} of {cores}"
    weak = cores <= 4
    potato = weak or bool(ram and ram < 8)   # few threads or under 8 GB RAM

    rows = [
        ("Compression", "Default",
         "chdman's default tries several codecs per block and keeps the best one. In tests on 40+ real discs, LZMA-only "
         "came out 0.5% larger, zlib ('Fast') about 8% larger, and zstd about 4% larger and up to 3x slower."),
        ("Hunk size", "Default",
         "chdman picks 19,584 bytes for CD images and 4,096 bytes for DVD images. In tests, doubling the hunk saved "
         "0.6-10% (about 5% on average, least on big images), and doubled hunks booted in DuckStation and Flycast, "
         "but I haven't tried every emulator, so stay on default unless you've tested yours."),
        ("Threads", shown_t, why_t + " In testing, speed stopped improving at around 16 threads." + (
            " Expect long conversions on a PC this size - that's normal; leave it running." if weak else "")),
        ("Low priority", "On" if potato else "Off",
         ("This looks like a modest PC (" + f"{cores} threads" + (f", {ram:.0f} GB RAM" if ram else "") + "), so chdman runs "
          "below your other programs and the PC stays usable. It still uses all free CPU time when you're idle.")
         if potato else
         f"Not needed: {cores} threads" + (f" and {ram:.0f} GB RAM" if ram else "") +
         " is plenty, and the spare threads already keep Windows responsive. You can still tick it in Settings, Convert page."),
        ("If output exists", "Skip", "Never overwrites a CHD you already made; re-run safely after an interruption."),
        ("Verify after create", "On", "Checks each CHD's SHA-1 before you trust it (and before any source is recycled)."),
    ]
    warnings, notes = [], []

    if total:
        est = total * 0.70      # typical CHD is 40-70% of the source; assume the worse end
        try:
            free = shutil.disk_usage(out_hint or conv[0].path.parent).free
            if free < est * 1.1:
                warnings.append(f"Low disk space: about {fmt_size(est)} may be needed but only {fmt_size(free)} is free "
                                "where the CHDs will be written. Pick another Output folder.")
        except OSError:
            pass
        if total > 20 * 1024 ** 3:
            notes.append(f"{fmt_size(total)} queued - this will take a while. Verify roughly doubles the disk reading, "
                         "but it's worth leaving on for a batch this size.")
    plats = {j.platform for j in conv if j.platform}
    if any(j.platform == "PS2" and j.type == "DVD" for j in conv):
        notes.append("Big PS2 ISOs become DVD-type CHDs ('createdvd'). Many PS2 CHDs you find online are CD-type instead. "
                     "Both are valid CHDs, and both booted in a PCSX2 nightly test, but check one game boots before converting a collection.")
    if any(j.platform == "PSP" for j in conv):
        notes.append("PSP images are treated as DVD-type CHDs.")
    if "Dreamcast" in plats:
        notes.append("Dreamcast .gdi images are CD-type CHDs; keep the .gdi and all its track files together.")
    if any(j.lone_bin for j in conv):
        notes.append("A lone .bin gets a temporary CUE assuming MODE2/2352 (right for most PS1 games). "
                     "If a game fails to boot, find its real .cue.")
    unknown = [j for j in conv if j.path.suffix.lower() == ".iso" and not j.platform]
    if unknown:
        notes.append(f"{len(unknown)} ISO(s) have no recognizable platform; CD/DVD was chosen by size only. "
                     "Right-click to force CD or DVD if one is wrong.")

    values = {"compression": "Default", "custom_comp": "", "hunk": "", "threads": threads, "exists": "Skip",
              "verify_after": True, "low_priority": potato}
    summary = (f"This PC: {cores} threads" + (f", {ram:.0f} GB RAM" if ram else "") + "\n"
               + (f"Queue: {len(conv)} to convert, {fmt_size(total)} ({n_cd} CD, {n_dvd} DVD)" if conv
                  else "Queue: nothing to convert yet - add files and this updates."))
    return {"values": values, "rows": rows, "warnings": warnings, "notes": notes, "summary": summary}


# ------------------------------------------- PBP and CDI images: unpacked to CUE/BIN, then handed to chdman
class UnpackError(Exception):
    pass


CD_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def _unbcd(b):
    return (b >> 4) * 10 + (b & 15)


def cue_time(frames):
    return "%02d:%02d:%02d" % (frames // 4500, frames // 75 % 60, frames % 75)


def pbp_discs(path):
    """Byte offset of each disc inside a PlayStation .pbp (one for most games, up to five for multi-disc)."""
    with open(path, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("not a PBP file")
        psar = struct.unpack("<I", head[0x24:0x28])[0]
        f.seek(psar)
        magic = f.read(16)
        if magic.startswith(b"PSISOIMG0000"):
            return [psar]
        if magic.startswith(b"PSTITLEIMG000000"):
            f.seek(psar + 0x200)
            offs = [o for o in struct.unpack("<5I", f.read(20)) if o]
            if offs:
                return [psar + o for o in offs]
    raise UnpackError("this PBP is not a PlayStation 1 game (PSP games and other PBPs can't be converted)")


def pbp_toc(f, base):
    """-> ([(is_data, start_lba)], total_sectors) for one disc section."""
    f.seek(base + 0x800)
    raw = f.read(0x400)
    tracks, total = [], 0
    for i in range(0, len(raw) - 9, 10):
        e = raw[i:i + 10]
        point = e[2]
        if point == 0:
            break
        lba = (_unbcd(e[7]) * 60 + _unbcd(e[8])) * 75 + _unbcd(e[9]) - 150
        if point == 0xA2:
            total = lba
        elif point < 0xA0:
            tracks.append((e[0] == 0x41, lba))
    if not tracks or total <= 0:
        raise UnpackError("the track table in this PBP is unreadable")
    return tracks, total


def unpack_pbp_disc(path, base, outdir, stem, progress, stopped):
    """Decompress one disc of a PBP into outdir/stem.bin + stem.cue. Returns the cue path."""
    outdir = Path(outdir)
    bin_path, cue_path = outdir / (stem + ".bin"), outdir / (stem + ".cue")
    with open(path, "rb") as f:
        tracks, total = pbp_toc(f, base)
        blocks = -(-total // 16)
        f.seek(base + 0x4000)
        index = f.read(blocks * 32)
        if len(index) < blocks * 32:
            raise UnpackError("the block table in this PBP is cut short")
        data0, full = base + 0x100000, 16 * 2352
        left = total * 2352
        with open(bin_path, "wb") as g:
            for b in range(blocks):
                off, ln = struct.unpack("<IH", index[b * 32:b * 32 + 6])
                f.seek(data0 + off)
                raw = f.read(ln)
                try:
                    chunk = raw if ln == full else zlib.decompress(raw, -15)
                except zlib.error:
                    raise UnpackError(f"block {b} of this PBP is damaged") from None
                if len(chunk) != full and b != blocks - 1:
                    raise UnpackError(f"block {b} of this PBP has the wrong size")
                g.write(chunk[:left])
                left -= len(chunk)
                if b % 64 == 0:
                    progress(b / blocks)
                    if stopped():
                        raise UnpackError("stopped")
    lines = [f'FILE "{bin_path.name}" BINARY']
    for n, (is_data, lba) in enumerate(tracks, 1):
        lines += [f"  TRACK {n:02d} {'MODE2/2352' if is_data else 'AUDIO'}", f"    INDEX 01 {cue_time(max(lba, 0))}"]
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    progress(1.0)
    return cue_path


CDI_V2, CDI_V3, CDI_V35 = 0x80000004, 0x80000005, 0x80000006
CDI_MARK = bytes([0, 0, 1, 0, 0, 0, 255, 255, 255, 255])


def parse_cdi(path):
    """Read a DiscJuggler .cdi. -> list of tracks: dict(session, mode 0=audio/1/2, ss=sector size, pregap,
    length, start_lba, position=byte offset of the track in the file)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size < 16:
            raise UnpackError("file too short to be a CDI")
        f.seek(size - 8)
        version, hdr = struct.unpack("<II", f.read(8))
        if version not in (CDI_V2, CDI_V3, CDI_V35) or hdr == 0 or hdr > size:
            raise UnpackError("not a DiscJuggler image this program understands")
        f.seek(size - hdr if version == CDI_V35 else hdr)

        def u(n):
            b = f.read(n)
            if len(b) != n:
                raise UnpackError("the CDI's track table is cut short")
            return int.from_bytes(b, "little")

        def skip(n):
            f.seek(n, 1)

        sessions = u(2)
        if not 0 < sessions < 100:
            raise UnpackError("no sessions found in this CDI")
        tracks, position = [], 0
        for s in range(1, sessions + 1):
            for _ in range(u(2)):
                if u(4) != 0:
                    skip(8)
                if f.read(10) != CDI_MARK or f.read(10) != CDI_MARK:
                    raise UnpackError("this CDI uses a track layout that isn't supported")
                skip(4)
                skip(u(1))
                skip(19)
                if u(4) == 0x80000000:
                    skip(8)
                skip(2)
                pregap, length = u(4), u(4)
                skip(6)
                mode = u(4)
                skip(12)
                start_lba, total = u(4), u(4)
                skip(16)
                size_code = u(4)
                if size_code not in (0, 1, 2) or mode > 2:
                    raise UnpackError("this CDI has a track type that isn't supported")
                ss = (2048, 2336, 2352)[size_code]
                skip(29)
                if version != CDI_V2:
                    skip(5)
                    if u(4) == 0xFFFFFFFF:
                        skip(78)
                tracks.append(dict(session=s, mode=mode, ss=ss, pregap=pregap, length=length,
                                   start_lba=start_lba, position=position))
                position += total * ss
            skip(4 + 8 + (0 if version == CDI_V2 else 1))
        if position > size or not tracks:
            raise UnpackError("the CDI's track table doesn't match the file")
        return tracks


def unpack_cdi(path, outdir, stem, progress, stopped):
    """Write each track of a CDI as outdir/stem (Track NN).bin plus stem.cue. Returns the cue path.
    Mode 2 sectors stored without their 16-byte header get a proper one back, carrying the track's real
    address. The empty gap between sessions (as on Dreamcast CD-Rs) is not stored: players such as Flycast
    refuse CHDs with pregaps and place the second session themselves."""
    outdir = Path(outdir)
    tracks = parse_cdi(path)
    all_sectors = sum(t["length"] for t in tracks) or 1
    done = 0
    lines = []
    with open(path, "rb") as f:
        for n, t in enumerate(tracks, 1):
            mode, ss = t["mode"], t["ss"]
            if mode == 0 and ss != 2352:
                raise UnpackError("this CDI has an audio track with an unusual sector size")
            if mode == 1 and ss == 2336 or mode == 2 and ss == 2048:
                raise UnpackError("this CDI has a track type that isn't supported")
            start = t["start_lba"] + t["pregap"] - 150          # where the track's INDEX 01 really is
            name = f"{stem} (Track {n:02d}).bin"
            kind = "AUDIO" if mode == 0 else "MODE1/2048" if ss == 2048 else f"MODE{mode}/2352"
            lines += [f'FILE "{name}" BINARY', f"  TRACK {n:02d} {kind}"]
            lines.append("    INDEX 01 00:00:00")
            f.seek(t["position"] + t["pregap"] * ss)
            step = 4096
            with open(outdir / name, "wb") as g:
                for first in range(0, t["length"], step):
                    count = min(step, t["length"] - first)
                    raw = f.read(count * ss)
                    if len(raw) != count * ss:
                        raise UnpackError("the CDI file is shorter than its track table says")
                    if mode == 2 and ss == 2336:
                        parts = []
                        for i in range(count):
                            a = start + first + i + 150
                            parts.append(CD_SYNC + bytes((_bcd(a // 4500), _bcd(a // 75 % 60), _bcd(a % 75), 2)))
                            parts.append(raw[i * 2336:(i + 1) * 2336])
                        raw = b"".join(parts)
                    g.write(raw)
                    done += count
                    progress(done / all_sectors)
                    if stopped():
                        raise UnpackError("stopped")
    cue_path = outdir / (stem + ".cue")
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    return cue_path


# ------------------- MDS/MDF, NRG, CloneCD and plain IMG: read into a track list, unpacked to CUE/BIN
IMAGE_EXTS = {".mds", ".mdf", ".nrg", ".ccd", ".img", ".vcd"}
VALID_STRIDES = (2048, 2336, 2352, 2368, 2448)


def _sniff_kind(f, off, stride):
    """AUDIO / MODE1 / MODE2 from the first sector of a track."""
    if stride == 2048:
        return "MODE1"
    if stride == 2336:
        return "MODE2"
    f.seek(off)
    s = f.read(16)
    if s[:12] != CD_SYNC:
        return "AUDIO"
    return "MODE2" if s[15] == 2 else "MODE1"


def _mk_track(path, f, size, off, stride, count, kind=None, gap=0):
    """One track of a disc image: file, byte offset of its first sector, bytes per sector as stored (stride),
    payload sector size (ss), sector count, kind, and how many sectors of the next track's pregap follow it."""
    if stride not in VALID_STRIDES:
        raise UnpackError(f"this image stores sectors of {stride} bytes, which isn't supported")
    if count <= 0 or off < 0:
        raise UnpackError("this image's track table is damaged")
    if off + count * stride > size:
        count = (size - off) // stride
        gap = 0
        if count <= 0:
            raise UnpackError("the image file is shorter than its track table says")
    return dict(file=Path(path), off=off, stride=stride, ss=min(stride, 2352), count=count, gap=gap,
                kind=kind or _sniff_kind(f, off, stride))


def read_mds(path):
    """Alcohol 120% .mds (version 1.x) + its .mdf data file -> track list."""
    path = Path(path)
    d = path.read_bytes()
    if len(d) < 0x58 or d[:16] != b"MEDIA DESCRIPTOR":
        raise UnpackError("not an Alcohol .mds file")
    if d[16] != 1:
        raise UnpackError("this .mds is a newer version than this program understands (only version 1 is supported)")
    try:
        if struct.unpack_from("<H", d, 0x14)[0] != 1:
            raise UnpackError("multi-session .mds images aren't supported")
        so = struct.unpack_from("<I", d, 0x50)[0]
        _s, s_end, _n, nblk, _sp, _f, _l, _z, dbo = struct.unpack_from("<iiHBBHHII", d, so)
        blocks = [d[dbo + i * 0x50:dbo + (i + 1) * 0x50] for i in range(nblk)]
        trk = sorted((b for b in blocks if len(b) == 0x50 and 1 <= b[4] <= 99), key=lambda b: b[4])
        if not trk:
            raise UnpackError("no tracks found in this .mds")
        if any(struct.unpack_from("<I", b, 0x30)[0] != 1 for b in trk):
            raise UnpackError("this .mds spreads the disc over several data files, which isn't supported")
        footer = struct.unpack_from("<I", trk[0], 0x34)[0]
        noff, wide = struct.unpack_from("<II", d, footer)
        raw = d[noff:noff + 520]
        name = raw.decode("utf-16le", "ignore").split("\0")[0] if wide else raw.split(b"\0")[0].decode("latin1")
    except struct.error:
        raise UnpackError("this .mds file is damaged") from None
    cands = []
    if name.startswith("*."):
        cands.append(path.with_suffix(name[1:]))
    elif name:
        cands.append(path.parent / Path(name).name)
    cands += [path.with_suffix(e) for e in (".mdf", ".img")]
    img = next((c for c in cands if c.is_file()), None)
    if not img:
        raise UnpackError("the data file (.mdf) that belongs to this .mds wasn't found next to it")
    size = img.stat().st_size
    out = []
    with open(img, "rb") as f:
        for i, b in enumerate(trk):
            stride = struct.unpack_from("<H", b, 0x10)[0]
            start, off = struct.unpack_from("<IQ", b, 0x24)
            end = struct.unpack_from("<I", trk[i + 1], 0x24)[0] if i + 1 < len(trk) else s_end
            out.append(_mk_track(img, f, size, off, stride, end - start, "AUDIO" if b[0] == 0xA9 else None))
    return out


NRG_TAO_STRIDE = {0: 2048, 2: 2336, 3: 2352, 5: 2352, 6: 2352, 7: 2352, 0x0f: 2448, 0x10: 2448, 0x11: 2448}


def read_nrg(path):
    """Nero .nrg (v1 and v2) -> track list."""
    path = Path(path)
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size < 16:
            raise UnpackError("not a Nero .nrg file")
        f.seek(size - 12)
        t = f.read(12)
        if t[:4] == b"NER5":
            cpos = struct.unpack(">Q", t[4:])[0]
        elif t[4:8] == b"NERO":
            cpos = struct.unpack(">I", t[8:])[0]
        else:
            raise UnpackError("not a Nero .nrg file")
        if not 0 < cpos < size:
            raise UnpackError("the track table of this .nrg is damaged")
        f.seek(cpos)
        chunks = []
        while f.tell() + 8 <= size:
            cid = f.read(4)
            ln = struct.unpack(">I", f.read(4))[0]
            if cid == b"END!":
                break
            chunks.append((cid, f.read(ln)))
        ids = [c for c, _ in chunks]
        if ids.count(b"SINF") > 1 or ids.count(b"DAOX") + ids.count(b"DAOI") > 1:
            raise UnpackError("multi-session .nrg images aren't supported")
        out = []
        dao = next(((c, b) for c, b in chunks if c in (b"DAOX", b"DAOI")), None)
        if dao:
            wide = dao[0] == b"DAOX"
            ent, body = (42 if wide else 30), dao[1]
            n = body[21] - body[20] + 1 if len(body) >= 22 else 0
            if n < 1 or len(body) < 22 + n * ent:
                raise UnpackError("this .nrg uses a track layout that isn't understood")
            rows = []
            for i in range(n):
                e = body[22 + i * ent:22 + (i + 1) * ent]
                stride = struct.unpack(">H", e[12:14])[0]
                rows.append((stride,) + struct.unpack(">3Q" if wide else ">3I", e[18:]))
            for i, (stride, idx0, idx1, end) in enumerate(rows):
                if stride not in VALID_STRIDES or end <= idx1 or (end - idx1) % stride:
                    raise UnpackError("this .nrg uses a track layout that isn't understood")
                gap = 0
                if i + 1 < len(rows) and rows[i + 1][0] == stride and rows[i + 1][2] >= end:
                    gap = (rows[i + 1][2] - end) // stride
                out.append(_mk_track(path, f, cpos, idx1, stride, (end - idx1) // stride, gap=gap))
        else:
            for cid, body in chunks:
                if cid == b"ETN2" and len(body) >= 32:
                    off, ln, mode, _u, _sec, cnt = struct.unpack(">QQIIII", body[:32])
                elif cid == b"ETNF" and len(body) >= 20:
                    off, ln, mode, _u, _sec = struct.unpack(">5I", body[:20])
                    cnt = 0
                else:
                    continue
                stride = ln // cnt if cnt and ln % cnt == 0 else NRG_TAO_STRIDE.get(mode, 0)
                if stride not in VALID_STRIDES or ln % stride:
                    raise UnpackError("this .nrg has a track type that isn't supported")
                out.append(_mk_track(path, f, cpos, off, stride, ln // stride))
        if not out:
            raise UnpackError("no tracks found in this .nrg")
        return out


def read_ccd(path):
    """CloneCD .ccd + .img (raw 2352-byte sectors) -> track list. The .sub file is not used."""
    path = Path(path)
    text = path.read_text("latin1")
    img = path.with_suffix(".img")
    if not img.is_file():
        raise UnpackError("the .img file that belongs to this .ccd wasn't found next to it")
    sess = re.search(r"^\s*Sessions\s*=\s*(\d+)", text, re.M | re.I)
    if sess and int(sess.group(1)) > 1:
        raise UnpackError("multi-session CloneCD images aren't supported")
    trk = []
    for m in re.finditer(r"^\[TRACK (\d+)\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S | re.I):
        mode = re.search(r"^\s*MODE\s*=\s*(\d+)", m.group(2), re.M | re.I)
        idx = re.search(r"^\s*INDEX 1\s*=\s*(\d+)", m.group(2), re.M | re.I)
        if not (mode and idx):
            raise UnpackError("this .ccd has a track without a mode or start position")
        trk.append((int(m.group(1)), int(mode.group(1)), int(idx.group(1))))
    if not trk:                                     # some writers only give the [Entry] table of contents
        for m in re.finditer(r"^\[Entry \d+\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S | re.I):
            pt = re.search(r"^\s*Point\s*=\s*(0x[0-9a-f]+|\d+)", m.group(1), re.M | re.I)
            ctl = re.search(r"^\s*Control\s*=\s*(0x[0-9a-f]+|\d+)", m.group(1), re.M | re.I)
            lba = re.search(r"^\s*PLBA\s*=\s*(-?\d+)", m.group(1), re.M | re.I)
            if pt and ctl and lba and 1 <= int(pt.group(1), 0) <= 99:
                trk.append((int(pt.group(1), 0), 0 if not int(ctl.group(1), 0) & 4 else 1, int(lba.group(1))))
    trk.sort()
    if not trk:
        raise UnpackError("no tracks found in this .ccd")
    size = img.stat().st_size
    if size % 2352:
        raise UnpackError("the .img isn't made of 2352-byte sectors, so it can't belong to this .ccd")
    total, out = size // 2352, []
    with open(img, "rb") as f:
        for i, (_n, mode, start) in enumerate(trk):
            end = trk[i + 1][2] if i + 1 < len(trk) else total
            out.append(_mk_track(img, f, size, start * 2352, 2352, end - start, "AUDIO" if mode == 0 else None))
    return out


def read_plain_img(path):
    """A bare .img: either raw 2352-byte sectors (a BIN) or a 2048-byte ISO. One data track."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        raw = f.read(12) == CD_SYNC
        if raw and size % 2352 == 0:
            return [_mk_track(path, f, size, 0, 2352, size // 2352)]
        if not raw and size % 2048 == 0 and size:
            return [_mk_track(path, f, size, 0, 2048, size // 2048)]
    raise UnpackError("can't tell what kind of disc image this .img is")


def image_primary(path):
    """The file that describes an image set: dropping a .mdf means its .mds, and so on."""
    path = Path(path)
    if path.suffix.lower() == ".mdf" and path.with_suffix(".mds").is_file():
        return path.with_suffix(".mds")
    if path.suffix.lower() == ".sub" and path.with_suffix(".ccd").is_file():
        return path.with_suffix(".ccd")
    return path


# ------------------------------------------------------------- POPStarter VCD (PlayStation 1 discs on a PS2)
# A VCD is a 1 MiB header (the table of contents, stored as BCD minute/second/frame positions that count from the
# start of the lead-in, so 150 sectors more than the file position) followed by the raw 2352-byte sectors of the disc.
VCD_HEADER = 0x100000
VCD_SIGNATURE = b"\x6b\x48\x6e\x20"


def _vcd_msf(b):
    if len(b) != 3 or any((x & 15) > 9 or (x >> 4) > 9 for x in b):
        raise UnpackError("this VCD's track table is damaged")
    return (_unbcd(b[0]) * 60 + _unbcd(b[1])) * 75 + _unbcd(b[2])


def vcd_header_tracks(h):
    """The track table of a VCD header -> [(type byte, INDEX 00 sector, INDEX 01 sector)], sectors as stored (+150)."""
    if len(h) < 1040 or h[2] != 0xA0 or h[12] != 0xA1 or h[22] != 0xA2:
        raise UnpackError("this isn't a PlayStation 1 VCD file")
    if (h[17] >> 4) > 9 or (h[17] & 15) > 9 or not 1 <= _unbcd(h[17]) <= 99:
        raise UnpackError("this VCD's track count is damaged")
    out = []
    for i in range(_unbcd(h[17])):
        o = 30 + 10 * i
        if h[o] not in (0x41, 0x01) or h[o + 2] != _bcd(i + 1):
            raise UnpackError("this VCD's track table is damaged")
        out.append((h[o], _vcd_msf(h[o + 3:o + 6]), _vcd_msf(h[o + 7:o + 10])))
    return out


def read_vcd(path):
    """A POPStarter .vcd -> the track list read_image works with (the data follows the 1 MiB header)."""
    path = Path(path)
    size = path.stat().st_size
    with open(path, "rb") as f:
        h = f.read(VCD_HEADER)
        ents = vcd_header_tracks(h)
        if size <= VCD_HEADER or (size - VCD_HEADER) % 2352:
            raise UnpackError("this VCD isn't made of whole 2352-byte sectors")
        total = (size - VCD_HEADER) // 2352
        starts = [e[2] - 150 for e in ents]
        if starts[0] != 0 or any(b <= a for a, b in zip(starts, starts[1:])) or starts[-1] >= total:
            raise UnpackError("this VCD's track positions don't fit its data")
        return [_mk_track(path, f, size, VCD_HEADER + s * 2352, 2352, (starts[i + 1] if i + 1 < len(starts) else total) - s,
                          "AUDIO" if ents[i][0] == 0x01 else None) for i, s in enumerate(starts)]


def _cue_index00(cue):
    """{track number: INDEX 00 sector} of a .cue (only those that have one); PREGAP/POSTGAP lines make it refuse."""
    out, n = {}, 0
    for line in Path(cue).read_text("utf-8", "replace").splitlines():
        t = line.strip()
        if re.match(r"(PREGAP|POSTGAP)\b", t, re.I):
            raise UnpackError("this disc uses PREGAP/POSTGAP lines, which a VCD can't hold reliably")
        m = re.match(r"TRACK\s+(\d+)", t, re.I)
        if m:
            n = int(m.group(1))
        m = re.match(r"INDEX\s+00\s+(\d+):(\d+):(\d+)", t, re.I)
        if m and n:
            out[n] = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
    return out


def vcd_header(cue):
    """Build the 1 MiB header for the single-BIN .cue chdman writes. Returns (header, bin path, sectors)."""
    binf, tracks = cue_tracks(cue)
    if tracks[0]["type"] != "MODE2/2352" or any(t["type"] not in ("MODE2/2352", "AUDIO") for t in tracks[1:]):
        raise UnpackError("a VCD can only hold a PlayStation 1 disc (MODE2/2352 data, then audio)")
    if len(tracks) > 99 or tracks[0]["idx"] != 0:
        raise UnpackError("this disc's track layout can't be put in a VCD")
    size = binf.stat().st_size
    if size % 2352 or not size:
        raise UnpackError("the disc data isn't made of whole 2352-byte sectors")
    total = size // 2352
    idx0 = _cue_index00(cue)
    h = bytearray(VCD_HEADER)
    ct = 0x01 if tracks[-1]["type"] == "AUDIO" else 0x41
    h[0], h[2], h[7], h[8] = 0x41, 0xA0, 0x01, 0x20
    h[10], h[12], h[17], h[20] = ct, 0xA1, _bcd(len(tracks)), ct
    h[22] = 0xA2
    lead = total + 150
    h[27:30] = bytes((_bcd(lead // 4500), _bcd(lead // 75 % 60), _bcd(lead % 75)))
    for i, t in enumerate(tracks):
        o = 30 + 10 * i
        i1 = t["idx"] + 150
        i0 = (idx0.get(i + 1, t["idx"]) + 150) if i else idx0.get(1, t["idx"])
        h[o] = 0x01 if t["type"] == "AUDIO" else 0x41
        h[o + 2] = _bcd(i + 1)
        h[o + 3:o + 6] = bytes((_bcd(i0 // 4500), _bcd(i0 // 75 % 60), _bcd(i0 % 75)))
        h[o + 7:o + 10] = bytes((_bcd(i1 // 4500), _bcd(i1 // 75 % 60), _bcd(i1 % 75)))
    h[1024:1028] = VCD_SIGNATURE
    h[1032:1036] = h[1036:1040] = total.to_bytes(4, "little")
    return bytes(h), binf, total


def write_vcd(out_path, cue, progress, stopped):
    """CD CHD (extracted to cue/bin) -> POPStarter .vcd."""
    h, binf, total = vcd_header(cue)
    done = 0
    with open(binf, "rb") as f, open(out_path, "wb") as g:
        g.write(h)
        while True:
            raw = f.read(2048 * 2352)
            if not raw:
                break
            g.write(raw)
            done += len(raw) // 2352
            progress(done / total)
            if stopped():
                raise UnpackError("stopped")


def verify_vcd(out_path, cue, progress, stopped):
    """Read the finished VCD back: same header as would be written now, and every sector equal to the disc's."""
    h, binf, total = vcd_header(cue)
    done = 0
    with open(out_path, "rb") as f, open(binf, "rb") as b:
        if f.read(VCD_HEADER) != h:
            return False
        while True:
            a, raw = f.read(2048 * 2352), b.read(2048 * 2352)
            if a != raw:
                return False
            if not a:
                return True
            done += len(a) // 2352
            progress(done / total)
            if stopped():
                raise UnpackError("stopped")


def read_image(path):
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".vcd":
        return read_vcd(path)
    if ext == ".mds":
        return read_mds(path)
    if ext == ".nrg":
        return read_nrg(path)
    if ext == ".ccd":
        return read_ccd(path)
    if ext == ".img":
        if path.with_suffix(".ccd").is_file():
            return read_ccd(path.with_suffix(".ccd"))
        if path.with_suffix(".mds").is_file():
            return read_mds(path.with_suffix(".mds"))
        return read_plain_img(path)
    raise UnpackError("not a disc image type this program reads")


def image_sources(path, tracks):
    """Every file that belongs to an image set (for size, and for the Recycle Bin after a verified conversion)."""
    path = Path(path)
    primary = path
    if path.suffix.lower() == ".img":
        primary = next((path.with_suffix(e) for e in (".ccd", ".mds") if path.with_suffix(e).is_file()), path)
    files = [path, primary] + [t["file"] for t in tracks]
    if primary.suffix.lower() == ".ccd" and primary.with_suffix(".sub").is_file():
        files.append(primary.with_suffix(".sub"))
    seen, out = set(), []
    for f in files:
        if str(f).lower() not in seen:
            seen.add(str(f).lower())
            out.append(f)
    return out


def unpack_tracks(tracks, outdir, stem, progress, stopped, iso_ok=False):
    """Write a track list as CUE/BIN (one BIN per track) in outdir. 2336-byte mode 2 sectors get their sync and
    header back, sectors stored with subchannel data lose it (a CHD can't hold it). With iso_ok, a lone 2048-byte
    data track becomes stem.iso instead. Returns the .cue (or .iso) path."""
    outdir = Path(outdir)
    total = sum(t["count"] for t in tracks) or 1
    done = lba = 0
    lines = []
    for n, t in enumerate(tracks, 1):
        stride, ss, kind = t["stride"], t["ss"], t["kind"]
        count = t["count"]
        if n < len(tracks) and tracks[n]["stride"] == stride:
            count += t["gap"]                       # the next track's pregap is part of this track's sectors
        if kind == "AUDIO":
            cue_kind = "AUDIO"
        elif ss == 2048 and kind == "MODE1":
            cue_kind = "MODE1/2048"
        elif ss in (2336, 2352) and kind == "MODE2" or ss == 2352 and kind == "MODE1":
            cue_kind = "MODE2/2352" if kind == "MODE2" else "MODE1/2352"
        else:
            raise UnpackError("this image has a track type that isn't supported")
        if kind == "AUDIO" and ss != 2352:
            raise UnpackError("this image has an audio track with an unusual sector size")
        iso = iso_ok and len(tracks) == 1 and cue_kind == "MODE1/2048"
        name = f"{stem}.iso" if iso else f"{stem}.bin" if len(tracks) == 1 else f"{stem} (Track {n:02d}).bin"
        lines += [f'FILE "{name}" BINARY', f"  TRACK {n:02d} {cue_kind}", "    INDEX 01 00:00:00"]
        with open(t["file"], "rb") as f, open(outdir / name, "wb") as g:
            f.seek(t["off"])
            for first in range(0, count, 2048):
                c = min(2048, count - first)
                raw = f.read(c * stride)
                if len(raw) != c * stride:
                    raise UnpackError("the image file is shorter than its track table says")
                if stride != ss:
                    raw = b"".join(raw[i * stride:i * stride + ss] for i in range(c))
                if ss == 2336:
                    parts = []
                    for i in range(c):
                        a = lba + first + i + 150
                        parts.append(CD_SYNC + bytes((_bcd(a // 4500), _bcd(a // 75 % 60), _bcd(a % 75), 2)))
                        parts.append(raw[i * 2336:(i + 1) * 2336])
                    raw = b"".join(parts)
                g.write(raw)
                done += c
                progress(done / total)
                if stopped():
                    raise UnpackError("stopped")
        lba += count
        if iso:
            return outdir / name
    cue_path = outdir / (stem + ".cue")
    cue_path.write_text("\n".join(lines) + "\n", "utf-8")
    return cue_path


def image_platform(tracks):
    for t in tracks:
        if t["kind"] != "AUDIO":
            return sniff_platform(t["file"], t["off"]) if t["stride"] in (2048, 2352) else ""
    return ""


# ------------------------------------------------------ plain 2048-byte ISO -> raw 2352-byte sectors (for PBP)
def _edc_tables():
    t1 = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ (0xD8018001 if c & 1 else 0)
        t1.append(c)
    t2 = []
    for x in range(65536):                    # two zero bytes fed to a CRC whose low 16 bits are x
        c = x
        for _ in range(2):
            c = (c >> 8) ^ t1[c & 0xFF]
        t2.append(c)
    return t2


def _ecc_tables():
    f = [((i << 1) ^ (0x11D if i & 0x80 else 0)) & 0xFF for i in range(256)]
    b = [0] * 256
    for i in range(256):
        b[i ^ f[i]] = i

    def kth(n):                               # byte -> parity contribution of a byte that is followed by n more steps
        out = bytearray(256)
        for t in range(256):
            v = t
            for _ in range(n + 1):
                v = f[v]
            out[t] = b[v ^ t]
        return bytes(out)
    return kth


_EDC_T2 = None
_ECC_P_TABLES = _ECC_Q_TABLES = _ECC_Q_PERM = None


def _ecc_setup():
    global _EDC_T2, _ECC_P_TABLES, _ECC_Q_TABLES, _ECC_Q_PERM
    if _EDC_T2 is not None:
        return
    _EDC_T2 = _edc_tables()
    kth = _ecc_tables()
    _ECC_P_TABLES = [kth(24 - k) for k in range(24)]
    _ECC_Q_TABLES = [kth(43 - k) for k in range(43)]
    perm = [0] * (52 * 43)
    for major in range(52):
        index = (major >> 1) * 86 + (major & 1)
        for minor in range(43):
            perm[minor * 52 + major] = index
            index += 88
            if index >= 2236:
                index -= 2236
    _ECC_Q_PERM = perm


def _ecc_parity(rows, tables, width):
    a = b = 0
    for k, tab in enumerate(tables):
        row = rows[k * width:(k + 1) * width]
        a ^= int.from_bytes(row.translate(tab), "little")
        b ^= int.from_bytes(row, "little")
    return a.to_bytes(width, "little") + (a ^ b).to_bytes(width, "little")


def raw_form1_sector(lba, user, sub=b"\x00\x00\x08\x00\x00\x00\x08\x00"):
    """The 2352-byte Mode 2 Form 1 sector for 2048 bytes of data: sync, MSF header, subheader, data, EDC and ECC."""
    import operator
    _ecc_setup()
    msf = lba + 150
    body = bytearray(b"\x00\xff\xff\xff\xff\xff\xff\xff\xff\xff\xff\x00")
    body += bytes((_bcd(msf // 4500), _bcd(msf // 75 % 60), _bcd(msf % 75), 2)) + sub + user
    c = 0
    for w in struct.unpack("<1028H", body[16:2072]):
        c ^= w
        c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    body += struct.pack("<I", c)
    src = bytes(4) + bytes(body[16:2076])                 # the address is treated as zero for Mode 2
    p = _ecc_parity(src[:2064], _ECC_P_TABLES, 86)
    q = _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)
    return bytes(body) + p + q


def cook_to_raw(iso, dst, progress, stopped):
    """A plain ISO (2048-byte sectors) -> one raw MODE2/2352 BIN. The usual way of making a PS1 disc image from an ISO:
    data sectors are rebuilt in full, but CD-XA audio and video (Form 2 sectors) can't come from a plain ISO."""
    total = os.path.getsize(iso) // 2048
    with open(iso, "rb") as f, open(dst, "wb") as g:
        for n in range(total):
            g.write(raw_form1_sector(n, f.read(2048)))
            if n % 512 == 0:
                progress(n / max(total, 1))
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def _edc(data):
    c = 0
    for w in struct.unpack("<%dH" % (len(data) // 2), data):
        c ^= w
        c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    return c


def fix_sector(s):
    """A raw Mode 1 or Mode 2 Form 1 sector with its EDC and ECC recomputed from its own data (header and subheader kept).
    Any other kind of sector (audio, Form 2) is returned as it was."""
    import operator
    if len(s) == 2352 and s[:12] == b"\x00" + b"\xff" * 10 + b"\x00" and s[15] == 1:
        return mode1_sector(s[12:15], s[16:2064])
    if len(s) != 2352 or s[:12] != b"\x00" + b"\xff" * 10 + b"\x00" or s[15] != 2 or s[18] & 0x20 or s[22] & 0x20:
        return s
    _ecc_setup()
    body = bytearray(s[:2072])
    body += struct.pack("<I", _edc(bytes(body[16:2072])))
    src = bytes(4) + bytes(body[16:2076])
    p = _ecc_parity(src[:2064], _ECC_P_TABLES, 86)
    q = _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)
    return bytes(body) + p + q


def repair_bin(src, dst, progress, stopped):
    """Copy a raw BIN, rebuilding the EDC/ECC of every Mode 2 Form 1 sector. -> number of sectors that changed."""
    total = max(1, os.path.getsize(src) // 2352)
    changed = 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        n = 0
        while True:
            s = f.read(2352)
            if not s:
                break
            fixed = fix_sector(s)
            changed += fixed != s
            g.write(fixed)
            n += 1
            if n % 512 == 0:
                progress(n / total)
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)
    return changed


def sniff_raw_image(path):
    """'MODE1/2352' or 'MODE2/2352' if the file starts with a raw CD sector (sync pattern), else '' (a plain image)."""
    try:
        with open(path, "rb") as f:
            h = f.read(16)
    except OSError:
        return ""
    if len(h) == 16 and h[:12] == b"\x00" + b"\xff" * 10 + b"\x00" and h[15] in (1, 2):
        return f"MODE{h[15]}/2352"
    return ""


# ------------------------------------------------------------------ Mode 1 sectors, ECM, LZ4
_SYNC = b"\x00" + b"\xff" * 10 + b"\x00"
_EDC_T1 = None


def _edc_crc(c, data):
    """Continue the CD EDC checksum c over data (any length); start from 0."""
    global _EDC_T1
    _ecc_setup()
    n = len(data) & ~1
    if n:
        for w in struct.unpack("<%dH" % (n // 2), data[:n]):
            c ^= w
            c = _EDC_T2[c & 0xFFFF] ^ (c >> 16)
    if len(data) > n:
        if _EDC_T1 is None:
            t1 = []
            for i in range(256):
                x = i
                for _ in range(8):
                    x = (x >> 1) ^ (0xD8018001 if x & 1 else 0)
                t1.append(x)
            _EDC_T1 = t1
        for b in data[n:]:
            c = (c >> 8) ^ _EDC_T1[(c ^ b) & 0xFF]
    return c


def _pq(src):
    """P and Q parity (172 + 104 bytes) of the 2064 bytes a sector's ECC covers."""
    import operator
    _ecc_setup()
    p = _ecc_parity(src, _ECC_P_TABLES, 86)
    return p + _ecc_parity(bytes(operator.itemgetter(*_ECC_Q_PERM)(src + p)), _ECC_Q_TABLES, 52)


def mode1_sector(addr, user):
    """A full 2352-byte Mode 1 sector from its 3 address bytes and 2048 bytes of data: sync, header, EDC, ECC."""
    body = bytearray(_SYNC) + bytes(addr) + b"\x01" + user
    body += struct.pack("<I", _edc_crc(0, bytes(body))) + bytes(8)
    return bytes(body) + _pq(bytes(body[12:2076]))


def xa_body(flags_data, form2=False):
    """The 2336 bytes after the header of a Mode 2 sector: flags (4 bytes + the same again), data, EDC and (Form 1) ECC.
    flags_data = the 4 flag bytes followed by 2048 (Form 1) or 2324 (Form 2) bytes of data."""
    body = bytearray(flags_data[:4] + flags_data)
    body += struct.pack("<I", _edc_crc(0, bytes(body)))
    if form2:
        return bytes(body)
    return bytes(body) + _pq(bytes(4) + bytes(body))         # the address counts as zero in a Mode 2 sector's ECC


def ecm_head(typ, num):
    """The type/count bytes of an ECM record (num >= 1)."""
    num -= 1
    c = typ | ((num & 0x1F) << 2)
    num >>= 5
    out = bytearray()
    while num:
        out.append(c | 0x80)
        c = num & 0x7F
        num >>= 7
    out.append(c)
    return bytes(out)


def ecm_decode(src, dst, progress, stopped):
    """ECM (Neill Corlett's error-code-modeler format) -> the original CD image. -> the track type to put in a CUE:
    'MODE1/2352', 'MODE2/2352', 'MODE2/2336' or ''. The checksum stored at the end of the file is checked."""
    total = max(1, os.path.getsize(src))
    crc, first, done = 0, b"", 0
    with open(src, "rb") as f, open(dst, "wb") as g:
        if f.read(4) != b"ECM\x00":
            raise UnpackError("this is not an ECM file")

        def need(n):
            b = f.read(n)
            if len(b) != n:
                raise UnpackError("the ECM file is cut short")
            return b
        while True:
            c = need(1)[0]
            typ, num, bits = c & 3, (c >> 2) & 0x1F, 5
            while c & 0x80:
                c = need(1)[0]
                num |= (c & 0x7F) << bits
                bits += 7
            if num == 0xFFFFFFFF:
                break
            num += 1
            if typ == 0:
                while num:
                    chunk = need(min(num, 1 << 20))
                    if len(first) < 16:
                        first += chunk[:16]
                    g.write(chunk)
                    crc = _edc_crc(crc, chunk)
                    num -= len(chunk)
            else:
                for _ in range(num):
                    if typ == 1:
                        a = need(3)
                        sec = mode1_sector(a, need(2048))
                    elif typ == 2:
                        sec = xa_body(need(2052))
                    else:
                        sec = xa_body(need(2328), True)
                    if len(first) < 16:
                        first += sec[:16] if typ == 1 else b""
                    g.write(sec)
                    crc = _edc_crc(crc, sec)
                    done += 1
                    if done % 256 == 0:
                        progress(f.tell() / total)
                        if stopped():
                            raise UnpackError("stopped")
            progress(f.tell() / total)
            if stopped():
                raise UnpackError("stopped")
        if struct.unpack("<I", need(4))[0] != crc:
            raise UnpackError("the ECM file is damaged (its checksum doesn't match)")
        size = g.tell()
    progress(1.0)
    if len(first) >= 16 and first[:12] == _SYNC and first[15] in (1, 2) and size % 2352 == 0:
        return f"MODE{first[15]}/2352"
    return "MODE2/2336" if size and size % 2336 == 0 else ""


def ecm_encode(src, dst, progress, stopped):
    """A raw 2352-byte-sector CD image -> ECM. Mode 1 sectors and Mode 2 Form 1/Form 2 sectors that carry correct EDC/ECC
    lose those bytes; everything else is stored as it is."""
    total = os.path.getsize(src)
    n = max(1, total // 2352)
    crc = 0
    state = {"typ": None, "count": 0, "parts": [], "bytes": 0}

    def flush(g):
        if state["typ"] is None:
            return
        g.write(ecm_head(state["typ"], state["bytes"] if state["typ"] == 0 else state["count"]))
        g.write(b"".join(state["parts"]))
        state.update(typ=None, count=0, parts=[], bytes=0)

    def add(g, typ, payload):
        if state["typ"] != typ or state["bytes"] > (1 << 22) or state["count"] >= (1 << 16):
            flush(g)
        state["typ"] = typ
        state["parts"].append(payload)
        state["bytes"] += len(payload)
        state["count"] += 1
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(b"ECM\x00")
        for i in range(n + 1):
            s = f.read(2352)
            if not s:
                break
            crc = _edc_crc(crc, s)
            if len(s) == 2352 and s[:12] == _SYNC and s[15] == 1 and mode1_sector(s[12:15], s[16:2064]) == s:
                add(g, 1, s[12:15] + s[16:2064])
            elif len(s) == 2352 and s[:12] == _SYNC and s[15] == 2 and s[16:20] == s[20:24] and not s[18] & 0x20 \
                    and xa_body(s[20:2072]) == s[16:]:
                add(g, 0, s[:16])
                add(g, 2, s[20:2072])
            elif len(s) == 2352 and s[:12] == _SYNC and s[15] == 2 and s[16:20] == s[20:24] and s[18] & 0x20 \
                    and xa_body(s[20:2348], True) == s[16:]:
                add(g, 0, s[:16])
                add(g, 3, s[20:2348])
            else:
                add(g, 0, s)
            if i % 256 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        flush(g)
        g.write(ecm_head(0, 0x100000000) + struct.pack("<I", crc))
    progress(1.0)


def verify_ecm(ecm, image, progress, stopped):
    """True when the ECM file decodes to exactly this image."""
    tmp = Path(tempfile.mkdtemp(prefix="chdm_ecm_"))
    try:
        out = tmp / "t.bin"
        ecm_decode(ecm, out, progress, stopped)
        return out.stat().st_size == Path(image).stat().st_size and files_equal(out, image)
    except UnpackError:
        if stopped():
            raise
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def files_equal(a, b):
    with open(a, "rb") as f, open(b, "rb") as g:
        while True:
            x, y = f.read(1 << 22), g.read(1 << 22)
            if x != y:
                return False
            if not x:
                return True


def lz4_decompress(src, size):
    """One LZ4 block -> exactly `size` bytes (extra bytes after the block are ignored)."""
    out, i = bytearray(), 0
    try:
        while len(out) < size:
            tok = src[i]
            i += 1
            ll = tok >> 4
            if ll == 15:
                while True:
                    b = src[i]
                    i += 1
                    ll += b
                    if b != 255:
                        break
            out += src[i:i + ll]
            i += ll
            if len(out) >= size:
                break
            off = src[i] | src[i + 1] << 8
            i += 2
            ml = tok & 15
            if ml == 15:
                while True:
                    b = src[i]
                    i += 1
                    ml += b
                    if b != 255:
                        break
            ml += 4
            if off == 0 or off > len(out):
                raise ValueError
            start = len(out) - off
            if off >= ml:
                out += out[start:start + ml]
            else:
                seg = bytes(out[start:])
                out += (seg * (ml // off + 1))[:ml]
    except (IndexError, ValueError):
        raise UnpackError("a block of this ZSO is damaged") from None
    return bytes(out[:size])


def lz4_compress(data):
    """One LZ4 block (greedy matching; valid for any LZ4 decoder)."""
    n = len(data)
    out = bytearray()

    def seq(lits, off, ml):
        ll = len(lits)
        out.append(min(ll, 15) << 4 | (min(ml - 4, 15) if off else 0))
        if ll >= 15:
            r = ll - 15
            while r >= 255:
                out.append(255)
                r -= 255
            out.append(r)
        out.extend(lits)
        if off:
            out.extend((off & 255, off >> 8))
            if ml - 4 >= 15:
                r = ml - 4 - 15
                while r >= 255:
                    out.append(255)
                    r -= 255
                out.append(r)
    anchor = i = 0
    table = {}
    while i <= n - 12:
        key = data[i:i + 4]
        cand = table.get(key)
        table[key] = i
        if cand is not None and i - cand <= 65535:
            m, maxm = 4, n - 5 - i
            while m + 8 <= maxm and data[cand + m:cand + m + 8] == data[i + m:i + m + 8]:
                m += 8
            while m < maxm and data[cand + m] == data[i + m]:
                m += 1
            seq(data[anchor:i], i - cand, m)
            i += m
            anchor = i
        else:
            i += 1
    seq(data[anchor:n], 0, 0)
    return bytes(out)


# ------------------------------------------------------------------ GameCube / Wii disc images (ISO, WBFS, CISO, GCZ, WIA, RVZ)
# The WIA/RVZ, GCZ, CISO and WBFS layouts and the lagged-Fibonacci "junk" generator follow the Dolphin emulator's
# DiscIO code (GPL-2.0-or-later), re-written here in Python.
GC_MAGIC, WII_MAGIC = 0xC2339F3D, 0x5D1C9EA3
WII_ISO_SL, WII_ISO_DL = 4699979776, 8511160320
CL, CLD = 0x8000, 0x7C00                        # a Wii cluster, and the part of it that is data (the rest is hashes)
GCZ_MAGIC = b"\x01\xc0\x0b\xb1"
_BC = None


def _bcrypt():
    global _BC
    if _BC is None:
        c = ctypes
        dll = c.WinDLL("bcrypt")
        P, V, U = c.POINTER, c.c_void_p, c.c_ulong
        dll.BCryptOpenAlgorithmProvider.argtypes = [P(V), c.c_wchar_p, c.c_wchar_p, U]
        dll.BCryptSetProperty.argtypes = [V, c.c_wchar_p, V, U, U]
        dll.BCryptGenerateSymmetricKey.argtypes = [V, P(V), V, U, c.c_char_p, U, U]
        for fn in (dll.BCryptEncrypt, dll.BCryptDecrypt):
            fn.argtypes = [V, c.c_char_p, U, V, V, U, V, U, P(U), U]
        _BC = dll
    return _BC


class AesCbc:
    """AES-128 in CBC mode through Windows' own crypto library (no extra package needed)."""

    def __init__(self, key):
        dll = _bcrypt()
        self.dll = dll
        alg = ctypes.c_void_p()
        if dll.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0):
            raise UnpackError("Windows' AES library isn't available")
        mode = ctypes.create_unicode_buffer("ChainingModeCBC")
        dll.BCryptSetProperty(alg, "ChainingMode", ctypes.cast(mode, ctypes.c_void_p), ctypes.sizeof(mode), 0)
        self.key = ctypes.c_void_p()
        dll.BCryptGenerateSymmetricKey(alg, ctypes.byref(self.key), None, 0, bytes(key), 16, 0)

    def crypt(self, data, iv=bytes(16), decrypt=False):
        n = len(data)
        out = ctypes.create_string_buffer(n)
        res = ctypes.c_ulong()
        ivb = ctypes.create_string_buffer(bytes(iv), 16)
        fn = self.dll.BCryptDecrypt if decrypt else self.dll.BCryptEncrypt
        if fn(self.key, bytes(data), n, None, ivb, 16, out, n, ctypes.byref(res), 0):
            raise UnpackError("AES failed")
        return out.raw


# ---- the lagged-Fibonacci generator that fills the unused parts of GameCube and Wii discs
def lfg_forward(buf):
    """Advance the 521-word state (a bytearray of 2084 bytes) by one full turn."""
    fb = int.from_bytes
    buf[0:128] = (fb(buf[0:128], "little") ^ fb(buf[1956:2084], "little")).to_bytes(128, "little")
    for k in range(128, 2048, 128):
        buf[k:k + 128] = (fb(buf[k:k + 128], "little") ^ fb(buf[k - 128:k], "little")).to_bytes(128, "little")
    buf[2048:2084] = (fb(buf[2048:2084], "little") ^ fb(buf[1920:1956], "little")).to_bytes(36, "little")


def lfg_backward(buf, start_word=0, end_word=521):
    """The inverse of lfg_forward over the words start_word..end_word (the whole state by default)."""
    fb = int.from_bytes
    hi = min(end_word, 521)
    stop = max(32, start_word)
    while hi > stop:
        lo = max(hi - 32, stop)
        a, b = lo * 4, hi * 4
        buf[a:b] = (fb(buf[a:b], "little") ^ fb(buf[a - 128:b - 128], "little")).to_bytes(b - a, "little")
        hi = lo
    top = min(end_word, 32)
    if top > start_word:
        a, b = start_word * 4, top * 4
        buf[a:b] = (fb(buf[a:b], "little") ^ fb(buf[a + 1956:b + 1956], "little")).to_bytes(b - a, "little")


def _lfg_load(words):
    """521 seed-expanded words -> the generator state (the output shuffle is applied here, as Dolphin does)."""
    buf = bytearray(struct.pack(">521I", *[(x & 0xFF00FFFF) | ((x >> 2) & 0x00FF0000) for x in words]))
    for _ in range(4):
        lfg_forward(buf)
    return buf


def _lfg_expand(seed_words):
    w = list(seed_words) + [0] * 504
    for i in range(17, 521):
        w[i] = ((w[i - 17] << 23) & 0xFFFFFFFF) ^ (w[i - 16] >> 9) ^ w[i - 1]
    return w


class Lfg:
    def __init__(self, seed68, skip=0):
        self.buf = _lfg_load(_lfg_expand(struct.unpack(">17I", seed68)))
        self.pos = 0
        self.skip(skip)

    def skip(self, n):
        self.pos += n
        while self.pos >= 2084:
            lfg_forward(self.buf)
            self.pos -= 2084

    def get(self, n):
        out = []
        while n > 0:
            take = min(n, 2084 - self.pos)
            out.append(bytes(self.buf[self.pos:self.pos + take]))
            self.pos += take
            n -= take
            if self.pos == 2084:
                lfg_forward(self.buf)
                self.pos = 0
        return b"".join(out)


def lfg_find_seed(data, word_offset):
    """The seed of the generator that produced `data` (>= 521 words, starting `word_offset` words into the stream), or None."""
    if len(data) < 2084:
        return None
    words = struct.unpack(">521I", data[:2084])
    for x in words:
        if (x & 0x00C00000) != ((x >> 2) & 0x00C00000):
            return None
    mod, div = word_offset % 521, word_offset // 521
    buf = bytearray(2084)
    # the words as the generator holds them (output shuffle already applied, i.e. the bytes as they appear on disc)
    buf[mod * 4:] = data[:(521 - mod) * 4]
    buf[:mod * 4] = data[(521 - mod) * 4:2084]
    lfg_backward(buf, 0, mod)
    for _ in range(div):
        lfg_backward(buf)
    for _ in range(4):
        lfg_backward(buf)
    w = list(struct.unpack(">521I", bytes(buf)))
    # undo the shuffle: bits 16 and 17 are rebuilt from neighbouring words
    for i in range(17):
        w[i] = (w[i] & 0xFF00FFFF) | ((w[i] << 2) & 0x00FC0000) | (((w[i + 16] ^ w[i + 15]) << 9) & 0x00030000)
    seed = struct.pack(">17I", *w[:17])
    for i in range(17, 521):
        calc = ((w[i - 17] << 23) & 0xFFFFFFFF) ^ (w[i - 16] >> 9) ^ w[i - 1]
        if (calc & 0xFFFCFFFF) != ((w[i] & 0xFF00FFFF) | ((w[i] << 2) & 0x00FC0000)):
            return None
        w[i] = calc
    return seed


def common_prefix(a, b):
    n = min(len(a), len(b))
    if a[:n] == b[:n]:
        return n
    lo, step = 0, 4096
    while lo + step <= n and a[lo:lo + step] == b[lo:lo + step]:
        lo += step
    while lo < n and a[lo] == b[lo]:
        lo += 1
    return lo


def rvz_unpack(buf, data_offset):
    """Undo RVZ packing: (size, bytes) pieces, or (size | 0x80000000, 68-byte seed) pieces of generated junk."""
    out, i, n = [], 0, len(buf)
    produced = 0
    while i < n:
        size = struct.unpack_from(">I", buf, i)[0]
        i += 4
        if size & 0x80000000:
            size &= 0x7FFFFFFF
            seed = bytes(buf[i:i + 68])
            i += 68
            out.append(Lfg(seed, (data_offset + produced) % CL).get(size))
        else:
            out.append(bytes(buf[i:i + size]))
            i += size
        produced += size
    return b"".join(out)


# ---- reading the containers: every one of them presents the disc as the bytes of the ISO
class DiscImage:
    size = 0
    kind = "ISO"

    def read(self, off, n):
        raise NotImplementedError

    def close(self):
        pass

    def info(self):
        h = self.read(0, 0x60)
        if h[:4] == b"WUP-":                                  # Wii U: "WUP-P-ARPE-02-10" (product code, version)
            return dict(platform="Wii U", id=h[6:10].decode("ascii", "replace"), title="")
        wii = struct.unpack(">I", h[0x18:0x1C])[0] == WII_MAGIC
        gc = struct.unpack(">I", h[0x1C:0x20])[0] == GC_MAGIC
        return dict(platform="Wii" if wii else "GameCube" if gc else "", id=h[:6].decode("ascii", "replace"),
                    title=h[0x20:0x60].split(b"\0")[0].decode("ascii", "replace").strip())


class IsoDisc(DiscImage):
    def __init__(self, path):
        self.f = open(path, "rb")
        self.size = os.path.getsize(path)

    def read(self, off, n):
        self.f.seek(off)
        return self.f.read(n).ljust(n, b"\0") if off < self.size else bytes(n)

    def close(self):
        self.f.close()


class WuxDisc(DiscImage):
    """Wii U disc compressed with WUX: a table of sector numbers, then each different sector once."""
    kind = "WUX"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(24)
        if len(h) < 24 or struct.unpack("<II", h[:8]) != (0x30585557, 0x1099D02E):
            raise UnpackError("this is not a WUX file")
        self.ss, _, self.size = struct.unpack("<IIQ", h[8:24])
        if self.ss < 0x100 or self.ss >= 0x10000000:
            raise UnpackError("this WUX file has an odd sector size")
        n = (self.size + self.ss - 1) // self.ss
        self.idx = struct.unpack("<%dI" % n, self.f.read(4 * n))
        self.base = (24 + 4 * n + self.ss - 1) // self.ss * self.ss

    def read(self, off, n):
        out, want = [], n
        while n > 0 and off < self.size:
            i, inb = divmod(off, self.ss)
            run = 1                                           # neighbouring sectors that are stored next to each other
            while run * self.ss - inb < n and i + run < len(self.idx) and self.idx[i + run] == self.idx[i] + run:
                run += 1
            take = min(n, run * self.ss - inb, self.size - off)
            self.f.seek(self.base + self.idx[i] * self.ss + inb)
            out.append(self.f.read(take))
            off += take
            n -= take
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def write_wux(disc, dst, progress, stopped, sector=0x8000):
    """A Wii U disc -> WUX: each different 32 KiB sector is stored once and a table says where every sector of the disc is."""
    n = (disc.size + sector - 1) // sector
    base = (24 + 4 * n + sector - 1) // sector * sector
    index, seen = [0] * n, {}
    with open(dst, "wb") as g:
        g.seek(base)
        step = 128
        for a in range(0, n, step):
            chunk = disc.read(a * sector, step * sector)
            for k in range(min(step, n - a)):
                s = chunk[k * sector:(k + 1) * sector]
                d = hashlib.sha1(s).digest()
                u = seen.get(d)
                if u is None:
                    u = seen[d] = len(seen)
                    g.write(s)
                index[a + k] = u
            progress(min(1.0, (a + step) / n))
            if stopped():
                raise UnpackError("stopped")
        g.seek(0)
        g.write(struct.pack("<IIIIQ", 0x30585557, 0x1099D02E, sector, 0, disc.size) + struct.pack("<%dI" % n, *index))
    progress(1.0)


class WbfsDisc(DiscImage):
    kind = "WBFS"

    def __init__(self, path):
        p = Path(path)
        self.files = [open(p, "rb")]
        for i in range(1, 10):                          # .wbf1, .wbf2 ... when the file was split for FAT32
            q = p.with_suffix(p.suffix[:-1] + str(i))
            if not q.is_file():
                break
            self.files.append(open(q, "rb"))
        self.sizes = [os.fstat(f.fileno()).st_size for f in self.files]
        f = self.files[0]
        head = f.read(12)
        if head[:4] != b"WBFS":
            raise UnpackError("this is not a WBFS file")
        n_sec, hd_shift, wb_shift = struct.unpack(">IBB", head[4:10])
        self.hd, self.wb = 1 << hd_shift, 1 << wb_shift
        if sum(self.sizes) != n_sec * self.hd or self.wb < CL:
            raise UnpackError("this WBFS file is damaged or cut short")
        per_disc = (143432 * 2 * CL + self.wb - 1) // self.wb
        f.seek(self.hd + 256)
        self.wlba = struct.unpack(">%dH" % per_disc, f.read(2 * per_disc))
        used = [i for i, v in enumerate(self.wlba) if v]
        end = (used[-1] + 1) * self.wb if used else 0
        self.size = WII_ISO_SL if end <= WII_ISO_SL else WII_ISO_DL

    def _pread(self, pos, n):
        for f, sz in zip(self.files, self.sizes):
            if pos < sz:
                f.seek(pos)
                got = f.read(min(n, sz - pos))
                return got + (self._pread(pos + len(got), n - len(got)) if len(got) < n else b"")
            pos -= sz
        return b""

    def read(self, off, n):
        out = []
        while n > 0:
            blk, inb = divmod(off, self.wb)
            take = min(n, self.wb - inb)
            lba = self.wlba[blk] if blk < len(self.wlba) else 0
            out.append(self._pread(lba * self.wb + inb, take).ljust(take, b"\0") if lba else bytes(take))
            off += take
            n -= take
        return b"".join(out)

    def close(self):
        for f in self.files:
            f.close()


class CisoDisc(DiscImage):
    kind = "CISO"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(0x8000)
        if len(h) < 0x8000 or h[:4] != b"CISO":
            raise UnpackError("this is not a GameCube/Wii CISO file")
        self.block = struct.unpack("<I", h[4:8])[0]
        self.pos, count = [], 0
        for v in h[8:]:
            self.pos.append(count if v == 1 else None)
            count += v == 1
        last = max((i for i, v in enumerate(self.pos) if v is not None), default=-1)
        self.size = (last + 1) * self.block
        head = self.read(0, 0x20)                       # the file only holds whole blocks, but a real disc ends part-way through the last one:
        for std in ((1459978240,) if head[0x1C:0x20] == bytes.fromhex("C2339F3D") else    # a GameCube disc is exactly this big,
                    (4699979776, 8511160320) if head[0x18:0x1C] == bytes.fromhex("5D1C9EA3") else ()):    # a Wii disc one of these two
            if self.size - self.block < std <= self.size:
                self.size = std

    def read(self, off, n):
        out = []
        while n > 0:
            blk, inb = divmod(off, self.block)
            take = min(n, self.block - inb)
            p = self.pos[blk] if blk < len(self.pos) else None
            if p is None:
                out.append(bytes(take))
            else:
                self.f.seek(0x8000 + p * self.block + inb)
                out.append(self.f.read(take).ljust(take, b"\0"))
            off += take
            n -= take
        return b"".join(out)

    def close(self):
        self.f.close()


class GczDisc(DiscImage):
    kind = "GCZ"

    def __init__(self, path):
        self.f = open(path, "rb")
        h = self.f.read(32)
        if h[:4] != GCZ_MAGIC:
            raise UnpackError("this is not a GCZ file")
        self.sub, self.csize, self.size, self.block, n = struct.unpack("<IQQII", h[4:32])
        self.ptr = struct.unpack("<%dQ" % n, self.f.read(8 * n))
        self.hash = struct.unpack("<%dI" % n, self.f.read(4 * n))
        self.base = 32 + 12 * n
        self.last = (None, b"")

    def _block(self, i):
        if self.last[0] == i:
            return self.last[1]
        p = self.ptr[i]
        raw_flag = p >> 63
        start = p & 0x7FFFFFFFFFFFFFFF
        end = (self.ptr[i + 1] & 0x7FFFFFFFFFFFFFFF) if i + 1 < len(self.ptr) else self.csize
        self.f.seek(self.base + start)
        raw = self.f.read(end - start)
        if zlib.adler32(raw) != self.hash[i]:
            raise UnpackError(f"the GCZ file is damaged (block {i})")
        data = raw if raw_flag else zlib.decompress(raw)
        self.last = (i, data)
        return data

    def read(self, off, n):
        out, want = [], n
        while n > 0 and off < self.size:
            blk, inb = divmod(off, self.block)
            data = self._block(blk)
            take = min(n, len(data) - inb, self.size - off)
            out.append(data[inb:inb + take])
            off += take
            n -= take
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def _purge_decode(data, size):
    out = bytearray(size)
    i = 0
    while i + 8 <= len(data) - 20:
        o, s = struct.unpack_from(">II", data, i)
        out[o:o + s] = data[i + 8:i + 8 + s]
        i += 8 + s
    return bytes(out)


class RvzDisc(DiscImage):
    """WIA and RVZ files."""

    def __init__(self, path):
        self.f = open(path, "rb")
        h1 = self.f.read(0x48)
        magic = h1[:4]
        if magic not in (b"WIA\x01", b"RVZ\x01"):
            raise UnpackError("this is not a WIA or RVZ file")
        self.rvz = magic == b"RVZ\x01"
        self.kind = "RVZ" if self.rvz else "WIA"
        h2size = struct.unpack(">I", h1[12:16])[0]
        self.size, fsize = struct.unpack(">QQ", h1[36:52])
        h2 = self.f.read(h2size)
        if hashlib.sha1(h1[:52]).digest() != h1[52:72] or hashlib.sha1(h2).digest() != h1[16:36]:
            raise UnpackError("the file's header checksums don't match; it is damaged")
        if fsize != os.path.getsize(path):
            raise UnpackError("the file is cut short")
        self.disc_type, self.method, self.level, self.chunk = struct.unpack(">IIiI", h2[:16])
        self.dhead = h2[16:0x90]
        if self.method > 5 or (self.rvz and self.method == 1):
            raise UnpackError(f"unsupported compression method {self.method}")
        npart, psize, poff = struct.unpack(">IIQ", h2[0x90:0xA0])
        nraw, roff, rsize, ngrp, goff, gsize = struct.unpack(">IQIIQI", h2[0xB4:0xD4])
        self.cdata = h2[0xD5:0xD5 + h2[0xD4]]
        self.f.seek(poff)
        pbytes = self.f.read(npart * psize)
        if hashlib.sha1(pbytes).digest() != h2[0xA0:0xB4]:
            raise UnpackError("the partition table is damaged")
        self.parts = []
        for i in range(npart):
            e = pbytes[i * psize:i * psize + 0x30].ljust(0x30, b"\0")
            pds = [struct.unpack(">4I", e[16 + 16 * j:32 + 16 * j]) for j in range(2)]
            self.parts.append((e[:16], pds, AesCbc(e[:16])))
        raw = self._decomp(self.method, self._pread(roff, rsize), nraw * 24)
        self.raws = [struct.unpack(">QQII", raw[i * 24:i * 24 + 24]) for i in range(nraw)]
        gs = 12 if self.rvz else 8
        graw = self._decomp(self.method, self._pread(goff, gsize), ngrp * gs)
        self.groups = [struct.unpack(">III" if self.rvz else ">II", graw[i * gs:(i + 1) * gs]) + ((0,) if not self.rvz else ())
                       for i in range(ngrp)]
        regions = []
        for i, (o, s, gi, ng) in enumerate(self.raws):
            if s:
                regions.append((o, o + s, "r", i))
        for i, (_, pds, _) in enumerate(self.parts):
            end = pds[1][0] + pds[1][1] if pds[1][1] else pds[0][0] + pds[0][1]
            if end > pds[0][0]:
                regions.append((pds[0][0] * CL, end * CL, "p", i))
        regions.sort()
        self.regions = regions
        self.starts = [r[0] for r in regions]
        self.cache = {}

    def _pread(self, pos, n):
        self.f.seek(pos)
        return self.f.read(n)

    def _decomp(self, method, data, size=0):
        if method == 0:
            return data
        if method == 1:
            return _purge_decode(data, size)
        if method == 2:
            return bz2.decompress(data)
        if method == 5:
            try:
                import zstandard
            except ImportError:
                raise UnpackError("this file uses zstd compression, which needs the 'zstandard' package") from None
            return zstandard.ZstdDecompressor().decompressobj().decompress(data)
        cd = self.cdata
        if method == 3:
            if len(cd) != 5 or cd[0] >= 225:
                raise UnpackError("unreadable LZMA settings")
            d = cd[0]
            filt = {"id": lzma.FILTER_LZMA1, "dict_size": struct.unpack("<I", cd[1:5])[0], "lc": d % 9, "lp": d // 9 % 5, "pb": d // 45}
        else:
            if len(cd) != 1 or cd[0] > 40:
                raise UnpackError("unreadable LZMA2 settings")
            p = cd[0]
            filt = {"id": lzma.FILTER_LZMA2, "dict_size": 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)}
        return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt]).decompress(data)

    def _group(self, gi, size, nlists, data_off):
        """One group's data (`size` bytes) and its lists of hash exceptions."""
        key = (gi, size)
        if key in self.cache:
            return self.cache[key]
        d4, ds, packed = self.groups[gi]
        method = self.method
        if self.rvz:
            if not ds & 0x80000000:
                method = 0
            ds &= 0x7FFFFFFF
        if ds == 0:
            res = (bytes(size), [[] for _ in range(nlists)])
        else:
            raw = self._pread(d4 * 4, ds)
            if method > 1:
                raw = self._decomp(method, raw)
            lists, pos = [], 0
            for _ in range(nlists):
                cnt = struct.unpack_from(">H", raw, pos)[0]
                lists.append([(struct.unpack_from(">H", raw, pos + 2 + 22 * k)[0], raw[pos + 4 + 22 * k:pos + 24 + 22 * k]) for k in range(cnt)])
                pos += 2 + 22 * cnt
            if method <= 1 and nlists:
                pos = (pos + 3) & ~3
            body = raw[pos:]
            if method == 1:
                body = _purge_decode(body, packed or size)
            if packed:
                body = rvz_unpack(body, data_off)
            if len(body) < size:
                raise UnpackError("a block of the file is shorter than it should be")
            res = (body[:size], lists)
        if len(self.cache) > 3:
            self.cache.pop(next(iter(self.cache)))
        self.cache[key] = res
        return res

    def _raw_part(self, ri, off, n):
        o, s, gi, ng = self.raws[ri]
        base = o - o % CL
        end = o + s
        g = (off - base) // self.chunk
        gstart = base + g * self.chunk
        gsize = min(self.chunk, end - gstart)
        body, _ = self._group(gi + g, gsize, 0, g * self.chunk)
        a = off - gstart
        return body[a:a + n]

    def _wii_group(self, pi, g):
        key, pds, aes = self.parts[pi]
        p0 = pds[0][0]
        total = (pds[1][0] + pds[1][1] - p0) if pds[1][1] else pds[0][1]
        lo_c, hi_c = g * 64, min(g * 64 + 64, total)
        data = bytearray(64 * CLD)
        exc = []
        cs = self.chunk // CL
        for first, n, gidx, ngr in pds:
            if not n:
                continue
            rel0 = first - p0
            lo, hi = max(lo_c, rel0), min(hi_c, rel0 + n)
            if lo >= hi:
                continue
            for ci in range((lo - rel0) // cs, (hi - 1 - rel0) // cs + 1):
                cstart = rel0 + ci * cs
                cn = min(cs, rel0 + n - cstart)
                nl = max(1, cs // 64)
                body, lists = self._group(gidx + ci, cn * CLD, nl, ci * cs * CLD)
                a, b = max(lo, cstart), min(hi, cstart + cn)
                data[(a - lo_c) * CLD:(b - lo_c) * CLD] = body[(a - cstart) * CLD:(b - cstart) * CLD]
                if cs >= 64:
                    lst, add = lists[(lo_c - cstart) // 64], 0
                else:
                    lst, add = lists[0], (cstart - lo_c) * 0x400
                exc += [(o + add, h) for o, h in lst]
        hb = wii_hash_group(data)
        for o, h in exc:
            if o // 0x400 < 64:
                hb[o // 0x400][o % 0x400:o % 0x400 + 20] = h
        out = []
        for b in range(hi_c - lo_c):
            eh = aes.crypt(hb[b])
            out.append(eh + aes.crypt(bytes(data[b * CLD:(b + 1) * CLD]), eh[0x3D0:0x3E0]))
        return b"".join(out)

    def read(self, off, n):
        out = []
        end = min(off + n, self.size)
        want = n
        while off < end:
            if off < 0x80:
                take = min(0x80 - off, end - off)
                out.append(self.dhead[off:off + take])
                off += take
                continue
            ri = bisect.bisect_right(self.starts, off) - 1
            if ri < 0 or off >= self.regions[ri][1]:
                nxt = self.starts[ri + 1] if ri + 1 < len(self.starts) else end
                take = min(nxt, end) - off
                out.append(bytes(take))
                off += take
                continue
            s, e, kind, idx = self.regions[ri]
            if kind == "r":
                piece = self._raw_part(idx, off, min(end, e) - off)
            else:
                g, inb = divmod(off - s, 64 * CL)
                ck = ("w", idx, g)
                if ck not in self.cache:
                    if len(self.cache) > 3:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[ck] = self._wii_group(idx, g)
                piece = self.cache[ck][inb:inb + min(end, e) - off]
            out.append(piece)
            off += len(piece)
        return b"".join(out).ljust(want, b"\0")

    def close(self):
        self.f.close()


def wii_hash_group(data):
    """The 64 hash blocks (H0, H1, H2) of one Wii group of 64 x 0x7C00 bytes of data."""
    sha1 = hashlib.sha1
    hb = [bytearray(0x400) for _ in range(64)]
    h0s = []
    for b in range(64):
        blk = data[b * CLD:(b + 1) * CLD]
        h0 = b"".join([sha1(blk[j:j + 0x400]).digest() for j in range(0, CLD, 0x400)])
        hb[b][0:620] = h0
        h0s.append(sha1(h0).digest())
    h2 = []
    for s in range(8):
        h1 = b"".join(h0s[s * 8:s * 8 + 8])
        for b in range(s * 8, s * 8 + 8):
            hb[b][0x280:0x320] = h1
        h2.append(sha1(h1).digest())
    h2 = b"".join(h2)
    for b in range(64):
        hb[b][0x340:0x3E0] = h2
    return hb


def open_disc(path):
    """Open any GameCube/Wii disc container by what is inside it."""
    with open(path, "rb") as f:
        h = f.read(8)
    if h[:4] == b"WBFS":
        return WbfsDisc(path)
    if h[:4] == b"WUX0":
        return WuxDisc(path)
    if h[:4] == b"CISO" and ciso_is_wii(struct.unpack("<I", h[4:8])[0]):
        return CisoDisc(path)
    if h[:4] == GCZ_MAGIC:
        return GczDisc(path)
    if h[:4] in (b"WIA\x01", b"RVZ\x01"):
        return RvzDisc(path)
    return IsoDisc(path)


def disc_kind(path):
    """'Wii' or 'GameCube' when the file is one of those discs in any of the supported containers, else ''."""
    try:
        d = open_disc(path)
    except (OSError, UnpackError, struct.error, ValueError):
        return ""
    try:
        return d.info()["platform"]
    except (UnpackError, OSError, struct.error, ValueError, zlib.error):
        return ""
    finally:
        d.close()


def disc_to_iso(disc, dst, progress, stopped, step=1 << 22):
    with open(dst, "wb") as g:
        off = 0
        while off < disc.size:
            n = min(step, disc.size - off)
            g.write(disc.read(off, n))
            off += n
            progress(off / disc.size)
            if stopped():
                raise UnpackError("stopped")


# ---- writing the containers
class KeyNeeded(UnpackError):
    """A Wii conversion that can't go on without the user's own Wii key."""


WII_KEYS = [None, None]                  # the Wii common key and the Korean one, only ever from the user's own files
WII_TOOL = [""]                          # the user's DolphinTool.exe, when there is one
WII_SCRUB = [True]
KEYS_DIR = HERE / "keys"
KEYS_README = """Put your own Wii keys in this folder. CHD Manager doesn't contain them and never downloads them.

A file here can be called anything ending in .txt, .key, .bin or .hex. It holds the key as 32 hexadecimal
characters (16 bytes), for example one line:

    common: <32 hex characters>
    korean: <32 hex characters>   (only for Korean discs)

A line with no label is taken as the common key. A file of exactly 16 raw bytes also works (a name containing
"korea" makes it the Korean key). You can instead paste the key into Settings, Formats.

The key is only needed to write RVZ, WIA or WBFS from a Wii disc. Reading every format, and everything for GameCube and
Wii U discs, works without it. If Dolphin is on this PC, its DolphinTool.exe can do the RVZ and WIA conversions instead.
"""


def parse_wii_keys(data, name=""):
    """[common key or None, Korean key or None] found in the contents of one key file."""
    out = [None, None]
    if len(data) == 16:
        out["korea" in name.lower()] = bytes(data)
        return out
    for line in data.decode("utf-8", "replace").splitlines():
        line = re.split(r"#|;|//", line)[0]
        m = re.search(r"(?<![0-9a-fA-F])(?:0x)?([0-9a-fA-F]{32})(?![0-9a-fA-F])", line) or \
            re.search(r"((?:[0-9a-fA-F]{2}[ :\-]){15}[0-9a-fA-F]{2})", line)
        if m:
            key = bytes.fromhex(re.sub(r"[^0-9a-fA-F]", "", m.group(1)))
            out["korea" in line.lower() or " new" in line.lower()] = key
    return out


def load_wii_keys(pasted_common="", pasted_korean=""):
    """Read the keys folder, then whatever was pasted into Settings; sets WII_KEYS."""
    keys = [None, None]
    try:
        for f in sorted(KEYS_DIR.iterdir()):
            if f.is_file() and f.suffix.lower() in (".txt", ".key", ".bin", ".hex") and f.name.lower() != "readme.txt" and f.stat().st_size < 4096:
                for i, k in enumerate(parse_wii_keys(f.read_bytes(), f.name)):
                    keys[i] = keys[i] or k
    except OSError:
        pass
    for i, text in enumerate((pasted_common, pasted_korean)):
        if not keys[i] and text:
            keys[i] = parse_wii_keys(text.encode("ascii", "ignore"))[0]
    WII_KEYS[:] = keys


def ensure_keys_dir():
    try:
        KEYS_DIR.mkdir(exist_ok=True)
        if not (KEYS_DIR / "README.txt").exists():
            (KEYS_DIR / "README.txt").write_text(KEYS_README, "utf-8")
    except OSError:
        pass


def find_dolphintool(configured=""):
    """The user's DolphinTool.exe: the path set in Settings, next to this program, on the PATH, or in a folder named Dolphin*
    directly inside the usual program folders. Nothing else is searched."""
    cands = [Path(configured)] if configured else []
    cands += [HERE / "DolphinTool.exe", HERE / "Dolphin" / "DolphinTool.exe", HERE / "Dolphin-x64" / "DolphinTool.exe"]
    w = shutil.which("DolphinTool.exe") or shutil.which("dolphin-tool") or shutil.which("DolphinTool")
    if w:
        cands.append(Path(w))
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = os.environ.get(env)
        base = Path(root) / ("Programs" if env == "LOCALAPPDATA" else "") if root else None
        try:
            if base and base.is_dir():
                cands += [d / "DolphinTool.exe" for d in sorted(base.iterdir()) if d.name.lower().startswith("dolphin")]
        except OSError:
            pass
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            pass
    return ""


def wii_can(what):
    """Whether a conversion that needs the Wii key can be done on this PC: 'rvz' (RVZ and WIA) or 'wbfs'."""
    return bool(WII_KEYS[0] or WII_TOOL[0]) or (what == "wbfs" and not WII_SCRUB[0])


def wii_partitions(disc):
    """[(partition offset, data offset, data size, title key)] of the Wii partitions this code can handle."""
    out = []
    try:
        tbl = disc.read(0x40000, 0x20)
        for g in range(4):
            n, off = struct.unpack(">II", tbl[g * 8:g * 8 + 8])
            if not n or n > 16:
                continue
            ents = disc.read(off << 2, n * 8)
            for i in range(n):
                poff = struct.unpack(">I", ents[i * 8:i * 8 + 4])[0] << 2
                h = disc.read(poff, 0x2C0)
                if struct.unpack(">I", h[:4])[0] != 0x10001 or h[0x1F1] > 1:
                    continue
                doff, dsize = (x << 2 for x in struct.unpack(">II", h[0x2B8:0x2C0]))
                if (poff + doff) % CL or dsize < CL or poff + doff + dsize > disc.size + CL:
                    continue
                ckey = WII_KEYS[h[0x1F1]]
                if not ckey:
                    raise KeyNeeded("this Wii disc needs your Wii key: put it in the keys folder or paste it into Settings")
                key = AesCbc(ckey).crypt(h[0x1BF:0x1CF], h[0x1DC:0x1E4] + bytes(8), decrypt=True)
                first = disc.read(poff + doff, 0x420)
                plain = AesCbc(key).crypt(first[0x400:0x420], first[0x3D0:0x3E0], decrypt=True)
                if struct.unpack(">I", plain[0x18:0x1C])[0] != WII_MAGIC:
                    raise KeyNeeded("the Wii key doesn't fit this disc (is it the right key?)")
                out.append((poff, doff, min(dsize, disc.size - poff - doff), key))
    except KeyNeeded:
        raise
    except (UnpackError, struct.error, ValueError):
        return []
    out.sort()
    ok, last = [], 0
    for p in out:
        if p[0] >= last:
            ok.append(p)
            last = p[0] + p[1] + p[2]
    return ok


def wii_used_clusters(disc):
    """One byte per 0x8000 of a Wii disc: 1 when something on the disc lives there (headers, system files, game files).
    None when the disc can't be read that way."""
    parts = wii_partitions(disc)
    if not parts:
        return None
    used = bytearray((disc.size + CL - 1) // CL + 1)

    def mark(a, b):
        for s in range(a // CL, min((b - 1) // CL + 1, len(used))):
            used[s] = 1
    mark(0, 0x50000)
    try:
        for poff, doff, dsize, key in parts:
            mark(poff, poff + doff)
            aes = AesCbc(key)
            start, nclu = poff + doff, dsize // CL

            def pdata(off, n):
                out = []
                c = off // CLD
                while c * CLD < off + n and c < nclu:
                    raw = disc.read(start + c * CL, CL)
                    out.append(aes.crypt(raw[0x400:], raw[0x3D0:0x3E0], decrypt=True))
                    c += 1
                return b"".join(out)[off - off // CLD * CLD:][:n]

            def mark_data(a, b):
                if b > a:
                    mark(start + a // CLD * CL, start + ((b - 1) // CLD + 1) * CL)
            boot = pdata(0, 0x440)
            dol, fst, fsz = (x << 2 for x in struct.unpack(">III", boot[0x420:0x42C]))
            ap = pdata(0x2440, 0x20)
            ap_end = 0x2460 + struct.unpack(">II", ap[0x14:0x1C])[0] + struct.unpack(">II", ap[0x14:0x1C])[1] if len(ap) >= 0x1C else 0x2440
            dol_end = dol
            if dol:
                hd = pdata(dol, 0x100)
                if len(hd) == 0x100:
                    offs, sizes = struct.unpack(">18I", hd[:72]), struct.unpack(">18I", hd[0x90:0xD8])
                    dol_end = dol + max([o + s for o, s in zip(offs, sizes)] + [0x100])
            mark_data(0, max(fst + fsz, dol_end, ap_end))
            f = pdata(fst, fsz)
            n = struct.unpack(">I", f[8:12])[0] if len(f) >= 12 else 0
            for i in range(1, n):
                flags, off, ln = struct.unpack(">BxxxII", f[i * 12:i * 12 + 12])
                if not flags:
                    mark_data(off << 2, (off << 2) + ln)
    except KeyNeeded:
        raise
    except (UnpackError, struct.error, ValueError):
        return None
    return used


def write_wbfs(disc, dst, progress, stopped, scrub=True):
    """A Wii disc -> WBFS: the disc is cut into 2 MiB blocks and blocks nothing uses are left out (scrubbing)."""
    info = disc.info()
    if info["platform"] != "Wii":
        raise UnpackError("WBFS can only hold Wii discs")
    wb, hd = 1 << 21, 512
    per_disc = (143432 * 2 * CL + wb - 1) // wb
    used = wii_used_clusters(disc) if scrub else None
    nblocks = (disc.size + wb - 1) // wb
    keep = []
    for b in range(nblocks):
        if used is not None:
            if any(used[b * 64:b * 64 + 64]):
                keep.append(b)
        elif disc.read(b * wb, wb).strip(b"\0"):
            keep.append(b)
        if b % 64 == 0:
            progress(0.1 * b / nblocks)
            if stopped():
                raise UnpackError("stopped")
    if not keep:
        raise UnpackError("this disc has no data in it")
    total_secs = len(keep) + 1
    wlba = [0] * per_disc
    for i, b in enumerate(keep):
        wlba[b] = i + 1
    head = bytearray(hd)
    head[0:4] = b"WBFS"
    struct.pack_into(">IBB", head, 4, total_secs * wb // hd, 9, 21)
    head[12] = 1
    with open(dst, "wb") as g:
        g.write(bytes(head))
        g.write(disc.read(0, 256))
        g.write(struct.pack(">%dH" % per_disc, *wlba))
        g.write(bytes(wb - g.tell()))
        for i, b in enumerate(keep):
            g.write(disc.read(b * wb, wb).ljust(wb, b"\0"))
            if i % 8 == 0:
                progress(0.1 + 0.9 * i / len(keep))
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def write_ciso(disc, dst, progress, stopped, block=1 << 21):
    """A disc -> CISO (Dolphin's layout: a 0x8000-byte header with a map of the blocks that are present, then those blocks)."""
    n = (disc.size + block - 1) // block
    if n > 0x7FF8:
        raise UnpackError("this disc is too big for a CISO with 2 MiB blocks")
    present = bytearray(0x7FF8)
    with open(dst, "wb") as g:
        g.write(bytes(0x8000))
        for b in range(n):
            data = disc.read(b * block, block)
            if data.strip(b"\0") or b == n - 1:         # the last block is always kept, so the size survives
                present[b] = 1
                g.write(data)
            if b % 16 == 0:
                progress(b / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(0)
        g.write(b"CISO" + struct.pack("<I", block) + bytes(present))
    progress(1.0)


def write_gcz(disc, dst, progress, stopped, block=None, level=9):
    """A disc -> GCZ (deflate-compressed blocks with a table of offsets and checksums)."""
    info = disc.info()
    block = block or (0x4000 if info["platform"] != "Wii" else 0x20000)
    n = (disc.size + block - 1) // block
    offsets, hashes = [], []
    with open(dst, "wb") as g:
        g.write(bytes(32 + 12 * n))
        pos = 0
        for i in range(n):
            data = disc.read(i * block, block).ljust(block, b"\0")
            c = zlib.compressobj(level)
            comp = c.compress(data) + c.flush()
            if len(comp) > block - 10:
                offsets.append(pos | 1 << 63)
                comp = data
            else:
                offsets.append(pos)
            hashes.append(zlib.adler32(comp))
            g.write(comp)
            pos += len(comp)
            if i % 256 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(0)
        g.write(GCZ_MAGIC + struct.pack("<IQQII", 1 if info["platform"] == "Wii" else 0, pos, disc.size, block, n))
        g.write(struct.pack("<%dQ" % n, *offsets) + struct.pack("<%dI" % n, *hashes))
    progress(1.0)


def rvz_pack(buf, bytes_per_chunk, nchunks, data_offset):
    """Find the pseudo-random junk in buf and write each chunk the way RVZ stores it: [(main data, packed size)]."""
    total = len(buf)
    junk = []                                              # (end, start, seed), in order
    nz = re.compile(rb"[^\x00]")
    pos, off = 0, data_offset
    while pos < total:
        m = nz.search(buf, pos)
        z = (m.start() if m else total) - pos
        pos += z
        off += z
        if pos >= total:
            break
        to_read = min(CL - off % CL, total - pos)
        dm = off % CL
        skip = -dm % 4
        seg = buf[pos + skip:pos + to_read]
        if len(seg) >= 2084:
            seed = lfg_find_seed(seg, (dm + skip) // 4)
            if seed:
                n = common_prefix(Lfg(seed, dm).get(to_read), buf[pos:pos + to_read])
                if n > 0:
                    junk.append((pos + n, pos, seed))
        pos += to_read
        off += to_read
    out = []
    ends = [j[0] for j in junk]
    for i in range(nchunks):
        cur = i * bytes_per_chunk
        end = min(cur + bytes_per_chunk, total)
        main, packed, first = bytearray(), 0, True
        while cur < end:
            js = je = end
            seed = None
            if end - cur > 68:
                k = bisect.bisect_right(ends, cur + 68)
                if k < len(junk) and junk[k][1] + 68 < end:
                    js, je, seed = max(cur, junk[k][1]), min(end, junk[k][0]), junk[k][2]
            if first:
                if js == end:
                    main += buf[cur:end]
                    break
                first = False
            if js > cur:
                main += struct.pack(">I", js - cur) + buf[cur:js]
                packed += 4 + js - cur
                cur = js
            if je > cur:
                main += struct.pack(">I", (je - cur) | 0x80000000) + seed
                packed += 4 + 68
                cur = je
        out.append((bytes(main), packed))
    return out


class _Compressor:
    def __init__(self, method, level, chunk):
        self.method = method
        self.level = level = max(1, min(9, level)) if method in (2, 3, 4) else level
        dict_size = max(1 << 16, 1 << (max(chunk, 1) - 1).bit_length())
        self.cdata = b""
        if method == 3:
            self.filt = {"id": lzma.FILTER_LZMA1, "preset": level, "dict_size": dict_size, "lc": 3, "lp": 0, "pb": 2}
            self.cdata = bytes([93]) + struct.pack("<I", dict_size)
        elif method == 4:
            p = 0
            while ((2 | (p & 1)) << (p // 2 + 11)) < dict_size:
                p += 1
            self.filt = {"id": lzma.FILTER_LZMA2, "preset": level, "dict_size": (2 | (p & 1)) << (p // 2 + 11)}
            self.cdata = bytes([p])
        elif method == 5:
            try:
                import zstandard
            except ImportError:
                raise UnpackError("zstd compression needs the 'zstandard' package") from None
            self.zc = zstandard.ZstdCompressor(level=level)

    def __call__(self, data):
        if self.method == 0:
            return data
        if self.method == 2:
            return bz2.compress(data, self.level)
        if self.method == 5:
            return self.zc.compress(data)
        return lzma.compress(data, lzma.FORMAT_RAW, filters=[self.filt])


def write_rvz(disc, dst, progress, stopped, rvz=True, method=5, level=5, chunk=0x20000):
    """A GameCube/Wii disc -> RVZ (or WIA). Wii partitions are stored decrypted and without their hashes; the pseudo-random
    padding is stored as a seed (RVZ only); every chunk is compressed on its own."""
    info = disc.info()
    if info["platform"] not in ("Wii", "GameCube"):
        raise UnpackError("this isn't a GameCube or Wii disc")
    if rvz:
        if chunk < CL or chunk > 0x200000 or chunk & (chunk - 1):
            raise UnpackError("RVZ chunk size must be a power of two from 32 KiB to 2 MiB")
    else:
        chunk = 0x200000
        method = 4 if method not in (0, 2, 3, 4) else method
    if method > (5 if rvz else 4) or (rvz and method == 1):
        raise UnpackError("unsupported compression method")
    comp = _Compressor(method, level, chunk)
    size = disc.size
    parts = wii_partitions(disc) if info["platform"] == "Wii" else []
    # ---- layout: raw regions and partition regions in disc order, each with its range of groups
    raws, pents, todo, groups = [], [], [], 0

    def add_raw(offset, n):
        nonlocal groups
        skip = min(0x80 - offset, n) if offset < 0x80 else 0
        offset, n = offset + skip, n - skip
        if n <= 0:
            return
        ng = -(-(n + offset % CL) // chunk)
        raws.append((offset, n, groups, ng))
        todo.append(("r", len(raws) - 1))
        groups += ng
    last = 0
    for poff, doff, dsize, key in parts:
        add_raw(last, poff - last)
        add_raw(poff, doff)
        fl = dsize // CL * CL
        ng = -(-fl // chunk)
        first = (poff + doff) // CL
        pents.append((key, [(first, fl // CL, groups, ng), (first + fl // CL, 0, groups + ng, 0)]))
        todo.append(("p", len(pents) - 1))
        groups += ng
        last = poff + doff + fl
    add_raw(last, size - last)
    entries = [None] * groups                                # (offset/4, size field, packed size)
    cs = chunk // CL

    def finish(gi, exc, main, packed, zero):
        nonlocal pos
        if zero and not any(exc):
            entries[gi] = (0, 0, 0)                           # all zeroes, no exceptions: a size of 0 says so
            return
        lists = b"".join(struct.pack(">H", len(x)) + b"".join(struct.pack(">H", o) + h for o, h in x) for x in exc)
        flag, payload = 0, None
        if method:
            cdat = comp(lists + main)
            if not rvz:
                payload = cdat
            elif len(cdat) < len(main) + len(lists) + -len(lists) % 4:
                payload, flag = cdat, 0x80000000
        if payload is None:                                   # not compressed: lists padded to 4 bytes, then the data
            payload = lists + bytes(-len(lists) % 4) + main
        entries[gi] = (pos // 4, len(payload) | flag, packed)
        g.write(payload)
        pos += len(payload)
        pad = -pos % 4
        if pad:
            g.write(bytes(pad))
            pos += pad
    with open(dst, "wb") as g:
        g.write(bytes(0x48 + 0xDC))
        pos = 0x48 + 0xDC
        done = 0
        for kind, idx in todo:
            if kind == "r":
                off, n, gi, ng = raws[idx]
                base = off - off % CL
                end = off + n
                for k in range(ng):
                    a = base + k * chunk
                    data = disc.read(a, min(chunk, end - a))
                    if rvz:
                        main, packed = rvz_pack(data, len(data), 1, a)[0]
                    else:
                        main, packed = data, 0
                    finish(gi + k, [], main, packed, not data.strip(b"\0"))
                    done += len(data)
                    progress(done / size)
                    if stopped():
                        raise UnpackError("stopped")
            else:
                key, pds = pents[idx]
                first, nclu, gi, ng = pds[0]
                aes = AesCbc(key)
                start = first * CL
                ngroups64 = -(-nclu // 64)
                for g64 in range(ngroups64):
                    nb = min(64, nclu - g64 * 64)
                    raw = disc.read(start + g64 * 64 * CL, nb * CL)
                    data = bytearray(64 * CLD)
                    actual = []
                    for b in range(nb):
                        c = raw[b * CL:(b + 1) * CL]
                        actual.append(aes.crypt(c[:0x400], bytes(16), decrypt=True))
                        data[b * CLD:(b + 1) * CLD] = aes.crypt(c[0x400:], c[0x3D0:0x3E0], decrypt=True)
                    hb = wii_hash_group(data)
                    bpc = min(64, cs)
                    nch = -(-nb // bpc)
                    exc = [[] for _ in range(nch)]
                    for b in range(nb):
                        ch, bi = divmod(b, bpc)
                        want, got = actual[b], bytes(hb[b])
                        if want == got:
                            continue
                        for fo, fs in ((0, 620), (620, 20), (0x280, 160), (0x320, 32), (0x340, 160), (0x3E0, 32)):
                            for l in range(0, fs, 20):
                                o = fo + min(l, fs - 20)
                                if want[o:o + 20] != got[o:o + 20]:
                                    exc[ch].append((bi * 0x400 + o, want[o:o + 20]))
                    body = bytes(data[:nb * CLD])
                    if rvz:
                        packs = rvz_pack(body, bpc * CLD, nch, g64 * 64 * CLD)
                    else:
                        packs = [(body[c * bpc * CLD:(c + 1) * bpc * CLD], 0) for c in range(nch)]
                    for c in range(nch):
                        main, packed = packs[c]
                        zero = not body[c * bpc * CLD:(c + 1) * bpc * CLD].strip(b"\0")
                        finish(gi + g64 * (64 // bpc) + c, [exc[c]], main, packed, zero)
                    done += nb * CL
                    progress(done / size)
                    if stopped():
                        raise UnpackError("stopped")
        # ---- the tables and headers
        praw = b"".join(struct.pack(">16s", k) + b"".join(struct.pack(">4I", *p) for p in pd) for k, pd in pents)
        rawt = b"".join(struct.pack(">QQII", *r) for r in raws)
        grpt = b"".join(struct.pack(">III" if rvz else ">II", *(e if rvz else e[:2])) for e in entries)
        crawt, cgrpt = comp(rawt), comp(grpt)
        poff = pos
        g.write(praw)
        roff = poff + len(praw)
        g.write(crawt)
        goff = roff + len(crawt)
        g.write(cgrpt)
        fsize = goff + len(cgrpt)
        h2 = struct.pack(">IIiI", 2 if info["platform"] == "Wii" else 1, method, level, chunk) + disc.read(0, 0x80)
        h2 += struct.pack(">IIQ", len(pents), 0x30, poff) + hashlib.sha1(praw).digest()
        h2 += struct.pack(">IQIIQI", len(raws), roff, len(crawt), len(entries), goff, len(cgrpt))
        h2 += bytes([len(comp.cdata)]) + comp.cdata.ljust(7, b"\0")
        h1 = (b"RVZ\x01" if rvz else b"WIA\x01") + struct.pack(">II", 0x01000000, 0x00030000 if rvz else 0x01000000)
        h1 += struct.pack(">I", len(h2)) + hashlib.sha1(h2).digest() + struct.pack(">QQ", size, fsize)
        h1 += hashlib.sha1(h1).digest()
        g.seek(0)
        g.write(h1 + h2)
    progress(1.0)


def ecm_probe(path):
    """The track type in the first sector of an ECM file ('MODE1/2352', 'MODE2/2352', 'MODE2/2336' or '')."""
    with open(path, "rb") as f:
        if f.read(4) != b"ECM\x00":
            raise UnpackError("this is not an ECM file")
        c = f.read(1)
        if not c:
            return ""
        c = c[0]
        typ, num, bits = c & 3, (c >> 2) & 0x1F, 5
        while c & 0x80:
            c = f.read(1)[0]
            num |= (c & 0x7F) << bits
            bits += 7
        if typ == 1:
            return "MODE1/2352"
        if typ in (2, 3):
            return "MODE2/2336"
        lit = f.read(min(num + 1, 16))
        if len(lit) >= 16 and lit[:12] == _SYNC and lit[15] in (1, 2):
            return f"MODE{lit[15]}/2352"
    return ""


def verify_disc_file(path, src, kind, progress, stopped):
    """True when the container at `path` holds the disc `src`. A WBFS is checked on the blocks it stores and a CISO up to
    the size of the disc, since those formats leave out (or pad) data that nothing uses."""
    d = open_disc(path)
    try:
        if kind == "wbfs":
            for i, v in enumerate(d.wlba):
                if v and d.read(i * d.wb, d.wb) != src.read(i * d.wb, d.wb):
                    return False
                if i % 16 == 0:
                    progress(i / len(d.wlba))
                    if stopped():
                        raise UnpackError("stopped")
            return True
        if kind == "ciso":
            off = 0
            while off < src.size:
                n = min(1 << 22, src.size - off)
                if d.read(off, n) != src.read(off, n):
                    return False
                off += n
                progress(off / src.size)
                if stopped():
                    raise UnpackError("stopped")
            return True
        return discs_equal(d, src, progress, stopped)
    finally:
        d.close()


def discs_equal(a, b, progress=lambda x: None, stopped=lambda: False, step=1 << 22):
    """True when two disc images hold exactly the same bytes."""
    if a.size != b.size:
        return False
    off = 0
    while off < a.size:
        n = min(step, a.size - off)
        if a.read(off, n) != b.read(off, n):
            return False
        off += n
        progress(off / a.size)
        if stopped():
            raise UnpackError("stopped")
    return True


# ------------------------------------------------------ CHD -> PBP (PlayStation 1), written from the layout notes above
PBP_BLOCK = 16 * 2352


def cue_layout(cue):
    """Read the .cue chdman wrote (one BIN) -> dict(bin, tracks=[(is_data, lba)], inserts=[(file sector, count)], total).
    A PREGAP (silence that isn't stored in the BIN) is put back into the stream so every track lands where it
    does on the real disc; the first track's 2-second lead-in is the standard offset and never part of the stream."""
    cue = Path(cue)
    binf, tracks, cur = None, [], None
    for line in cue.read_text("utf-8", "replace").splitlines():
        t = line.strip()
        m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
        if m:
            if binf is not None:
                raise UnpackError("this disc is stored in several files")
            binf = cue.parent / (m.group(1) or m.group(2))
            continue
        m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
        if m:
            cur = dict(data=m.group(1).upper() != "AUDIO", pregap=0, idx=None)
            tracks.append(cur)
            continue
        m = re.match(r"(PREGAP|INDEX\s+01)\s+(\d+):(\d+):(\d+)", t, re.I)
        if m and cur is not None:
            frames = (int(m.group(2)) * 60 + int(m.group(3))) * 75 + int(m.group(4))
            cur["pregap" if m.group(1).upper() == "PREGAP" else "idx"] = frames
    if binf is None or not tracks or any(t["idx"] is None for t in tracks):
        raise UnpackError("can't read the track list of this disc")
    inserts, extra, out = [], 0, []
    for n, t in enumerate(tracks):
        if n and t["pregap"]:
            inserts.append((t["idx"], t["pregap"]))
            extra += t["pregap"]
        out.append((t["data"], t["idx"] + extra))
    sectors = binf.stat().st_size // 2352
    return dict(bin=binf, tracks=out, inserts=inserts, total=sectors + extra)


def iter_disc_blocks(lay):
    """The disc as consecutive 37632-byte blocks (16 sectors each); the last block is padded with zeros."""
    def pieces():
        done = 0
        with open(lay["bin"], "rb") as f:
            for at, count in lay["inserts"] + [(lay["total"], 0)]:
                left = (at - done) * 2352
                while left > 0:
                    chunk = f.read(min(left, 1 << 20))
                    if not chunk:
                        return
                    left -= len(chunk)
                    yield chunk
                done = at
                for _ in range(count):
                    yield bytes(2352)
    buf = bytearray()
    for piece in pieces():
        buf += piece
        while len(buf) >= PBP_BLOCK:
            yield bytes(buf[:PBP_BLOCK])
            del buf[:PBP_BLOCK]
    if buf:
        yield bytes(buf) + bytes(PBP_BLOCK - len(buf))


def pbp_toc_bytes(tracks, total):
    """The disc's table of contents the way PBP files store it: 10-byte entries (A0 first track, A1 last track,
    A2 lead-out, then one per track)."""
    def msf(frames):
        return bytes((_bcd(frames // 4500), _bcd(frames // 75 % 60), _bcd(frames % 75)))
    ctrl = lambda is_data: 0x41 if is_data else 0x01
    out = [bytes((ctrl(tracks[0][0]), 0, 0xA0, 0, 0, 0, 0, _bcd(1), 0x20, 0)),
           bytes((ctrl(tracks[-1][0]), 0, 0xA1, 0, 0, 0, 0, _bcd(len(tracks)), 0, 0)),
           bytes((ctrl(tracks[-1][0]), 0, 0xA2, 0, 0, 0, 0)) + msf(total + 150)]
    for n, (is_data, lba) in enumerate(tracks, 1):
        out.append(bytes((ctrl(is_data), 0, _bcd(n))) + (msf(lba) if n > 1 else bytes((0, 2, 1))) + bytes((0,)) + msf(lba + 150))
    return b"".join(out)


def build_sfo(entries):
    """A PARAM.SFO. entries: [(key, value, max_len)] sorted by key; value is an int or a string."""
    keys, data, index = b"", b"", b""
    for key, val, maxlen in entries:
        raw = key.encode() + b"\0"
        if isinstance(val, int):
            fmt, ln, body, maxlen = 0x0404, 4, struct.pack("<I", val), 4
        else:
            body = val.encode("utf-8", "replace")[:maxlen - 1] + b"\0"
            fmt, ln = 0x0204, len(body)
            body += bytes(maxlen - len(body))
        index += struct.pack("<HHIII", len(keys), fmt, ln, maxlen, len(data))
        keys += raw
        data += body
    keys += bytes((-len(keys)) % 4)
    key_off = 20 + len(index)
    return b"\0PSF" + struct.pack("<IIII", 0x101, key_off, key_off + len(keys), len(entries)) + index + keys + data


def ps1_boot_id(path, stride_off, stride=2352, ps2=False):
    """The game code ('SLUS01545') from the BOOT line of SYSTEM.CNF, read straight from the extracted BIN; '' if
    this isn't a PlayStation 1 disc."""
    try:
        r = DiscReader(path, stride, stride_off)
        pvd = r.sector(16)
        if pvd[1:6] != b"CD001":
            return ""
        rec = pvd[156:190]
        extent = struct.unpack("<I", rec[2:6])[0]
        size = struct.unpack("<I", rec[10:14])[0]
        data = b"".join(r.sector(extent + i) for i in range(min(8, (size + 2047) // 2048)))
        pos = 0
        while pos < len(data):
            ln = data[pos]
            if ln == 0:
                pos = (pos // 2048 + 1) * 2048
                continue
            name = data[pos + 33:pos + 33 + data[pos + 32]].decode("latin1").upper()
            if name.startswith("SYSTEM.CNF"):
                ext = struct.unpack("<I", data[pos + 2:pos + 6])[0]
                txt = r.sector(ext).decode("latin1", "replace")
                pat = r"BOOT2\s*=\s*cdrom0:\\?(?:.*[\\/])?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})" if ps2 else \
                    r"BOOT\s*=\s*cdrom:\\?(?:.*[\\/])?([A-Z]{4})[_-]?(\d{3})\.?(\d{2})"
                m = re.search(pat, txt, re.I)
                return (m.group(1) + m.group(2) + m.group(3)).upper() if m else ""
            pos += ln
    except (OSError, struct.error, IndexError):
        pass
    return ""


def _disc_head(lay, game_id, title, multi):
    """Everything in a disc section before its block index (0x4000 bytes)."""
    head = bytearray(0x4000)
    head[0:12] = b"PSISOIMG0000"
    code = "_" + game_id[:4] + "_" + game_id[4:9]
    head[0x400:0x400 + len(code)] = code.encode("ascii", "replace")
    head[0x400 + 2046] = 0x10
    toc = pbp_toc_bytes(lay["tracks"], lay["total"])
    head[0x800:0x800 + len(toc)] = toc
    d2 = 0x1224
    head[d2 + 4:d2 + 6] = b"\xff\x07"
    t = title.encode("ascii", "replace")[:120]
    head[d2 + 8:d2 + 8 + len(t)] = t
    head[d2 + 136] = 3
    return head


ART_FILES = (("icon0", "ICON0.PNG", (144, 80)), ("icon1", "ICON1.PMF", None), ("pic0", "PIC0.PNG", (310, 180)),
             ("pic1", "PIC1.PNG", (480, 272)), ("snd0", "SND0.AT3", None))


def read_art_folder(folder):
    """Your own PBP artwork: ICON0.PNG, ICON1.PMF, PIC0.PNG, PIC1.PNG and SND0.AT3 in a folder (any may be missing).
    -> (dict of what was found, [warnings]); the dict is empty when the folder has none of them."""
    art, warn = {}, []
    folder = Path(folder)
    if not folder.is_dir():
        return art, warn
    have = {p.name.upper(): p for p in folder.iterdir() if p.is_file()}
    for key, name, size in ART_FILES:
        p = have.get(name)
        if not p:
            continue
        data = p.read_bytes()
        if size:
            if data[:8] != b"\x89PNG\r\n\x1a\n" or len(data) < 24:
                warn.append(f"{name} isn't a PNG and was ignored")
                continue
            w, h = struct.unpack(">II", data[16:24])
            if (w, h) != size:
                warn.append(f"{name} is {w}x{h}; a PSP expects {size[0]}x{size[1]}")
        art[key] = data
    return art, warn


def extract_pbp_art(pbp, outdir):
    """Write the icon, pictures and sound stored in a PBP into outdir. -> [file names written]"""
    with open(pbp, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("this isn't a PBP file")
        o = struct.unpack("<10I", head)
        Path(outdir).mkdir(parents=True, exist_ok=True)
        made = []
        for i, (_, name, _) in enumerate(ART_FILES):
            f.seek(o[3 + i])
            data = f.read(o[4 + i] - o[3 + i])
            if data:
                (Path(outdir) / name).write_bytes(data)
                made.append(name)
    return made


# A small 5x7 bitmap font (ASCII 32..126), one byte per column, bit 0 = top row. Drawn here, so the title cards need
# no font file and nothing copyrighted.
_FONT5X7 = bytes.fromhex(
    "0000000000" "00005f0000" "0007000700" "147f147f14" "242a7f2a12" "2313086462" "3649552250" "0005030000"
    "001c224100" "0041221c00" "14083e0814" "08083e0808" "0050300000" "0808080808" "0060600000" "2010080402"
    "3e5149453e" "00427f4000" "4261514946" "2141454b31" "1814127f10" "2745454539" "3c4a494930" "0171090503"
    "3649494936" "064949291e" "0036360000" "0056360000" "0814224100" "1414141414" "0041221408" "0201510906"
    "324979413e" "7e1111117e" "7f49494936" "3e41414122" "7f4141221c" "7f49494941" "7f09090901" "3e4149497a"
    "7f0808087f" "00417f4100" "2040413f01" "7f08142241" "7f40404040" "7f020c027f" "7f0408107f" "3e4141413e"
    "7f09090906" "3e4151215e" "7f09192946" "4649494931" "01017f0101" "3f4040403f" "1f2040201f" "3f4038403f"
    "6314081463" "0708700807" "6151494543" "007f414100" "0204081020" "00417f4100" "0402010204" "4040404040"
    "0001020400" "2054545478" "7f48444438" "3844444420" "384444487f" "3854545418" "087e090102" "0c5252523e"
    "7f08040478" "00447d4000" "2040443d00" "7f10284400" "00417f4000" "7c04180478" "7c08040478"
    "3844444438" "7c14141408" "081414187c" "7c08040408" "4854545420" "043f444020" "3c4040207c" "1c2040201c"
    "3c4030403c" "4428102844" "0c5050503c" "4464544c44" "0008364100" "00007f0000" "0041360800" "1008081008")

_CARD_COLORS = ((200, 60, 60), (200, 120, 40), (180, 150, 30), (70, 160, 70), (40, 150, 150), (60, 110, 200),
                (120, 80, 200), (180, 70, 160))


def png_rgb(width, height, rows):
    """A minimal PNG writer: rows is a list of `height` byte strings of `width` RGB pixels."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _wrap_text(text, per_line):
    lines, cur = [], ""
    for word in text.split():
        while len(word) > per_line:                # a word longer than a line is broken
            if cur:
                lines.append(cur)
                cur = ""
            lines.append(word[:per_line])
            word = word[per_line:]
        if not cur:
            cur = word
        elif len(cur) + 1 + len(word) <= per_line:
            cur += " " + word
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def title_card(width, height, title, code, scales):
    """A plain 'title card' PNG: a gradient in a colour taken from the game code, the title in white and the code below.
    scales = the text sizes to try for the title, biggest first (the first one that fits is used)."""
    seed = zlib.crc32((code or title).encode("ascii", "replace"))
    r, g, b = _CARD_COLORS[seed % len(_CARD_COLORS)]
    top, bot = (r // 3, g // 3, b // 3), (r // 8, g // 8, b // 8)
    rows = []
    for y in range(height):
        t = y / max(1, height - 1)
        px = bytes(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        rows.append(bytearray(px * width))
    margin = max(4, width // 36)

    def blit(x0, y0, ch, scale, color):
        code_ = ord(ch) - 32 if 32 <= ord(ch) <= 126 else ord("?") - 32
        cols = _FONT5X7[code_ * 5:code_ * 5 + 5]
        for cx, col in enumerate(cols):
            for ry in range(7):
                if col >> ry & 1:
                    for dy in range(scale):
                        y = y0 + ry * scale + dy
                        if 0 <= y < height:
                            for dx in range(scale):
                                x = x0 + cx * scale + dx
                                if 0 <= x < width:
                                    rows[y][x * 3:x * 3 + 3] = bytes(color)

    def draw(lines, scale, top_y, color):
        step = 8 * scale + scale
        for i, line in enumerate(lines):
            w = len(line) * 6 * scale - scale
            x = (width - w) // 2
            for k, ch in enumerate(line):
                blit(x + k * 6 * scale, top_y + i * step, ch, scale, color)

    text = re.sub(r"\s+", " ", title.encode("ascii", "replace").decode("ascii")).strip().upper() or "?"
    code = (code or "").strip().upper()
    code_scale = max(1, scales[-1])
    code_h = (8 * code_scale) + margin if code else 0
    avail_h, avail_w = height - 2 * margin - code_h, width - 2 * margin
    chosen = None
    for sc in scales:
        lines = _wrap_text(text, max(1, (avail_w + sc) // (6 * sc)))
        if len(lines) * (9 * sc) - sc <= avail_h and all(len(l) * 6 * sc - sc <= avail_w for l in lines):
            chosen = (sc, lines)
            break
    if not chosen:                                   # too long even at the smallest size: cut it
        sc = scales[-1]
        per = max(1, (avail_w + sc) // (6 * sc))
        fit = max(1, (avail_h + sc) // (9 * sc))
        lines = _wrap_text(text, per)[:fit]
        lines[-1] = lines[-1][:max(0, per - 3)].rstrip() + "..."
        chosen = (sc, lines)
    sc, lines = chosen
    block = len(lines) * 9 * sc - sc
    y0 = margin + max(0, (avail_h - block) // 2)
    draw(lines, sc, y0, (255, 255, 255))
    if code:
        bar_y = height - margin - code_h + margin // 2
        for y in range(bar_y - 2, bar_y - 2 + max(1, code_scale)):          # a coloured line above the code
            if 0 <= y < height:
                rows[y][margin * 3:(width - margin) * 3] = bytes((r, g, b)) * (width - 2 * margin)
        draw([code], code_scale, bar_y + 3, (r + (255 - r) // 2, g + (255 - g) // 2, b + (255 - b) // 2))
    return png_rgb(width, height, rows)


def make_title_art(title, game_id, have=()):
    """ICON0 (144x80) and PIC1 (480x272) title cards for a PBP, for the pictures not in `have`.
    game_id like SLUS00594 is shown as SLUS-00594."""
    code = f"{game_id[:4]}-{game_id[4:]}" if re.fullmatch(r"[A-Z]{4}\d{5}", game_id or "") else (game_id or "")
    art = {}
    if "icon0" not in have:
        art["icon0"] = title_card(144, 80, title, code, (2, 1))
    if "pic1" not in have:
        art["pic1"] = title_card(480, 272, title, code, (6, 5, 4, 3, 2))
    return art


def read_base_pbp(path):
    """The parts of a working PS1 .pbp that a PSP needs and that can't be made from a disc: the loader (DATA.PSP),
    the icon and pictures, and the boot-logo block (STARTDAT) that follows the disc data."""
    with open(path, "rb") as f:
        head = f.read(0x28)
        if len(head) < 0x28 or head[:4] != b"\x00PBP":
            raise UnpackError("the base file isn't a PBP")
        o = struct.unpack("<10I", head)
        f.seek(0, 2)
        size = f.tell()
        if not all(o[i] <= o[i + 1] <= size for i in range(2, 9)) or o[9] + 16 > size:
            raise UnpackError("the base PBP's header is damaged")

        def part(i):
            f.seek(o[i])
            return f.read(o[i + 1] - o[i])
        psar = o[9]
        f.seek(psar)
        if not f.read(16).startswith((b"PSISOIMG0000", b"PSTITLEIMG000000")):
            raise UnpackError("the base PBP isn't a PlayStation 1 game")
        f.seek(psar + 0xC)
        start = psar + struct.unpack("<I", f.read(4))[0]
        f.seek(start)
        if f.read(8) != b"STARTDAT":
            raise UnpackError("the base PBP has no boot-logo block (STARTDAT)")
        f.seek(start)
        startdat = f.read()
        loader = part(8)
        if loader[:4] == b"~PSP":
            # Sony's encrypted loader only runs signed content: a PSP shuts down on an unsigned PBP. A plain (ELF)
            # DATA.PSP next to the base file takes its place, as it does for the popstation family of tools.
            side = Path(path).with_name("DATA.PSP")
            plain = side.read_bytes() if side.is_file() else b""
            if plain[:4] != b"\x7fELF":
                raise UnpackError("this base PBP carries an encrypted loader that a PSP won't run unsigned; "
                                  "use a PBP with a plain loader, or put a plain DATA.PSP next to it")
            loader = plain
        return dict(icon0=part(3), icon1=part(4), pic0=part(5), pic1=part(6), snd0=part(7), loader=loader, startdat=startdat)


# Two fixed byte runs in the header of a multi-disc PBP (offsets 0x214 and 0x28C). They are identical in every real
# multi-disc PBP examined (two different games) and a PSP shuts down if they are missing.
PBP_MULTI_BLOCK_A = bytes.fromhex("2900000023480000be18000084670000e14a00006c3d0000d62c0000ae72000052690000905f000049160000f16d0000")
PBP_MULTI_BLOCK_B = bytes.fromhex(
    "8c0eae9d398cfe246521ad2d65a961dfd44a1303b4cd325fbdb0f4f98a70be1e392c7dc0c1c66b81aa3c0653941bcee54416cfdbb1e3897ba2d2e7d6c1266b588d2ce6c31597d029c91681b6cc42ee0c2810eaf26b6f903005be4a2f4abcdce587ce19b980deb832ddad8967e0927889e8dc451d0cbe8b994d50b3d658966175")


def write_pbp(out_path, layouts, game_ids, title, progress, stopped, donor=None, level=6, art=None):
    """Write layouts (one per disc, from cue_layout) as a PBP. One disc -> PSISOIMG0000, several -> PSTITLEIMG000000.
    With donor (from read_base_pbp) the PBP also carries the loader, icon and boot logo a real PSP needs."""
    n = len(layouts)
    sfo = build_sfo([("BOOTABLE", 1, 4), ("CATEGORY", "ME", 4), ("DISC_ID", game_ids[0], 16), ("DISC_VERSION", "1.00", 8),
                     ("LICENSE", "Copyright(C) Sony Computer Entertainment America Inc.", 512), ("PARENTAL_LEVEL", 3, 4),
                     ("PSP_SYSTEM_VER", "3.01", 8), ("REGION", 0x8000, 4), ("TITLE", title, 128)])
    res = [donor[k] for k in ("icon0", "icon1", "pic0", "pic1", "snd0", "loader")] if donor else [b""] * 6
    for i, k in enumerate(("icon0", "icon1", "pic0", "pic1", "snd0")):      # your own artwork replaces the base PBP's
        if art and art.get(k):
            res[i] = art[k]
    offs, pos = [0x28], 0x28 + len(sfo)
    for r in res:
        offs.append(pos)
        pos += len(r)
    psar = max(0x10000, -(-pos // 0x10000) * 0x10000)
    header = struct.pack("<4sI8I", b"\0PBP", 0x10000, *offs, psar)
    done, all_blocks = 0, sum(-(-l["total"] // 16) for l in layouts) or 1
    with open(out_path, "wb") as g:
        g.write(header + sfo + b"".join(res))
        g.write(bytes(psar - g.tell()))
        starts = []
        if n > 1:
            mh = bytearray(0x3FC)
            mh[0:16] = b"PSTITLEIMG000000"
            struct.pack_into("<4I", mh, 0x18, 0x2CC9C5BC, 0x33B5A90F, 0x06F6B4B3, 0xB25945BA)
            mh[0x214:0x214 + len(PBP_MULTI_BLOCK_A)] = PBP_MULTI_BLOCK_A
            code = ("_" + game_ids[0][:4] + "_" + game_ids[0][4:9]).encode("ascii", "replace")
            mh[0x264:0x264 + len(code)] = code
            mh[0x28C:0x28C + len(PBP_MULTI_BLOCK_B)] = PBP_MULTI_BLOCK_B
            t = title.encode("ascii", "replace")[:120]
            mh[0x30C:0x30C + len(t)] = t
            struct.pack_into("<I", mh, 0x38C, 7)
            g.write(mh)
        for d, lay in enumerate(layouts):
            if n > 1:
                g.write(bytes((-g.tell()) % 0x8000))
            base = g.tell()
            starts.append(base - psar)
            blocks = -(-lay["total"] // 16)
            head = _disc_head(lay, game_ids[d], f"{title} [Disc{d + 1}of{n}]" if n > 1 else title, n > 1)
            struct.pack_into("<I", head, 0xC, blocks * PBP_BLOCK + 0x100000)
            g.write(head)
            index = bytearray(blocks * 32)
            g.write(index)
            g.write(bytes(base + 0x100000 - g.tell()))
            off = 0
            for b, block in enumerate(iter_disc_blocks(lay)):
                z = block
                if level > 0:
                    comp = zlib.compressobj(level, zlib.DEFLATED, -15)
                    z = comp.compress(block) + comp.flush()
                    if len(z) >= PBP_BLOCK:
                        z = block
                struct.pack_into("<IH", index, b * 32, off, len(z))
                g.write(z)
                off += len(z)
                done += 1
                if b % 32 == 0:
                    progress(done / all_blocks)
                    if stopped():
                        raise UnpackError("stopped")
            g.write(bytes((-g.tell()) % 0x10))
            end = g.tell()
            g.seek(base + 0x4000)
            g.write(index)
            if n == 1:
                g.seek(base + 0xC)
                g.write(struct.pack("<I", end - psar))
                g.seek(base + 0x1220)
                g.write(struct.pack("<I", end - psar + 0x2D31))
            g.seek(end)
        if donor:
            g.write(donor["startdat"])                   # the boot-logo block sits right after the disc data
        if n > 1:
            end = g.tell() - (len(donor["startdat"]) if donor else 0)
            g.seek(psar + 0x10)
            g.write(struct.pack("<I", end - psar))
            g.seek(psar + 0x284)
            g.write(struct.pack("<I", end - psar + 0x2D31))
            g.seek(psar + 0x200)
            g.write(struct.pack("<5I", *(starts + [0] * (5 - n))))
    progress(1.0)


def verify_pbp(path, layouts, progress, stopped):
    """Read the finished PBP back with the same code that converts PBPs to CHDs and compare every block with the
    disc it was made from."""
    discs = pbp_discs(path)
    if len(discs) != len(layouts):
        return False
    done, all_blocks = 0, sum(-(-l["total"] // 16) for l in layouts) or 1
    with open(path, "rb") as f:
        for base, lay in zip(discs, layouts):
            tracks, total = pbp_toc(f, base)
            if total != lay["total"] or [(d, l) for d, l in tracks] != lay["tracks"]:
                return False
            blocks = -(-total // 16)
            f.seek(base + 0x4000)
            index = f.read(blocks * 32)
            for b, block in enumerate(iter_disc_blocks(lay)):
                off, ln = struct.unpack("<IH", index[b * 32:b * 32 + 6])
                f.seek(base + 0x100000 + off)
                raw = f.read(ln)
                try:
                    chunk = raw if ln == PBP_BLOCK else zlib.decompress(raw, -15)
                except zlib.error:
                    return False
                if chunk != block:
                    return False
                done += 1
                if b % 64 == 0:
                    progress(done / all_blocks)
                    if stopped():
                        raise UnpackError("stopped")
    return True


# A PBP only runs on a real PSP/Vita if it carries Sony's PS1 loader, which can't be made from a disc and is not
# shipped with CHD Manager. The user can point the app at any working PS1 PBP, or let it fetch this generic one
# (pinned to one commit of the public repository and checked against a fixed checksum).
BASE_PBP_FILES = [   # (name, url, sha-256)
    ("BASE.PBP", "https://raw.githubusercontent.com/julianxhokaxhiu/iPoPS/f8afadd32e9e55e0ea9bf73cf3306890ffebe629/BASE.PBP",
     "de0d743be8e721b4167679de5655c0df99667844e0da31bcd96397b09b192614"),
    ("DATA.PSP", "https://raw.githubusercontent.com/julianxhokaxhiu/iPoPS/f8afadd32e9e55e0ea9bf73cf3306890ffebe629/DATA.PSP",
     "9a995e281433c6671e8fc9efc16a130f865e5db03e2d5e6cfbba29ab34ec2fb8"),
]


def find_base_pbp(saved=""):
    """A usable base PBP: the one chosen in Settings, else BASE.PBP next to the program. None if there isn't one."""
    for c in (saved, str(HERE / "BASE.PBP")):
        if c and Path(c).is_file():
            try:
                read_base_pbp(c)
                return Path(c)
            except (UnpackError, OSError):
                continue
    return None


PATH_SETTINGS = ("chdman", "base_pbp", "art_dir", "out_dir", "dolphintool", "rpcs3", "last_dir")


def relocate_settings(settings, here=None):
    """The program folder was moved: saved paths that pointed inside the old folder now point inside the new one, and a
    saved file that is gone but sits next to the program under the same name (BASE.PBP, chdman.exe...) is found again.
    Changes `settings` in place; returns the names of the settings that changed."""
    here = Path(here or HERE)
    norm = lambda p: os.path.normcase(os.path.normpath(str(p)))  # noqa: E731
    old = settings.get("app_dir", "")
    changed = []
    for k in PATH_SETTINGS:
        v = settings.get(k) or ""
        if not v:
            continue
        new = None
        if old and norm(old) != norm(here):
            try:
                new = here / Path(v).relative_to(old)
            except ValueError:
                pass
        if new is None and k in ("chdman", "base_pbp", "dolphintool") and not Path(v).exists():
            new = here / Path(v).name
        if new is not None and norm(new) != norm(v) and new.exists():
            settings[k] = str(new)
            changed.append(k)
    return changed


def download_base_pbp(dest_dir):
    """Fetch BASE.PBP and DATA.PSP into dest_dir, each checked against its fixed checksum, and keep them only if
    together they make a usable base."""
    dest_dir = Path(dest_dir)
    tmp = Path(tempfile.mkdtemp(prefix="chdm_base_"))
    try:
        for name, url, want in BASE_PBP_FILES:
            h, got = hashlib.sha256(), 0
            with http_get(url, 60) as r, open(tmp / name, "wb") as f:
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if got > 20 << 20:
                        raise RuntimeError("the download is far larger than expected")
            if h.hexdigest() != want:
                raise RuntimeError(f"the checksum of {name} doesn't match, so the download was discarded")
        read_base_pbp(tmp / "BASE.PBP")
        for name, _url, _want in BASE_PBP_FILES:
            os.replace(tmp / name, dest_dir / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------- CHD -> CDI (DiscJuggler, version 2 layout)
CUE_SS = {"AUDIO": 2352, "MODE1/2352": 2352, "MODE2/2352": 2352, "MODE1/2048": 2048, "MODE2/2336": 2336}
CDI_GAP = 11250         # sectors between the two sessions of a Dreamcast disc


def cue_tracks(cue):
    """-> (bin path, [dict(type, ss, start_byte, sectors)]) for the single-BIN .cue chdman writes."""
    cue = Path(cue)
    binf, tracks = None, []
    for line in cue.read_text("utf-8", "replace").splitlines():
        t = line.strip()
        m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
        if m:
            if binf is not None:
                raise UnpackError("this disc is stored in several files")
            binf = cue.parent / (m.group(1) or m.group(2))
            continue
        m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
        if m:
            kind = m.group(1).upper()
            if kind not in CUE_SS:
                raise UnpackError(f"track type {kind} isn't supported")
            tracks.append(dict(type=kind, ss=CUE_SS[kind], idx=None))
            continue
        m = re.match(r"INDEX\s+01\s+(\d+):(\d+):(\d+)", t, re.I)
        if m and tracks:
            tracks[-1]["idx"] = (int(m.group(1)) * 60 + int(m.group(2))) * 75 + int(m.group(3))
    if binf is None or not tracks or any(t["idx"] is None for t in tracks):
        raise UnpackError("can't read the track list of this disc")
    size, off = binf.stat().st_size, 0
    for i, t in enumerate(tracks):
        t["start_byte"] = off
        t["sectors"] = (tracks[i + 1]["idx"] - t["idx"]) if i + 1 < len(tracks) else (size - off) // t["ss"]
        off += t["sectors"] * t["ss"]
    if off > size or any(t["sectors"] <= 0 for t in tracks):
        raise UnpackError("the track list doesn't match the data file")
    return binf, tracks


def cdi_plan(binf, tracks):
    """Decide the CDI layout: sessions, and for every track its stored sector size, mode and start sector."""
    first_data = next((i for i, t in enumerate(tracks) if t["type"] != "AUDIO"), None)
    dc = (first_data is not None and first_data > 0 and tracks[first_data]["type"] == "MODE2/2352"
          and sniff_platform(binf, tracks[first_data]["start_byte"]) == "Dreamcast")
    gap = CDI_GAP
    if dc:
        # A Dreamcast data session was mastered to start at one particular sector: the ISO's own directory
        # addresses say which, so read it from there rather than assuming the usual gap.
        t = tracks[first_data]
        r = DiscReader(binf, 2352, 24, t["start_byte"])
        ext = struct.unpack("<I", r.sector(16)[158:162])[0]
        for rel in range(17, 200):
            sec = r.sector(rel)
            if len(sec) > 33 and sec[0] >= 0x22 and sec[25] & 2 and sec[32] == 1 and sec[33] == 0 \
                    and struct.unpack("<I", sec[2:6])[0] == ext:
                before = sum(150 + q["sectors"] for q in tracks[:first_data])
                if 0 <= ext - rel - before < 100000:
                    gap = ext - rel - before
                break
    plan, lba = [], 0
    for i, t in enumerate(tracks):
        session = 1 if dc and i >= first_data else 0
        if dc and i == first_data:
            lba += gap
        kind = t["type"]
        mode = 0 if kind == "AUDIO" else 1 if kind.startswith("MODE1") else 2
        if kind == "MODE2/2352" and dc:
            ss, code = 2336, 1                      # DiscJuggler keeps Dreamcast data sectors without sync/header
        else:
            ss, code = t["ss"], {2048: 0, 2336: 1, 2352: 2}[t["ss"]]
        plan.append(dict(t, session=session, mode=mode, out_ss=ss, code=code, start_lba=lba, total=150 + t["sectors"]))
        lba += 150 + t["sectors"]
    return plan, dc, lba


def _cdi_volume_label(binf, tracks):
    for t in tracks:
        if t["type"] != "AUDIO":
            off = 16 if t["type"] == "MODE1/2352" else 24
            ss = t["ss"]
            r = DiscReader(binf, ss, off if ss == 2352 else 0, t["start_byte"])
            pvd = r.sector(16)
            if pvd[1:6] == b"CD001":
                return pvd[40:72].decode("latin1", "replace").strip()[:30] or "DISC"
    return "DISC"


def write_cdi(out_path, cue, progress, stopped):
    """CD CHD (extracted to cue/bin) -> DiscJuggler .cdi."""
    binf, tracks = cue_tracks(cue)
    plan, dc, end_lba = cdi_plan(binf, tracks)
    name = Path(out_path).name.replace(".part", "").encode("ascii", "replace")[:60]
    capacity = 333000 if end_lba <= 333000 else 360000 if end_lba <= 360000 else 405000 if end_lba <= 405000 else end_lba
    total_sectors = sum(p["sectors"] for p in plan) or 1
    done = 0
    rec = []
    with open(binf, "rb") as f, open(out_path, "wb") as g:
        for n, p in enumerate(plan):
            if p["mode"] == 2 and p["out_ss"] == 2336:
                g.write((b"\x00\x00\x20\x00\x00\x00\x20\x00" + bytes(2328)) * 150)      # an empty Form 2 sector, as on real discs
            else:
                g.write(bytes(150 * p["out_ss"]))
            f.seek(p["start_byte"])
            left = p["sectors"]
            while left:
                c = min(left, 2048)
                raw = f.read(c * p["ss"])
                if len(raw) != c * p["ss"]:
                    raise UnpackError("the extracted disc is shorter than its track list says")
                if p["out_ss"] != p["ss"]:
                    raw = b"".join(raw[i * p["ss"] + 16:(i + 1) * p["ss"]] for i in range(c))
                g.write(raw)
                left -= c
                done += c
                progress(done / total_sectors)
                if stopped():
                    raise UnpackError("stopped")
            idx_in_session = sum(1 for q in plan[:n] if q["session"] == p["session"])
            ctrl = 0 if p["mode"] == 0 else 4
            rec.append(
                struct.pack("<I", 0) + CDI_MARK * 2 + bytes(4) + bytes((len(name),)) + name
                + bytes.fromhex("00000000000000000000000200000000000000") + struct.pack("<I", capacity) + b"\x02\x00"
                + struct.pack("<II", 150, p["sectors"]) + bytes(6) + struct.pack("<I", p["mode"])
                + struct.pack("<III", 0, p["session"], idx_in_session) + struct.pack("<II", p["start_lba"], p["total"])
                + bytes(16) + struct.pack("<I", p["code"]) + bytes((ctrl, 0, 0, 0, 0)) + struct.pack("<I", p["total"]) + bytes(20))
        sessions = [[i for i, p in enumerate(plan) if p["session"] == s] for s in range(2 if dc else 1)]
        head = struct.pack("<H", len(sessions))
        for si, members in enumerate(sessions):
            head += struct.pack("<H", len(members)) + b"".join(rec[i] for i in members)
            if si == len(sessions) - 1:
                head += struct.pack("<III", 2, 0, plan[members[-1]]["start_lba"])
            else:
                head += bytes(12)
        label = _cdi_volume_label(binf, tracks).encode("ascii", "replace")
        head += b"\x00\x00" + struct.pack("<I", end_lba) + bytes((len(label),)) + label + b"\x01\x00\x00\x00\x01\x00\x00\x00" + bytes(29)
        pos = g.tell()
        g.write(head)
        g.write(struct.pack("<II", CDI_V2, pos))
    progress(1.0)


def verify_cdi(out_path, cue, progress, stopped):
    """Read the finished CDI back and compare every track's data with the disc it was made from."""
    binf, tracks = cue_tracks(cue)
    plan, _dc, _end = cdi_plan(binf, tracks)
    back = parse_cdi(out_path)
    if len(back) != len(plan):
        return False
    done, total = 0, sum(p["sectors"] for p in plan) or 1
    with open(out_path, "rb") as f, open(binf, "rb") as b:
        for p, t in zip(plan, back):
            if (t["mode"], t["ss"], t["length"], t["pregap"], t["session"] - 1, t["start_lba"]) != (
                    p["mode"], p["out_ss"], p["sectors"], 150, p["session"], p["start_lba"]):
                return False
            f.seek(t["position"] + 150 * p["out_ss"])
            b.seek(p["start_byte"])
            left = p["sectors"]
            while left:
                c = min(left, 2048)
                a, raw = f.read(c * p["out_ss"]), b.read(c * p["ss"])
                if p["out_ss"] != p["ss"]:
                    raw = b"".join(raw[i * p["ss"] + 16:(i + 1) * p["ss"]] for i in range(c))
                if a != raw:
                    return False
                left -= c
                done += c
                progress(done / total)
                if stopped():
                    raise UnpackError("stopped")
    return True


# ------------------------------------------------------------------ CSO (compressed ISO, used for PSP games)
def ciso_is_wii(field):
    """Both CISO kinds start with 'CISO' and a 32-bit number. A PSP CSO has its header size there (24, but many tools write 0
    or leave junk); a GameCube/Wii CISO has its block size, a power of two of 32 KiB or more (Dolphin writes 2 MiB)."""
    return field >= 0x8000 and field & (field - 1) == 0


def read_cso_header(path):
    """-> (size of the ISO in bytes, block size, offset shift). Works for CSO (version 1 and maxcso's version 2) and ZSO (LZ4)."""
    with open(path, "rb") as f:
        h = f.read(24)
    if len(h) < 24 or h[:4] not in (b"CISO", b"ZISO"):
        raise UnpackError("this is not a CSO or ZSO file")
    hsize, total, block, ver, shift = struct.unpack("<IQIBB2x", h[4:])
    if h[:4] == b"CISO" and ciso_is_wii(hsize):
        raise UnpackError("this is a GameCube/Wii CISO, not a PSP CSO")
    if ver > (2 if h[:4] == b"CISO" else 1) or block < 512 or block & (block - 1) or shift > 8 or not total:
        raise UnpackError("this CSO uses a version or layout CHD Manager can't read")
    return total, block, shift


def cso_version(path):
    """1 or 2: the layout version of a CSO (a ZSO is always 1)."""
    with open(path, "rb") as f:
        h = f.read(24)
    return 2 if len(h) == 24 and h[:4] == b"CISO" and h[20] == 2 else 1


def cso_is_zso(path):
    with open(path, "rb") as f:
        return f.read(4) == b"ZISO"


def iter_cso(path):
    """Yield (block count, the ISO bytes of each block) of a CSO or ZSO, in order."""
    total, block, shift = read_cso_header(path)
    lz = cso_is_zso(path)
    n = (total + block - 1) // block
    with open(path, "rb") as f:
        v2 = f.read(24)[20] == 2 and not lz              # version 2: a block of block_size or more is stored raw; the index's top bit means LZ4
        f.seek(24)
        idx = struct.unpack("<%dI" % (n + 1), f.read(4 * (n + 1)))
        for i in range(n):
            a, b = idx[i], idx[i + 1]
            start, end = (a & 0x7FFFFFFF) << shift, (b & 0x7FFFFFFF) << shift
            if f.tell() != start:
                f.seek(start)
            raw = f.read(end - start)
            need = min(block, total - i * block)
            try:
                if v2:
                    data = raw[:block] if end - start >= block else lz4_decompress(raw, block) if a & 0x80000000 else zlib.decompressobj(-15).decompress(raw)
                elif a & 0x80000000:
                    data = raw[:block]
                elif lz:
                    data = lz4_decompress(raw, block)
                else:
                    data = zlib.decompressobj(-15).decompress(raw)
            except zlib.error:
                data = b""
            if len(data) < need:
                raise UnpackError(f"the {'ZSO' if lz else 'CSO'} is damaged (block {i})")
            yield n, data[:need]


def cso_to_iso(src, dst, progress, stopped):
    done = 0
    with open(dst, "wb") as g:
        for n, data in iter_cso(src):
            g.write(data)
            done += 1
            if done % 2048 == 0:
                progress(done / n)
                if stopped():
                    raise UnpackError("stopped")
    progress(1.0)


def iso_to_cso(src, dst, progress, stopped, level=9, lz4=False, v2=False):
    """ISO -> CSO version 1 (blocks raw-deflated) or, with lz4, ZSO (blocks LZ4-compressed): 2048-byte blocks,
    stored as they are when compressing doesn't shrink them. With v2, maxcso's experimental version 2 layout
    (deflate blocks; a block that would take 2048 bytes or more is stored raw and the index bit stays clear)."""
    total, block = os.path.getsize(src), 2048
    n = (total + block - 1) // block
    shift = 0
    # offsets must fit in 31 bits; an LZ4 block can't be padded safely, so a ZSO is aligned only when it has to be
    while 24 + 4 * (n + 1) + total + n * ((1 << shift) - 1) >= 1 << (31 + shift):
        shift += 1
    align = 1 << shift
    index = [0] * (n + 1)
    pos = 24 + 4 * (n + 1)
    with open(src, "rb") as f, open(dst, "wb") as g:
        g.write(struct.pack("<4sIQIBB2x", b"ZISO" if lz4 else b"CISO", 24, total, block, 2 if v2 else 1, shift) + b"\0" * (4 * (n + 1)))
        for i in range(n + 1):
            pad = -pos % align
            if pad:
                g.write(b"\0" * pad)
                pos += pad
            if i == n:
                index[i] = pos >> shift
                break
            data = f.read(block).ljust(block, b"\0")
            if lz4:
                comp = lz4_compress(data)
            else:
                c = zlib.compressobj(level, zlib.DEFLATED, -15)
                comp = c.compress(data) + c.flush()
            if len(comp) >= block or (v2 and len(comp) + (-(pos + len(comp)) % align) >= block):
                index[i] = (pos >> shift) | (0 if v2 else 0x80000000)
                g.write(data)
                pos += block
            else:
                index[i] = pos >> shift
                g.write(comp)
                pos += len(comp)
            if i % 2048 == 0:
                progress(i / n)
                if stopped():
                    raise UnpackError("stopped")
        g.seek(24)
        g.write(struct.pack("<%dI" % (n + 1), *index))
    progress(1.0)


def verify_cso(cso, iso, progress, stopped):
    """True when the CSO unpacks to exactly this ISO."""
    done = 0
    with open(iso, "rb") as f:
        for n, data in iter_cso(cso):
            if f.read(len(data)) != data:
                return False
            done += 1
            if done % 2048 == 0:
                progress(done / n)
                if stopped():
                    raise UnpackError("stopped")
        return f.read(1) == b""


def cso_platform(path):
    """Platform of the disc inside a CSO, found by unpacking its first 2 MB."""
    try:
        tmp = Path(tempfile.mkdtemp(prefix="chdm_sniff_"))
        try:
            head = tmp / "head.iso"
            with open(head, "wb") as g:
                for i, (_, data) in enumerate(iter_cso(path)):
                    g.write(data)
                    if i >= 1000:
                        break
            return sniff_platform(head)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except (UnpackError, OSError):
        return ""


# ------------------------------------------------------ game IDs and titles (for naming output files)
GAMEDB_FILE = ("gameInfo.db", "https://raw.githubusercontent.com/RupertAvery/PSXPackager/453c0e7b11fa46d78fa1939473923699894fde03/"
               "Popstation.Database/Resources/gameInfo.db", "fdea8792f8f173e434fd094038e81c14b77db71c00d1c64a11f6ce5b49c4cce9")
_GAMEDB = {}


def find_gamedb():
    p = HERE / GAMEDB_FILE[0]
    return p if p.is_file() else None


def load_gamedb(path):
    """gameInfo.db (from the PSXPackager project): game ID -> title, title of the whole game, region."""
    key = str(path)
    if key not in _GAMEDB:
        db = {}
        try:
            for line in Path(path).read_text("utf-8", "replace").splitlines():
                p = line.split(";")
                if len(p) >= 6:
                    db[p[5].strip().upper()] = dict(main_id=p[1], main_title=p[2], title=p[3], region=p[4])
        except OSError:
            pass
        _GAMEDB[key] = db
    return _GAMEDB[key]


def download_gamedb(dest_dir):
    name, url, want = GAMEDB_FILE
    tmp = Path(tempfile.mkdtemp(prefix="chdm_db_"))
    try:
        h, got = hashlib.sha256(), 0
        with http_get(url, 60) as r, open(tmp / name, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if got > 10 << 20:
                    raise RuntimeError("the download is far larger than expected")
        if h.hexdigest() != want:
            raise RuntimeError("the checksum doesn't match, so the download was discarded")
        os.replace(tmp / name, Path(dest_dir) / name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def sfo_get(sfo, key):
    """One text value of a PARAM.SFO ('' if missing)."""
    if sfo[:4] != b"\0PSF":
        return ""
    kofs, dofs, n = struct.unpack("<III", sfo[8:20])
    for i in range(n):
        ko, fmt, ln, _mx, do = struct.unpack("<HHIII", sfo[20 + 16 * i:36 + 16 * i])
        if sfo[kofs + ko:sfo.index(b"\0", kofs + ko)].decode("latin1") == key:
            return sfo[dofs + do:dofs + do + ln].split(b"\0")[0].decode("utf-8", "replace")
    return ""


def pbp_game_id(path):
    """DISC_ID of a PBP ('SLUS01545'), '' if it has none."""
    try:
        with open(path, "rb") as f:
            o = struct.unpack("<10I", f.read(0x28))
            f.seek(o[2])
            return sfo_get(f.read(o[3] - o[2]), "DISC_ID").upper()
    except (OSError, struct.error, ValueError):
        return ""


def target_label(key):
    """Label of a target key, including the per-disc ones ('chd_d2' = CHD of disc 2 only)."""
    base, _, d = key.partition("_d")
    return TARGET_LABELS.get(base, "") + (f", disc {d} only" if d else "") if key not in TARGET_LABELS else TARGET_LABELS[key]


# What a row's Action drop-down can offer. 'auto' is for CHDs that hold something else (hard disk, laserdisc, raw).
TARGET_LABELS = {"chd": "→ CHD", "cue": "→ CUE/BIN", "cue_split": "→ CUE/BIN, one BIN per track",
                 "cue_single": "→ CUE/BIN, one single BIN", "iso": "→ ISO", "gdi": "→ GDI",
                 "pbp": "→ PlayStation PBP", "cdi": "→ DiscJuggler CDI (experimental)", "vcd": "→ POPStarter VCD (PS1 on PS2)", "cso": "→ CSO (compressed ISO)",
                 "verify": "Verify only", "recompress": "Recompress", "auto": "Extract", "art": "→ Artwork images (PNG)",
                 "cue_fix": "→ CUE/BIN, error correction rebuilt", "zso": "→ ZSO (LZ4 compressed ISO)", "cso2": "→ CSO version 2 (real PSP only)", "zar": "→ ZAR (Xbox 360, for Xenia)", "folder": "→ Game folder (loose files)", "pkg": "→ PKG (for RPCS3; needs your key)", "nup": "→ NUP (Switch, experimental)", "xci": "→ XCI (cartridge image)", "nsp": "→ NSP (package)", "split": "→ Separate NSPs (game, update, each extra content)",
                 "ecm": "→ ECM (error codes removed)", "wbfs": "→ WBFS", "ciso": "→ CISO", "gcz": "→ GCZ",
                 "rvz": "→ RVZ (Dolphin, compressed)", "wia": "→ WIA", "wud": "→ WUD (raw Wii U disc)",
                 "wux": "→ WUX (compressed Wii U disc)"}
# how the CHD extraction code names each target
TARGET_FMT = {"cue": "auto", "cue_split": "split", "cue_single": "single", "iso": "iso", "gdi": "auto", "pbp": "pbp",
              "cdi": "cdi", "vcd": "vcd", "cso": "cso", "cso2": "cso2", "zar": "zar", "folder": "folder", "auto": "auto", "cue_fix": "fix", "zso": "zso", "ecm": "ecm"}
CUE_TYPE = {"AUDIO": "AUDIO", "MODE1/2048": "MODE1", "MODE1/2352": "MODE1_RAW", "MODE2/2352": "MODE2_RAW"}


def collections_counter(jobs):
    c = {}
    for j in jobs:
        c[j.kind] = c.get(j.kind, 0) + 1
    return c


def parse_cue_tracks(p):
    """[(bin file, track type)] for each TRACK in a .cue"""
    out, cur = [], None
    try:
        for line in p.read_text("utf-8", "replace").splitlines():
            t = line.strip()
            m = re.match(r'FILE\s+(?:"([^"]+)"|(\S+))', t, re.I)
            if m:
                cur = ref_path(p.parent, m.group(1) or m.group(2))
                continue
            m = re.match(r"TRACK\s+\d+\s+(\S+)", t, re.I)
            if m and cur is not None:
                out.append((cur, m.group(1).upper()))
    except OSError:
        pass
    return out


# ------------------------------------------------------------------------- Job
# ---- Disk space: a conversion that clearly cannot fit is refused before anything is written
SPACE_MARGIN = 64 * 1024 ** 2        # file system overhead and a little slack
# What a target's file is, against the size of the unpacked disc. Compressed targets use a typical figure, not the
# best case, so a job is only refused when it clearly won't fit. Anything not listed is taken at the full size.
SPACE_RATIO = {"chd": 0.7, "cso": 0.95, "cso2": 0.95, "zar": 0.95, "zso": 0.95, "ecm": 0.95, "pbp": 0.8, "rvz": 0.8, "wia": 0.8, "gcz": 0.8,
               "wbfs": 0.95, "art": 0.0}
SPACE_EXT = {"chd": ".chd", "recompress": ".chd", "cue": ".cue", "cue_split": ".cue", "cue_single": ".cue", "iso": ".iso",
             "gdi": ".gdi", "pbp": ".pbp", "cdi": ".cdi", "vcd": ".vcd", "zar": ".zar", "pkg": ".pkg", "nup": ".nup", "xci": ".xci", "nsp": ".nsp", "cso": ".cso", "cso2": ".cso", "zso": ".zso", "ecm": ".ecm", "rvz": ".rvz",
             "wia": ".wia", "gcz": ".gcz", "ciso": ".ciso", "wbfs": ".wbfs", "wux": ".wux", "wud": ".wud"}


def chd_logical_size(path):
    """The unpacked size of a CHD in bytes, read from its header (None when it can't be read)."""
    try:
        with open(path, "rb") as f:
            h = f.read(40)
    except OSError:
        return None
    if len(h) < 40 or h[:8] != b"MComprHD":
        return None
    ver = struct.unpack(">I", h[12:16])[0]
    if ver == 5:
        return struct.unpack(">Q", h[32:40])[0]
    if ver in (3, 4):
        return struct.unpack(">Q", h[28:36])[0]
    return None


def free_space(path):
    """(the nearest folder that exists, bytes free on its drive)."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    return p, shutil.disk_usage(p).free


class Job:
    def __init__(self, path, root=None):
        self.path = Path(path)
        self.root = Path(root) if root else None
        self.kind = "convert"          # convert | extract | verify | recompress
        self.home = self.path          # the file the user added; outputs are named after it even while a temp copy is worked on
        self.target = None             # what this row becomes: a key of TARGET_LABELS (chosen in the Action drop-down)
        self.types = []                # track types in CHD style: AUDIO, MODE1, MODE1_RAW, MODE2_RAW (empty = unknown)
        self.action_text = ""
        self.override = "Auto"         # Auto | CD | DVD
        self.auto_type, self.reason = "CD", ""
        self.platform = ""
        self.disc = ""                 # for CHDs: CD / GD-ROM / DVD / HD / ...
        self.tracks = 0                # for CD CHDs: number of tracks
        self.container = None          # "pbp" | "cdi" | "image": unpacked to CUE/BIN before chdman sees it
        self.disc_count = 1            # discs inside a PBP
        self.m3u_chds = []             # for a playlist: its CHD files, one per disc
        self.cso_ver = 1               # a CSO source: 1 or 2 (maxcso's experimental layout)
        self.zar_raw = 0               # a ZAR source or an Xbox 360 game folder: bytes of the files in it
        self.xbox = False              # an ISO that is an Xbox 360 disc image
        self.ps3 = False               # a PlayStation 3 disc image (an ISO), or a PS3 game folder (container 'ps3folder')
        self.nsps = []                 # a Switch package (container 'swnsp'): its info, with the game's updates and extra content added to it
        self.pkgs = []                 # a PS3 package (container 'ps3pkg'): its header info, with the game's other packages (updates) added to it
        self.split_stem = ""           # a PS3 image kept in pieces (container 'ps3parts'): its name without '.iso' and the piece number
        self.ps3_info = {}             # its game ID, title, region and whether its protected parts are still encrypted
        self.warn = ""                 # something to tell the person when the item is added (it still works as far as it can)
        self.note = ""                 # something to tell the person when it is finished
        self.size = 0
        self._raw = None
        self.src_files = []
        self.missing = []
        self.lone_bin = False
        self.game_id, self.name_warned, self.disc_pick = "", False, None
        self.archive, self.extract_dir = None, None   # set when the file was unpacked from an archive
        self.wrap_cue = None           # a raw image without a CUE: the track type found in its first sector ('MODE2/2352')
        self.disc_info = {}            # GameCube/Wii discs: id, title and the container format
        self.status = "Queued"
        self.result = ""
        self.out_path = None
        self.out_size = 0

    @property
    def type(self):
        return self.override if self.override != "Auto" else self.auto_type

    def raw_size(self):
        """About how many bytes the disc holds unpacked: a CHD, CSO, PBP or Wii disc file is smaller than what is in it."""
        if self._raw is None:
            self._raw = self._raw_size()
        return self._raw

    def _raw_size(self):
        p, ext = self.path, self.path.suffix.lower()
        try:
            if ext == ".chd":
                return chd_logical_size(p) or self.size * 3
            if ext == ".m3u":
                return sum(chd_logical_size(c) or c.stat().st_size * 3 for c in self.m3u_chds)
            if self.container == "cso":
                return read_cso_header(p)[0]
            if self.container in ("psnfolder", "swfolder", "nup"):
                return self.zar_raw
            if self.container in ("zar", "xfolder", "ps3folder"):
                return self.zar_raw + 4 * XISO_MODULUS
            if self.container == "disc":
                d = open_disc(p)
                try:
                    return d.size
                finally:
                    d.close()
            if self.container == "ecm":
                return int(self.size * 1.2)
            if self.container == "pbp":
                n = int(self.size * 1.6)
                return n // self.disc_count if self.disc_pick and self.disc_count > 1 else n
        except (OSError, UnpackError, ValueError, struct.error):
            pass
        return self.size

    def set_target(self, target):
        self.target = target
        base, _, d = target.partition("_d")
        self.disc_pick = int(d) if d else None          # 'chd_d2': only disc 2 of a multi-disc PBP
        self.kind = {"chd": "convert", "verify": "verify", "recompress": "recompress"}.get(base, "extract")

    def default_target(self):
        t = [k for k, _ in self.available_targets()]
        if self.path.suffix.lower() == ".chd":
            return next((k for k in ("cue_split", "cue", "gdi", "iso", "auto") if k in t), "verify")
        return t[0] if t else "chd"

    def available_targets(self):
        """[(key, label)] of everything this item can safely become. Nothing is offered that is known not to work."""
        if self.container == "zar":                 # Xbox 360: the files become a disc image, or a CHD of one
            return [(k, target_label(k)) for k in ("chd", "iso", "folder")]
        if self.container == "xfolder":
            return [(k, target_label(k)) for k in ("chd", "iso", "zar")]
        if self.container == "ps3folder":           # a PS3 game folder: a disc image of it (RPCS3 loads those), or a CHD of one
            return [(k, target_label(k)) for k in ("iso", "chd")]
        if self.container == "psnfolder":           # a PSN game folder: a package of it that RPCS3 can install
            return [("pkg", target_label("pkg"))]
        if self.container == "ps3pkg":              # PS3 packages: the game's files, with its updates merged in
            return [("folder", target_label("folder"))]
        if self.container == "swfolder":            # a decrypted Switch game folder: a NUP of it (experimental)
            return [("nup", target_label("nup"))]
        if self.container == "nup":                 # a NUP: the folder it holds
            return [("folder", target_label("folder"))]
        if self.container in ("swnsp", "swxci"):    # Switch files: one cartridge image or package of everything, or one package per title
            titles = [t for i in self.nsps for t in i["titles"]]
            keys = ["xci", "nsp"] if len(self.nsps) > 1 else ["xci"] if self.container == "swnsp" else ["nsp"]
            if len(titles) > 1 and all(t["kind"] for t in titles):
                keys.append("split")
            return [(k, target_label(k)) for k in keys]
        if self.container == "ps3parts":            # a PS3 disc image kept in pieces: joined into one ISO, a CHD of it, or its files
            return [(k, target_label(k)) for k in (("iso", "chd") if self.ps3_info.get("encrypted") else ("iso", "chd", "folder"))]
        if self.ps3:                                # a PS3 disc image: a CHD of it, or its files (not while they are still encrypted)
            return [(k, target_label(k)) for k in (("chd",) if self.ps3_info.get("encrypted") else ("chd", "folder"))]
        if self.xbox:                               # an Xbox 360 ISO: a CHD of it, or Xenia's ZAR
            return [(k, target_label(k)) for k in ("chd", "zar", "folder")]
        if self.container == "disc":                # GameCube / Wii: Dolphin's formats (a CHD of these runs nowhere)
            own = self.disc_info.get("format", "ISO").lower()
            if self.platform == "Wii U":
                return [(k, target_label(k)) for k in ("wux", "wud") if k != own]
            keys = [k for k in ["rvz", "iso", "gcz", "wia", "ciso"] + (["wbfs"] if self.platform == "Wii" else []) if k != own]
            if own != "iso":
                keys.insert(0, keys.pop(keys.index("iso")))
            if self.platform == "Wii":                  # RVZ, WIA and WBFS from a Wii disc need the user's key (or their Dolphin)
                keys = [k for k in keys if not (k in ("rvz", "wia") and not wii_can("rvz")) and not (k == "wbfs" and not wii_can("wbfs"))]
            return [(k, target_label(k)) for k in keys]
        ext, ty = self.path.suffix.lower(), self.types
        data = [x for x in ty if x != "AUDIO"]
        single = len(ty) == 1 and ty[0].startswith("MODE")
        raw = bool(data) and all(x.endswith("_RAW") for x in data)
        keys = []
        if ext == ".chd":
            if self.disc == "CD":
                keys += ["cue"] if len(ty) <= 1 else ["cue_split", "cue_single"]
                if single:
                    keys += ["iso", "cso", "zso", "cso2"]
                if raw and data[0].startswith("MODE2"):
                    keys += ["pbp", "vcd"]
                if raw:
                    keys.append("cdi")
                if raw:
                    keys.append("cue_fix")
                if raw and len(ty) == 1:
                    keys.append("ecm")
            elif self.disc == "GD-ROM":
                keys.append("gdi")
            elif self.disc == "DVD":
                keys += ["iso", "cso", "zso", "cso2", "zar", "folder"]
            else:
                keys.append("auto")
            keys.append("verify")
            if self.disc in ("CD", "GD-ROM", "DVD"):
                keys.append("recompress")
        elif ext == ".m3u":
            keys.append("pbp")
        else:
            keys.append("chd")
            if ty and self.container in ("pbp", "cdi", "image", "ecm"):
                keys.append("cue")
            if self.container == "pbp":
                keys.append("art")
            if ty and self.disc_count == 1:
                if single:
                    keys += [k for k, skip in (("iso", ".iso"), ("cso", ".cso"), ("zso", ".zso")) if ext != skip or self.wrap_cue
                             or (k == "cso" and self.cso_ver == 2)]
                    if self.cso_ver != 2:
                        keys.append("cso2")
                if raw and self.container != "cso":
                    if self.platform == "PS1" and ext != ".pbp":
                        keys.append("pbp")
                    if self.platform == "PS1" and ext != ".vcd" and data[0].startswith("MODE2"):
                        keys.append("vcd")
                    if ext != ".cdi":
                        keys.append("cdi")
                    keys.append("cue_fix")
                    if len(ty) == 1 and ext != ".ecm":
                        keys.append("ecm")
                elif ext in (".iso", ".cso") and self.platform == "PS1":
                    keys.append("pbp")            # a plain PS1 ISO: raw sectors are rebuilt for the PBP
        if self.container == "pbp" and self.disc_count > 1:
            keys += [f"{b}_d{n}" for b in ("chd", "cue") for n in range(1, self.disc_count + 1)]
        return [(k, target_label(k)) for k in dict.fromkeys(keys)]

    @property
    def can_pick(self):
        return len(self.available_targets()) > 1

    def analyze(self, _=None):
        self._analyze()
        if self.target not in [k for k, _ in self.available_targets()]:
            self.set_target(self.default_target())
        self.game_id = self.guess_game_id()

    def guess_game_id(self):
        """The PlayStation game code ('SLUS01545') when it can be read straight from the file, else ''."""
        ext = self.path.suffix.lower()
        if self.ps3_info:
            return self.ps3_info.get("id", "")
        if self.pkgs:
            return self.pkgs[0]["title_id"]
        if self.container == "disc":
            return self.disc_info.get("id", "")
        try:
            if ext == ".pbp":
                return pbp_game_id(self.path)
            if self.platform not in ("PS1", "PS2"):
                return ""
            if ext == ".cue":
                c = cue_data_start(self.path)
                files = [c[0]] if c and c[0].exists() else []
            else:
                files = [self.path] if ext in (".iso", ".bin") else []
            for f in files:
                for stride, off in ((2352, 24), (2352, 16), (2048, 0)):
                    gid = ps1_boot_id(f, off, stride, ps2=self.platform == "PS2")
                    if gid:
                        return gid
        except (OSError, ValueError, struct.error):
            pass
        return ""

    def _analyze(self):
        p, ext = self.path, self.path.suffix.lower()
        sp = split_set(p) if not p.is_dir() else None
        if sp is not None:                          # one piece of a PS3 disc image kept in several files: the whole set is this item
            stem, pieces, problem = sp
            if problem:
                raise ValueError(problem)
            info = ps3_iso_kind(pieces)
            if info is None:
                raise ValueError(f"these {len(pieces)} file(s) ({pieces[0].name}" + (f" … {pieces[-1].name}" if len(pieces) > 1 else "") +
                                 ") don't join into a PlayStation 3 disc image. Only PlayStation 3 images that were split into pieces can be joined")
            self.path = self.home = pieces[0]
            self.split_stem = stem
            self.container, self.platform, self.types = "ps3parts", "PS3", ["MODE1"]
            self.ps3, self.ps3_info = True, info
            self.src_files = pieces
            self.size = sum(x.stat().st_size for x in pieces)
            self.auto_type, self.reason = "DVD", f"PS3 disc image in {len(pieces)} piece{'s' if len(pieces) != 1 else ''}"
            if info["encrypted"]:
                self.warn = ("this PS3 disc image is still encrypted, so its files can't be unpacked until it has been decrypted with your own "
                             "disc key. It can still be joined into one ISO, or become a CHD, which store it exactly as it is")
            return
        if ext in (".nsp", ".xci") and not p.is_dir():
            info = nsp_info(p, SWITCH_KEYS[0])
            self.container, self.platform, self.types = "swxci" if ext == ".xci" else "swnsp", "Switch", ["MODE1"]
            self.nsps = [info]
            self.size, self.src_files = info["size"], [p]
            self.auto_type = "DVD"
            self.reason = sw_reason(self.nsps)
            if info["ticketed"] and info["key_idx"] is not None and info["key_idx"] > sw_level(SWITCH_KEYS[0]):
                self.warn = sw_fw_message("it", info["key_idx"], sw_level(SWITCH_KEYS[0]))
            elif info["ticketed"] and not SWITCH_KEYS[0]:
                self.warn = ("it needs your own keys to be converted to an XCI, and none are set up yet "
                             "(Settings, Formats, Nintendo Switch: choose your prod.keys file or paste it in)")
            return
        if ext == ".pkg" and not p.is_dir():
            info = pkg_info(p)
            self.container, self.platform, self.types = "ps3pkg", "PS3", ["MODE1"]
            self.pkgs = [info]
            self.size, self.src_files = info["size"], [p]
            self.auto_type = "DVD"
            self.reason = "PS3 update package" if info["patch"] else "PS3 DLC package" if info["dlc"] else "PS3 game package"
            if not RPCS3[0] and not PKG_KEY[0]:
                self.warn = ("it needs your own RPCS3 or your own package key to be unpacked, and neither is set up yet "
                             "(Settings, Formats, PlayStation 3: show it where rpcs3.exe is, or give it your key)")
            return
        if p.is_dir() and nup_bundle(p) is not None:           # a decrypted Switch game (exefs/romfs, with its updates and extra content): can become a NUP
            pkgs = nup_bundle(p)
            tree = [t for x in pkgs for t in nup_tree(x["root"])]
            self.container, self.platform, self.types = "swfolder", nup_folder_console(pkgs) or "Switch", ["MODE1"]
            self.zar_raw = sum(t[2] for t in tree)
            self.size, self.src_files = self.zar_raw, [p]
            u, d = sum(1 for x in pkgs if x["kind"] == 1), sum(1 for x in pkgs if x["kind"] == 2)
            what = ("game" if any(x["kind"] == 0 for x in pkgs) else "no game") + (f" + {u} update{'s' if u != 1 else ''}" if u else "") + (f" + {d} extra content" if d else "")
            self.auto_type, self.reason = "DVD", f"Switch game folder, decrypted ({what}; {len(tree)} files)"
            self.warn = nup_resolve(pkgs, p) or ""
            return
        if ext == ".nup" and not p.is_dir():
            try:
                pk, nfiles, _tree, _rf, close = nup_open(p)
            except UnpackError as e:
                raise ValueError(str(e)) from None
            close()
            self.container, self.platform, self.types = "nup", nup_file_console(p) or "Switch 2", ["MODE1"]     # the NUP header itself doesn't say; its main.npdm does (when it can't tell, these are in practice Switch 2)
            self.zar_raw = sum(x["size"] for x in nfiles)
            self.size, self.src_files = p.stat().st_size, [p]
            self.auto_type = "DVD"
            self.reason = f"NUP file ({', '.join(NUP_KINDS.get(x['kind'], 'package') for x in pk)}; {len(nfiles)} files)"
            return
        if p.is_dir() and psn_folder_info(p) is not None:     # a PSN game as RPCS3 keeps it (PARAM.SFO and USRDIR): can become a package for RPCS3
            ents = ps3_folder_source(p, True)[0]
            self.container, self.platform, self.types = "psnfolder", "PS3", ["MODE1"]
            self.ps3_info = dict(psn_folder_info(p), encrypted=False)
            self.zar_raw = sum(sz for _p, f, _o, sz in ents if f)
            self.size, self.src_files = self.zar_raw, [p]
            self.auto_type, self.reason = "DVD", "PS3 game folder from the PlayStation Store"
            if not PKG_KEY[0]:
                self.warn = ("making a package from it needs your own package key, and none is set up yet (Settings, Formats, PlayStation 3: give it your key)")
            return
        if p.is_dir():
            info = ps3_folder_info(p)
            if info is not None:                    # a PS3 game folder (PS3_GAME with a PARAM.SFO)
                ents = ps3_folder_source(p)[0]
                self.container, self.platform, self.types = "ps3folder", "PS3", ["MODE1"]
                self.ps3, self.ps3_info = True, dict(info, encrypted=False)
                self.zar_raw = sum(sz for _p, f, _o, sz in ents if f)
                self.size, self.src_files = self.zar_raw, [p]
                self.auto_type, self.reason = "DVD", "PS3 game folder"
                return
            ents = folder_source(p)[0]              # an Xbox 360 game folder (the disc's files, with default.xex at the top)
            if not any(len(pp) == 1 and pp[0].lower() == b"default.xex" and f for pp, f, _o, _s in ents):
                raise ValueError("this folder doesn't look like an Xbox 360 or PlayStation 3 game (it has no default.xex at its top level "
                                 "and no PS3_GAME folder with a PARAM.SFO)")
            self.container, self.platform, self.types = "xfolder", "Xbox 360", ["MODE1"]
            self.zar_raw = sum(sz for _p, f, _o, sz in ents if f)
            self.size, self.src_files = self.zar_raw, [p]
            self.auto_type, self.reason = "DVD", "Xbox 360 game folder"
            return
        if ext == ".chd":
            self.size = p.stat().st_size
            self.disc, self.types = chd_probe(p) if CHDMAN else ("?", [])
            self.tracks = len(self.types)
            self.src_files = [p]
            return
        kind = disc_kind(p) if ext in (".iso", ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud") else ""
        if kind:
            d = open_disc(p)
            try:
                self.disc_info = dict(d.info(), format="WUD" if kind == "Wii U" and d.kind == "ISO" else d.kind)
                self.src_files = [Path(f.name) for f in getattr(d, "files", [])] or [p]
            finally:
                d.close()
            self.container, self.platform, self.disc = "disc", kind, f"{kind} disc"
            self.size = sum(f.stat().st_size for f in self.src_files)
            self.auto_type, self.reason = "DVD", f"{kind} disc image"
            return
        if ext in (".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".wux", ".wud"):
            raise ValueError("this doesn't look like a readable GameCube/Wii/Wii U disc image (damaged, or not a disc)")
        if ext == ".ecm":
            t = ecm_probe(p)
            self.container, self.size, self.src_files = "ecm", p.stat().st_size, [p]
            self.types = [CUE_TYPE[t]] if t in CUE_TYPE else []
            self.auto_type, self.reason = "CD", f"ECM file ({t or 'sector type unknown'})"
            return
        if ext in (".cso", ".zso", ".ciso"):
            total = read_cso_header(p)[0]
            self.cso_ver = cso_version(p)
            self.size, self.src_files, self.container, self.types = p.stat().st_size, [p], "cso", ["MODE1"]
            self.platform = cso_platform(p)
            self.auto_type, self.reason = decide_type(".iso", total, "")
            return
        if ext == ".m3u":
            lines = [ln.strip() for ln in p.read_text("utf-8-sig", "replace").splitlines() if ln.strip() and not ln.startswith("#")]
            self.m3u_chds = [p.parent / ln for ln in lines]
            bad = [c.name for c in self.m3u_chds if c.suffix.lower() != ".chd" or not c.is_file()]
            if not self.m3u_chds or bad or len(self.m3u_chds) > 5:
                raise ValueError("a playlist must list 1-5 existing .chd files" + (f" (problem: {', '.join(bad[:2])})" if bad else ""))
            self.disc, self.platform, self.disc_count = "CD", "PS1", len(self.m3u_chds)
            self.src_files = [p] + self.m3u_chds
            self.size = sum(f.stat().st_size for f in self.src_files)
            return
        if ext == ".zar":
            z = ZarArchive(p)
            try:
                ents = list(z.entries())
            finally:
                z.close()
            if not any(len(pp) == 1 and pp[0].lower() == b"default.xex" and f for pp, f, _o, _s in ents):
                raise ValueError("this ZAR doesn't look like an Xbox 360 game (there is no default.xex at its top level)")
            self.container, self.platform, self.types = "zar", "Xbox 360", ["MODE1"]
            self.size, self.src_files = p.stat().st_size, [p]
            self.zar_raw = sum(sz for _p, f, _o, sz in ents if f)
            self.auto_type, self.reason = decide_type(ext, self.zar_raw, "Xbox 360")
            return
        if ext in IMAGE_EXTS:
            tracks = read_image(p)
            self.container = "image"
            self.types = ["AUDIO" if t["kind"] == "AUDIO" else "MODE1" if t["kind"] == "MODE1" and t["stride"] == 2048 else
                          f"{t['kind']}_RAW" for t in tracks]
            self.src_files = image_sources(p, tracks)
            self.size = sum(f.stat().st_size for f in self.src_files)
            self.platform = image_platform(tracks)
            self.auto_type, self.reason = decide_type(ext, sum(t["count"] * t["ss"] for t in tracks), self.platform)
            return
        if ext in (".pbp", ".cdi"):
            self.container = ext[1:]
            self.size = p.stat().st_size
            self.src_files = [p]
            if ext == ".pbp":
                self.disc_count = len(pbp_discs(p))
                self.platform = "PS1"
                self.types = ["MODE2_RAW", "AUDIO"]       # a PBP may hold audio tracks, so no ISO/CSO is offered for it
            else:
                cdi = parse_cdi(p)
                self.types = ["AUDIO" if t["mode"] == 0 else f"MODE{t['mode']}_RAW" for t in cdi]
                if any(t["session"] > 1 and t["mode"] == 2 for t in cdi):
                    self.platform = "Dreamcast"
            self.auto_type, self.reason = decide_type(ext, self.size, self.platform)
            return
        if ext == ".cue":
            refs = parse_cue(p)
        elif ext == ".gdi":
            refs = parse_gdi(p)
        elif ext == ".toc":
            refs = parse_toc(p)
        else:
            refs = [p]
            self.lone_bin = ext == ".bin"
        self.src_files = [p] + [r for r in refs if r != p]
        self.missing = [r.name for r in refs if not r.exists()]
        self.size = sum(f.stat().st_size for f in self.src_files if f.exists())
        data_size = sum(r.stat().st_size for r in refs if r.exists()) or self.size
        first = next((r for r in refs if r.exists()), None)
        if ext == ".gdi":
            self.platform = "Dreamcast"
        elif ext == ".cue" and cue_data_start(p) and cue_data_start(p)[0].exists():
            f0, base = cue_data_start(p)
            self.platform = sniff_platform(f0, base) or (sniff_platform(first) if first else "")
        elif first:
            self.platform = sniff_platform(first)
        self.auto_type, self.reason = decide_type(ext, data_size, self.platform)
        if ext in (".iso", ".bin") and not self.missing:
            self.wrap_cue = sniff_raw_image(p) or None
            if self.wrap_cue:
                self.types = [CUE_TYPE[self.wrap_cue]]
                self.auto_type, self.reason = "CD", f"raw {self.wrap_cue.split('/')[0]} sectors without a CUE"
            elif ext == ".iso":
                self.types = ["MODE1"]
        elif ext == ".cue":
            ty = [CUE_TYPE.get(t) for _, t in parse_cue_tracks(p)]
            self.types = ty if ty and None not in ty else []
        if ext == ".iso" and not self.missing and not self.wrap_cue:
            kind = xbox_iso_kind(p)
            if kind:
                self.platform, self.xbox = "Xbox 360" if kind == "360" else "Xbox", kind == "360"
                self.auto_type, self.reason = "DVD", "Xbox disc image"
            else:
                pinfo = ps3_iso_kind(p)
                if pinfo:
                    self.platform, self.ps3, self.ps3_info = "PS3", True, pinfo
                    self.auto_type, self.reason = "DVD", "PS3 disc image"
                    if pinfo["encrypted"]:
                        self.warn = ("this PS3 disc image is still encrypted, so its files can't be unpacked until it has been decrypted with your own "
                                     "disc key. It can still become a CHD, which stores it exactly as it is")
        if self.lone_bin:
            self.reason += " (BIN without a CUE)"
        if self.missing:
            self.status = "Missing: " + ", ".join(self.missing[:2])

    def action_label(self):
        text = self.action_text or target_label(self.target or "")
        return text + "  ▾" if self.can_pick and not self.status.startswith("Done") else text

    def type_label(self):
        if self.kind == "convert":
            return self.type + ("*" if self.override != "Auto" else "")
        return self.disc or self.type

    def add_pkg(self, other):
        """Another package of the same game (a base and its updates are installed in that order, as on a console) joins this item."""
        self.pkgs += other.pkgs
        self.pkgs.sort(key=lambda i: (2 if i["dlc"] else 1 if i["patch"] else 0, i["version"], i["path"].name.lower()))
        self.src_files = [i["path"] for i in self.pkgs]
        self.path = self.home = self.src_files[0]
        self.size = sum(i["size"] for i in self.pkgs)
        ups, dlcs = sum(1 for i in self.pkgs if i["patch"]), sum(1 for i in self.pkgs if i["dlc"])
        extra = ([f"{ups} update{'s' if ups != 1 else ''}"] if ups else []) + ([f"{dlcs} DLC"] if dlcs else [])
        self.reason = "PS3 game package" + (" with " + " and ".join(extra) if extra else "")
        self._raw = None

    def add_nsp(self, other):
        """Another Switch file for the same game (an update, extra content, or the game itself) joins this item."""
        self.nsps += other.nsps
        self.nsps.sort(key=lambda i: (SW_KIND_RANK.get(i["kind"], 0), i["tid"], i["version"], i["path"].name))
        self.src_files = [i["path"] for i in self.nsps]
        base = next((i for i in self.nsps if i["kind"] in ("base", "")), None)
        if base is not None:
            self.path = self.home = base["path"]
        self.size = sum(i["size"] for i in self.nsps)
        self.reason = sw_reason(self.nsps)
        self._raw = None

    def row(self):
        name = self.path.name + (f"  (+{len(self.src_files) - 1} more)" if self.container in ("ps3parts", "ps3pkg", "swnsp") and len(self.src_files) > 1 else "")
        return (name, self.action_label(), self.platform, self.type_label(),
                fmt_size(self.size), self.status, self.result, str(self.path.parent))


# --------------------------------------------------------------------------- Xbox 360: ZArchive (.zar) and the XISO disc image built from it
# ZArchive (by Exzap, MIT-0) is a file archive with 64 KiB zstd blocks; Xenia uses it for Xbox 360 games, which it holds as
# the disc's files. An XISO is the plain XDVDFS file system of an Xbox disc with those files in it (the layout extract-xiso
# writes). It is rebuilt from the files, so it is not the original disc dump: the copy-protection data a console checks is not in
# a ZAR and cannot be recreated. It is for emulators.
ZAR_MAGIC, ZAR_VERSION, ZAR_BLOCK, ZAR_FOOTER = 0x169F52D6, 0x61BF3A01, 65536, 144
XISO_SECTOR = 2048
XISO_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XISO_ROOT_SECTOR = 0x108                  # where extract-xiso puts the root directory
XISO_MODULUS = 0x10000                    # the image ends on a multiple of this
XISO_BASES = (0, 0x0FD90000, 0x02080000, 0x18300000)       # where a disc dump's file system can start (none, XGD2, XGD3, XGD1)
XISO_TIME = 0x01CA8E1E89D70700            # a fixed volume time (2010-01-01 UTC), so the same files always make the same image


def _upper(b):
    return bytes(c - 32 if 97 <= c <= 122 else c for c in b)


class ZarArchive:
    """Read a ZArchive: its files in the order of its tree, and any file's bytes."""

    def __init__(self, path):
        self.path = Path(path)
        self.f = open(self.path, "rb")
        try:
            self._open()
        except Exception:
            self.f.close()
            raise

    def _open(self):
        f = self.f
        size = os.fstat(f.fileno()).st_size
        if size <= ZAR_FOOTER:
            raise UnpackError("this is not a ZAR file")
        f.seek(size - ZAR_FOOTER)
        v = struct.unpack(">12Q32sQII", f.read(ZAR_FOOTER))
        self.footer_hash, total, version, magic = v[12], v[13], v[14], v[15]
        if magic != ZAR_MAGIC or version != ZAR_VERSION or total != size:
            raise UnpackError("this is not a ZAR file, or it is damaged or cut short")
        sec = [(v[i], v[i + 1]) for i in range(0, 12, 2)]
        if any(o + n > size for o, n in sec):
            raise UnpackError("this ZAR's index points outside the file, so it is damaged")
        (self.data_off, self.data_size), rec, names, tree = sec[0], sec[1], sec[2], sec[3]
        if rec[1] % 40 or tree[1] % 16 or not rec[1] or not tree[1] or names[1] > 0x7FFFFFFF:
            raise UnpackError("this ZAR's index is damaged")
        f.seek(rec[0])
        self.records = [(r[0], r[1:]) for r in struct.iter_unpack(">Q16H", f.read(rec[1]))]
        f.seek(names[0])
        self.names = f.read(names[1])
        f.seek(tree[0])
        self.tree = list(struct.iter_unpack(">IIII", f.read(tree[1])))
        if self.tree[0][0] & 0x80000000 or self._name(self.tree[0][0] & 0x7FFFFFFF):
            raise UnpackError("this ZAR's file tree is damaged")
        self._dctx = None
        self._cache = (-1, b"")

    def close(self):
        self.f.close()

    def _name(self, off):
        n = self.names
        if off == 0x7FFFFFFF or off >= len(n):
            return b""
        ln = n[off] & 0x7F
        if n[off] & 0x80:                       # a name of 128 characters or more: its length goes over two bytes
            if off + 1 >= len(n):
                return b""
            ln |= n[off + 1] << 7
            off += 2
        else:
            off += 1
        return bytes(n[off:off + ln])

    def entries(self):
        """Yield (path as bytes parts, is_file, stream offset, size) for every file and folder, parents first."""
        stack = [((), 0, 0)]
        seen = 0
        while stack:
            parts, idx, depth = stack.pop()
            if depth > 64:
                raise UnpackError("this ZAR has folders nested too deeply")
            e = self.tree[idx]
            start, count = e[1], e[2]
            if start + count > len(self.tree):
                raise UnpackError("this ZAR's file tree is damaged")
            kids = []
            for i in range(start, start + count):
                k = self.tree[i]
                nm = self._name(k[0] & 0x7FFFFFFF)
                if not nm:
                    raise UnpackError("this ZAR has an item with no name")
                is_file = bool(k[0] & 0x80000000)
                if is_file:
                    yield parts + (nm,), True, k[1] | ((k[3] & 0xFFFF) << 32), k[2] | ((k[3] >> 16) << 32)
                else:
                    yield parts + (nm,), False, 0, 0
                    kids.append((parts + (nm,), i, depth + 1))
                seen += 1
                if seen > len(self.tree):
                    raise UnpackError("this ZAR's file tree loops back on itself")
            stack.extend(reversed(kids))

    def _block(self, i):
        if self._cache[0] == i:
            return self._cache[1]
        ri, si = divmod(i, 16)
        if ri >= len(self.records):
            raise UnpackError("this ZAR is damaged (a block is missing)")
        base, sizes = self.records[ri]
        off = base + sum(s + 1 for s in sizes[:si])
        csize = sizes[si] + 1
        if off + csize > self.data_size:
            raise UnpackError("this ZAR is damaged (a block lies outside the data)")
        self.f.seek(self.data_off + off)
        raw = self.f.read(csize)
        if len(raw) != csize:
            raise UnpackError("this ZAR is cut short")
        if csize == ZAR_BLOCK:
            data = raw
        else:
            try:
                import zstandard
            except ImportError:
                raise UnpackError("a ZAR is compressed with zstd, which needs the 'zstandard' package") from None
            if self._dctx is None:
                self._dctx = zstandard.ZstdDecompressor()
            try:
                data = self._dctx.decompress(raw, max_output_size=ZAR_BLOCK)
            except zstandard.ZstdError as e:
                raise UnpackError(f"this ZAR is damaged (block {i}: {e})") from None
            if len(data) != ZAR_BLOCK:
                raise UnpackError(f"this ZAR is damaged (block {i} has the wrong size)")
        self._cache = (i, data)
        return data

    def read(self, offset, size, chunk=1 << 20):
        """Yield the bytes of stream[offset:offset+size] in pieces of at most `chunk`."""
        end = offset + size
        pos = offset
        out = []
        have = 0
        while pos < end:
            b = self._block(pos // ZAR_BLOCK)
            a = pos % ZAR_BLOCK
            piece = b[a:a + min(ZAR_BLOCK - a, end - pos)]
            out.append(piece)
            have += len(piece)
            pos += len(piece)
            if have >= chunk:
                yield b"".join(out)
                out, have = [], 0
        if out:
            yield b"".join(out)

    def hash_ok(self, progress=lambda x: None, stopped=lambda: False):
        """The archive carries a SHA-256 of itself: check it."""
        import hashlib
        size = os.fstat(self.f.fileno()).st_size
        h = hashlib.sha256()
        self.f.seek(0)
        left = size - ZAR_FOOTER
        while left:
            b = self.f.read(min(left, 4 << 20))
            if not b:
                return False
            h.update(b)
            left -= len(b)
            progress(1 - left / size)
            if stopped():
                raise UnpackError("stopped")
        foot = bytearray(self.f.read(ZAR_FOOTER))
        foot[96:128] = bytes(32)
        h.update(foot)
        return h.digest() == self.footer_hash


class _XNode:
    __slots__ = ("name", "is_dir", "size", "src", "kids", "sector", "tsize", "order", "pos", "left", "right")

    def __init__(self, name, is_dir, size=0, src=None):
        self.name, self.is_dir, self.size, self.src = name, is_dir, size, src
        self.kids, self.sector, self.tsize, self.order = [], 0, 0, []
        self.pos = self.left = self.right = 0


def xiso_tree(entries):
    """[(path parts, is_file, src, size)] -> the root _XNode, checking what XDVDFS can hold."""
    root = _XNode(b"", True)
    index = {(): root}
    for parts, is_file, src, size in entries:
        parent = index.get(parts[:-1])
        if parent is None or not parent.is_dir:
            raise UnpackError("the archive lists an item before the folder it is in")
        name = parts[-1]
        if len(name) > 255 or not name:
            raise UnpackError(f"a name is too long for an Xbox disc (255 characters at most): {name[:40].decode('latin1')}...")
        if any(_upper(k.name) == _upper(name) for k in parent.kids):
            raise UnpackError(f"two items in one folder differ only in upper/lower case ({name.decode('latin1')}), which an Xbox disc cannot hold")
        node = _XNode(name, not is_file, size if is_file else 0, src)
        parent.kids.append(node)
        index[parts] = node
    return root


def _xiso_balance(nodes):
    """A balanced search tree over names sorted the Xbox way (ASCII upper case); -> (root, in pre-order)."""
    nodes = sorted(nodes, key=lambda n: _upper(n.name))
    order = []

    def build(lo, hi):
        if lo >= hi:
            return None
        mid = (lo + hi) // 2
        n = nodes[mid]
        order.append(n)
        n.left = build(lo, mid)
        n.right = build(mid + 1, hi)
        return n
    root = build(0, len(nodes))
    return root, order


def _xiso_dir_plan(d):
    """Work out where every entry of directory d sits in its table: d.order (pre-order) with .pos, and d.tsize."""
    root, order = _xiso_balance(d.kids)
    d.order = order
    pos = 0
    for n in order:
        ln = (14 + len(n.name) + 3) & ~3
        if pos % XISO_SECTOR + ln > XISO_SECTOR:
            pos = (pos // XISO_SECTOR + 1) * XISO_SECTOR
        n.pos = pos
        pos += ln
    d.tsize = max(XISO_SECTOR, (pos + XISO_SECTOR - 1) // XISO_SECTOR * XISO_SECTOR)


def _xiso_dir_bytes(d):
    out = bytearray(b"\xff" * d.tsize)
    for n in d.order:
        left = n.left.pos // 4 if n.left else 0
        right = n.right.pos // 4 if n.right else 0
        e = struct.pack("<HHIIBB", left, right, n.sector, n.tsize if n.is_dir else n.size, 0x10 if n.is_dir else 0x20, len(n.name)) + n.name
        out[n.pos:n.pos + len(e)] = e
    return bytes(out)


def xiso_layout(root):
    """Give every directory table and file its sector (extract-xiso's order). -> (list in disc order, total sectors)."""
    _walk = [root]
    while _walk:
        d = _walk.pop()
        _xiso_dir_plan(d)
        _walk += [k for k in d.kids if k.is_dir]
    root.sector = XISO_ROOT_SECTOR
    nxt = XISO_ROOT_SECTOR + root.tsize // XISO_SECTOR
    items = [root]

    def alloc(d):
        nonlocal nxt
        for c in d.order:
            c.sector = nxt
            if c.is_dir:
                nxt += c.tsize // XISO_SECTOR
                items.append(c)
                alloc(c)
            else:
                nxt += (c.size + XISO_SECTOR - 1) // XISO_SECTOR
                if c.size:
                    items.append(c)
    alloc(root)
    return items, nxt


def build_xiso(entries, out_path, read, progress=lambda x: None, stopped=lambda: False):
    """Write an XISO of `entries` [(path parts, is_file, src, size)]; read(src, size) yields each file's bytes in pieces."""
    root = xiso_tree(entries)
    items, sectors = xiso_layout(root)
    total = sum(n.size for n in items if not n.is_dir) or 1
    done = 0
    pos = 0
    with open(out_path, "wb") as g:
        def zeros(upto):
            nonlocal pos
            while pos < upto:
                n = min(upto - pos, 1 << 20)
                g.write(bytes(n))
                pos += n
        zeros(0x10000)
        head = bytearray(XISO_SECTOR)
        head[:20] = XISO_MAGIC
        head[20:28] = struct.pack("<II", XISO_ROOT_SECTOR, root.tsize)
        head[28:36] = struct.pack("<Q", XISO_TIME)
        head[0x7EC:0x800] = XISO_MAGIC
        g.write(head)
        pos += XISO_SECTOR
        for n in items:
            zeros(n.sector * XISO_SECTOR)
            if n.is_dir:
                g.write(_xiso_dir_bytes(n))
                pos += n.tsize
            else:
                wrote = 0
                for piece in read(n.src, n.size):
                    g.write(piece)
                    wrote += len(piece)
                    done += len(piece)
                    progress(done / total)
                    if stopped():
                        raise UnpackError("stopped")
                if wrote != n.size:
                    raise UnpackError("a file came out shorter than the archive says")
                pos += wrote
                zeros((pos + XISO_SECTOR - 1) // XISO_SECTOR * XISO_SECTOR)
        zeros((pos + XISO_MODULUS - 1) // XISO_MODULUS * XISO_MODULUS)
    progress(1.0)


def xiso_files(path, root_only=False):
    """-> (base offset, [(path parts as bytes, is_file, absolute offset, size)]) of an XISO or an Xbox disc dump."""
    with open(path, "rb") as f:
        for base in XISO_BASES:
            f.seek(base + 0x10000)
            h = f.read(XISO_SECTOR)
            if len(h) == XISO_SECTOR and h[:20] == XISO_MAGIC and h[0x7EC:0x800] == XISO_MAGIC:
                break
        else:
            raise UnpackError("this is not an Xbox disc image (no XDVDFS volume was found)")
        root_sector, root_size = struct.unpack("<II", h[20:28])
        out = []
        seen = 0

        def walk(sector, size, parts, depth):
            nonlocal seen
            if depth > 64 or size > 1 << 28:
                raise UnpackError("this disc image's folders are damaged")
            f.seek(base + sector * XISO_SECTOR)
            tab = f.read(size)
            stack = [0]
            visited = set()
            while stack:
                p = stack.pop()
                if p in visited or p + 14 > len(tab):
                    continue
                visited.add(p)
                left, right, sec, sz, attr, nl = struct.unpack("<HHIIBB", tab[p:p + 14])
                if left == 0xFFFF and right == 0xFFFF:
                    continue
                name = tab[p + 14:p + 14 + nl]
                seen += 1
                if seen > 5_000_000:
                    raise UnpackError("this disc image has too many items")
                if attr & 0x10:
                    out.append((parts + (name,), False, 0, 0))
                    if sz and not root_only:
                        walk(sec, sz, parts + (name,), depth + 1)
                else:
                    out.append((parts + (name,), True, base + sec * XISO_SECTOR, sz))
                if left:
                    stack.append(left * 4)
                if right:
                    stack.append(right * 4)
        walk(root_sector, root_size, (), 0)
        return base, out


def xbox_iso_kind(path):
    """'360' for an XDVDFS disc image with a default.xex at its top, 'xbox' for one with default.xbe, else None."""
    try:
        _, ents = xiso_files(path, root_only=True)
    except (UnpackError, OSError, struct.error):
        return None
    top = {p[0].lower() for p, f, _o, _s in ents if f and len(p) == 1}
    return "360" if b"default.xex" in top else "xbox" if b"default.xbe" in top else None


def iso_source(path):
    """(entries, read, close) for the files inside an XISO on disk."""
    _, ents = xiso_files(path)
    f = open(path, "rb")

    def read(off, size, chunk=1 << 20):
        f.seek(off)
        while size > 0:
            b = f.read(min(chunk, size))
            if not b:
                raise UnpackError("the disc image is shorter than its file table says")
            size -= len(b)
            yield b
    return ents, read, f.close


def folder_source(path):
    """(entries, read, close) for the files in a game folder (names must fit an Xbox disc: Windows-1252 text)."""
    path = Path(path)
    ents = []
    for dp, dns, fns in os.walk(path):
        dns.sort(key=lambda x: x.lower())
        rel = Path(dp).relative_to(path).parts
        try:
            for d in dns:
                ents.append((tuple(x.encode("cp1252") for x in rel + (d,)), False, None, 0))
            for fn in sorted(fns, key=lambda x: x.lower()):
                fp = Path(dp, fn)
                ents.append((tuple(x.encode("cp1252") for x in rel + (fn,)), True, fp, fp.stat().st_size))
        except UnicodeEncodeError:
            raise UnpackError("a file or folder name has characters an Xbox disc can't hold (it must be plain Western text)") from None

    def read(fp, size, chunk=1 << 20):
        with open(fp, "rb") as f:
            while size > 0:
                b = f.read(min(chunk, size))
                if not b:
                    raise UnpackError(f"{Path(fp).name} is shorter than it was a moment ago")
                size -= len(b)
                yield b
    return ents, read, lambda: None


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_file_name(raw):
    """A name from a disc image or archive -> text that is safe to use as a file name on any system (no paths, no tricks)."""
    try:
        name = raw.decode("cp1252")
    except UnicodeDecodeError:
        raise UnpackError("a name in the archive isn't readable text") from None
    if (not name or name in (".", "..") or re.search(r'[\x00-\x1f/\\:*?"<>|]', name) or name[-1] in ". "
            or name.split(".")[0].upper() in _WIN_RESERVED):
        raise UnpackError(f"a name in this disc image can't be used as a file name on this computer: {name[:40]!r}")
    return name


def zar_source(path):
    z = ZarArchive(path)
    return list(z.entries()), z.read, z.close


def _rebuffer(gen, n):
    """Re-cut a stream of byte pieces into pieces of exactly n bytes (the last may be shorter)."""
    buf = bytearray()
    for piece in gen:
        buf += piece
        while len(buf) >= n:
            yield bytes(buf[:n])
            del buf[:n]
    if buf:
        yield bytes(buf)


def entries_match(a, b, progress=lambda x: None, stopped=lambda: False):
    """True when two sources (entries, read) hold the same folders and files with the same bytes. Names compare without case."""
    ea, ra = a
    eb, rb = b
    ma = {tuple(_upper(x) for x in p): (f, o, s) for p, f, o, s in ea}
    mb = {tuple(_upper(x) for x in p): (f, o, s) for p, f, o, s in eb}
    if ma.keys() != mb.keys():
        return False
    total = sum(v[2] for v in ma.values() if v[0]) or 1
    done = 0
    for k, (is_file, oa, sa) in ma.items():
        fb, ob, sb = mb[k]
        if fb != is_file or (is_file and sa != sb):
            return False
        if is_file and sa:
            for pa, pb in zip(_rebuffer(ra(oa, sa), 1 << 20), _rebuffer(rb(ob, sb), 1 << 20)):
                if pa != pb:
                    return False
                done += len(pa)
                progress(done / total)
                if stopped():
                    raise UnpackError("stopped")
    return True


# --------------------------------------------------------------------------- PlayStation 3: game folders and disc images
# A PS3 disc image (what ISO tools such as makeps3iso write, and what RPCS3 loads when it is decrypted) is an ISO 9660 volume with a Joliet
# name table (volume 'PS3VOLUME') in 2048-byte sectors, plus a small header in sector 0: a region table (which sectors are encrypted),
# the text 'PlayStation3' and the game ID. A game folder holds PS3_GAME (with PARAM.SFO), PS3_DISC.SFB and, on a real disc, PS3_UPDATE
# (a system update that no emulator uses). Nothing here decrypts anything: an image whose regions are still encrypted is recognised
# and said so, and the files that are plain are handled like any others.
PS3_SECTOR = 2048
PS3_EXTENT = 0xFFFFF800                   # the most one ISO 9660 record holds (a whole number of sectors); bigger files get several records
PS3_SPLIT_SECTORS = 0x1FFFE0              # a split image's parts: this many sectors (just under 4 GiB) each, named name.iso.0, name.iso.1, ...
PS3_REGIONS = {"U": "USA", "E": "Europe", "J": "Japan", "A": "Asia", "K": "Korea", "H": "Hong Kong"}
PS3_MAGIC = {".sprx": b"SCE\0", ".self": b"SCE\0", ".sfo": b"\0PSF", ".sfb": b".SFB", ".png": b"\x89PNG", ".pkg": b"\x7fPKG",
             ".edat": b"NPD\0", ".sdat": b"NPD\0", ".trp": b"\xdc\xa2\x4d\x00", ".at3": b"RIFF", ".jpg": b"\xff\xd8\xff"}


def read_sfo(data):
    """PARAM.SFO bytes -> {key: text or number}; {} when it isn't one."""
    try:
        if data[:4] != b"\0PSF":
            return {}
        _ver, kt, dt, n = struct.unpack("<IIII", data[4:20])
        out = {}
        for i in range(min(n, 512)):
            ko, fmt, ln, _mx, do = struct.unpack("<HHIII", data[20 + 16 * i:36 + 16 * i])
            k = data[kt + ko:data.index(b"\0", kt + ko)].decode("ascii", "replace")
            raw = data[dt + do:dt + do + ln]
            out[k] = struct.unpack("<I", raw[:4])[0] if fmt == 0x0404 and len(raw) >= 4 else raw.split(b"\0")[0].decode("utf-8", "replace")
        return out
    except (struct.error, ValueError):
        return {}


def ps3_sfo_info(sfo):
    """What a PARAM.SFO says about a game -> {'id': 'BLUS31077', 'title': ..., 'region': 'USA'}."""
    gid = str(sfo.get("TITLE_ID", "")).replace("-", "").strip()
    return {"id": gid, "title": str(sfo.get("TITLE", "")).strip(), "region": PS3_REGIONS.get(gid[2:3], "") if len(gid) >= 4 else ""}


def _child(path, name):
    """The entry of a folder called `name`, whatever its upper/lower case (Linux file systems care), or None."""
    try:
        for e in os.scandir(path):
            if e.name.upper() == name:
                return Path(e.path)
    except OSError:
        pass
    return None


def ps3_folder_info(path):
    """A PS3 game folder (it holds PS3_GAME with a PARAM.SFO) -> its game info, else None."""
    g = _child(path, "PS3_GAME")
    s = _child(g, "PARAM.SFO") if g is not None and g.is_dir() else None
    if s is None or not s.is_file():
        return None
    try:
        return ps3_sfo_info(read_sfo(s.read_bytes()[:1 << 20]))
    except OSError:
        return {"id": "", "title": "", "region": ""}


def psn_folder_info(path):
    """A game folder as RPCS3 installs a PSN download (PARAM.SFO and USRDIR at its top, no PS3_GAME) -> its game info, else None."""
    p = Path(path)
    if p.name.upper() == "PS3_GAME" or _child(p, "PS3_GAME") is not None:
        return None
    s = _child(p, "PARAM.SFO")
    u = _child(p, "USRDIR")
    if s is None or not s.is_file() or u is None or not u.is_dir():
        return None
    try:
        sfo = read_sfo(s.read_bytes()[:1 << 20])
    except OSError:
        return None
    info = ps3_sfo_info(sfo)
    info["category"] = str(sfo.get("CATEGORY", ""))
    if str(sfo.get("CATEGORY", "")) == "DG" or not re.fullmatch(r"[A-Z]{4}[0-9]{5}", info["id"]):
        return None
    return info


class _P3Parts:
    """One disc image that may be kept in several files (name.iso.0, name.iso.1, ...), read as if it were one."""
    def __init__(self, paths):
        self.files = [open(p, "rb") for p in paths]
        self.sizes = [os.fstat(f.fileno()).st_size for f in self.files]
        self.size = sum(self.sizes)
        self.pos = 0

    def seek(self, pos):
        self.pos = pos

    def read(self, n):
        out = []
        while n > 0 and self.pos < self.size:
            base = 0
            for f, s in zip(self.files, self.sizes):
                if self.pos < base + s:
                    f.seek(self.pos - base)
                    b = f.read(min(n, base + s - self.pos))
                    if not b:
                        return b"".join(out)
                    out.append(b)
                    self.pos += len(b)
                    n -= len(b)
                    break
                base += s
        return b"".join(out)

    def close(self):
        for f in self.files:
            f.close()


_SPLIT_NAME = re.compile(r"^(?P<pre>.+?)(?P<iso>\.iso)?\.(?P<n>666\d\d|\d{1,2})$", re.I)


def split_set(path):
    """A file that is one piece of a disc image kept in several files -> (the image's name without '.iso', [every piece in order], problem).
    The two ways pieces are named: 'Game.iso.0', 'Game.iso.1' ... (PS3 tools), and 'Game.66600', 'Game.66601' ... (multiMAN, which may
    also write 'Game.iso.66600'). problem is '' when the set is whole, else a plain sentence saying which piece is missing (pieces is then []).
    None for any other file."""
    p = Path(path)
    m = _SPLIT_NAME.match(p.name)
    if not m:
        return None
    pre, iso, n = m.group("pre"), m.group("iso") or "", m.group("n")
    mm = n.startswith("666") and len(n) == 5
    if not mm and not iso:                      # 'Game.0' alone could be anything; only 'Game.iso.0' is a piece
        return None
    shape = re.compile("^" + re.escape(pre + iso) + (r"\.666(\d\d)" if mm else r"\.(\d{1,2})") + "$", re.I)
    have = {}
    try:
        for e in os.scandir(p.parent):
            k = shape.match(e.name)
            if k and e.is_file():
                have[int(k.group(1))] = Path(e.path)
    except OSError:
        return None
    mine = int(n[3:]) if mm else int(n)
    if mine not in have:
        have[mine] = p
    top = max(have)
    gaps = [i for i in range(top + 1) if i not in have]
    label = (lambda i: f"{pre}{iso}.666{i:02d}") if mm else (lambda i: f"{pre}{iso}.{i}")
    if gaps:
        return pre, [], (f"the piece {label(gaps[0])} is missing, and a disc image can only be joined when every piece is there "
                         f"(found {len(have)} of {top + 1}; put all of the pieces in one folder)")
    return pre, [have[i] for i in range(top + 1)], ""


def split_first(path):
    """The first piece of the set a file belongs to (the file itself when the set can't be worked out)."""
    sp = split_set(path)
    return sp[1][0] if sp and sp[1] else Path(path)


def join_pieces(pieces, dest, prog, stopped, verify=False):
    """Copy the pieces one after the other into one file. With verify, the finished file is read back and compared with what was copied."""
    total = sum(os.path.getsize(x) for x in pieces) or 1
    span = 0.5 if verify else 1.0
    h, done = hashlib.sha1(), 0
    with open(dest, "wb") as g:
        for x in pieces:
            with open(x, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    g.write(b)
                    h.update(b)
                    done += len(b)
                    prog(done / total * span)
                    if stopped():
                        raise UnpackError("stopped")
    if os.path.getsize(dest) != total:
        raise UnpackError("the joined file came out a different size than its pieces together; it was discarded")
    if verify:
        h2, done = hashlib.sha1(), 0
        with open(dest, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                h2.update(b)
                done += len(b)
                prog(0.5 + done / total * 0.5)
                if stopped():
                    raise UnpackError("stopped")
        if h.digest() != h2.digest():
            raise UnpackError("the joined file didn't match its pieces when it was read back; it was discarded")


def _p3_open(src):
    return _P3Parts([Path(p) for p in ([src] if isinstance(src, (str, Path)) else src)])


def _p3_damaged():
    return UnpackError("this disc image's file system is damaged or cut short")


def _p3_quick(f):
    """True when the image has an ISO 9660 volume whose top folder holds PS3_GAME (reads two sectors, so non-PS3 images are dismissed fast)."""
    if f.size < 19 * PS3_SECTOR:
        return False
    f.seek(16 * PS3_SECTOR)
    d = f.read(PS3_SECTOR)
    if len(d) < PS3_SECTOR or d[0] != 1 or d[1:6] != b"CD001":
        return False
    lba, ln = struct.unpack("<I", d[158:162])[0], struct.unpack("<I", d[166:170])[0]
    if ln > 1 << 20 or (lba + 1) * PS3_SECTOR > f.size:
        return False
    f.seek(lba * PS3_SECTOR)
    t = f.read(max(ln, 0))
    off = 0
    while off < len(t):
        n = t[off]
        if n == 0:
            off = (off // PS3_SECTOR + 1) * PS3_SECTOR
            continue
        if t[off + 25] & 2 and t[off + 33:off + 33 + t[off + 32]].upper() == b"PS3_GAME":
            return True
        off += n
    return False


def _p3_parse(f):
    """The files of a PS3 disc image -> [(path parts as UTF-8 bytes, is_file, extents ((byte offset, length), ...), size)]. The Joliet
    name table is used when there is one (it keeps the real upper/lower case), else the plain ISO 9660 one."""
    size = f.size
    pvd = jol = None
    for i in range(16, 48):
        f.seek(i * PS3_SECTOR)
        d = f.read(PS3_SECTOR)
        if len(d) < PS3_SECTOR or d[1:6] != b"CD001":
            break
        if d[0] == 1 and pvd is None:
            pvd = d
        elif d[0] == 2 and jol is None and d[88:91] in (b"%/@", b"%/C", b"%/E"):
            jol = d
        elif d[0] == 255:
            break
    if pvd is None:
        raise UnpackError("this isn't a PS3 disc image (it has no ISO 9660 volume)")
    use, is_j = (jol or pvd), jol is not None
    entries, seen, visited = [], [0], set()

    def spell(nm):
        s = nm.decode("utf-16-be", "replace") if is_j else nm.decode("latin-1")
        s = re.sub(r";\d+$", "", s)
        if not is_j and len(s) > 1 and s.endswith("."):
            s = s[:-1]
        return s.encode("utf-8")

    def walk(lba, length, parts, depth):
        if depth > 64 or length > 1 << 28 or lba in visited or (lba * PS3_SECTOR + length) > size:
            raise _p3_damaged()
        visited.add(lba)
        f.seek(lba * PS3_SECTOR)
        d = f.read(length)
        if len(d) < length:
            raise _p3_damaged()
        off, acc, acc_name = 0, [], None
        while off < length:
            n = d[off]
            if n == 0:
                off = (off // PS3_SECTOR + 1) * PS3_SECTOR
                continue
            if n < 34 or off + n > length:
                raise _p3_damaged()
            flags, nl = d[off + 25], d[off + 32]
            ext, sz = struct.unpack("<I", d[off + 2:off + 6])[0], struct.unpack("<I", d[off + 10:off + 14])[0]
            nm = d[off + 33:off + 33 + nl]
            off += n
            if nm in (b"\0", b"\1"):
                continue
            seen[0] += 1
            if seen[0] > 2_000_000:
                raise UnpackError("this disc image has too many files")
            name = spell(nm)
            if flags & 2:
                entries.append((parts + (name,), False, (), 0))
                walk(ext, sz, parts + (name,), depth + 1)
                continue
            if sz and (ext * PS3_SECTOR + sz) > size:
                raise _p3_damaged()
            acc.append((ext * PS3_SECTOR, sz))
            if flags & 0x80:                                   # more pieces of this file follow
                acc_name = acc_name or name
                continue
            entries.append((parts + (acc_name or name,), True, tuple(acc), sum(s for _o, s in acc)))
            acc, acc_name = [], None

    rd = use[156:190]
    walk(struct.unpack("<I", rd[2:6])[0], struct.unpack("<I", rd[10:14])[0], (), 0)
    return entries, struct.unpack("<I", pvd[80:84])[0]


def _p3_read_extents(f, extents, size, chunk=1 << 20):
    left = size
    for off, ln in extents:
        f.seek(off)
        while ln > 0 and left > 0:
            b = f.read(min(chunk, ln, left))
            if not b:
                raise UnpackError("the disc image is shorter than its file table says")
            ln -= len(b)
            left -= len(b)
            yield b


def ps3_regions(head):
    """Sector 0 of a PS3 image -> [(first sector, last sector)] of the parts that are NOT encrypted, or None when it has no region table."""
    try:
        n = struct.unpack(">I", head[:4])[0]
        if not 0 < n <= 64:
            return None
        w = struct.unpack(">%dI" % (2 + 2 * n), head[:8 + 8 * n])
        return [(w[2 + 2 * i], w[3 + 2 * i]) for i in range(n)]
    except struct.error:
        return None


def ps3_encrypted_check(f, entries, total):
    """(looks encrypted, files checked, files that failed). Files that start inside an encrypted region should still begin with their
    format's marker (a program 'SCE', a PARAM.SFO '\\0PSF', a PNG ...); when most of those that can be checked don't, the region is
    still encrypted. An image without a region table is judged by the same markers on a sample of its files."""
    f.seek(0)
    plain = ps3_regions(f.read(PS3_SECTOR))
    if plain is not None:
        enc, pos = [], 0
        for a, b in sorted(plain):
            if a > pos:
                enc.append((pos, a - 1))
            pos = max(pos, b + 1)
        if pos < total:
            enc.append((pos, total - 1))
        if not enc:
            return False, 0, 0
    else:
        enc = [(0, total)]
    checked = bad = 0
    for parts, is_file, ext, sz in entries:
        if not is_file or not ext or sz < 4:
            continue
        name = parts[-1].decode("utf-8", "replace").lower()
        want = PS3_MAGIC.get(os.path.splitext(name)[1]) or (b"SCE\0" if name == "eboot.bin" else None)
        sector = ext[0][0] // PS3_SECTOR
        if not want or not any(a <= sector <= b for a, b in enc):
            continue
        f.seek(ext[0][0])
        checked += 1
        if f.read(len(want)) != want:
            bad += 1
        if checked >= 80:
            break
    return bool(checked and bad and bad * 2 >= checked), checked, bad


def ps3_iso_kind(path):
    """A PS3 disc image -> {'id', 'title', 'region', 'encrypted', 'checked', 'bad'}; None for anything else (a plain ISO, an Xbox disc...)."""
    try:
        f = _p3_open(path)
    except OSError:
        return None
    try:
        if not _p3_quick(f):
            return None
        entries, total = _p3_parse(f)
        info = {"id": "", "title": "", "region": ""}
        sfo = next((e for e in entries if e[1] and [x.upper() for x in e[0]] == [b"PS3_GAME", b"PARAM.SFO"]), None)
        if sfo is not None and sfo[3] < 1 << 20:
            info = ps3_sfo_info(read_sfo(b"".join(_p3_read_extents(f, sfo[2], sfo[3]))))
        enc, checked, bad = ps3_encrypted_check(f, entries, total)
        info.update(encrypted=enc, checked=checked, bad=bad)
        return info
    except (UnpackError, OSError, struct.error):
        return None
    finally:
        f.close()


def ps3_iso_source(path):
    """(entries, read, close) for the files inside a PS3 disc image (one file, or the parts of a split one)."""
    f = _p3_open(path)
    try:
        entries, _total = _p3_parse(f)
    except BaseException:
        f.close()
        raise

    def read(extents, size, chunk=1 << 20):
        return _p3_read_extents(f, extents, size, chunk)
    return entries, read, f.close


def ps3_folder_source(path, keep_update=False):
    """(entries, read, close) for the files of a PS3 game folder. PS3_UPDATE (a system update that no emulator uses) is left out
    unless keep_update. Names are kept as UTF-8."""
    path = Path(path)
    ents = []
    for dp, dns, fns in os.walk(path):
        rel = Path(dp).relative_to(path).parts
        dns.sort()
        shown = list(dns)                                    # the folder itself stays (empty, as on the tools' images); its contents don't
        if not rel and not keep_update:
            dns[:] = [d for d in dns if d.upper() != "PS3_UPDATE"]
        try:
            for d in shown:
                ents.append((tuple(x.encode("utf-8") for x in rel + (d,)), False, Path(dp, d), 0))
            for fn in sorted(fns):
                fp = Path(dp, fn)
                if fp.is_file():
                    ents.append((tuple(x.encode("utf-8") for x in rel + (fn,)), True, fp, fp.stat().st_size))
        except UnicodeEncodeError:
            raise UnpackError("a file or folder name in this game isn't valid text, so it can't be stored in a disc image") from None

    def read(fp, size, chunk=1 << 20):
        with open(fp, "rb") as f:
            while size > 0:
                b = f.read(min(chunk, size))
                if not b:
                    raise UnpackError(f"{Path(fp).name} is shorter than it was a moment ago")
                size -= len(b)
                yield b
    return ents, read, lambda: None


def fix_file_name(raw, enc="utf-8"):
    """A name from a disc image -> a name any computer accepts. Anything that would not do is changed to '_' (the caller says so)."""
    name = raw.decode(enc, "replace")
    fixed = re.sub(r'[\x00-\x1f/\\:*?"<>|\ufffd]', "_", name)
    if fixed in ("", ".", ".."):
        fixed = "_" * max(1, len(fixed))
    if fixed[-1] in ". ":
        fixed = fixed.rstrip(". ") + "_"
    if fixed.split(".")[0].upper() in _WIN_RESERVED:
        fixed = "_" + fixed
    return fixed


def fix_entry_names(ents, enc="utf-8"):
    """Entries [(path parts as bytes, is_file, src, size)] -> (the same with every name made usable on this computer, [(old, new)]).
    A name that can't be a file name here is changed (and listed), and two names that would collide (this computer ignores
    upper/lower case) are told apart with ~1, ~2."""
    mapped, taken, renamed, out = {(): ()}, set(), [], []
    for parts, is_file, src, size in ents:
        parent = mapped.get(parts[:-1])
        if parent is None:
            parent = tuple(fix_file_name(x, enc) for x in parts[:-1])
        nm = fix_file_name(parts[-1], enc)
        base, n = nm, 0
        while (parent, nm.lower()) in taken:
            n += 1
            stem, dot, ext = base.rpartition(".") if "." in base[1:] else (base, "", "")
            nm = f"{stem}~{n}{dot}{ext}"
        taken.add((parent, nm.lower()))
        if nm != parts[-1].decode(enc, "replace"):
            renamed.append((parts[-1].decode(enc, "replace"), nm))
        mapped[parts] = parent + (nm,)
        out.append((tuple(x.encode(enc) for x in parent + (nm,)), is_file, src, size))
    return out, renamed


def is_iso9660(path):
    """True when a file starts like an ISO 9660 disc image (a volume descriptor at sector 16)."""
    try:
        with open(path, "rb") as f:
            f.seek(16 * PS3_SECTOR)
            d = f.read(8)
        return d[:1] == b"\x01" and d[1:6] == b"CD001"
    except OSError:
        return False


# ---- PlayStation 3 packages (.pkg). Only the plain header is read here; the contents are opened by the user's own RPCS3, which holds
# whatever it needs for that. Nothing in this program decrypts a package.
RPCS3 = [""]                                   # the user's rpcs3.exe, when there is one
PKG_CONTENT = {4: "game data", 5: "game"}
PKG_OTHER = {9: "a theme", 11: "a widget", 13: "a licence file"}   # other kinds are named by their number


def pkg_info(path):
    """The plain header of a PS3 package -> {'path', 'content_id', 'title_id', 'size', 'files', 'patch', 'version', 'content_type'}.
    Raises ValueError with a plain sentence when the file is not a PS3 game package (or is cut short)."""
    path = Path(path)
    with open(path, "rb") as f:
        d = f.read(0x400)
    size = path.stat().st_size
    if len(d) < 0xC0 or d[:4] != b"\x7fPKG":
        raise ValueError("this isn't a PlayStation package (it doesn't start like one)")
    typ, plat, moff, mcnt, _msz, files, psz = struct.unpack(">HHIIIIQ", d[4:0x20])
    data_off, data_size = struct.unpack(">QQ", d[0x20:0x30])
    if plat != 1:
        raise ValueError("this is a package for a different PlayStation (a PSP or Vita one); only PlayStation 3 packages can be unpacked")
    if psz != size:
        raise ValueError("this package is damaged or cut short (the file isn't the size its header says); download it again")
    info = {"path": path, "size": size, "files": files, "patch": False, "version": 0, "content_type": 0, "type": typ,
            "data_offset": data_off, "data_size": data_size, "iv": bytes(d[0x70:0x80]),
            "content_id": d[0x30:0x54].split(b"\0")[0].decode("ascii", "replace")}
    off = moff
    for _ in range(min(mcnt, 32)):
        if off + 8 > len(d):
            break
        pid, ln = struct.unpack(">II", d[off:off + 8])
        body = d[off + 8:off + 8 + ln]
        if pid == 2 and ln >= 4:
            info["content_type"] = struct.unpack(">I", body[:4])[0]
        elif pid == 3 and ln >= 4:
            info["patch"] = bool(struct.unpack(">I", body[:4])[0] & 0x10)
        elif pid == 8 and ln >= 8:
            info["version"] = struct.unpack(">H", body[6:8])[0]
        elif pid == 0x0A and ln > 8:                        # an install folder of its own (extra content that doesn't go into the game's folder)
            info["install_dir"] = body[8:].split(b"\0")[0].decode("utf-8", "replace")
        off += 8 + ln
    cid = info["content_id"]
    info["dlc"] = info["content_type"] == 4                    # extra content for a game (installs into the game's own folder)
    info["title_id"] = cid[7:16] if len(cid) >= 16 and cid[6] == "-" else cid
    info["install_dir"] = info.get("install_dir") or info["title_id"]       # the folder under dev_hdd0/game it installs into (RPCS3: the game ID unless told otherwise)
    if not info["install_dir"] or info["install_dir"] in (".", "..") or re.search(r"[/\\]", info["install_dir"]):
        raise ValueError("this package names an install folder that isn't a plain folder name, so it was not trusted")
    if info["content_type"] not in PKG_CONTENT:
        what = PKG_OTHER.get(info["content_type"], f"a kind of package (type {info['content_type']})")
        raise ValueError(f"this package holds {what}, not a PlayStation 3 game or its update, so there is no game folder to unpack")
    return info


PKG_KEY = [None]                               # the user's own package key (16 bytes), only ever from what they pasted or chose


class PkgKeyError(UnpackError):
    """The key given doesn't open this package."""


def parse_hex_key(data):
    """A key given as 32 hexadecimal characters (spaces, colons, 0x allowed) or as a file of exactly 16 bytes -> 16 bytes, or None."""
    if isinstance(data, (bytes, bytearray)) and len(data) == 16:
        return bytes(data)
    text = data.decode("ascii", "ignore") if isinstance(data, (bytes, bytearray)) else str(data)
    text = re.sub(r"0x|[\s:\-]", "", text.strip(), flags=re.I)
    return bytes.fromhex(text) if re.fullmatch(r"[0-9a-fA-F]{32}", text) else None


def aes_ecb(key, decrypt=False):
    """-> a function that AES-128-ECB encrypts (or, with decrypt=True, decrypts) data (a multiple of 16 bytes, no padding), made with what this computer already has
    (the 'cryptography' package if installed, Windows' own bcrypt, or the system's OpenSSL). Nothing is bundled."""
    import ctypes
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        c = Cipher(algorithms.AES(key), modes.ECB())
        return (c.decryptor() if decrypt else c.encryptor()).update
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        bc = ctypes.WinDLL("bcrypt")
        alg, hk = ctypes.c_void_p(), ctypes.c_void_p()
        mode = "ChainingModeECB"
        if bc.BCryptOpenAlgorithmProvider(ctypes.byref(alg), "AES", None, 0) or \
                bc.BCryptSetProperty(alg, "ChainingMode", mode, (len(mode) + 1) * 2, 0) or \
                bc.BCryptGenerateSymmetricKey(alg, ctypes.byref(hk), None, 0, bytes(key), len(key), 0):
            raise UnpackError("Windows' own encryption code (bcrypt) didn't start, so the package can't be opened here")

        def enc_nt(data):
            out, got = ctypes.create_string_buffer(len(data)), ctypes.c_ulong(0)
            if (bc.BCryptDecrypt if decrypt else bc.BCryptEncrypt)(hk, bytes(data), len(data), None, None, 0, out, len(data), ctypes.byref(got), 0):
                raise UnpackError("Windows' own encryption code (bcrypt) failed")
            return out.raw[:got.value]
        return enc_nt
    import ctypes.util
    name = ctypes.util.find_library("crypto")
    if not name:
        raise UnpackError("this computer has no AES library the program can use (on Linux install OpenSSL, or Python's 'cryptography' package)")
    lib = ctypes.CDLL(name)
    lib.EVP_CIPHER_CTX_new.restype = ctypes.c_void_p
    lib.EVP_aes_128_ecb.restype = ctypes.c_void_p
    ctx = ctypes.c_void_p(lib.EVP_CIPHER_CTX_new())
    if not (lib.EVP_DecryptInit_ex if decrypt else lib.EVP_EncryptInit_ex)(ctx, ctypes.c_void_p(lib.EVP_aes_128_ecb()), None, bytes(key), None):
        raise UnpackError("the system's OpenSSL didn't start, so the package can't be opened here")
    lib.EVP_CIPHER_CTX_set_padding(ctx, 0)

    def enc_ssl(data):
        out, n = ctypes.create_string_buffer(len(data) + 16), ctypes.c_int(0)
        if not (lib.EVP_DecryptUpdate if decrypt else lib.EVP_EncryptUpdate)(ctx, out, ctypes.byref(n), bytes(data), len(data)):
            raise UnpackError("the system's OpenSSL failed")
        return out.raw[:n.value]
    return enc_ssl


def ctr_keystream(enc, iv, start, length):
    """The bytes of an AES-CTR keystream (counter = iv + block number, 128 bits, big endian) for stream positions start .. start+length."""
    from array import array
    first, skip = divmod(start, 16)
    n = (skip + length + 15) // 16
    c0 = (int.from_bytes(iv, "big") + first) % (1 << 128)
    hi0, lo0 = c0 >> 64, c0 & ((1 << 64) - 1)
    buf = bytearray(16 * n)
    if lo0 + n <= (1 << 64):                              # the usual case: only the low half counts
        hb = hi0.to_bytes(8, "big")
        for k in range(8):
            buf[k::16] = hb[k:k + 1] * n
        lo = array("Q", range(lo0, lo0 + n))
        if sys.byteorder == "little":
            lo.byteswap()
        lb = lo.tobytes()
        for k in range(8):
            buf[8 + k::16] = lb[k::8]
    else:                                                 # the counter carries into the high half
        for i in range(n):
            buf[16 * i:16 * i + 16] = ((c0 + i) % (1 << 128)).to_bytes(16, "big")
    return enc(bytes(buf))[skip:skip + length]


def _xor(a, b):
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def pkg_source(info, key):
    """(entries, read, close) for the files of a retail PS3 package, opened with the user's own key. entries are
    [(path parts, is_file, (offset, overwrite flag), size)]; read(src, size) yields the decrypted bytes."""
    if info["type"] != 0x8000:
        raise UnpackError("this is a debug package, not a retail one; only retail packages can be unpacked with a key")
    enc = aes_ecb(key)
    f = open(info["path"], "rb")
    dsize, base, iv = info["data_size"], info["data_offset"], info["iv"]

    def stream(pos, n):
        f.seek(base + pos)
        data = f.read(n)
        if len(data) != n:
            raise UnpackError("this package is cut short")
        return _xor(data, ctr_keystream(enc, iv, pos, n)) if n else b""

    try:
        count = info["files"]
        if not 0 < count < 1_000_000 or count * 32 > dsize:
            raise PkgKeyError("this package's file table is damaged")
        table = stream(0, count * 32)
        ents = []
        for i in range(count):
            name_off, name_size, file_off, file_size, etype, _pad = struct.unpack(">IIQQII", table[32 * i:32 * i + 32])
            if name_size > 4096 or name_off + name_size > dsize or file_off + file_size > dsize:
                raise PkgKeyError("that key doesn't open this package (its file list came out as nonsense)")
            raw = stream(name_off, name_size).rstrip(b"\0")
            try:
                name = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise PkgKeyError("that key doesn't open this package (its file names came out as nonsense)") from None
            if not name or any(ord(c) < 32 for c in name):
                raise PkgKeyError("that key doesn't open this package (its file names came out as nonsense)")
            parts = tuple(x.encode("utf-8") for x in name.replace("\\", "/").split("/") if x)
            is_file = (etype & 0xFF) != 4
            ents.append((parts, is_file, (file_off, bool(etype & 0x80000000)), file_size if is_file else 0))
    except BaseException:
        f.close()
        raise

    def read(src, size, chunk=1 << 20):
        pos = src[0]
        while size > 0:
            n = min(chunk, size)
            yield stream(pos, n)
            pos += n
            size -= n
    return ents, read, f.close


PKG_TRAILER = 96                               # the bytes real packages end with (a checksum and a signature field; only the checksum is filled here)
PKG_REGION = {"U": "UP", "E": "EP", "J": "JP", "H": "HP", "A": "HP", "K": "KP"}


def pkg_content_id(title_id, title=""):
    """A content ID in the usual shape (UP9000-NPUB31582_00-LABEL16CHARACTERS) for the game with this ID."""
    label = re.sub(r"[^A-Z0-9]", "", title.upper())[:16].ljust(16, "0")
    return f"{PKG_REGION.get(title_id[2:3], 'UP')}9000-{title_id}_00-{label}"


def build_pkg(ents, read, dest, key, content_id, prog, stopped, content_type=5, install_dir=""):
    """Entries [(path parts as bytes, is_file, src, size)] (a PSN-style game folder: PARAM.SFO and USRDIR at the top) -> a retail-layout
    package at dest, sealed with the user's own key. For RPCS3: a console would also want Sony's signature, which nobody can make."""
    enc = aes_ecb(key)
    iv = os.urandom(16)
    n = len(ents)
    names = [b"/".join(parts) for parts, _f, _s, _z in ents]
    table_len = 32 * n
    name_offs, pos = [], table_len
    for nm in names:
        name_offs.append(pos)
        pos += len(nm) + (-len(nm)) % 16
    pos += (-pos) % 16
    file_offs = []
    for _parts, is_file, _src, size in ents:
        file_offs.append(pos)
        if is_file:
            pos += size + (-size) % 16
    data_size = pos
    table = bytearray()
    for i, (parts, is_file, _src, size) in enumerate(ents):
        table += struct.pack(">IIQQII", name_offs[i], len(names[i]) + (-len(names[i])) % 16, file_offs[i] if is_file else 0, size if is_file else 0,
                             3 | 0x80000000 if is_file else 4, 0)
    head_data = bytes(table) + b"".join(nm + bytes((-len(nm)) % 16) for nm in names)
    head_data += bytes((-len(head_data)) % 16)
    meta = b"".join(struct.pack(">II", pid, len(v)) + v for pid, v in (
        (1, struct.pack(">I", 2)), (2, struct.pack(">I", content_type)), (3, struct.pack(">I", 0x4e)), (4, struct.pack(">Q", data_size)),
        (5, bytes.fromhex("15000100")), (8, bytes.fromhex("8102500001000100")), (9, bytes(8))))
    if install_dir:                                       # extra content that installs into a folder of its own
        meta += struct.pack(">II", 0x0A, 8 + len(install_dir.encode()) + 1) + bytes(8) + install_dir.encode() + b"\0"
    meta += bytes(64)                                      # (real packages keep a 64-byte signature block after the packets)
    data_off = 0xC0 + len(meta)
    total = data_off + data_size + PKG_TRAILER
    head = bytearray(0xC0)
    head[0:4] = b"\x7fPKG"
    struct.pack_into(">HHIIIIQQQ", head, 4, 0x8000, 1, 0xC0, 8 if install_dir else 7, len(meta), n, total, data_off, data_size)
    cid = content_id.encode("ascii")
    head[0x30:0x30 + len(cid)] = cid
    head[0x70:0x80] = iv
    sha = hashlib.sha1()
    done, stream = 0, 0
    with open(dest, "wb") as g:
        def put(b):
            g.write(b)
            sha.update(b)
        put(bytes(head) + meta)

        def seal(plain):
            nonlocal stream
            put(_xor(plain, ctr_keystream(enc, iv, stream, len(plain))))
            stream += len(plain)
        seal(head_data)
        for (parts, is_file, src, size), off in zip(ents, file_offs):
            if not is_file:
                continue
            got = 0
            for chunk in read(src, size):
                seal(chunk)
                got += len(chunk)
                done += len(chunk)
                prog(done / max(1, sum(z for _p, f, _s, z in ents if f)))
                if stopped():
                    raise UnpackError("stopped")
            if got != size:
                raise UnpackError("a file changed while the package was being made")
            pad = (-size) % 16
            if pad:
                seal(bytes(pad))
        if stream != data_size:
            raise UnpackError("the package came out the wrong size; it was discarded")
        g.write(sha.digest() + bytes(PKG_TRAILER - 20))


def find_rpcs3(configured=""):
    """The user's rpcs3.exe: the path set in Settings, or next to this program. Nothing else is searched."""
    cands = [Path(configured)] if configured else []
    cands += [HERE / "rpcs3.exe", HERE / "rpcs3" / "rpcs3.exe", HERE / "RPCS3" / "rpcs3.exe"]
    if os.name != "nt":                         # this way of using RPCS3 needs its Windows program folder
        return ""
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            pass
    return ""


def rpcs3_workdir(exe, work):
    """A throwaway copy of RPCS3's program files (nothing of the user's games, settings or library) in the folder work, so an install
    there leaves the real RPCS3 untouched. Files are linked where the drive allows it, else copied."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    (work / "log").mkdir(exist_ok=True)         # RPCS3 won't make its log folder itself, and then it has no log to say what it did
    src = Path(exe).parent
    for f in sorted(src.iterdir()):
        if f.is_file() and (f.suffix.lower() == ".dll" or f.name.lower() == "rpcs3.exe"):
            dst = work / f.name
            try:
                os.link(f, dst)
            except OSError:
                shutil.copy2(f, dst)
    return work / "rpcs3.exe"


# ---- Nintendo Switch: NSP <-> XCI (the user's own keys; none are shipped)
SWITCH_KEYS = [None]                           # {name: bytes} from the user's own prod.keys, only the few names this needs
SW_KEY_RE = re.compile(r"(header_key|aes_kek_generation_source|aes_key_generation_source|titlekek_source|xci_header_key|"
                       r"key_area_key_(?:application|ocean|system)_source|master_key_[0-9a-f]{2})")
SW_KEY_LEN = {"header_key": 32}
NCA_HEAD = 0xC00
SW_SIG_BODY = {0x10000: 0x240, 0x10001: 0x140, 0x10002: 0x80, 0x10003: 0x240, 0x10004: 0x140, 0x10005: 0x80}


class SwitchKeyError(UnpackError):
    """The keys given are missing something, or don't open this game."""


def parse_switch_keys(data):
    """The text of a prod.keys file (or the part of it the person copied) -> {name: bytes} of just the keys this program uses, or None."""
    text = data.decode("utf-8", "ignore") if isinstance(data, (bytes, bytearray)) else str(data)
    keys = {}
    for line in text.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*[=:]\s*([0-9A-Fa-f]+)\s*$", line)
        if not m:
            continue
        name, val = m.group(1).lower(), m.group(2)
        if SW_KEY_RE.fullmatch(name) and len(val) == 2 * SW_KEY_LEN.get(name, 16):
            keys[name] = bytes.fromhex(val)
    return keys or None


def switch_keys_text(keys):
    """The keys as the 'name=hex' lines that are kept in the settings file."""
    return "\n".join(f"{k}={v.hex()}" for k, v in sorted(keys.items())) if keys else ""


# the Switch firmware each master key goes with (master_key_00, _01, ...; checked against the Switchbrew NCA page's KeyGeneration table, 10 Sep 2026, where the generation is the key number + 1); a key past the end of this list is from a newer firmware than this program knows
SW_FW = ["1.0.0", "3.0.0", "3.0.1", "4.0.0", "5.0.0", "6.0.0", "6.2.0", "7.0.0", "8.1.0", "9.0.0", "9.1.0", "12.1.0", "13.0.0", "14.0.0", "15.0.0",
         "16.0.0", "17.0.0", "18.0.0", "19.0.0", "20.0.0", "21.0.0", "22.0.0", "23.0.0"]


def sw_fw(idx):
    return SW_FW[idx] if 0 <= idx < len(SW_FW) else (f"newer than {SW_FW[-1]}" if idx >= len(SW_FW) else "none")


def sw_level(keys):
    """The newest master key the person's keys have (counting up from master_key_00 without a gap), or -1."""
    i = -1
    while keys and f"master_key_{i + 1:02x}" in keys:
        i += 1
    return i


def sw_fw_message(name, idx, level):
    return (f"{name} was made for Switch firmware {sw_fw(idx)} or newer, but your keys only go as far as firmware {sw_fw(level)}"
            if level >= 0 else f"{name} needs master keys, and your keys have none") + \
        f", so it can't be converted. Use a prod.keys from a console that is updated to firmware {sw_fw(idx)} or newer, then try again " \
        "(or leave that file out)"


def sw_need(keys, *names):
    for n in names:
        if re.fullmatch(r"master_key_[0-9a-f]{2}", n) and (not keys or n not in keys):
            raise SwitchKeyError(sw_fw_message("this game", int(n[-2:], 16), sw_level(keys)))
    miss = [n for n in names if not keys or n not in keys]
    if miss:
        raise SwitchKeyError("your keys are missing " + ", ".join(miss[:4]) + (" and more" if len(miss) > 4 else "") +
                             ". Use a prod.keys that is complete and recent enough for this game")


def sw_xts(key32, data, sector0=0, decrypt=True):
    """AES-128-XTS as the Switch does it (sectors of 0x200, the sector number counted big-endian)."""
    k1, k2 = key32[:16], key32[16:]
    e2, ek = aes_ecb(k2), aes_ecb(k1, decrypt)
    mask, out = (1 << 128) - 1, bytearray()
    for s in range(len(data) // 0x200):
        t = int.from_bytes(e2((sector0 + s).to_bytes(16, "big")), "little")
        tw = bytearray(0x200)
        for j in range(32):
            tw[16 * j:16 * j + 16] = t.to_bytes(16, "little")
            t = ((t << 1) & mask) ^ (0x87 if t >> 127 else 0)
        out += _xor(ek(_xor(data[0x200 * s:0x200 * s + 0x200], bytes(tw))), bytes(tw))
    return bytes(out)


def sw_cbc_encrypt(key, iv, data):
    enc, prev, out = aes_ecb(key), iv, bytearray()
    for i in range(0, len(data), 16):
        prev = enc(_xor(data[i:i + 16], prev))
        out += prev
    return bytes(out)


def sw_master(keys, index):
    return keys.get(f"master_key_{index:02x}")


def sw_title_kek(keys, rev):
    """The key a ticket's title key is locked with. `rev` is the generation number the ticket carries (13 means master_key_0c)."""
    idx = max(rev - 1, 0)
    sw_need(keys, f"master_key_{idx:02x}", "titlekek_source")
    return aes_ecb(sw_master(keys, idx), True)(keys["titlekek_source"])


def sw_area_key(keys, gen, slot):
    """The key an NCA's key area is locked with: generation (from its header), and which of the three families (0 application, 1 ocean, 2 system)."""
    idx = max(gen - 1, 0)
    name = ("key_area_key_application_source", "key_area_key_ocean_source", "key_area_key_system_source")[slot if slot < 3 else 0]
    sw_need(keys, f"master_key_{idx:02x}", "aes_kek_generation_source", "aes_key_generation_source", name)
    kek = aes_ecb(sw_master(keys, idx), True)(keys["aes_kek_generation_source"])
    return aes_ecb(aes_ecb(kek, True)(keys[name]), True)(keys["aes_key_generation_source"])


def pfs0_list(f, base=0):
    """[(name, offset in file, size)] of a PFS0 (an NSP is one) that starts at `base`."""
    f.seek(base)
    head = f.read(16)
    if len(head) < 16 or head[:4] != b"PFS0":
        raise ValueError("isn't a Switch package (no PFS0 header)")
    n, strsz = struct.unpack("<II", head[4:12])
    if not 0 < n < 100000 or strsz > 1 << 24:
        raise ValueError("has a damaged file list")
    ents = f.read(24 * n)
    strs = f.read(strsz)
    if len(ents) < 24 * n or len(strs) < strsz:
        raise ValueError("is cut short")
    data0 = base + 16 + 24 * n + strsz
    out = []
    for i in range(n):
        off, size, so, _r = struct.unpack("<QQII", ents[24 * i:24 * i + 24])
        end = strs.find(b"\0", so)
        out.append((strs[so:end if end >= 0 else None].decode("utf-8", "replace"), data0 + off, size))
    return out


def hfs0_list(f, base=0):
    """[(name, offset in file, size, hashed size)] of an HFS0 that starts at `base`."""
    f.seek(base)
    head = f.read(16)
    if len(head) < 16 or head[:4] != b"HFS0":
        raise ValueError("isn't a Switch cartridge image (no HFS0 header)")
    n, strsz = struct.unpack("<II", head[4:12])
    if not 0 <= n < 100000 or strsz > 1 << 24:
        raise ValueError("has a damaged file list")
    ents = f.read(64 * n)
    strs = f.read(strsz)
    if len(ents) < 64 * n or len(strs) < strsz:
        raise ValueError("is cut short")
    data0 = base + 16 + 64 * n + strsz
    out = []
    for i in range(n):
        off, size, so, hs = struct.unpack("<QQII", ents[64 * i:64 * i + 24])
        end = strs.find(b"\0", so)
        out.append((strs[so:end if end >= 0 else None].decode("utf-8", "replace"), data0 + off, size, hs))
    return out


def xci_files(path):
    """[(name, offset, size)] of the files in the 'secure' part of an XCI (the game's own files), found from its header."""
    with open(path, "rb") as f:
        head = f.read(0x200)
        if len(head) < 0x200 or head[0x100:0x104] != b"HEAD":
            raise ValueError("isn't a Switch cartridge image (no HEAD mark)")
        root = struct.unpack("<Q", head[0x130:0x138])[0]
        if root < 0x200 or root > 0x100000:
            raise ValueError("has a damaged header")
        for name, off, size, _h in hfs0_list(f, root):
            if name == "secure":
                return [(n, o, s) for n, o, s, _ in hfs0_list(f, off)]
    raise ValueError("has no 'secure' part, so there is no game in it")


def xci_gen(path, keys):
    """The newest firmware an XCI's files were made for (a master key number), or None when that can't be read."""
    if not keys or "header_key" not in keys:
        return None
    best = -1
    try:
        with open(path, "rb") as f:
            for n, o, s in xci_files(path):
                if n.lower().endswith(".nca"):
                    best = max(best, sw_nca_head(f, o, keys)[0]["gen"] - 1)
    except (ValueError, OSError, SwitchKeyError):
        return None
    return best


def sw_tik(data):
    """A ticket -> (rights id, title key (encrypted), master key revision, type). Raises for a ticket tied to one console."""
    if len(data) < 0x170:
        raise ValueError("a ticket is cut short")
    st = struct.unpack("<I", data[:4])[0]
    b = SW_SIG_BODY.get(st)
    if b is None or len(data) < b + 0x170:
        raise ValueError("a ticket is in a form this program doesn't know")
    if data[b + 0x141] != 0:
        raise SwitchKeyError("this game's ticket belongs to one particular console (it is a personalised ticket), so its key can't be opened here")
    return bytes(data[b + 0x160:b + 0x170]), bytes(data[b + 0x40:b + 0x50]), data[b + 0x145], data[b + 0x141]


def sw_nca_head(f, off, keys):
    """An NCA's header opened with the user's header key -> a dict, and the decrypted 0xC00 bytes."""
    f.seek(off)
    raw = f.read(NCA_HEAD)
    if len(raw) < NCA_HEAD:
        raise ValueError("an NCA file is cut short")
    sw_need(keys, "header_key")
    h = sw_xts(keys["header_key"], raw)
    if h[0x200:0x204] not in (b"NCA3", b"NCA2"):
        raise SwitchKeyError("that header key doesn't open this game (its files came out as nonsense). Check the keys are complete and recent enough")
    gen = max(h[0x206], h[0x220])
    return {"dist": h[0x204], "ctype": h[0x205], "gen": gen, "slot": h[0x207], "size": struct.unpack("<Q", h[0x208:0x210])[0],
            "tid": struct.unpack("<Q", h[0x210:0x218])[0], "rights": bytes(h[0x230:0x240]), "titlekey": any(h[0x230:0x240])}, h


def sw_section_read(f, nca_off, h, idx, key, rel, n):
    """n bytes at `rel` inside section `idx` of an NCA, opened with the section's AES-CTR key (None if it isn't an AES-CTR section)."""
    start = struct.unpack("<I", h[0x240 + 0x10 * idx:0x244 + 0x10 * idx])[0] * 0x200
    fs = h[0x400 + 0x200 * idx:0x600 + 0x200 * idx]
    if fs[4] != 3 or not start:
        return None
    ab = start + rel
    f.seek(nca_off + ab)
    raw = f.read(n)
    iv = bytes(fs[0x140:0x148][::-1]) + bytes(8)
    return _xor(raw, ctr_keystream(aes_ecb(key), iv, ab, len(raw)))


def sw_pfs0_start(h, idx):
    """Where the PFS0 inside a section starts (relative to the section), or None."""
    fs = h[0x400 + 0x200 * idx:0x600 + 0x200 * idx]
    if fs[2] != 1 or fs[3] != 2:
        return None
    layers = struct.unpack("<I", fs[0x2C:0x30])[0]
    if not 2 <= layers <= 6:
        return None
    return struct.unpack("<Q", fs[0x30 + 16 * (layers - 1):0x38 + 16 * (layers - 1)])[0]


def sw_check_key(f, off, h, key):
    """True/False if the content key opens a PFS0 section of this NCA (its first bytes should read 'PFS0'); None if no section can be tested."""
    for idx in range(4):
        st = sw_pfs0_start(h, idx)
        if st is None:
            continue
        got = sw_section_read(f, off, h, idx, key, st, 4)
        if got is not None:
            return got == b"PFS0"
    return None


def sw_content_key(f, off, hd, h, keys, tiks):
    """The AES key that opens this NCA's data: the title key from its ticket, or the key from its own key area."""
    if hd["titlekey"]:
        t = tiks.get(hd["rights"])
        if t is None:
            raise SwitchKeyError("a file in this game needs a ticket that isn't in the package")
        rid, enc, rev, _ty = t
        return aes_ecb(sw_title_kek(keys, rev), True)(enc)
    return aes_ecb(sw_area_key(keys, hd["gen"], hd["slot"]), True)(h[0x300:0x340])[0x20:0x30]


def sw_meta(f, off, h, hd, key):
    """(title id, version, type, [content ids]) from the cnmt inside a meta NCA, or None. The content ids name the NCA files that belong to the title."""
    for idx in range(4):
        st = sw_pfs0_start(h, idx)
        if st is None:
            continue
        head = sw_section_read(f, off, h, idx, key, st, 0x10)
        if not head or head[:4] != b"PFS0":
            continue
        n, strsz = struct.unpack("<II", head[4:12])
        if not 0 < n < 64 or strsz > 0x1000:
            return None
        body = sw_section_read(f, off, h, idx, key, st + 16, 24 * n + strsz)
        for i in range(n):
            o, sz, so, _r = struct.unpack("<QQII", body[24 * i:24 * i + 24])
            nm = body[24 * n + so:].split(b"\0", 1)[0]
            if nm.endswith(b".cnmt") and sz >= 0x20:
                c = sw_section_read(f, off, h, idx, key, st + 16 + 24 * n + strsz + o, min(sz, 0x10000))
                ids = []
                ext, count = struct.unpack("<H", c[0xE:0x10])[0], struct.unpack("<H", c[0x10:0x12])[0]
                for k in range(count):
                    r = c[0x20 + ext + 0x38 * k:0x20 + ext + 0x38 * k + 0x38]
                    if len(r) == 0x38:
                        ids.append(r[0x20:0x30].hex())
                return struct.unpack("<Q", c[:8])[0], struct.unpack("<I", c[8:12])[0], c[12], ids
    return None


SW_KIND_RANK = {"base": 0, "": 0, "update": 1, "dlc": 2}


def nsp_info(path, keys=None):
    """What is in an NSP or an XCI. Without keys that's its files; with them also which titles it holds (the game, updates, extra content), their
    versions, and the firmware they were made for. -> dict (raises ValueError with a plain reason)."""
    path = Path(path)
    is_xci = path.suffix.lower() == ".xci"
    with open(path, "rb") as f:
        try:
            files = xci_files(path) if is_xci else pfs0_list(f)
        except ValueError as e:
            raise ValueError(f"this {e}") from None
        ncas = [x for x in files if x[0].lower().endswith(".nca")]
        if not ncas:
            raise ValueError("this has no game files in it (no .nca files)")
        tiks, tikfiles = {}, {}
        byname = {n.lower(): (n, o, sz) for n, o, sz in files}
        for nm, o, sz in files:
            if nm.lower().endswith(".tik") and sz < 0x1000:
                f.seek(o)
                try:
                    t = sw_tik(f.read(sz))
                    tiks[t[0]] = t
                    cert = byname.get(nm.lower()[:-4] + ".cert")
                    tikfiles[t[0]] = [(nm, o, sz)] + ([cert] if cert else [])
                except SwitchKeyError:
                    tiks[None] = None
                except ValueError:
                    pass
        info = {"path": path, "size": path.stat().st_size, "files": files, "ncas": ncas, "tiks": tiks, "tikfiles": tikfiles, "ticketed": bool(tiks),
                "xci": is_xci, "kind": "", "tid": 0, "base": 0, "version": 0, "gen_idx": None, "key_idx": None, "titles": []}
        fv = re.search(r"\[v(\d+)\]", path.name)
        fver = int(fv.group(1)) if fv else 0
        info["version"] = fver
        pseudo = {"kind": "", "tid": 0, "base": 0, "version": fver, "type": 0, "ncas": list(ncas), "rights": set(tiks) - {None}, "gen_idx": None, "key_idx": None}
        info["titles"] = [pseudo]
        if keys and "header_key" in keys:
            info["gen_idx"], info["key_idx"] = -1, -1
            heads, titles = {}, []
            for nm, o, sz in ncas:
                try:
                    hd, h = sw_nca_head(f, o, keys)
                except (SwitchKeyError, ValueError):
                    continue
                heads[nm.lower()] = (hd, h)
                info["gen_idx"] = max(info["gen_idx"], hd["gen"] - 1)
                if hd["titlekey"]:
                    info["key_idx"] = max(info["key_idx"], hd["gen"] - 1)
                if hd["ctype"] == 1:                    # a meta NCA: one title (the game, an update or extra content)
                    tid = hd["tid"]
                    low = tid & 0xFFF
                    t = {"kind": "base" if low == 0 else "update" if low == 0x800 else "dlc", "tid": tid, "version": 0, "type": 0, "ncas": [(nm, o, sz)],
                         "base": tid if low == 0 else (tid & ~0xFFF) if low == 0x800 else ((tid >> 12) - 1) << 12, "rights": set(), "ids": [],
                         "gen_idx": None, "key_idx": None}
                    try:
                        mt = sw_meta(f, o, h, hd, sw_content_key(f, o, hd, h, keys, tiks))
                    except (SwitchKeyError, ValueError, struct.error):
                        mt = None
                    if mt and mt[0] == tid:
                        t["version"], t["type"], t["ids"] = mt[1], mt[2], mt[3]
                    titles.append(t)
            if titles:
                by = {n.lower(): (n, o, sz) for n, o, sz in ncas}
                claimed = {t["ncas"][0][0].lower() for t in titles}
                for t in titles:
                    for cid in t["ids"]:
                        x = by.get(cid + ".nca") or by.get(cid + ".cnmt.nca")
                        if x and x[0].lower() not in claimed:
                            t["ncas"].append(x)
                            claimed.add(x[0].lower())
                titles.sort(key=lambda t: (SW_KIND_RANK[t["kind"]], t["tid"]))
                home = next((t for t in titles if t["kind"] == "base"), titles[0])
                for nm, o, sz in ncas:                  # anything no title lists stays with the game, so nothing is lost
                    if nm.lower() not in claimed:
                        home["ncas"].append((nm, o, sz))
                for t in titles:
                    g = [heads[n.lower()][0] for n, _o, _s in t["ncas"] if n.lower() in heads]
                    t["gen_idx"] = max([x["gen"] - 1 for x in g], default=-1)
                    t["key_idx"] = max([x["gen"] - 1 for x in g if x["titlekey"]], default=-1)
                    t["rights"] = {x["rights"] for x in g if x["titlekey"]}
                    if len(titles) == 1 and not t["version"]:
                        t["version"] = fver
                info["titles"] = titles
                info["kind"], info["tid"], info["base"], info["version"] = home["kind"], home["tid"], home["base"], home["version"]
    return info


def sw_reason(infos):
    """A short sentence for the list: what the item holds, and the firmware it was made for."""
    ts = [t for i in infos for t in i["titles"]]
    one_xci = len(infos) == 1 and infos[0]["xci"]
    if all(t["kind"] for t in ts):
        b, u, d = (sum(1 for t in ts if t["kind"] == k) for k in ("base", "update", "dlc"))
        extra = ([f"{u} update{'s' if u != 1 else ''}"] if u else []) + ([f"{d} extra content"] if d else [])
        text = "Switch game" + (" with " + " and ".join(extra) if extra else "") if b else "Switch " + " and ".join(extra)
        if one_xci:
            text += f" (cartridge image, {len(infos[0]['ncas'])} files)"
    else:
        text = f"Switch cartridge image ({len(infos[0]['ncas'])} files)" if one_xci else "Switch package"
    gens = [i["gen_idx"] for i in infos if i["gen_idx"] is not None]
    if gens and max(gens) >= 0:
        text += f", made for firmware {sw_fw(max(gens))}+"
    return text


def sw_select(infos, latest_only=True):
    """Which titles go into the result: the game, then updates, then extra content. When the same title is in more than one place (an update in an
    XCI and a newer one added beside it), the newest version is the one used; on a tie the one added last wins. -> (chosen [(info, title)], left out [(info, title)])."""
    best, order = {}, []
    for info in infos:
        for t in info["titles"]:
            key = (t["kind"], t["tid"]) if t["kind"] else ("", id(t))
            if not latest_only:
                key = key + (t["version"], id(t))
            old = best.get(key)
            if old is None:
                order.append(key)
            if old is None or t["version"] >= old[1]["version"]:
                best[key] = (info, t)
    chosen = sorted(best.values(), key=lambda x: (SW_KIND_RANK[x[1]["kind"]], x[1]["tid"], x[1]["version"]))
    keep = {id(t) for _i, t in chosen}
    return chosen, [(i, t) for i in infos for t in i["titles"] if id(t) not in keep]


def sw_plan(infos, keys, latest_only=True):
    """The NCA files that go into an XCI from NSP/XCI infos (game first, then update, then extra content) ->
    [(source path, name, offset, size, rewrite)] where rewrite is None (copied as it is) or (new 0xC00 header bytes). Raises SwitchKeyError."""
    chosen, _left = sw_select(infos, latest_only)
    plan, seen = [], set()
    level = sw_level(keys)
    for i, t in chosen:
        if i["ticketed"] and t["key_idx"] is not None and t["key_idx"] > level:
            raise SwitchKeyError(sw_fw_message(i["path"].name, t["key_idx"], level))
    for i, t in chosen:
        with open(i["path"], "rb") as f:
            for nm, o, sz in t["ncas"]:
                if nm in seen:
                    continue
                seen.add(nm)
                new = None
                if i["ticketed"]:
                    sw_need(keys, "header_key")
                    hd, h = sw_nca_head(f, o, keys)
                    if hd["titlekey"]:
                        tk_ = i["tiks"].get(hd["rights"])
                        if tk_ is None:
                            raise SwitchKeyError(f"{nm} needs a ticket that {i['path'].name} doesn't hold")
                        tk = aes_ecb(sw_title_kek(keys, tk_[2]), True)(tk_[1])
                        ok = sw_check_key(f, o, h, tk)
                        if ok is False:
                            raise SwitchKeyError("the title key from the ticket doesn't open the game's data, so these keys don't match this game")
                        hb = bytearray(h)
                        hb[0x230:0x240] = bytes(16)
                        hb[0x300:0x340] = aes_ecb(sw_area_key(keys, hd["gen"], hd["slot"]))(tk * 4)
                        new = sw_xts(keys["header_key"], bytes(hb), decrypt=False)
                plan.append((i["path"], nm, o, sz, new))
    return plan


def sw_title_files(info, t, seen=None):
    """[(name, size, source path, offset)] for one title as an NSP holds it: its NCA files, and its ticket and certificate if it came with them."""
    seen = set() if seen is None else seen
    out = []
    for nm, o, sz in t["ncas"]:
        if nm not in seen:
            seen.add(nm)
            out.append((nm, sz, info["path"], o))
    for r in (info["tikfiles"] if not t["kind"] else t["rights"]):
        for nm, o, sz in info["tikfiles"].get(r, []):
            if nm not in seen:
                seen.add(nm)
                out.append((nm, sz, info["path"], o))
    return out


def sw_nsp_files(infos, latest_only=True):
    """All the files of the chosen titles, for one NSP."""
    chosen, _left = sw_select(infos, latest_only)
    seen, out = set(), []
    for i, t in chosen:
        out += sw_title_files(i, t, seen)
    return out


def _digest(f, off, n, stopped, skip=0):
    import hashlib
    h = hashlib.sha256()
    f.seek(off + skip)
    left = n - skip
    while left > 0:
        if stopped():
            raise UnpackError("stopped")
        b = f.read(min(1 << 22, left))
        if not b:
            raise UnpackError("a file is cut short")
        h.update(b)
        left -= len(b)
    return h.digest()


def xci_verify(path, plan, prog, stopped):
    """Reads a finished XCI back: the same files, and the same data after each rewritten header (or the whole file where it wasn't rewritten)."""
    got = {n: (o, s) for n, o, s in xci_files(path)}
    if len(got) != len(plan) or any(r[1] not in got or got[r[1]][1] != r[3] for r in plan):
        raise UnpackError("the XCI that was built doesn't list the same files as the packages; it was discarded")
    total, done = max(1, sum(r[3] for r in plan)), 0
    with open(path, "rb") as x:
        for src, nm, off, size, new in plan:
            skip = len(new) if new else 0
            with open(src, "rb") as f:
                same = _digest(f, off, size, stopped, skip) == _digest(x, got[nm][0], size, stopped, skip)
                if new:
                    x.seek(got[nm][0])
                    same = same and x.read(len(new)) == new
            if not same:
                raise UnpackError(f"{nm} in the XCI that was built doesn't match its source; it was discarded")
            done += size
            prog(done / total)


def nsp_verify(path, files, prog, stopped):
    """Reads a finished NSP back against the files it was built from."""
    with open(path, "rb") as f:
        lst = pfs0_list(f)
        if [(n, s) for n, _o, s in lst] != [(n, s) for n, s, _p, _q in files]:
            raise UnpackError("the NSP that was built doesn't list the same files; it was discarded")
        total, done = max(1, sum(x[1] for x in files)), 0
        for (n, o, s), (_n, _s, src, so) in zip(lst, files):
            with open(src, "rb") as g:
                if _digest(f, o, s, stopped) != _digest(g, so, s, stopped):
                    raise UnpackError(f"{n} in the NSP that was built doesn't match its source; it was discarded")
            done += s
            prog(done / total)


def sw_gc_size(total):
    for code, size in ((0xFA, 1 << 30), (0xF8, 2 << 30), (0xF0, 4 << 30), (0xE0, 8 << 30), (0xE1, 16 << 30), (0xE2, 32 << 30)):
        if total <= size:
            return code
    return 0xE2


def hfs0_head(entries):
    """HFS0 header bytes for [(name, size, hashed size, sha256 of its first bytes)] laid end to end, padded to a multiple of 0x200."""
    strs, offs = b"", []
    for e in entries:
        offs.append(len(strs))
        strs += e[0].encode() + b"\0"
    strs += bytes(-(16 + 64 * len(entries) + len(strs)) % 0x200)       # the padding counts as part of the name table, so the header ends where the data starts
    body, pos = b"", 0
    for (nm, size, hs, dg), so in zip(entries, offs):
        body += struct.pack("<QQIIQ", pos, size, so, hs, 0) + dg
        pos += size
    return b"HFS0" + struct.pack("<III", len(entries), len(strs), 0) + body + strs


def build_xci(plan, dest, keys, prog=None, stopped=lambda: False):
    """Writes an XCI (a cartridge image holding the game's files) from a sw_plan. It is meant for emulators: the header's signature is not a real one."""
    import hashlib
    opened = {}

    def src(p):
        if p not in opened:
            opened[p] = open(p, "rb")
        return opened[p]
    try:
        members = []                                          # [(name, size, first bytes, plan row)]
        for p, nm, o, sz, new in plan:
            first = new[:0x200] if new else (src(p).seek(o), src(p).read(min(0x200, sz)))[1]
            members.append((nm, sz, first, (p, o, new)))
        sec = [(nm, sz, min(0x200, sz), hashlib.sha256(first[:0x200]).digest()) for nm, sz, first, _r in members]
        sechead = hfs0_head(sec)
        secsize = len(sechead) + sum(m[1] for m in members)
        empty = hfs0_head([])
        parts = [("update", empty), ("normal", empty), ("secure", sechead)]
        rooth = hfs0_head([(n, len(h) if n != "secure" else secsize, 0x200 if n != "secure" else len(sechead), hashlib.sha256(h[:len(h)]).digest())
                           for n, h in parts])
        total = 0xF000 + len(rooth) + len(empty) * 2 + secsize
        secstart = 0xF000 + len(rooth) + 2 * len(empty)
        iv = os.urandom(16)
        head = bytearray(0x200)
        head[0:0x100] = os.urandom(0x100)
        head[0x100:0x104] = b"HEAD"
        head[0x104:0x108] = struct.pack("<I", secstart // 0x200)
        head[0x108:0x10C] = b"\xff\xff\xff\xff"
        head[0x10D] = sw_gc_size(total)
        head[0x110:0x118] = bytes.fromhex("8750F4C0A9C5A966")
        head[0x118:0x120] = struct.pack("<Q", (total - 1) // 0x200)
        head[0x120:0x130] = iv
        head[0x130:0x138] = struct.pack("<Q", 0xF000)
        head[0x138:0x140] = struct.pack("<Q", len(rooth))
        head[0x140:0x160] = hashlib.sha256(rooth).digest()
        head[0x160:0x180] = hashlib.sha256(bytes(0x200)).digest()
        head[0x180:0x184] = struct.pack("<I", 1)
        head[0x184:0x188] = struct.pack("<I", 2)
        head[0x18C:0x190] = struct.pack("<I", secstart // 0x200)
        if keys and "xci_header_key" in keys:
            info = bytes.fromhex("0100000000000000" "88130000" "00110C00" "5a000200" "9bfb03ddbb7c5fca" "1608000000000001")
            info = info.ljust(0x70, bytes(1))
            head[0x190:0x200] = sw_cbc_encrypt(keys["xci_header_key"], iv[::-1], info)
        done, grand = 0, max(1, sum(m[1] for m in members))
        with open(dest, "wb") as out:
            out.write(bytes(head) + bytes(0x6E00) + b"\xff" * 0x8000)
            out.write(rooth + empty + empty + sechead)
            for nm, sz, first, (p, o, new) in members:
                f, left, pos = src(p), sz, 0
                if new:
                    out.write(new)
                    pos = len(new)
                    left -= pos
                    done += pos
                f.seek(o + pos)
                while left > 0:
                    if stopped():
                        raise UnpackError("stopped")
                    b = f.read(min(1 << 22, left))
                    if not b:
                        raise UnpackError(f"{nm} is cut short")
                    out.write(b)
                    left -= len(b)
                    done += len(b)
                    if prog:
                        prog(done / grand)
    finally:
        for f in opened.values():
            f.close()


def build_nsp(files, dest, prog=None, stopped=lambda: False):
    """Writes an NSP (the plain package format) from [(name, size, path, offset)]; the files are copied as they are."""
    strs, offs = b"", []
    for nm, *_r in files:
        offs.append(len(strs))
        strs += nm.encode() + b"\0"
    strs += bytes(-(16 + 24 * len(files) + len(strs)) % 0x20)
    head, pos = b"PFS0" + struct.pack("<III", len(files), len(strs), 0), 0
    for (nm, sz, _p, _o), so in zip(files, offs):
        head += struct.pack("<QQII", pos, sz, so, 0)
        pos += sz
    head += strs
    opened = {}
    done, grand = 0, max(1, sum(x[1] for x in files))
    try:
        with open(dest, "wb") as out:
            out.write(head)
            for nm, sz, p, o in files:
                if p not in opened:
                    opened[p] = open(p, "rb")
                f, left = opened[p], sz
                f.seek(o)
                while left > 0:
                    if stopped():
                        raise UnpackError("stopped")
                    b = f.read(min(1 << 22, left))
                    if not b:
                        raise UnpackError(f"{nm} is cut short")
                    out.write(b)
                    left -= len(b)
                    done += len(b)
                    if prog:
                        prog(done / grand)
    finally:
        for f in opened.values():
            f.close()


# ---- Switch 2: NUP, a community container for decrypted game folders (experimental)
# Written from the public draft "NUP - NX Package Format" 1.0-draft by a55uka (github.com/a55uka/nup). It holds a game's decrypted exefs and romfs
# folders (and optionally an update and extra content), each file stored raw or as one zstd frame, with a SHA-256 of every file and of the table.
# It uses no keys and does no decrypting. Nothing here has been tried in an emulator by the author.
NUP_KINDS = {0: "game", 1: "update", 2: "extra content"}


def nup_folder_ok(p):
    """True for a decrypted Switch game folder: exefs (with main or main.npdm) beside romfs."""
    p = Path(p)
    return p.is_dir() and (p / "exefs").is_dir() and (p / "romfs").is_dir() and ((p / "exefs" / "main").is_file() or (p / "exefs" / "main.npdm").is_file())


def npdm_console(b):
    """'Switch' or 'Switch 2' from the bytes of a main.npdm, or '' when it can't tell. The program's access-control block (ACID) starts with a
    signature: 0x200 bytes long on Switch 1 programs and 0x300 on Switch 2 ones, so its 'ACID' mark is found at a different place.
    (Checked on three Switch 1 programs and one Switch 2 game; a guess from that, not a documented rule.)"""
    if len(b) < 0x80 or b[:4] != b"META":
        return ""
    off = struct.unpack("<I", b[0x78:0x7C])[0]
    if b[off + 0x200:off + 0x204] == b"ACID":
        return "Switch"
    if b[off + 0x300:off + 0x304] == b"ACID":
        return "Switch 2"
    return ""


def nup_folder_console(pkgs):
    """The console a game folder's programs are for, read from the first main.npdm that says."""
    for x in pkgs:
        try:
            c = npdm_console((Path(x["root"]) / "exefs" / "main.npdm").read_bytes()[:0x2000])
        except OSError:
            c = ""
        if c:
            return c
    return ""


def nup_file_console(path):
    """The same for a .nup file, from the main.npdm stored in its first package's meta block ('' when it can't tell)."""
    try:
        pk, _files, _tree, _rf, close = nup_open(path)
        try:
            with open(path, "rb") as f:
                for x in pk:
                    mo, ms = x["meta"]
                    if ms >= 8 and mo:
                        f.seek(mo)
                        blk = f.read(ms)
                        o = 0
                        while o + 8 <= len(blk):
                            ty, _r, sz = struct.unpack("<HHI", blk[o:o + 8])
                            if ty == 1:
                                c = npdm_console(blk[o + 8:o + 8 + min(sz, 0x2000)])
                                if c:
                                    return c
                            o += 8 + sz
        finally:
            close()
    except (UnpackError, OSError, struct.error):
        pass
    return ""


def nup_npdm_id(folder):
    """The title id inside a folder's exefs/main.npdm (the ACI0 block), or None."""
    try:
        b = (Path(folder) / "exefs" / "main.npdm").read_bytes()[:0x2000]
        off = struct.unpack("<I", b[0x70:0x74])[0] if b[:4] == b"META" else -1
        if b[off:off + 4] != b"ACI0":
            off = b.find(b"ACI0")
        if off >= 0:
            tid = struct.unpack("<Q", b[off + 0x10:off + 0x18])[0]
            return tid or None
    except (OSError, struct.error):
        pass
    return None


def nup_title_id(folder):
    """The game's title id: from main.npdm, else from 16 hex digits in the folder's name; None when neither says."""
    tid = nup_npdm_id(folder)
    if tid:
        return tid
    m_ = re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}(?![0-9A-Fa-f])", Path(folder).name)
    return int(m_[0], 16) if m_ else None


def _zstd():
    try:
        import zstandard
    except ImportError:
        raise UnpackError("NUP files are compressed with zstd, which needs the 'zstandard' package") from None
    return zstandard


def nup_tree(folder):
    """The files under a folder -> [(path parts as str, absolute path, size)] in a stable order. Skips nothing but links."""
    out = []
    for dp, dn, fn in os.walk(folder):
        dn.sort()
        for f in sorted(fn):
            fp = Path(dp) / f
            if fp.is_symlink():
                continue
            out.append((tuple(fp.relative_to(folder).parts), fp, fp.stat().st_size))
    return out


def nup_content_ok(d):
    """A folder that holds a game's files: an exefs or romfs folder with at least one file in it."""
    d = Path(d)
    return d.is_dir() and any((d / x).is_dir() and any(f for _dp, _dn, fn in os.walk(d / x) for f in fn) for x in ("exefs", "romfs"))


def nup_bundle(p):
    """The packages in a game folder -> [{'kind', 'version', 'root', 'tid'}] or None. Either the layout the NUP tools use (base/, updates/<version>/,
    dlc/<16-digit id>/<n>/, each holding exefs and/or romfs), or a plain game folder (exefs beside romfs)."""
    p = Path(p)
    if not p.is_dir():
        return None
    pk = []
    if nup_content_ok(p / "base"):
        pk.append({"kind": 0, "version": 0, "root": p / "base", "tid": None})
    u = p / "updates"
    if u.is_dir():
        for d in sorted(os.listdir(u)):
            if re.fullmatch(r"\d+", d) and nup_content_ok(u / d):
                pk.append({"kind": 1, "version": int(d), "root": u / d, "tid": None})
    dl = p / "dlc"
    if dl.is_dir():
        for d in sorted(os.listdir(dl)):
            if re.fullmatch(r"[0-9A-Fa-f]{16}", d) and (dl / d).is_dir():
                for ix in sorted(os.listdir(dl / d)):
                    if nup_content_ok(dl / d / ix):
                        pk.append({"kind": 2, "version": 0, "root": dl / d / ix, "tid": int(d, 16)})
    if pk:
        return pk
    if nup_folder_ok(p):
        m_ = re.search(r"\[v(\d+)\]", p.name)
        return [{"kind": 0, "version": int(m_.group(1)) if m_ else 0, "root": p, "tid": None}]
    return None


def nup_resolve(pkgs, folder):
    """Works out each package's title id: the game's from its main.npdm (or the id in the folder's name), an update's from its own main.npdm
    (or the game's), extra content's from its folder name. -> None when all are known, else a plain sentence about what is missing."""
    base = next((x for x in pkgs if x["kind"] == 0), None)
    btid = nup_title_id(base["root"]) if base else None
    if btid is None:
        m_ = re.findall(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{16}(?![0-9A-Fa-f])", Path(folder).name)
        btid = int(m_[0], 16) if m_ else None
    for x in pkgs:
        if x["kind"] == 0:
            x["tid"] = btid
        elif x["kind"] == 1:
            own = nup_npdm_id(x["root"])
            x["tid"] = own or btid
    if any(x["tid"] is None for x in pkgs):
        return ("it can't tell which game this is; put the game's 16-digit title id in square brackets in the folder's name, "
                "like [0100ABCD12345000]")
    return None


def build_nup(pkgs, dest, prog=None, stopped=lambda: False, level=3):
    """Packages (from nup_bundle, ids from nup_resolve) -> a .nup: the game, its updates, its extra content, each package with its own tree of files.
    Writes the files first, then the table, then the superblock."""
    import hashlib
    z = _zstd()
    allf = []                                                # (package number, path parts, file, size)
    for pi, x in enumerate(pkgs):
        for parts, fp, size in nup_tree(x["root"]):
            allf.append((pi, parts, fp, size))
    if not allf:
        raise UnpackError("the folder has no files in it")
    dirs = {}
    for pi, x in enumerate(pkgs):
        dirs[(pi,)] = {"name": "", "dirs": set(), "files": []}
    for pi, parts, fp, size in allf:
        for k in range(1, len(parts)):
            key = (pi,) + parts[:k]
            if key not in dirs:
                dirs[key] = {"name": parts[k - 1], "dirs": set(), "files": []}
                dirs[key[:-1]]["dirs"].add(key)
        dirs[(pi,) + parts[:-1]]["files"].append((pi,) + parts)
    order = sorted(dirs, key=lambda k: (k[0], len(k), [s.encode() for s in k[1:]]))
    dix = {k: i for i, k in enumerate(order)}
    fix = {(pi,) + parts: i for i, (pi, parts, _fp, _sz) in enumerate(allf)}
    nd, nf = len(order), len(allf)
    pool, names = bytearray(), {}

    def intern(s):
        b = s.encode("utf-8")
        if b not in names:
            names[b] = len(pool)
            pool.extend(b)
        return names[b], len(b)
    dir_rec, children = [], []
    for k in order:
        d = dirs[k]
        kids = [(x[-1].encode(), dix[x]) for x in d["dirs"]] + [(f[-1].encode(), nd + fix[f]) for f in d["files"]]
        kids.sort()
        no, nl = intern(d["name"])
        dir_rec.append([no, nl, len(children), len(kids)])
        children += [i for _n, i in kids]
    file_names = [intern(parts[-1]) for _pi, parts, _fp, _sz in allf]
    has_base = any(x["kind"] == 0 for x in pkgs)
    npk = len(pkgs)
    pkg_off = 0x30
    dir_off = pkg_off + 0x40 * npk
    file_off = dir_off + 0x18 * nd
    child_off = file_off + 0x50 * nf
    pool_off = child_off + 4 * len(children)
    toc_end = pool_off + len(pool)
    pool_size = len(pool) + (-toc_end % 16)                  # the string pool carries the padding, so the table ends on a 16-byte boundary
    toc_size = pool_off + pool_size
    blob_off = 0x80 + toc_size
    blob_off += -blob_off % 16
    uuid = hashlib.sha256(b"".join(struct.pack("<QIB", x["tid"], x["version"], x["kind"]) for x in pkgs)).digest()[:16]
    ext = [None] * nf
    metas = []
    with open(dest, "wb") as out:
        out.seek(blob_off)
        for x in pkgs:                                       # each package's main.npdm goes in its meta block
            npdm = Path(x["root"]) / "exefs" / "main.npdm"
            if npdm.is_file() and npdm.stat().st_size < 1 << 20:
                nb = npdm.read_bytes()
                metas.append((out.tell(), 8 + len(nb)))
                out.write(struct.pack("<HHI", 1, 0, len(nb)) + nb)
                out.write(bytes(-out.tell() % 16))
            else:
                metas.append((0, 0))
        total, done = max(1, sum(f[3] for f in allf)), 0
        cctx = z.ZstdCompressor(level=level)
        for i, (pi, parts, fp, size) in enumerate(allf):
            if size == 0:
                ext[i] = (0, 0, hashlib.sha256(b"").digest(), 0)
                continue
            start = out.tell()
            h = hashlib.sha256()
            co = cctx.compressobj(size=size)
            with open(fp, "rb") as f:
                while True:
                    if stopped():
                        raise UnpackError("stopped")
                    b = f.read(1 << 20)
                    if not b:
                        break
                    h.update(b)
                    out.write(co.compress(b))
                    done += len(b)
                    if prog:
                        prog(done / total)
            out.write(co.flush())
            csize = out.tell() - start
            codec = 1
            if csize >= size:                                # no gain: store it as it is
                out.seek(start)
                out.truncate()
                with open(fp, "rb") as f:
                    while True:
                        b = f.read(1 << 22)
                        if not b:
                            break
                        out.write(b)
                csize, codec = size, 0
            out.write(bytes(-out.tell() % 16))
            ext[i] = (start, csize, h.digest(), codec)
        toc = bytearray()
        toc += b"TOC\x01" + struct.pack("<IQQQQQ", npk, nd, nf, pool_off, pool_size, 0)
        for pi, x in enumerate(pkgs):
            standalone = 1 if (x["kind"] == 1 and not has_base) else 0
            toc += struct.pack("<QIIIIBBHI", x["tid"], dix[(pi,)], dix.get((pi, "exefs"), 0xFFFFFFFF), dix.get((pi, "romfs"), 0xFFFFFFFF),
                               x["version"], x["kind"], standalone, 0, 0) + struct.pack("<QI", metas[pi][0], metas[pi][1]) + bytes(20)
        for no, nl, fc, cc in dir_rec:
            toc += struct.pack("<IIIIQ", no, nl, fc, cc, 0)
        for (pi, parts, fp, size), (no, nl), (eo, es, sh, codec) in zip(allf, file_names, ext):
            toc += struct.pack("<IIQQQ", no, nl, size, eo, es) + sh + struct.pack("<BBH", codec, 0, 0) + bytes(12)
        for c in children:
            toc += struct.pack("<I", c)
        toc += pool + bytes(pool_size - len(pool))
        assert len(toc) == toc_size, (len(toc), toc_size)
        end = out.seek(0, 2)
        sup = bytearray(0x80)
        sup[0:4] = b"NUP\x01"
        sup[4:0x18] = struct.pack("<HHQQ", 0x80, 1, 0x80, toc_size)
        sup[0x18:0x38] = hashlib.sha256(bytes(toc)).digest()
        sup[0x38:0x48] = struct.pack("<QQ", blob_off, end)
        sup[0x48:0x4C] = struct.pack("<I", npk)
        sup[0x4C:0x50] = struct.pack("<I", 0)
        sup[0x50:0x60] = uuid
        out.seek(0)
        out.write(bytes(sup) + bytes(toc))


def nup_open(path):
    """Reads and checks a .nup's tables. -> (packages, dirs, files, read_file) where packages are dicts and read_file(file index) yields the file's
    bytes in pieces, checked against the file's SHA-256 as it goes. Raises UnpackError with a plain reason."""
    import hashlib
    path = Path(path)
    size = path.stat().st_size
    f = open(path, "rb")
    try:
        sup = f.read(0x80)
        if len(sup) < 0x80 or sup[:4] != b"NUP\x01":
            raise UnpackError("this isn't a NUP file (or it is a version this program doesn't know)")
        hsize, hver, toc_off, toc_size = struct.unpack("<HHQQ", sup[4:0x18])
        blob_off, fsize, npk, flags = struct.unpack("<QQII", sup[0x38:0x50])
        if hsize < 0x80 or toc_off != hsize or fsize != size or flags or npk < 1 or blob_off < toc_off + toc_size or toc_size > 1 << 31:
            raise UnpackError("this NUP file is damaged or cut short (its header doesn't add up)")
        f.seek(toc_off)
        toc = f.read(toc_size)
        if len(toc) != toc_size or hashlib.sha256(toc).digest() != sup[0x18:0x38]:
            raise UnpackError("this NUP file is damaged (its table of files doesn't match its checksum)")
        if toc[:4] != b"TOC\x01":
            raise UnpackError("this NUP file is damaged (no table of files)")
        npk2, nd, nf, pool_off, pool_size = struct.unpack("<IQQQQ", toc[4:0x28])
        if npk2 != npk or 0x30 + 0x40 * npk + 0x18 * nd + 0x50 * nf > toc_size or pool_off + pool_size > toc_size:
            raise UnpackError("this NUP file is damaged (its table of files is the wrong size)")
        pool = toc[pool_off:pool_off + pool_size]

        def name(o, n):
            return pool[o:o + n].decode("utf-8", "replace")
        pk = []
        for i in range(npk):
            r = toc[0x30 + 0x40 * i:0x70 + 0x40 * i]
            tid, root, exe, rom, ver, kind, pf = struct.unpack("<QIIIIBB", r[:0x1A])
            mo, ms = struct.unpack("<QI", r[0x20:0x2C])
            pk.append({"title_id": tid, "root": root, "version": ver, "kind": kind, "flags": pf, "meta": (mo, ms)})
        d0 = 0x30 + 0x40 * npk
        dirs = [struct.unpack("<IIII", toc[d0 + 0x18 * i:d0 + 0x18 * i + 16]) for i in range(nd)]
        f0 = d0 + 0x18 * nd
        files = []
        for i in range(nf):
            r = toc[f0 + 0x50 * i:f0 + 0x50 * i + 0x50]
            no, nl, sz, eo, es = struct.unpack("<IIQQQ", r[:0x20])
            files.append({"name": name(no, nl), "size": sz, "off": eo, "esize": es, "sha": r[0x20:0x40], "codec": r[0x40]})
        c0 = f0 + 0x50 * nf
        nch = (pool_off - c0) // 4
        children = struct.unpack(f"<{nch}I", toc[c0:c0 + 4 * nch])
        dnames = [name(d[0], d[1]) for d in dirs]
        for fl in files:
            if fl["codec"] not in (0, 1):
                raise UnpackError("this NUP file uses a way of storing files that this program doesn't know yet")
            if fl["off"] + fl["esize"] > size or (fl["esize"] and fl["off"] < blob_off):
                raise UnpackError("this NUP file is damaged (a file points outside it)")
    except BaseException:
        f.close()
        raise
    if any(fl["codec"] == 1 for fl in files):
        _zstd()

    def read_file(i):
        fl = files[i]
        f.seek(fl["off"])
        left, h, got = fl["esize"], hashlib.sha256(), 0
        if fl["codec"] == 0:
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    raise UnpackError("this NUP file is cut short")
                h.update(b)
                left -= len(b)
                got += len(b)
                yield b
        else:
            do = _zstd().ZstdDecompressor().decompressobj()
            while left > 0:
                b = f.read(min(1 << 20, left))
                if not b:
                    raise UnpackError("this NUP file is cut short")
                left -= len(b)
                try:
                    out_ = do.decompress(b)
                except _zstd().ZstdError:
                    raise UnpackError(f"{fl['name']} in this NUP file is damaged (it can't be decompressed)") from None
                h.update(out_)
                got += len(out_)
                yield out_
        if got != fl["size"] or h.digest() != fl["sha"]:
            raise UnpackError(f"{fl['name']} in this NUP file is damaged (its checksum doesn't match)")

    def tree(pi):
        """[(path parts, is_file, file index or None)] of one package."""
        out_ = []

        def walk(d, prefix):
            first, cnt = dirs[d][2], dirs[d][3]
            for c in children[first:first + cnt]:
                if c < nd:
                    out_.append((prefix + (dnames[c],), False, None))
                    walk(c, prefix + (dnames[c],))
                else:
                    out_.append((prefix + (files[c - nd]["name"],), True, c - nd))
        walk(pk[pi]["root"], ())
        return out_
    return pk, files, tree, read_file, f.close


def nup_layout(pk):
    """Where each package's files go when a NUP is unpacked: a lone game goes straight into the folder; anything else uses the layout the NUP
    tools read back (base/, updates/<version>/, dlc/<id>/<n>/)."""
    if len(pk) == 1 and pk[0]["kind"] == 0:
        return [()]
    out, dlc = [], {}
    for p in pk:
        if p["kind"] == 0:
            out.append(("base",))
        elif p["kind"] == 1:
            out.append(("updates", str(p["version"])))
        else:
            k = dlc.get(p["title_id"], 0)
            dlc[p["title_id"]] = k + 1
            out.append(("dlc", f"{p['title_id']:016x}", str(k)))
    return out


def nup_verify(path, pkgs, prog, stopped):
    """Reads a finished NUP back: the same packages with the same files in the same places as the folders they came from, each file checked
    against its checksum."""
    pk, files, tree, read_file, close = nup_open(path)
    try:
        if len(pk) != len(pkgs):
            raise UnpackError("the NUP that was built doesn't hold the same packages as the folder; it was discarded")
        for pi, x in enumerate(pkgs):
            want = {tuple(p_): sz for p_, _fp, sz in nup_tree(x["root"])}
            got = {parts: files[fi]["size"] for parts, is_file, fi in tree(pi) if is_file}
            if want != got:
                raise UnpackError("the NUP that was built doesn't list the same files as the folder; it was discarded")
        total, done = max(1, sum(f["size"] for f in files)), 0
        for pi in range(len(pk)):
            for parts, is_file, fi in tree(pi):
                if is_file:
                    for b in read_file(fi):
                        if stopped():
                            raise UnpackError("stopped")
                        done += len(b)
                        prog(min(1.0, done / total))
    finally:
        close()


def nup_extract(path, dest, prog=None, stopped=lambda: False):
    """A .nup -> a folder (see nup_layout). Every file is checked against its checksum as it is written."""
    pk, files, tree, read_file, close = nup_open(path)
    dest = Path(dest)
    try:
        total, done = max(1, sum(f["size"] for f in files)), 0
        for pi, prefix in enumerate(nup_layout(pk)):
            base = dest.joinpath(*prefix)
            for parts, is_file, fi in tree(pi):
                if any(x in ("", ".", "..") or "/" in x or "\\" in x or ":" in x for x in parts):
                    raise UnpackError("this NUP file holds a file name that isn't safe to unpack, so nothing was written")
                tgt = base.joinpath(*parts)
                if not is_file:
                    tgt.mkdir(parents=True, exist_ok=True)
                    continue
                tgt.parent.mkdir(parents=True, exist_ok=True)
                with open(tgt, "wb") as g:
                    for b in read_file(fi):
                        if stopped():
                            raise UnpackError("stopped")
                        g.write(b)
                        done += len(b)
                        if prog:
                            prog(done / total)
    finally:
        close()
    return pk


# ---- writing a disc image
class _P3Node:
    __slots__ = ("name", "is_dir", "size", "src", "kids", "pname", "jname", "lba", "ex", "dbytes", "num", "stamp", "skey")

    def __init__(self, name, is_dir, size=0, src=None):
        self.name, self.is_dir, self.size, self.src = name, is_dir, size, src
        self.kids, self.pname, self.jname, self.lba = [], b"", b"", 0
        self.ex, self.dbytes, self.num = {}, {}, {}
        self.stamp = None                                    # the file's own date (7 bytes), when it has one
        self.skey = (is_dir, name.decode("utf-8", "replace").upper(), name)       # a folder's contents are listed files first, then folders (as on real discs)


def _p3_date(t):
    g = time.gmtime(t)
    return bytes([min(max(g.tm_year - 1900, 0), 255), g.tm_mon, g.tm_mday, g.tm_hour, g.tm_min, g.tm_sec, 0])


def _p3_spell(raw, is_dir, used):
    """The plain ISO 9660 spelling of a name: capital ASCII letters, ';1' on files, never two alike in one folder."""
    text = raw.decode("utf-8", "replace")
    asc = "".join(c if 32 <= ord(c) < 127 and c not in '/\\;:*?"<>|' else "_" for c in text).upper()[:200] or "_"
    cand, n = asc, 0
    while cand in used:
        n += 1
        stem, dot, ext = asc.rpartition(".") if (not is_dir and "." in asc) else (asc, "", "")
        cand = f"{stem[:190]}~{n}{dot}{ext}"
    used.add(cand)
    return (cand + ("" if is_dir else ";1")).encode("ascii")


def _p3_tree(entries):
    root = _P3Node(b"", True)
    index = {(): root}
    for parts, is_file, src, size in entries:
        parent = index.get(parts[:-1])
        if parent is None:
            raise UnpackError("a file in this game has no folder around it")
        node = _P3Node(parts[-1], not is_file, size if is_file else 0, src)
        if isinstance(src, (str, Path)):
            try:
                node.stamp = _p3_date(os.stat(src).st_mtime)
            except (OSError, OverflowError, ValueError):
                pass
        parent.kids.append(node)
        if not is_file:
            index[parts] = node
    todo = [root]
    while todo:
        d = todo.pop()
        used = set()
        for k in sorted(d.kids, key=lambda x: x.name):
            k.pname = _p3_spell(k.name, k.is_dir, used)
            units = len(k.name.decode("utf-8", "replace").encode("utf-16-be")) // 2
            if units + (0 if k.is_dir else 2) > 100:
                raise UnpackError("the name '" + k.name.decode("utf-8", "replace")[:40] + "…' is too long for a PS3 disc image (about 100 characters at most)")
            k.jname = k.name.decode("utf-8", "replace").encode("utf-16-be") + (b"" if k.is_dir else ";1".encode("utf-16-be"))
            if k.is_dir:
                todo.append(k)
    return root


def _p3_record(extent, size, flags, name, when, sysuse=True):
    """One directory record. Real PS3 discs, and the tools that make images of them, end every record with 6 zero bytes of 'system use'
    after the name's padding (the volume descriptor's own root record is the plain 34 bytes); written the same here."""
    n = len(name)
    ln = 33 + n + (1 if n % 2 == 0 else 0) + (6 if sysuse else 0)
    r = bytearray(ln)
    r[0] = ln
    r[2:10] = struct.pack("<I", extent) + struct.pack(">I", extent)
    r[10:18] = struct.pack("<I", size) + struct.pack(">I", size)
    r[18:25] = when
    r[25] = flags
    r[28:32] = struct.pack("<H", 1) + struct.pack(">H", 1)
    r[32] = n
    r[33:33 + n] = name
    return bytes(r)


def _p3_extents(lba, size):
    """The records one file needs: a single one, or several of PS3_EXTENT bytes (all but the last marked 'more follows')."""
    if size <= PS3_EXTENT:
        return [(lba, size, 0)]
    out, off = [], 0
    while size - off > PS3_EXTENT:
        out.append((lba + off // PS3_SECTOR, PS3_EXTENT, 0x80))
        off += PS3_EXTENT
    out.append((lba + off // PS3_SECTOR, size - off, 0))
    return out


def _p3_dir_bytes(d, key, parent, when):
    """One folder's records (itself, its parent, then its contents in name order), packed so that none crosses a sector.
    Uses the extents and sizes recorded so far (zero before they are known: the length of the records doesn't depend on them)."""
    name = "pname" if key == "p" else "jname"
    recs = [_p3_record(d.ex.get(key, 0), d.dbytes.get(key, 0), 2, b"\0", d.stamp or when),
            _p3_record(parent.ex.get(key, 0), parent.dbytes.get(key, 0), 2, b"\1", parent.stamp or when)]
    for k in sorted(d.kids, key=lambda x: x.skey):
        nm = getattr(k, name)
        if k.is_dir:
            recs.append(_p3_record(k.ex.get(key, 0), k.dbytes.get(key, 0), 2, nm, k.stamp or when))
        else:
            for e, s, fl in _p3_extents(k.lba, k.size):
                recs.append(_p3_record(e, s, fl, nm, k.stamp or when))
    out = bytearray()
    for r in recs:
        room = PS3_SECTOR - len(out) % PS3_SECTOR
        if len(r) > room:
            out += bytes(room)
        out += r
    out += bytes(-len(out) % PS3_SECTOR)
    return bytes(out)


def _p3_path_table(order, key, big):
    name = "pname" if key == "p" else "jname"
    out = bytearray()
    for i, d in enumerate(order):
        nm = b"\0" if i == 0 else getattr(d, name)
        parent = 1 if i == 0 else d.num["parent_" + key]
        out += bytes([len(nm), 0])
        out += struct.pack(">I" if big else "<I", d.ex[key])
        out += struct.pack(">H" if big else "<H", parent)
        out += nm
        if len(nm) % 2:
            out += b"\0"
    return bytes(out)


def _p3_pt_size(order, key):
    name = "pname" if key == "p" else "jname"
    n = 0
    for i, d in enumerate(order):
        ln = 1 if i == 0 else len(getattr(d, name))
        n += 8 + ln + (ln & 1)
    return n


def _p3_volume(joliet, total, ptsize, lpath, mpath, root_rec, stamp):
    """A primary (or Joliet supplementary) volume descriptor, written the way makeps3iso writes it."""
    v = bytearray(PS3_SECTOR)
    v[0] = 2 if joliet else 1
    v[1:6] = b"CD001"
    v[6] = 1
    uni = "PS3VOLUME".encode("utf-16-be")
    if joliet:
        v[40:72] = uni.ljust(32, b"\0")
        v[88:91] = b"\x25\x2f\x40"
    else:
        v[8:40] = b" " * 32
        v[40:72] = b"PS3VOLUME".ljust(32)
    v[80:88] = struct.pack("<I", total) + struct.pack(">I", total)
    v[120:124] = struct.pack("<H", 1) + struct.pack(">H", 1)
    v[124:128] = struct.pack("<H", 1) + struct.pack(">H", 1)
    v[128:132] = struct.pack("<H", PS3_SECTOR) + struct.pack(">H", PS3_SECTOR)
    v[132:140] = struct.pack("<I", ptsize) + struct.pack(">I", ptsize)
    v[140:144] = struct.pack("<I", lpath)
    v[148:152] = struct.pack(">I", mpath)
    v[156:190] = root_rec
    if joliet:
        v[190:318] = uni.ljust(128, b"\0")
    else:
        v[190:881] = b" " * (881 - 190)
        v[190:199] = b"PS3VOLUME"
    v[813:830] = stamp + b"\0"
    for off in (830, 847, 864):
        v[off:off + 17] = b"0000000000000000\0"
    v[881] = 1
    return bytes(v)


class _P3Out:
    """Writes the image as one file, or as parts of PS3_SPLIT_SECTORS sectors named base.0, base.1, ... Keeps the names it made."""
    def __init__(self, base, split):
        self.base, self.split, self.files, self.f, self.left = str(base), split, [], None, 0
        self._next()

    def _next(self):
        if self.f:
            self.f.close()
        p = self.base + (f".{len(self.files)}" if self.split else "")
        self.files.append(Path(p))
        self.f = open(p, "wb")
        self.left = PS3_SPLIT_SECTORS * PS3_SECTOR

    def write(self, b):
        if not self.split:
            self.f.write(b)
            return
        mv = memoryview(b)
        while len(mv):
            if self.left == 0:
                self._next()
            k = min(len(mv), self.left)
            self.f.write(mv[:k])
            self.left -= k
            mv = mv[k:]

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


def build_ps3_iso(entries, out_base, read, title_id="", progress=lambda x: None, stopped=lambda: False, split=False):
    """Write a PS3 disc image of `entries` [(path parts as bytes, is_file, src, size)]; read(src, size) yields each file's bytes.
    Layout as makeps3iso writes it (so the same tools and RPCS3 read it): the header sector with a single 'not encrypted' region over the
    whole disc, an ISO 9660 volume and a Joliet one that share the files, folders in name order. With split, an image of more than
    4 GiB goes into parts out_base.0, out_base.1 ... Returns the list of files written."""
    import datetime
    root = _p3_tree(entries)
    now = datetime.datetime.now(datetime.timezone.utc)
    when = bytes([now.year - 1900, now.month, now.day, now.hour, now.minute, now.second, 0])
    stamp = now.strftime("%Y%m%d%H%M%S00").encode()
    orders, parents = {}, {}
    for key, name in (("p", "pname"), ("j", "jname")):
        order, level = [], [root]
        parents[(key, id(root))] = root
        while level:
            nxt = []
            for d in level:
                order.append(d)
                kids = sorted((k for k in d.kids if k.is_dir), key=lambda x: x.skey)
                for k in kids:
                    parents[(key, id(k))] = d
                nxt += kids
            level = nxt
        for i, d in enumerate(order):
            d.num[key] = i + 1
            d.num["parent_" + key] = parents[(key, id(d))].num[key] if i else 1
        orders[key] = order
    for key in ("p", "j"):                                           # folder sizes first: they don't depend on where anything lies
        for d in orders[key]:
            d.dbytes[key] = len(_p3_dir_bytes(d, key, parents[(key, id(d))], when))
    ptsz = {k: _p3_pt_size(orders[k], k) for k in ("p", "j")}
    secs = {k: (ptsz[k] + PS3_SECTOR - 1) // PS3_SECTOR for k in ("p", "j")}
    lba = 20
    tables = {}
    for k in ("p", "j"):
        tables[k] = (lba, lba + secs[k])
        lba += 2 * secs[k]
    lba = max(32, lba)
    for key in ("p", "j"):
        for d in orders[key]:
            d.ex[key] = lba
            lba += d.dbytes[key] // PS3_SECTOR
    files = []

    def walk(d):
        for k in sorted((x for x in d.kids if not x.is_dir), key=lambda x: x.skey):
            files.append(k)
        for k in sorted((x for x in d.kids if x.is_dir), key=lambda x: x.skey):
            walk(k)
    walk(root)
    first_file = lba
    for k in files:
        k.lba = lba
        lba += (k.size + PS3_SECTOR - 1) // PS3_SECTOR
    total = lba
    head = bytearray(first_file * PS3_SECTOR)
    gid = title_id.replace("-", "")
    head[0:4] = struct.pack(">I", 1)
    head[8:16] = struct.pack(">II", 0, total - 1)
    head[0x800:0x80C] = b"PlayStation3"
    head[0x810:0x830] = (gid[:4] + "-" + gid[4:] if len(gid) > 4 else gid).encode("ascii", "replace")[:32].ljust(32)
    head[0x840:0x9F0] = os.urandom(0x1B0)
    head[0x9F0:0xA00] = os.urandom(0x10)
    for key, idx in (("p", 16), ("j", 17)):
        rr = _p3_record(root.ex[key], root.dbytes[key], 2, b"\0", when, sysuse=False)
        head[idx * PS3_SECTOR:(idx + 1) * PS3_SECTOR] = _p3_volume(key == "j", total, ptsz[key], tables[key][0], tables[key][1], rr, stamp)
    head[18 * PS3_SECTOR:18 * PS3_SECTOR + 6] = b"\xffCD001"
    for key in ("p", "j"):
        for big, at in ((False, tables[key][0]), (True, tables[key][1])):
            t = _p3_path_table(orders[key], key, big)
            head[at * PS3_SECTOR:at * PS3_SECTOR + len(t)] = t
        for d in orders[key]:
            b = _p3_dir_bytes(d, key, parents[(key, id(d))], when)
            head[d.ex[key] * PS3_SECTOR:d.ex[key] * PS3_SECTOR + len(b)] = b
    out = _P3Out(out_base, bool(split) and total > PS3_SPLIT_SECTORS)
    try:
        out.write(bytes(head))
        grand = sum(k.size for k in files) or 1
        done = 0
        for k in files:
            wrote = 0
            for piece in read(k.src, k.size):
                out.write(piece)
                wrote += len(piece)
                done += len(piece)
                progress(done / grand)
                if stopped():
                    raise UnpackError("stopped")
            if wrote != k.size:
                raise UnpackError(f"{k.name.decode('utf-8', 'replace')} came out shorter than it should")
            pad = -wrote % PS3_SECTOR
            if pad:
                out.write(bytes(pad))
    finally:
        out.close()
    progress(1.0)
    return list(out.files)


def _lower(b):
    return bytes(c + 32 if 65 <= c <= 90 else c for c in b)


def write_zar(out_path, entries, read, progress=lambda x: None, stopped=lambda: False, level=6):
    """Write a ZArchive of `entries` [(path parts as bytes, is_file, src, size)]; read(src, size) yields each file's bytes.
    Files are stored in the order given, back to back, in 64 KiB blocks of zstd (a block that doesn't shrink is stored as it is)."""
    import hashlib
    try:
        import zstandard
    except ImportError:
        raise UnpackError("making a ZAR needs the 'zstandard' package") from None

    class N:
        __slots__ = ("name", "is_file", "off", "size", "kids", "nidx", "start")

        def __init__(self, name, is_file, off=0, size=0):
            self.name, self.is_file, self.off, self.size, self.kids, self.nidx, self.start = name, is_file, off, size, [], 0, 0

    root = N(b"", False)
    index = {(): root}
    names, name_at = [], {}

    def nidx(nm):
        if nm not in name_at:
            name_at[nm] = len(names)
            names.append(nm)
        return name_at[nm]

    def make_dir(parts):
        node = index.get(parts)
        if node is None:
            parent = make_dir(parts[:-1])
            node = N(parts[-1], False)
            node.nidx = nidx(node.name)
            parent.kids.append(node)
            index[parts] = node
        return node

    stream, files = 0, []
    for parts, is_file, src, size in entries:
        nm = parts[-1]
        if not nm or len(nm) > 0x7FFF:
            raise UnpackError("a name is empty or too long for a ZAR")
        parent = make_dir(parts[:-1])
        if any(_lower(k.name) == _lower(nm) for k in parent.kids):
            raise UnpackError(f"two items in one folder differ only in upper/lower case ({nm.decode('latin1')}), which a ZAR cannot hold")
        if not is_file:
            make_dir(parts)
            continue
        node = N(nm, True, stream, size)
        node.nidx = nidx(nm)
        parent.kids.append(node)
        index[parts] = node
        files.append((node, src, size))
        stream += size
    if not stream:
        raise UnpackError("there are no files to put in the archive")

    comp = zstandard.ZstdCompressor(level=level)
    h = hashlib.sha256()
    pos = 0
    records = []
    nblocks = 0
    pending = bytearray()
    BATCH = 64 * ZAR_BLOCK

    with open(out_path, "wb") as out:
        def put(b):
            nonlocal pos
            out.write(b)
            h.update(b)
            pos += len(b)

        def store(blocks):
            nonlocal nblocks
            try:
                res = comp.multi_compress_to_buffer(blocks, threads=-1)
                packed = [res[i].tobytes() for i in range(len(blocks))]
            except (AttributeError, TypeError):
                packed = [comp.compress(b) for b in blocks]
            for raw, c in zip(blocks, packed):
                at = pos
                if len(c) >= ZAR_BLOCK:
                    c = raw
                put(c)
                if nblocks % 16 == 0:
                    records.append([at, [0] * 16])
                records[-1][1][nblocks % 16] = len(c) - 1
                nblocks += 1

        def flush(final=False):
            nonlocal pending
            n = len(pending) // ZAR_BLOCK
            if n >= (1 if final else 64):
                blocks = [bytes(pending[i * ZAR_BLOCK:(i + 1) * ZAR_BLOCK]) for i in range(n)]
                del pending[:n * ZAR_BLOCK]
                store(blocks)

        done = 0
        for node, src, size in files:
            got = 0
            for piece in read(src, size):
                pending += piece
                got += len(piece)
                done += len(piece)
                if len(pending) >= BATCH:
                    flush()
                progress(done / stream * 0.98)
                if stopped():
                    raise UnpackError("stopped")
            if got != size:
                raise UnpackError("a file came out shorter than expected")
        if pending:
            pending += bytes(-len(pending) % ZAR_BLOCK)
        flush(final=True)
        data_size = pos
        put(bytes(-pos % 8))
        rec_off = pos
        for base, sizes in records:
            put(struct.pack(">Q16H", base, *sizes))
        rec_size = pos - rec_off
        names_off = pos
        offs = []
        cur = 0
        for nm in names:
            offs.append(cur)
            hdr = bytes((len(nm) & 0x7F | 0x80, len(nm) >> 7)) if len(nm) >= 0x80 else bytes((len(nm),))
            put(hdr + nm)
            cur += len(hdr) + len(nm)
        names_size = pos - names_off
        tree_off = pos
        order, i, nxt = [root], 0, 1
        while i < len(order):
            node = order[i]
            i += 1
            if not node.is_file:
                node.kids.sort(key=lambda k: _lower(k.name))
                node.start = nxt
                nxt += len(node.kids)
                order += node.kids
        for node in order:
            nm_off = 0x7FFFFFFF if node is root else offs[node.nidx]
            if node.is_file:
                put(struct.pack(">IIII", nm_off | 0x80000000, node.off & 0xFFFFFFFF, node.size & 0xFFFFFFFF,
                                ((node.off >> 32) & 0xFFFF) | (((node.size >> 32) & 0xFFFF) << 16)))
            else:
                put(struct.pack(">IIII", nm_off, node.start, len(node.kids), 0))
        tree_size = pos - tree_off
        secs = (0, data_size, rec_off, rec_size, names_off, names_size, tree_off, tree_size, pos, 0, pos, 0)
        total = pos + ZAR_FOOTER
        h.update(struct.pack(">12Q32sQII", *secs, bytes(32), total, ZAR_VERSION, ZAR_MAGIC))
        out.write(struct.pack(">12Q32sQII", *secs, h.digest(), total, ZAR_VERSION, ZAR_MAGIC))
    progress(1.0)


# --------------------------------------------------------------------------- Linux / macOS: what has to be installed, and why
LINUX_PKGS = {"chdman": {"apt": "mame-tools", "dnf": "mame-tools", "pacman": "mame-tools", "brew": "rom-tools"},
              "tkdnd": {"apt": "tkdnd", "dnf": "tkdnd"},
              "zstandard": {"apt": "python3-zstandard", "dnf": "python3-zstandard", "pacman": "python-zstandard"}}
LINUX_WHY = {
    "chdman": ("CHD Manager is the friendly front end. chdman, part of MAME's tools, is the program that actually makes and reads "
               "CHD files, and CHD Manager asks it to do that work. Without chdman nothing can be turned into a CHD or back, and most "
               "other conversions, which pass through a CHD on the way, won't work either. Only a few direct ones (ISO to and from "
               "CSO or ZSO, and the GameCube/Wii formats) work without it. There is no official chdman download for Linux or macOS, so "
               "it has to come from your system's package manager."),
    "tkdnd": ("Lets you drag files and folders from your file manager onto this window. It is optional: Add Files, Add Folder and "
              "the right-click menu all work without it."),
    "zstandard": ("Reads and writes zstd compression, which two formats use: Wii/GameCube RVZ files and Xbox 360 ZAR archives. "
                  "It is optional: every other format works without it."),
}
LINUX_NAMES = {"chdman": "chdman (needed for CHD files)", "tkdnd": "Drag and drop (optional)",
               "zstandard": "zstd compression (optional: RVZ and Xbox 360 ZAR)"}


def linux_pkg_manager():
    for pm, tool in (("apt", "apt-get"), ("dnf", "dnf"), ("pacman", "pacman"), ("brew", "brew")):
        if shutil.which(tool):
            return pm
    return None


def linux_is_steamos():
    try:
        text = Path("/etc/os-release").read_text("utf-8", "replace").lower()
    except OSError:
        return False
    return "steamos" in text


def linux_install_command(piece):
    """The command that installs this piece with the package manager found on this system, or None."""
    pm = linux_pkg_manager()
    pkg = LINUX_PKGS[piece].get(pm) if pm else None
    if not pkg:
        return None
    return {"apt": "sudo apt install ", "dnf": "sudo dnf install ", "pacman": "sudo pacman -S ", "brew": "brew install "}[pm] + pkg


# --------------------------------------------------------------------------- Linux right-click menu (KDE Dolphin, GNOME Files, "Open with")
LINUX_MIME = "application/x-chdmanager-disc"
LINUX_NEW_EXTS = (".zar", ".pbp", ".vcd", ".zso", ".ecm", ".rvz", ".wia", ".wbfs", ".gcz", ".ciso", ".wux", ".wud", ".gcm", ".nrg", ".mds",
                  ".mdf", ".ccd")   # types Linux doesn't know: registered as LINUX_MIME (the ones it does know are listed below instead)
LINUX_SYS_MIMES = ("application/x-iso9660-image", "application/x-cd-image", "application/vnd.efi.iso", "application/vnd.efi.img",
                   "application/x-cue", "application/x-gd-rom-cue", "application/x-cdrdao-toc", "application/x-discjuggler-cd-image",
                   "application/x-compressed-iso", "application/x-mame-chd", "application/x-raw-disk-image", "application/x-nrg",
                   "application/x-gamecube-rom", "application/x-wii-rom", "application/x-gamecube-iso-image",
                   "application/x-wii-iso-image", "application/x-wbfs")
LINUX_TO = ("chd", "cue", "iso", "pbp", "cdi", "vcd", "cso", "cso2", "zso", "ecm", "rvz", "gcz", "wia", "wbfs", "ciso", "zar", "folder")


def linux_data_home():
    return Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))


def linux_program():
    """[the command that starts this program] as a list: the launcher script next to it, or python + this script."""
    here = Path(__file__).resolve()
    sh = here.with_name("chd-manager.sh")
    return [str(sh)] if sh.is_file() else [sys.executable, str(here)]


def linux_menu_files(prog=None, data=None):
    """{path: (text, executable)} of every file the Linux right-click menu consists of (all inside the user's own ~/.local/share)."""
    import shlex
    prog = prog or linux_program()
    data = Path(data) if data else linux_data_home()
    bad = [x for x in prog if re.search(r'["`$\\]|[\n\r]', x)]
    if bad:
        raise ValueError("the program's path has characters (a quote, $, ` or \\) that a menu entry can't hold: " + bad[0])
    ex = " ".join('"%s"' % x for x in prog)                 # desktop-entry style: double-quoted words
    sh = " ".join(shlex.quote(x) for x in prog)             # shell style, for the Nautilus scripts
    mimes = ";".join(LINUX_SYS_MIMES + (LINUX_MIME,)) + ";"
    files = {}
    files[data / "applications" / "chd-manager.desktop"] = (
        "[Desktop Entry]\nType=Application\nName=CHD Manager\nComment=Convert game disc images between formats\n"
        f"Exec={ex} %F\nTerminal=false\nCategories=Utility;\nMimeType={mimes}\n", False)
    acts = [("convert", "Convert with CHD Manager", "--start"), ("add", "Add to the CHD Manager list", "")]
    acts += [(f"to_{k}", "Convert to " + TARGET_LABELS[k].replace("→ ", "").strip(), f"--start --to {k}") for k in LINUX_TO]
    text = ("[Desktop Entry]\nType=Service\nX-KDE-ServiceTypes=KonqPopupMenu/Plugin\n"
            f"MimeType={mimes}inode/directory;\nX-KDE-Submenu=CHD Manager\nActions={';'.join(a[0] for a in acts)};\n")
    for key, name, args in acts:
        text += f"\n[Desktop Action {key}]\nName={name}\nExec={ex} {args + ' ' if args else ''}%F\n"
    files[data / "kio" / "servicemenus" / "chd-manager.desktop"] = (text, True)
    files[data / "mime" / "packages" / "chd-manager.xml"] = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">\n'
        f'  <mime-type type="{LINUX_MIME}">\n    <comment>Game disc image</comment>\n'
        + "".join(f'    <glob pattern="*{e}"/>\n' for e in LINUX_NEW_EXTS) + "  </mime-type>\n</mime-info>\n", False)
    for name, args in (("CHD Manager - Convert", "--start"), ("CHD Manager - Add to list", "")):
        files[data / "nautilus" / "scripts" / name] = (f'#!/bin/sh\nexec {sh} {args + " " if args else ""}"$@"\n', True)
    return files


def linux_menu_installed(data=None):
    data = Path(data) if data else linux_data_home()
    return (data / "applications" / "chd-manager.desktop").is_file()


def _linux_refresh(data):
    for cmd in (["update-mime-database", str(data / "mime")], ["update-desktop-database", str(data / "applications")]):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
            except (OSError, subprocess.SubprocessError):
                pass


def linux_menu_install(data=None, prog=None):
    data = Path(data) if data else linux_data_home()
    for path, (text, executable) in linux_menu_files(prog, data).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, "utf-8", newline="\n")
        path.chmod(0o755 if executable else 0o644)
    _linux_refresh(data)


def linux_menu_remove(data=None):
    data = Path(data) if data else linux_data_home()
    for path in linux_menu_files(["x"], data):
        try:
            path.unlink()
        except OSError:
            pass
    _linux_refresh(data)


# --------------------------------------------------------------------------- Windows right-click menu
CTX_CD = (".cue", ".gdi", ".toc", ".bin", ".mds", ".mdf", ".nrg", ".ccd", ".img", ".pbp", ".cdi", ".vcd", ".ecm", ".m3u", ".chd")
CTX_TARGETS = {**{e: ("chd", "cue", "iso", "pbp", "cdi", "vcd", "cso", "cso2", "zso", "ecm", "verify", "recompress") for e in CTX_CD},
               **{e: ("chd", "iso", "cso", "cso2", "zso") for e in (".cso", ".zso")},
               **{e: ("iso", "rvz", "gcz", "wia", "wbfs", "ciso", "wux", "wud") for e in (".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud")},
               ".iso": ("chd", "cso", "cso2", "zso", "rvz", "gcz", "wia", "wbfs", "ciso", "zar", "folder"), ".zar": ("chd", "iso", "folder"), ".pkg": ("folder",), ".nsp": ("xci",), ".xci": ("nsp",), ".nup": ("folder",)}
CTX_FOLDER = ("chd", "cue", "iso", "zar")                              # what a folder's 'Convert to' offers
CTX_EXTS = tuple(CTX_TARGETS)                                    # archives are left out on purpose: no menu on every zip
CTX_BASE = r"Software\Classes\SystemFileAssociations"          # per user (HKCU): no administrator needed, doesn't change what a double-click opens
CTX_DIR = r"Software\Classes\Directory"
CTX_TEXT = {False: ("Convert with CHD Manager", "Add to CHD Manager", "Convert with CHD Manager to"),
            True: ("Convert this folder with CHD Manager", "Add this folder to CHD Manager", "Convert this folder with CHD Manager to")}


def ctx_program():
    """The command that starts this program: the exe, or pythonw + this script when run from source."""
    if FROZEN:
        return f'"{sys.executable}"'
    py = Path(sys.executable)
    pyw = py.with_name("pythonw.exe")
    return f'"{pyw if pyw.exists() else py}" "{Path(__file__).resolve()}"'


def ctx_roots(base=CTX_BASE, dirbase=CTX_DIR):
    """[(registry path, formats offered, is a folder)] for every place the menu goes."""
    return [(rf"{base}\{e}", t, False) for e, t in CTX_TARGETS.items()] + [(dirbase, CTX_FOLDER, True)]


def ctx_menu_installed(reg=None, base=CTX_BASE, dirbase=CTX_DIR):
    if reg is None and os.name != "nt":
        return linux_menu_installed()
    if reg is None:
        import winreg as reg
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, rf"{ctx_roots(base, dirbase)[0][0]}\shell\CHDManager.Convert\command"):
            return True
    except OSError:
        return False


def ctx_menu_install(reg=None, base=CTX_BASE, dirbase=CTX_DIR):
    """Add 'Convert with CHD Manager', 'Convert with CHD Manager to' (a submenu of formats) and 'Add to CHD Manager' to the
    right-click menu of disc image files and folders (this user only)."""
    if reg is None and os.name != "nt":
        return linux_menu_install()
    if reg is None:
        import winreg as reg
    prog = ctx_program()
    icon = sys.executable if FROZEN else ""

    def put(path, label, command=None, cascade=False):
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as k:
            if cascade:
                reg.SetValueEx(k, "MUIVerb", 0, reg.REG_SZ, label)
                reg.SetValueEx(k, "SubCommands", 0, reg.REG_SZ, "")
            else:
                reg.SetValueEx(k, "", 0, reg.REG_SZ, label)
            if icon and "\\shell\\" not in path.split("CHDManager.", 1)[1]:
                reg.SetValueEx(k, "Icon", 0, reg.REG_SZ, icon)
        if command:
            with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path + r"\command", 0, reg.KEY_WRITE) as k:
                reg.SetValueEx(k, "", 0, reg.REG_SZ, command)

    for root, targets, folder in ctx_roots(base, dirbase):
        conv, add, to = CTX_TEXT[folder]
        put(rf"{root}\shell\CHDManager.Convert", conv, f'{prog} --start "%1"')
        put(rf"{root}\shell\CHDManager.Open", add, f'{prog} "%1"')
        put(rf"{root}\shell\CHDManager.To", to, cascade=True)
        for i, key in enumerate(targets, 1):
            put(rf"{root}\shell\CHDManager.To\shell\{i:02d}{key}", TARGET_LABELS[key].replace("→ ", "").strip(),
                f'{prog} --start --to {key} "%1"')


def _ctx_delete_tree(reg, path):
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path) as k:
            names, i = [], 0
            while True:
                try:
                    names.append(reg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
    except OSError:
        return
    for n in names:
        _ctx_delete_tree(reg, rf"{path}\{n}")
    try:
        reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
    except OSError:
        pass


def ctx_menu_remove(reg=None, base=CTX_BASE, dirbase=CTX_DIR):
    """Take exactly the entries ctx_menu_install made out again (nothing else under those extensions is touched)."""
    if reg is None and os.name != "nt":
        return linux_menu_remove()
    if reg is None:
        import winreg as reg
    for root, _targets, folder in ctx_roots(base, dirbase):
        for key in ("CHDManager.Convert", "CHDManager.Open", "CHDManager.To"):
            _ctx_delete_tree(reg, rf"{root}\shell\{key}")
        if not folder:                                              # tidy up the folders if that left them empty (Windows refuses if not)
            for sub in (rf"{root}\shell", root):
                try:
                    reg.DeleteKey(reg.HKEY_CURRENT_USER, sub)
                except OSError:
                    pass
        else:
            try:
                reg.DeleteKey(reg.HKEY_CURRENT_USER, rf"{root}\shell")
            except OSError:
                pass


# One window takes everything the right-click menu sends: the first program to start owns this local port,
# and any other copy started with files hands them over to it and quits.
IPC_PORT = 47615
IPC_HELLO = b"CHDM-OK\n"


def ipc_listen():
    s = socket.socket()
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        s.bind(("127.0.0.1", IPC_PORT))
        s.listen(16)
        return s
    except OSError:
        s.close()
        return None


def ipc_send(msg, timeout=15):
    """True when the window that owns the port took the files (a long wait is fine: it may still be starting up)."""
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=timeout) as c:
            c.sendall(json.dumps(msg).encode() + b"\n")
            return c.makefile("rb").readline() == IPC_HELLO
    except OSError:
        return False


# --------------------------------------------------------------------------- multi-disc playlists
M3U_EXTS = (".chd", ".iso", ".cso", ".zso", ".cdi")
DISC_TAG = re.compile(r"\s*[\(\[]\s*(?:disc|disk|cd)\s*0*(\d{1,2})(?:\s*(?:of|-|/)\s*\d{1,2})?\s*[\)\]]", re.I)


def disc_split(name):
    """'Game (Disc 2) (USA).chd' -> ('Game (USA)', 2); None when the name has no disc tag."""
    m = DISC_TAG.search(name)
    if not m:
        return None
    base = (name[:m.start()] + name[m.end():]).strip()
    return re.sub(r"\s{2,}", " ", base), int(m.group(1))


def write_m3u_playlists(outputs, exists="Skip"):
    """For every folder that got a new disc named like 'Game (Disc 1)', write 'Game.m3u' listing the discs of that game
    found in the folder, in order. Needs two or more different disc numbers. Returns the log lines."""
    said = []
    seen = set()
    for out in outputs:
        out = Path(out)
        sp = disc_split(out.stem)
        if not sp or out.suffix.lower() not in M3U_EXTS or (out.parent, sp[0], out.suffix.lower()) in seen:
            continue
        seen.add((out.parent, sp[0], out.suffix.lower()))
        discs = {}
        for f in out.parent.iterdir():
            s2 = disc_split(f.stem) if f.is_file() and f.suffix.lower() == out.suffix.lower() else None
            if s2 and s2[0] == sp[0]:
                discs.setdefault(s2[1], f.name)
        if len(discs) < 2:
            continue
        text = "".join(discs[k] + "\n" for k in sorted(discs))
        pl = out.parent / (sp[0] + ".m3u")
        try:
            old = pl.read_text("utf-8") if pl.exists() else None
        except (OSError, UnicodeDecodeError):
            old = ""
        if old == text:
            continue
        if old is not None and exists == "Skip":
            said.append(f"A playlist named {pl.name} already exists, so it was left as it is (Settings, If output exists).")
            continue
        pl.write_text(text, "utf-8")
        said.append(f"OK  {pl.name}  (playlist of {len(discs)} discs)")
    return said


# --------------------------------------------------------------------------- App
DEFAULTS = {
    "chdman": "", "compression": "Default", "custom_comp": "", "hunk": "", "threads": 0,
    "exists": "Skip", "verify_after": True, "delete_src": False, "delete_forever": False, "auto_cue": True, "make_m3u": True, "space_check": True, "ps3_split": False, "ps3_update": False,
    "extract_subfolder": False,
    "out_mode": "same", "out_dir": "", "keep_structure": False, "last_dir": "", "geometry": "",
    "base_pbp": "", "base_pbp_ask": True, "pbp_level": 6, "rvz_method": "zstd", "rvz_level": 5, "rvz_chunk": 131072, "wbfs_scrub": True, "dolphintool": "", "rpcs3": "", "pkg_key": "", "switch_keys": "", "wii_common_key": "", "wii_korean_key": "", "wii_warn_off": False, "linux_help_off": False, "art_dir": "", "title_cards": True, "name_format": "%FILENAME%", "auto_log": False, "auto_update_check": True, "theme": "dark", "auto_suggest": True, "low_priority": False, "skip_version": "", "auto_app_check": True,
}


def sentence(text):
    """A reason as a tidy sentence: capital first letter, full stop at the end."""
    text = " ".join(str(text).split()).strip()
    if not text:
        return ""
    text = text[0].upper() + text[1:]
    return text if text[-1] in ".!?)" else text + "."


def plain_exc(e):
    """An exception raised while reading a file -> a sentence a person can act on."""
    if isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32):
        return sentence("the file is in use by another program or can't be read. If it is a disc image, Windows may have mounted it as a drive: "
                        "right-click that drive, choose Eject, and add the file again")
    if isinstance(e, OSError):
        return sentence(f"the file couldn't be opened ({e.strerror or e})")
    if isinstance(e, UnpackError):
        return sentence(f"CHD Manager can't use this file: {e}")
    if isinstance(e, ValueError):
        return sentence(e)
    return sentence(f"an unexpected problem stopped it ({type(e).__name__}: {e})")


def plain_reason(job):
    """Why a finished item did not become what was asked, in words for the person who clicked Start."""
    st, r = job.status or "", (job.result or "").strip()
    if st.startswith("Skipped (exists)"):
        return sentence("a file or folder with that name is already in the output folder, so nothing was written. "
                        "Settings, 'If output exists' lets you replace it or keep both")
    if st.startswith("Failed (missing files)"):
        return sentence("the files it points to weren't found next to it" + (": " + ", ".join(job.missing) if job.missing else "")
                        + ". Put them in the same folder, or add the .cue/.gdi that belongs to them")
    if st == "No CUE":
        return sentence("this .bin has no .cue sheet describing it. Add the .cue, or turn on creating one automatically "
                        "(Settings, Convert page)")
    if st == "Verify failed":
        if r.upper().startswith("CORRUPT"):
            return sentence("this CHD is damaged: it failed chdman's checksum test")
        return sentence(r or "the finished file didn't pass its check, so it was thrown away. Your original was not touched")
    if st.startswith("Failed (code"):
        return sentence("the conversion tool (chdman) stopped with an error" + (f": {r}" if r else "") + ". The log has its full output")
    if st.startswith("Failed (unknown CHD)"):
        return sentence("this CHD couldn't be read (it may be damaged, or not a disc image)")
    if st == "Error":
        return sentence("an unexpected error stopped it. The log has the details")
    if st.startswith("Failed (disk)") or st.startswith("Failed (disk or file)"):
        return sentence(f"a disk problem stopped it ({r})" if r else "a disk problem stopped it")
    if r:
        return sentence(r)
    return sentence(st.lower() if st and not st.startswith(("Working", "Queued")) else "it did not finish. The log has the details")


class App:
    def __init__(self, root, silent=False, initial_paths=None, auto_start=False, target=None, listener=None):
        global CHDMAN
        self.root = root
        self.silent = silent
        self.notices = []               # every "this was refused" window (headline, rows, outro); the self-test reads it
        self._problems = []             # refusals collected while files are being added
        self.run_jobs = []              # the items of the run in progress
        self._busy_told = False
        self.adding = False             # files are being read into the list right now
        self._add_queue = []            # drops that arrive meanwhile wait here
        self._close_when_done = False
        self.echo = None                # the command-line mode prints log lines through this
        self.no_save = False
        self.jobs = []
        self.q = queue.Queue()
        self.running = False
        self.stop_flag = False
        self.proc = None
        self.worker = None
        self.tree_ids = {}
        self.arc_dirs = {}              # unpacked archive folder -> how many jobs still use it
        self.settings = dict(DEFAULTS)
        try:
            self.settings.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:  # noqa: BLE001
            pass
        moved = relocate_settings(self.settings)
        CHDMAN = find_chdman(self.settings.get("chdman", ""))
        restored = None
        if not CHDMAN:
            restored = restore_bundled_chdman()
            CHDMAN = find_chdman("")
        self.chd_version, self.has_dvd = chdman_info()
        self.build_ui()
        if moved:
            self.log(f"The program folder has moved, so {len(moved)} saved path(s) were updated to the new location.", "info")
        if restored:
            self.log(f"chdman.exe was missing, so the spare copy built into this program was restored: {restored}", "warn")
        if CHDMAN:
            self.log(f"Using {CHDMAN}  (chdman {self.chd_version})", "info")
            if not self.has_dvd:
                self.log("This chdman is too old to have 'createdvd'. DVD images will fall back to "
                         "'createcd'. Get a newer chdman (MAME 0.255 or later).", "warn")
        else:
            if os.name == "nt":
                self.log("chdman.exe not found. Put it next to this script or set it in Settings, Output page.", "err")
            else:
                self.log("chdman wasn't found, so CHD files can't be made or read yet. More, 'What to install' says what to do and why.", "err")
        self.latest = None
        self.refresh_suggestions()
        ensure_keys_dir()
        self.refresh_wii()
        if initial_paths:
            self.root.after(400, lambda: self.handle_external([str(x) for x in initial_paths], auto_start, target))
        self.ipc_q, self.pending_start, self.start_due = queue.Queue(), [], 0.0
        if listener is not None:
            threading.Thread(target=self.ipc_serve, args=(listener,), daemon=True).start()
            self.root.after(300, self.poll_ipc)
        self.root.after(80, self.pump)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        if self.v["auto_update_check"].get() and not silent:
            self.root.after(1500, lambda: self.check_update_async(manual=False))
        if self.v["auto_app_check"].get() and not silent:
            self.root.after(2500, lambda: self.check_app_update_async(manual=False))
        if not silent:
            self.root.after(1200, self.wii_warning)
            if os.name != "nt" and not CHDMAN and not self.v["linux_help_off"].get():
                self.root.after(1800, lambda: self.show_linux_setup(auto=True))

    # ------------------------------------------------------------------ UI
    def build_ui(self):
        root = self.root
        root.title(f"{APP_NAME} {APP_VER}")
        root.geometry(self.settings.get("geometry") or "1100x720")
        root.minsize(940, 580)
        # variables
        self.v = {}
        for k, d in DEFAULTS.items():
            if k in ("last_dir", "geometry", "chdman"):
                continue
            val = self.settings.get(k, d)
            var = (tk.BooleanVar if isinstance(d, bool) else tk.IntVar if isinstance(d, int) else tk.StringVar)(value=val)
            self.v[k] = var
        if self.v["theme"].get() not in THEMES:
            self.v["theme"].set("dark")
        self.t = THEMES[self.v["theme"].get()]
        self.apply_ttk_theme()
        self.make_icon()
        self.build_header()

        # ---- toolbar: only the everyday actions. Everything else lives in the "More" menu or the settings window.
        bar = ttk.Frame(root, padding=(18, 14, 18, 8))
        bar.pack(fill="x")
        self.toolbar = bar

        def btn(text, cmd):
            b = ttk.Button(bar, text=text, command=cmd)
            b.pack(side="left", padx=(0, 8))
            return b

        btn("+  Add Files", self.add_files_dialog)
        btn("+  Add Folder", self.add_folder_dialog)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=(4, 12))
        btn("Remove", self.remove_selected)
        btn("Clear all", self.clear_all)
        self.more_btn = ttk.Button(bar, text="More  ▾", command=self.show_more_menu)
        self.more_btn.pack(side="right")
        self.more_menu = tk.Menu(root, tearoff=0)
        self.more_menu.add_command(label="Settings…", command=lambda: self.open_settings("Convert"))
        self.more_menu.add_separator()
        self.more_menu.add_command(label="Info about the selected disc…", command=self.info_selected)
        self.more_menu.add_command(label="Verify the selected CHD files", command=self.verify_selected)
        if os.name != "nt":
            self.more_menu.add_command(label="What to install (and why)…", command=self.show_linux_setup)
        self.more_menu.add_separator()
        self.more_menu.add_command(label=self.theme_menu_label(), command=self.toggle_theme)
        self.theme_menu_index = self.more_menu.index("end")
        self.more_menu.add_separator()
        self.more_menu.add_command(label="About CHD Manager…", command=lambda: self.open_settings("About"))

        # ---- bottom, packed first so it always keeps its space: footer, then action bar, then the settings line
        foot = ttk.Frame(root, padding=(18, 0, 18, 10))
        foot.pack(fill="x", side="bottom")
        self._footer_note = {"chdman": "", "app": ""}
        self.chdman_btn = ttk.Button(foot, style="Subtle.TButton", command=self.update_clicked)
        if os.name == "nt":
            self.chdman_btn.pack(side="left")
        self.upd_btn = self.chdman_btn                      # (state changes during a chdman update use this name)
        self.app_btn = ttk.Button(foot, style="Subtle.TButton", command=self.app_footer_clicked)
        self.app_btn.pack(side="right")
        self.set_footer("chdman")
        self.set_footer("app")
        _Tip(self.chdman_btn, lambda: "Click to install the chdman update" if self.update_available()
             else "Click to check for a newer chdman")
        _Tip(self.app_btn, lambda: "Click to install the update" if self.app_update_available()
             else "Click to check for a newer CHD Manager")

        act = ttk.Frame(root, padding=(18, 8, 18, 8))
        act.pack(fill="x", side="bottom")
        sv = ttk.Frame(root, padding=(18, 4, 18, 0))                 # "Save to": where the finished files go, right where the work is started
        sv.pack(fill="x", side="bottom")
        ttk.Label(sv, text="Save to:").pack(side="left")
        ttk.Radiobutton(sv, text="next to each original", variable=self.v["out_mode"], value="same").pack(side="left", padx=(8, 4))
        ttk.Radiobutton(sv, text="this folder:", variable=self.v["out_mode"], value="custom").pack(side="left", padx=(8, 4))
        self.out_entry = ttk.Entry(sv, textvariable=self.v["out_dir"])
        self.out_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        ttk.Button(sv, text="Browse…", command=self.pick_out_dir).pack(side="left")
        ttk.Checkbutton(sv, text="keep sub-folders (when adding a folder full of games)", variable=self.v["keep_structure"]).pack(side="left", padx=(12, 0))
        self.start_btn = ttk.Button(act, text="▶  Start", style="Accent.TButton", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="■  Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        self.log_btn = ttk.Button(act, text="Show log", command=self.toggle_log)
        self.log_btn.pack(side="right")
        ttk.Button(act, text="Open output folder", command=self.open_output_folder).pack(side="right", padx=(0, 8))
        pf = ttk.Frame(act)
        pf.pack(side="left", fill="x", expand=True, padx=(18, 18))
        self.status = ttk.Label(pf, text="Ready")
        self.status.pack(anchor="w")
        self.cur_bar = ttk.Progressbar(pf, maximum=100, style="Accent.Horizontal.TProgressbar")
        self.cur_bar.pack(fill="x", pady=(4, 2))
        self.all_bar = ttk.Progressbar(pf, maximum=100, style="Slim.Horizontal.TProgressbar")
        self.all_bar.pack(fill="x")

        strip = ttk.Frame(root, padding=(18, 2, 18, 2))
        strip.pack(fill="x", side="bottom")
        self.summary_label = ttk.Label(strip, style="Muted.TLabel")
        self.summary_label.pack(side="left")
        ttk.Button(strip, text="Change settings…", style="Subtle.TButton",
                   command=lambda: self.open_settings("Convert")).pack(side="right")

        # ---- main area: the list of discs. The log joins it only when asked for.
        pw = ttk.PanedWindow(root, orient="vertical")
        pw.pack(fill="both", expand=True, padx=18, pady=(2, 4))
        self.pw = pw

        top = ttk.Frame(pw)
        pw.add(top, weight=1)
        cols = ("file", "action", "platform", "type", "size", "status", "result", "folder")
        self.tree = ttk.Treeview(top, columns=cols, displaycolumns=("file", "action", "platform", "type", "size", "status", "result"),
                                 show="headings", selectmode="extended")
        for c, t, w, a in (("file", "File", 240, "w"), ("action", "Action  ▾", 150, "w"), ("platform", "Platform", 100, "w"),
                           ("type", "Disc type", 90, "w"), ("size", "Size", 90, "e"), ("status", "Status", 150, "w"),
                           ("result", "Result", 230, "w"), ("folder", "Folder", 300, "w")):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor=a, stretch=(c in ("file", "status", "result", "folder")))
        ys = ttk.Scrollbar(top, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(top, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        top.rowconfigure(0, weight=1)
        top.columnconfigure(0, weight=1)
        self.empty = ttk.Label(top, justify="center", style="Empty.TLabel", font=("Segoe UI", 12),
                               text="Drag files or folders here, or use “Add Files” / “Add Folder”\n\n"
                                    "Then choose what each one becomes in its Action column.\n"
                                    "CHD, CUE/BIN, GDI, ISO, CSO, PBP and CDI, plus MDS, NRG and CCD/IMG as sources")
        self.empty.place(relx=0.5, rely=0.45, anchor="center")
        self.picker, self.manual_cols, self.auto_w, self.tip, self.tip_after, self.tip_cell = None, set(), {}, None, None, None
        self.tree.bind("<Double-1>", self.on_tree_double)
        self.tree.bind("<ButtonPress-1>", self.on_tree_press, add="+")
        self.tree.bind("<ButtonRelease-1>", self.on_tree_release, add="+")
        self.tree.bind("<MouseWheel>", lambda e: self.close_picker(), add="+")
        self.tree.bind("<Motion>", self.on_tree_motion)
        self.tree.bind("<Leave>", lambda e: self.hide_tip())
        self.tree.bind("<Configure>", lambda e: self.root.after_idle(self.autofit_columns), add="+")
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self.on_right_click)
        root.bind("<Control-o>", lambda e: self.add_files_dialog())

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Disc type: Auto-detect", command=lambda: self.set_override("Auto"))
        self.menu.add_command(label="Disc type: force CD", command=lambda: self.set_override("CD"))
        self.menu.add_command(label="Disc type: force DVD", command=lambda: self.set_override("DVD"))
        self.menu.add_separator()
        self.menu.add_command(label="Info…", command=self.info_selected)
        self.menu.add_separator()
        self.menu.add_command(label="Open folder", command=self.open_selected_folder)
        self.menu.add_separator()
        self.menu.add_command(label="Remove from list", command=self.remove_selected)

        # the log (hidden until "Show log", or until something fails)
        self.logf = ttk.Frame(pw)
        self.logf.rowconfigure(0, weight=1)
        self.logf.columnconfigure(0, weight=1)
        self.logbox = tk.Text(self.logf, height=9, wrap="word", font=("Consolas", 9) if os.name == "nt" else "TkFixedFont", state="disabled")
        ls = ttk.Scrollbar(self.logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=ls.set)
        self.logbox.grid(row=0, column=0, sticky="nsew")
        ls.grid(row=0, column=1, sticky="ns")
        lb = ttk.Frame(self.logf)
        lb.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(lb, text="Copy log", style="Subtle.TButton", command=self.copy_log).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Save log…", style="Subtle.TButton", command=self.save_log).pack(side="left", padx=(0, 6))
        ttk.Button(lb, text="Clear log", style="Subtle.TButton", command=self.clear_log).pack(side="left")

        # ---- settings live in their own window, not on the main screen
        sw = tk.Toplevel(root)
        sw.withdraw()
        sw.title("CHD Manager settings")
        sw.transient(root)
        sw.minsize(700, 520)
        sw.protocol("WM_DELETE_WINDOW", sw.withdraw)
        self.settings_win = sw
        nb = ttk.Notebook(sw)
        nb.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        self.nb = nb
        self.build_options(nb)
        bf = ttk.Frame(sw)
        bf.pack(fill="x", padx=16, pady=(0, 14))
        ttk.Button(bf, text="Done", style="Accent.TButton", command=sw.withdraw).pack(side="right")

        for key in ("compression", "custom_comp", "hunk", "threads", "verify_after", "low_priority", "delete_src", "delete_forever",
                    "exists", "out_mode", "out_dir", "auto_suggest"):
            self.v[key].trace_add("write", lambda *a: self.refresh_summary())
        for key in ("dolphintool", "wii_common_key", "wii_korean_key", "wbfs_scrub", "rpcs3", "pkg_key", "switch_keys"):
            self.v[key].trace_add("write", lambda *a: self.refresh_wii())
        self.style_widgets()
        self.refresh_summary()
        self.refresh_chd_controls()
        self.root.after(120, self.fit_toolbar)
        self.enable_file_drop()

    def build_options(self, nb):
        v = self.v

        def tab(title):
            f = ttk.Frame(nb, padding=10)
            nb.add(f, text=title)
            return f

        def row(f, r, label, widget):
            ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            widget.grid(row=r, column=1, sticky="w", pady=3)

        # --- Suggested
        f = tab("Suggested")
        sf = ttk.Frame(f)
        sf.pack(fill="both", expand=True)
        self.sugg_text = tk.Text(sf, width=58, height=14, wrap="word", state="disabled", cursor="arrow")
        sg = ttk.Scrollbar(sf, command=self.sugg_text.yview)
        self.sugg_text.configure(yscrollcommand=sg.set)
        sg.pack(side="right", fill="y")
        self.sugg_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Frame(f)
        sb.pack(fill="x", pady=(8, 0))
        ttk.Button(sb, text="Apply suggestions", command=lambda: self.apply_suggestions(False)).pack(side="left")
        ttk.Checkbutton(sb, text="Apply automatically when files are added", variable=v["auto_suggest"]).pack(
            side="left", padx=12)
        self.sugg = None

        # --- Convert
        f = tab("Convert")
        cf = ttk.Frame(f)
        cb = ttk.Combobox(cf, textvariable=v["compression"], width=11, state="readonly",
                          values=["Default", "None", "Fast", "Smallest", "Custom"])
        cb.pack(side="left")
        ttk.Entry(cf, textvariable=v["custom_comp"], width=18).pack(side="left", padx=6)
        ttk.Label(cf, text="(custom: e.g. cdlz,cdzl,cdfl)", style="Muted.TLabel").pack(side="left")
        row(f, 0, "Compression", cf)
        hf = ttk.Frame(f)
        ttk.Entry(hf, textvariable=v["hunk"], width=10).pack(side="left")
        ttk.Label(hf, text="bytes (blank = default)", style="Muted.TLabel").pack(side="left", padx=6)
        row(f, 1, "Hunk size", hf)
        tf = ttk.Frame(f)
        ttk.Spinbox(tf, from_=0, to=64, textvariable=v["threads"], width=5).pack(side="left")
        ttk.Label(tf, text=f"0 = use all ({os.cpu_count()} available)", style="Muted.TLabel").pack(side="left", padx=6)
        row(f, 2, "Threads", tf)
        row(f, 3, "If output exists", ttk.Combobox(f, textvariable=v["exists"], width=11, state="readonly",
                                                    values=["Skip", "Overwrite", "Rename"]))
        ttk.Checkbutton(f, text="Run chdman at low priority (keeps Windows responsive)", variable=v["low_priority"]).grid(
            row=8, column=0, columnspan=2, sticky="w")
        ttk.Label(f, style="Muted.TLabel", wraplength=560, justify="left",
                  text="Tip: 'Fast' uses zlib only, the safest pick if an emulator rejects a CHD "
                       "(NetherSX2/AetherSX2 are reported to need zlib). 'None' makes full-size files, and chdman can't verify an uncompressed CHD.").grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Verify each CHD after creating it", variable=v["verify_after"]).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Send source files to the Recycle Bin after a VERIFIED CHD",
                        variable=v["delete_src"], command=self.on_delete_toggle).grid(
            row=5, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="Delete them permanently instead (frees the space at once; cannot be undone)",
                        variable=v["delete_forever"], command=self.on_forever_toggle).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=(22, 0))
        ttk.Checkbutton(f, text="Auto-create a CUE for a lone .bin (assumes MODE2/2352, typical PS1)",
                        variable=v["auto_cue"]).grid(row=7, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(f, text="Make an .m3u playlist for multi-disc games (files named like 'Game (Disc 1)', 'Game (Disc 2)')",
                        variable=v["make_m3u"]).grid(row=10, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Check that there is enough free disk space before each conversion (turn off only if you know better)",
                        variable=v["space_check"]).grid(row=11, column=0, columnspan=2, sticky="w")
        if os.name == "nt" or sys.platform.startswith("linux"):
            cf = ttk.LabelFrame(f, text=" Right-click menu in Windows Explorer " if os.name == "nt" else " Right-click menu in your file manager ", padding=10)
            cf.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(10, 0))
            self.ctx_label = ttk.Label(cf, style="Muted.TLabel", wraplength=560, justify="left")
            self.ctx_label.grid(row=0, column=0, columnspan=2, sticky="w")
            self.ctx_btn = ttk.Button(cf, command=self.on_ctx_toggle)
            self.ctx_btn.grid(row=1, column=0, sticky="w", pady=(8, 0))
            self.refresh_ctx()

        # --- Formats (what each file becomes is chosen in its Action column; this tab only holds the extras)
        f = tab("Formats")
        ttk.Label(f, style="Muted.TLabel", wraplength=600, justify="left",
                  text="What each file becomes is chosen in the Action column of the list; only choices that can work for that file are "
                       "offered. The options below are the extras for those formats.").grid(row=0, column=0, sticky="w", pady=(0, 8))

        g1 = ttk.LabelFrame(f, text=" Output file names ", padding=10)
        g1.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(g1, text="Always put converted discs in a subfolder (discs with several files get one automatically)",
                        variable=v["extract_subfolder"]).grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Label(g1, text="Name pattern (%FILENAME% %GAMEID% %TITLE% %REGION%):").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(g1, textvariable=v["name_format"], width=30).grid(row=1, column=1, padx=6, pady=(8, 0))
        ttk.Button(g1, text="Get game titles…", command=self.download_gamedb_ui).grid(row=1, column=2, pady=(8, 0))

        g2 = ttk.LabelFrame(f, text=" PlayStation 1 (PBP) ", padding=10)
        g2.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(g2, text="Base PBP for a real PSP/Vita (optional):").grid(row=0, column=0, columnspan=3, sticky="w")
        ttk.Entry(g2, textvariable=v["base_pbp"], width=44).grid(row=1, column=0)
        ttk.Button(g2, text="Browse…", command=self.pick_base_pbp).grid(row=1, column=1, padx=4)
        ttk.Button(g2, text="Download…", command=self.download_base_pbp_ui).grid(row=1, column=2)
        ttk.Label(g2, text="Artwork folder (optional; one subfolder per game):").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Entry(g2, textvariable=v["art_dir"], width=44).grid(row=3, column=0)
        ttk.Button(g2, text="Browse…", command=self.pick_art_dir).grid(row=3, column=1, padx=4)
        ttk.Checkbutton(g2, text="Draw a title card (game name and code) for PBPs that have no artwork of your own",
                        variable=v["title_cards"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
        pf = ttk.Frame(g2)
        pf.grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(pf, text="Compression (0 = none, 9 = smallest):").grid(row=0, column=0, sticky="w")
        ttk.Combobox(pf, textvariable=v["pbp_level"], width=4, state="readonly", values=list(range(10))).grid(row=0, column=1, padx=6)

        g3 = ttk.LabelFrame(f, text=" Wii and GameCube (RVZ, WIA, WBFS) ", padding=10)
        g3.grid(row=3, column=0, sticky="ew")
        ttk.Label(g3, text="RVZ / WIA compression:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(g3, textvariable=v["rvz_method"], width=7, state="readonly",
                     values=["zstd", "lzma", "lzma2", "bzip2", "none"]).grid(row=0, column=1, padx=(8, 4))
        ttk.Label(g3, text="level").grid(row=0, column=2)
        ttk.Combobox(g3, textvariable=v["rvz_level"], width=4, state="readonly", values=list(range(1, 10))).grid(row=0, column=3, padx=4)
        ttk.Label(g3, text="block").grid(row=0, column=4)
        ttk.Combobox(g3, textvariable=v["rvz_chunk"], width=8, state="readonly",
                     values=[32768, 65536, 131072, 262144, 524288, 1048576, 2097152]).grid(row=0, column=5, padx=4)
        ttk.Checkbutton(g3, text="WBFS: leave out data nothing uses (scrub)", variable=v["wbfs_scrub"]).grid(
            row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Button(g3, text="Wii setup (Dolphin or your key)…", command=self.show_wii_setup).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.wii_status = ttk.Label(g3, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.wii_status.grid(row=3, column=0, columnspan=6, sticky="w", pady=(4, 0))

        g4 = ttk.LabelFrame(f, text=" PlayStation 3 (game folder ↔ ISO, .pkg packages) ", padding=10)
        g4.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(g4, text="Split ISOs of more than 4 GB into .iso.0, .iso.1 … parts (only for FAT32 drives on a real PS3; leave off for RPCS3)",
                        variable=v["ps3_split"]).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(g4, text="Keep the PS3_UPDATE folder when making an ISO (a system update on real discs; no emulator uses it)",
                        variable=v["ps3_update"]).grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Label(g4, text="To unpack .pkg packages (a game, with its updates merged in), CHD Manager uses your own RPCS3 and contains no keys.\n"
                           "Show it where rpcs3.exe is (your RPCS3 and its games are never changed):").grid(row=2, column=0, sticky="w", pady=(10, 0))
        pr = ttk.Frame(g4)
        pr.grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(pr, textvariable=v["rpcs3"], width=44).grid(row=0, column=0)
        ttk.Button(pr, text="Browse…", command=self.pick_rpcs3).grid(row=0, column=1, padx=4)
        ttk.Label(g4, text="No RPCS3? Then give your own package key instead (32 letters and numbers, or a file that holds it).\n"
                           "CHD Manager contains no keys and never downloads them. RPCS3 is used first when both are set.").grid(
            row=4, column=0, sticky="w", pady=(10, 0))
        kf = ttk.Frame(g4)
        kf.grid(row=5, column=0, sticky="w", pady=(4, 0))
        self.pkg_key_entry = tk.StringVar()
        ttk.Entry(kf, textvariable=self.pkg_key_entry, width=44, show="•").grid(row=0, column=0)
        ttk.Button(kf, text="Use this key", command=self.take_pkg_key).grid(row=0, column=1, padx=4)
        ttk.Button(kf, text="Choose key file…", command=self.pick_pkg_key_file).grid(row=0, column=2)
        ttk.Button(kf, text="Forget the key", command=lambda: self.v["pkg_key"].set("")).grid(row=0, column=3, padx=4)
        self.rpcs3_status = ttk.Label(g4, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.rpcs3_status.grid(row=6, column=0, sticky="w", pady=(4, 0))

        g5 = ttk.LabelFrame(f, text=" Nintendo Switch (NSP ↔ XCI) ", padding=10)
        g5.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(g5, text="Some Switch games are locked with a ticket, and turning them into an XCI needs your own keys (your prod.keys).\n"
                           "CHD Manager contains no keys and never downloads them. Choose your prod.keys file, or open it in a text editor,\n"
                           "copy everything and paste it here. Only the few keys this uses are kept; nothing else is stored.").grid(row=0, column=0, sticky="w")
        self.sw_keys_box = tk.Text(g5, width=60, height=3, wrap="none")
        self.sw_keys_box.grid(row=1, column=0, sticky="w", pady=(6, 0))
        sf = ttk.Frame(g5)
        sf.grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Button(sf, text="Use these keys", command=self.take_switch_keys).grid(row=0, column=0)
        ttk.Button(sf, text="Choose prod.keys file…", command=self.pick_switch_keys_file).grid(row=0, column=1, padx=4)
        ttk.Button(sf, text="Forget the keys", command=lambda: self.v["switch_keys"].set("")).grid(row=0, column=2)
        self.sw_status = ttk.Label(g5, text="", style="Muted.TLabel", wraplength=560, justify="left")
        self.sw_status.grid(row=3, column=0, sticky="w", pady=(4, 0))

        # --- Output
        f = tab("Output")
        ttk.Label(f, text="Where the finished files go is chosen at the bottom of the main window (“Save to”).", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        ttk.Label(f, text="chdman.exe:").grid(row=3, column=0, sticky="w")
        self.chd_path_var = tk.StringVar(value=CHDMAN or "")
        ttk.Entry(f, textvariable=self.chd_path_var, width=34).grid(row=3, column=1, padx=6)
        ttk.Button(f, text="Browse…", command=self.pick_chdman).grid(row=3, column=2)
        ttk.Checkbutton(f, text="Check for a newer chdman at startup (only asks GitHub; never downloads by itself)",
                        variable=v["auto_update_check"]).grid(row=4, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Checkbutton(f, text="Also write every run's log to chd_manager_log.txt next to the program",
                        variable=v["auto_log"]).grid(row=5, column=0, columnspan=3, sticky="w")

        # --- About
        f = tab("About")
        ttk.Label(f, text=f"CHD Manager {APP_VER}", font=("Segoe UI Semibold", 14)).grid(row=0, column=0, columnspan=4, sticky="w")
        lf = ttk.Frame(f)
        lf.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        for i, (text, url) in enumerate((("GitHub page and downloads", f"https://github.com/{APP_REPO}"),
                                         ("Report a problem", f"https://github.com/{APP_REPO}/issues"),
                                         ("chdman (MAME project)", "https://github.com/mamedev/mame"))):
            lab = ttk.Label(lf, text=text, style="Link.TLabel", cursor="hand2")
            lab.grid(row=0, column=i, padx=(0, 18))
            lab.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))
        ttk.Label(f, style="Muted.TLabel", wraplength=600, justify="left",
                  text="Free and open source (GNU GPL v3 or later). chdman and the other bundled parts keep their own licenses, "
                       "listed in THIRD_PARTY_NOTICES.txt. Not affiliated with MAME. No games or BIOS files are included; "
                       "use it only with discs you own.").grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 6))
        ttk.Separator(f).grid(row=3, column=0, columnspan=4, sticky="ew", pady=4)
        self.app_upd_btn = ttk.Button(f, text="Check for CHD Manager update", command=lambda: self.check_app_update_async(True))
        self.app_upd_btn.grid(row=4, column=0, sticky="w", pady=(6, 0))
        self.app_upd_label = ttk.Label(f, text=f"You have {APP_VER}.", style="Muted.TLabel")
        self.app_upd_label.grid(row=4, column=1, columnspan=3, sticky="w", padx=(10, 0), pady=(6, 0))
        ttk.Checkbutton(f, text="Check for CHD Manager updates at startup (never installs by itself)",
                        variable=v["auto_app_check"]).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))
        bf = ttk.Frame(f)
        bf.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(bf, text="Copy diagnostics", command=self.copy_diagnostics).pack(side="left")
        ttk.Button(bf, text="Open program folder", command=lambda: open_folder(HERE)).pack(side="left", padx=8)


    # ------------------------------------------------------------------ look & feel
    def apply_ttk_theme(self):
        t = self.t
        st = ttk.Style()
        st.theme_use("clam")
        self.root.configure(bg=t["bg"])
        self.root.option_add("*TCombobox*Listbox.background", t["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", t["fg"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", t["accent"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        st.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["field"], bordercolor=t["border"],
                     lightcolor=t["border"], darkcolor=t["border"], troughcolor=t["trough"], focuscolor=t["bg"],
                     font=("Segoe UI", 10))
        st.configure("TLabel", background=t["bg"], foreground=t["fg"])
        st.configure("Muted.TLabel", foreground=t["muted"])
        st.configure("Warn.TLabel", foreground=t["warn"])
        st.configure("Link.TLabel", foreground=t["g2"] if self.v["theme"].get() == "dark" else t["accent"],
                     font=("Segoe UI", 10, "underline"))
        st.configure("Empty.TLabel", foreground=t["muted"], background=t["panel"])
        st.configure("TButton", background=t["btn"], foreground=t["fg"], borderwidth=0, padding=(10, 5), width=0,
                     focusthickness=0, relief="flat")
        st.map("TButton", background=[("disabled", t["bg"]), ("pressed", t["accent"]), ("active", t["btn_hover"])],
               foreground=[("disabled", t["muted"])])
        st.configure("Accent.TButton", background=t["accent"], foreground="#ffffff", font=("Segoe UI Semibold", 12),
                     padding=(26, 10))
        st.map("Accent.TButton", background=[("disabled", t["btn"]), ("pressed", t["accent2"]), ("active", t["accent2"])],
               foreground=[("disabled", t["muted"])])
        st.configure("Subtle.TButton", background=t["bg"], foreground=t["muted"], padding=(8, 3), font=("Segoe UI", 9))
        st.map("Subtle.TButton", background=[("active", t["btn"])], foreground=[("active", t["fg"]), ("disabled", t["muted"])])
        st.configure("SubtleWarn.TButton", background=t["bg"], foreground=t["warn"], padding=(8, 3), font=("Segoe UI Semibold", 9))
        st.map("SubtleWarn.TButton", background=[("active", t["btn"])], foreground=[("disabled", t["muted"])])
        st.configure("Treeview", background=t["panel"], fieldbackground=t["panel"], foreground=t["fg"], rowheight=31,
                     borderwidth=0, relief="flat")
        st.map("Treeview", background=[("selected", t["sel"])], foreground=[("selected", t["fg"])])
        st.configure("Treeview.Heading", background=t["head"], foreground=t["muted"], relief="groove", borderwidth=1,
                     bordercolor=t["border"], lightcolor=t["border"], darkcolor=t["border"],
                     font=("Segoe UI Semibold", 9), padding=(10, 8))
        st.map("Treeview.Heading", background=[("active", t["btn_hover"])])
        st.configure("TNotebook", background=t["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
        st.configure("TNotebook.Tab", background=t["btn"], foreground=t["muted"], padding=(16, 7), borderwidth=0,
                     font=("Segoe UI Semibold", 9))
        st.map("TNotebook.Tab", background=[("selected", t["panel2"])], foreground=[("selected", t["fg"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            st.configure(w, background=t["bg"], foreground=t["fg"], indicatorbackground=t["field"],
                         indicatorforeground="#ffffff")
            st.map(w, background=[("active", t["bg"])], indicatorbackground=[("selected", t["accent"]), ("active", t["btn_hover"])])
        st.configure("TCombobox", fieldbackground=t["field"], background=t["btn"], foreground=t["fg"], arrowcolor=t["fg"],
                     bordercolor=t["border"], selectbackground=t["field"], selectforeground=t["fg"])
        st.map("TCombobox", fieldbackground=[("readonly", t["field"])], foreground=[("readonly", t["fg"])],
               background=[("active", t["btn_hover"])])
        st.configure("TEntry", fieldbackground=t["field"], foreground=t["fg"], insertcolor=t["fg"], bordercolor=t["border"])
        st.configure("TSpinbox", fieldbackground=t["field"], foreground=t["fg"], arrowcolor=t["fg"], background=t["btn"],
                     insertcolor=t["fg"])
        st.configure("TScrollbar", background=t["btn"], troughcolor=t["panel"], arrowcolor=t["muted"], bordercolor=t["panel"],
                     relief="flat")
        st.map("TScrollbar", background=[("active", t["btn_hover"])])
        st.configure("TSeparator", background=t["border"])
        st.configure("TPanedwindow", background=t["bg"])
        st.configure("Accent.Horizontal.TProgressbar", background=t["accent"], troughcolor=t["trough"], thickness=12,
                     borderwidth=0, lightcolor=t["accent"], darkcolor=t["accent"])
        st.configure("Slim.Horizontal.TProgressbar", background=t["g2"], troughcolor=t["trough"], thickness=5,
                     borderwidth=0, lightcolor=t["g2"], darkcolor=t["g2"])

    def style_widgets(self):
        """Colors for the plain-tk widgets (they don't follow ttk styles)."""
        t = self.t
        self.root.configure(bg=t["bg"])
        self.logbox.configure(bg=t["panel"], fg=t["fg"], insertbackground=t["fg"], relief="flat", bd=0, padx=10, pady=8,
                              highlightthickness=1, highlightbackground=t["border"], highlightcolor=t["border"],
                              selectbackground=t["sel"])
        for tag in ("info", "cmd", "ok", "err", "warn"):
            self.logbox.tag_configure(tag, foreground=t[tag])
        for tag, key in (("done", "ok"), ("fail", "err"), ("skip", "skip"), ("work", "work")):
            self.tree.tag_configure(tag, foreground=t[key])
        self.tree.tag_configure("even", background=t["panel"])
        self.tree.tag_configure("odd", background=t["panel2"])
        self.sugg_text.configure(bg=t["panel2"], fg=t["fg"], relief="flat", bd=0, padx=12, pady=10, highlightthickness=0,
                                 selectbackground=t["sel"], font=("Segoe UI", 9))
        for tag, col, font in (("muted", t["muted"], None), ("key", t["fg"], ("Segoe UI Semibold", 10)),
                               ("val", t["g2"] if self.v["theme"].get() == "dark" else t["accent"], ("Segoe UI Semibold", 10)),
                               ("warn", t["warn"], ("Segoe UI Semibold", 9)), ("note", t["fg"], None),
                               ("ok", t["ok"], ("Segoe UI Semibold", 9))):
            self.sugg_text.tag_configure(tag, foreground=col)
            if font:
                self.sugg_text.tag_configure(tag, font=font)
        self.settings_win.configure(bg=t["bg"])
        self.more_menu.configure(bg=t["panel"], fg=t["fg"], activebackground=t["accent"], activeforeground="#ffffff",
                                 bd=0, relief="flat")
        self.menu.configure(bg=t["panel"], fg=t["fg"], activebackground=t["accent"], activeforeground="#ffffff",
                            bd=0, relief="flat")
        self.draw_header()

    def make_icon(self):
        img = tk.PhotoImage(width=32, height=32)
        for y in range(32):
            for x in range(32):
                d = ((x - 15.5) ** 2 + (y - 15.5) ** 2) ** 0.5
                if d <= 15:
                    col = "#10121a" if d <= 3.5 else "#ffffff" if d <= 5 else lerp_hex("#7c5cff", "#22d3ee", (x + y) / 62)
                    img.put(col, (x, y))
        self.root.iconphoto(True, img)
        self._icon = img

    def build_header(self):
        self.hdr = tk.Canvas(self.root, height=62, highlightthickness=0, bd=0)
        self.hdr.pack(fill="x")
        self.hdr_state, self.spin_angle, self.arcs = "idle", 0, []
        self.hdr.bind("<Configure>", lambda e: self.draw_header())

    def draw_header(self):
        c, t = self.hdr, self.t
        w, h = max(c.winfo_width(), 400), 62
        c.configure(bg=t["bg"])
        c.delete("all")
        n = max(1, w // 5)
        for i in range(n):
            c.create_rectangle(int(i * w / n), 0, int((i + 1) * w / n) + 1, h,
                               fill=lerp_hex(t["g1"], t["g2"], i / n), outline="")
        cx, cy, r = 40, 31, 20
        base = lerp_hex(t["g1"], t["g2"], cx / w)
        c.create_oval(cx - r - 3, cy - r - 3, cx + r + 3, cy + r + 3, fill=lerp_hex(base, "#000000", 0.25), outline="")
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="#f4f6ff", outline="")
        self.arcs = [c.create_arc(cx - r + 3, cy - r + 3, cx + r - 3, cy + r - 3, start=self.spin_angle + k * 90,
                                  extent=42, style="pieslice", fill=lerp_hex("#f4f6ff", base, 0.45), outline="")
                     for k in range(4)]
        c.create_oval(cx - 7, cy - 7, cx + 7, cy + 7, fill=base, outline="#ffffff", width=2)
        c.create_text(76, 23, text="CHD Manager", anchor="w", fill="#ffffff", font=("Segoe UI Semibold", 17))
        c.create_text(78, 47, text="Convert game disc images between formats  ·  CD / DVD detected for you", anchor="w",
                      fill="#eef0ff", font=("Segoe UI", 9))
        label, col = {"idle": ("●  Ready", "#ffffff"), "run": ("●  Working…", "#fff1b8"),
                      "done": ("✔  Finished", "#c9ffe4"), "fail": ("●  Finished with errors", "#ffd4d4")}[self.hdr_state]
        c.create_text(w - 20, 24, text=label, anchor="e", fill=col, font=("Segoe UI Semibold", 11))
        if self.app_update_available():
            nv = ".".join(str(x) for x in ver_tuple(self.app_latest["tag"]))
            c.create_text(w - 20, 46, text=f"▲  Update {nv} available - click", anchor="e", fill="#fff1b8",
                          font=("Segoe UI Semibold", 10), tags="updbadge")
            c.tag_bind("updbadge", "<Button-1>", lambda e: self.confirm_app_update())
            c.tag_bind("updbadge", "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind("updbadge", "<Leave>", lambda e: c.configure(cursor=""))

    def set_state(self, state):
        self.hdr_state = state
        self.draw_header()
        if state == "run":
            self.spin_tick()
        elif state in ("done", "fail"):
            self.root.after(9000, lambda: self.set_state("idle") if self.hdr_state in ("done", "fail") else None)

    def spin_tick(self):
        if self.hdr_state != "run":
            return
        self.spin_angle = (self.spin_angle + 16) % 360
        try:
            for k, item in enumerate(self.arcs):
                self.hdr.itemconfigure(item, start=self.spin_angle + k * 90)
            self.root.after(45, self.spin_tick)
        except tk.TclError:
            pass

    def toggle_theme(self):
        new = "light" if self.v["theme"].get() == "dark" else "dark"
        self.v["theme"].set(new)
        self.t = THEMES[new]
        self.more_menu.entryconfigure(self.theme_menu_index, label=self.theme_menu_label())
        self.apply_ttk_theme()
        self.style_widgets()
        self.restripe()

    def tags_for(self, iid, job):
        tag = ("done" if job.status.startswith("Done") else
               "fail" if job.status.startswith(("Failed", "Error", "Missing", "Verify failed", "No CUE")) else
               "skip" if job.status.startswith(("Skipped", "Stopped")) else
               "work" if job.status not in ("Queued",) else "")
        stripe = "odd" if self.tree.index(iid) % 2 else "even"
        return (tag, stripe) if tag else (stripe,)

    def restripe(self):
        for iid, job in self.tree_ids.items():
            self.tree.item(iid, tags=self.tags_for(iid, job))

    # ------------------------------------------------------------------ smart suggestions
    def refresh_suggestions(self, first_add=False):
        o = self.options()
        hint = None
        if o["out_mode"] == "custom" and o["out_dir"]:
            hint = Path(o["out_dir"])
            while not hint.exists() and hint != hint.parent:
                hint = hint.parent
        self.sugg = build_suggestions(self.jobs, hint)
        self.render_suggestions()
        if self.v["auto_suggest"].get():
            has_convert = any(j.kind == "convert" for j in self.jobs)
            self.apply_suggestions(quiet=True, hardware_only=not has_convert)   # PC-specific settings apply at startup
            if first_add and has_convert:
                self.refresh_summary()

    def render_suggestions(self):
        s, tx = self.sugg, self.sugg_text
        tx.configure(state="normal")
        tx.delete("1.0", "end")
        tx.insert("end", s["summary"] + "\n\n", "muted")
        for key, val, why in s["rows"]:
            tx.insert("end", f"{key}:  ", "key")
            tx.insert("end", val + "\n", "val")
            tx.insert("end", why + "\n\n", "muted")
        for w in s["warnings"]:
            tx.insert("end", "⚠  " + w + "\n\n", "warn")
        for n in s["notes"]:
            tx.insert("end", "•  " + n + "\n", "note")
        auto = self.v["auto_suggest"].get() and any(j.kind == "convert" for j in self.jobs)
        if auto:
            tx.insert("end", "\n✔ These settings were applied (see the settings line under the list).", "ok")
        tx.configure(state="disabled")

    def apply_suggestions(self, quiet=False, hardware_only=False):
        if not self.sugg:
            self.refresh_suggestions()
        for k, val in self.sugg["values"].items():
            if hardware_only and k not in ("threads", "low_priority"):
                continue
            self.v[k].set(val)
        if not quiet:
            self.status.config(text="Suggested settings applied.")
            self.log("Suggested settings applied: compression Default, hunk default, "
                     f"threads {self.sugg['values']['threads'] or 'all'}, low priority {'on' if self.sugg['values']['low_priority'] else 'off'}, skip existing, verify on.", "info")
        self.render_suggestions()

    # ------------------------------------------------------------------ drag and drop (native Windows)
    def enable_file_drop(self):
        """Let files/folders be dragged from Explorer onto the window. Uses the Windows shell API directly."""
        if os.name != "nt":
            return self.enable_file_drop_tkdnd()
        try:
            self.root.update_idletasks()
            user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            WM_DROPFILES, GWLP_WNDPROC = 0x0233, -4
            WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            user32.GetWindowLongPtrW.restype = ctypes.c_void_p
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongPtrW.restype = ctypes.c_void_p
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            user32.CallWindowProcW.restype = ctypes.c_ssize_t
            user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPWSTR, wintypes.UINT]
            shell32.DragQueryFileW.restype = wintypes.UINT
            shell32.DragFinish.argtypes = [wintypes.HANDLE]
            old_proc = user32.GetWindowLongPtrW(hwnd, GWLP_WNDPROC)

            def proc(h, msg, wparam, lparam):
                if msg == WM_DROPFILES:
                    paths = []
                    try:
                        count = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                        for i in range(count):
                            n = shell32.DragQueryFileW(wparam, i, None, 0)
                            buf = ctypes.create_unicode_buffer(n + 1)
                            shell32.DragQueryFileW(wparam, i, buf, n + 1)
                            paths.append(buf.value)
                    finally:
                        shell32.DragFinish(wparam)
                    if paths:
                        self.root.after(10, lambda: self.on_files_dropped(paths))
                    return 0
                return user32.CallWindowProcW(old_proc, h, msg, wparam, lparam)

            self._drop_proc = WNDPROC(proc)            # keep a reference or Python frees the callback
            user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(self._drop_proc, ctypes.c_void_p).value)
            shell32.DragAcceptFiles(hwnd, True)
            for msg in (WM_DROPFILES, 0x0049, 0x004A):   # also accept drops when the sender runs at another privilege level
                user32.ChangeWindowMessageFilterEx(hwnd, msg, 1, None)
            self.drop_hwnd = hwnd
        except Exception as e:  # noqa: BLE001
            self.log(f"Drag and drop is unavailable: {e}", "warn")

    dnd_ok = False

    def show_linux_setup(self, auto=False):
        """Linux and macOS: what this program needs from the system, why, whether it's there, and the command that installs it."""
        old = getattr(self, "linux_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                return
        except tk.TclError:
            pass
        w, fr = self.themed_dialog("What to install")
        self.linux_win = w
        try:
            import zstandard  # noqa: F401
            has_zstd = True
        except ImportError:
            has_zstd = False
        have = {"chdman": bool(CHDMAN), "tkdnd": self.dnd_ok, "zstandard": has_zstd}
        ttk.Label(fr, justify="left", wraplength=520, text=(
            "CHD Manager uses a few things from your system. Here is what each one is for, whether it was found, and the command "
            "that installs it. The program never installs anything by itself: you copy the command and run it in a terminal, and "
            "your system asks for your password.")).grid(row=0, column=0, columnspan=2, sticky="w")
        r = 1
        if linux_is_steamos():
            ttk.Label(fr, justify="left", wraplength=520, style="Muted.TLabel", text=(
                "This looks like a Steam Deck (SteamOS). Its system files are read-only, and a system update removes anything "
                "installed into them, so these commands may need repeating after an update. Treat this as experimental.")
                      ).grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))
            r += 1
        for piece in ("chdman", "tkdnd", "zstandard"):
            box = ttk.LabelFrame(fr, text=f" {LINUX_NAMES[piece]}: {'found' if have[piece] else 'not found'} ", padding=10)
            box.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(12, 0))
            r += 1
            ttk.Label(box, justify="left", wraplength=500, text=LINUX_WHY[piece]).grid(row=0, column=0, columnspan=2, sticky="w")
            if have[piece]:
                continue
            cmd = linux_install_command(piece)
            if cmd is None:
                ttk.Label(box, justify="left", wraplength=500, style="Muted.TLabel", text=(
                    "I couldn't match your system's package manager to a package for this. Look for it in your distribution's software "
                    "list" + (" (on Arch it is only in the AUR)." if piece == "tkdnd" else ", under the name 'mame-tools' or 'chdman'.")
                )).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
                continue
            field = ttk.Entry(box, width=48)
            field.insert(0, cmd)
            field.state(["readonly"])
            field.grid(row=1, column=0, sticky="w", pady=(8, 0))

            btn = ttk.Button(box, text="Copy command")
            btn.grid(row=1, column=1, padx=(8, 0), pady=(8, 0))

            def copy(c=cmd, b=btn):
                self.root.clipboard_clear()
                self.root.clipboard_append(c)
                b.config(text="Copied: paste it in a terminal")
            btn.config(command=copy)
        dont = tk.BooleanVar(value=self.v["linux_help_off"].get())
        ttk.Checkbutton(fr, text="Don't show this at start", variable=dont).grid(row=r, column=0, sticky="w", pady=(14, 0))

        def close():
            self.v["linux_help_off"].set(dont.get())
            self.save_settings()
            w.destroy()
        ttk.Button(fr, text="Close", style="Accent.TButton", command=close).grid(row=r, column=1, sticky="e", pady=(14, 0))
        w.protocol("WM_DELETE_WINDOW", close)
        self.place_dialog(w)

    def enable_file_drop_tkdnd(self):
        """Linux and macOS: drag and drop through the 'tkdnd' Tcl package when the system has it (Debian/Ubuntu: sudo apt install
        tkdnd). Without it everything else works; only dragging is missing, and the log says how to get it."""
        if self.silent:
            return
        tcl = self.root.tk
        try:
            tcl.eval("package require tkdnd")
        except tk.TclError:
            self.log("Drag and drop is off because the optional 'tkdnd' package isn't installed (More, 'What to install' explains). "
                     "Add Files and Add Folder work as usual.", "info")
            return
        self.dnd_ok = True
        try:
            cb = self.root.register(lambda data: self.root.after(10, lambda: self.on_files_dropped(list(tcl.splitlist(data)))))

            def walk(w):
                yield w
                for c in w.winfo_children():
                    if not isinstance(c, tk.Toplevel):
                        yield from walk(c)

            for w in walk(self.root):
                path = str(w)
                tcl.eval(f"tkdnd::drop_target register {path} DND_Files")
                tcl.eval("bind %s <<Drop:DND_Files>> {%s %%D; return %%A}" % (path, cb))
        except tk.TclError as e:
            self.log(f"Drag and drop is unavailable: {e}", "warn")

    def on_files_dropped(self, paths):
        self.log(f"Dropped {len(paths)} item(s).", "info")
        self.add_paths(paths)

    # ------------------------------------------------------------------ layout helpers (menu, settings window, log, summary)
    def footer_base(self, which):
        if which == "chdman":
            return f"chdman {self.chd_version}" if CHDMAN else "chdman not found"
        return f"CHD Manager {APP_VER}"

    def set_footer(self, which, note="", tone="muted", revert_after=0):
        """Each footer button shows its component and version; a short note (checking, up to date, update available)
        is appended and can clear itself after a few seconds."""
        btn = self.chdman_btn if which == "chdman" else self.app_btn
        self._footer_note[which] = note
        btn.config(text=self.footer_base(which) + (f"  ·  {note}" if note else ""),
                   style="SubtleWarn.TButton" if tone == "warn" else "Subtle.TButton")
        self.root.after(50, self.fit_toolbar)
        if revert_after:
            self.root.after(revert_after, lambda: self.set_footer(which) if self._footer_note.get(which) == note else None)

    def app_footer_clicked(self):
        if self.running:
            self.status.config(text="Wait for the current jobs to finish before updating CHD Manager.")
            self.busy_notice("updating CHD Manager")
            return
        if self.app_update_available():
            self.confirm_app_update()
        else:
            self.check_app_update_async(True)

    # ------------------------------------------------------------------ what each file becomes (its Action drop-down)
    def refresh_chd_controls(self):
        self.root.after(50, self.fit_toolbar)

    def fmt_for(self, job, o=None):
        """How the CHD extraction code names this row's target."""
        return TARGET_FMT.get((job.target or "").partition("_d")[0], "auto")

    def action_choices(self, job):
        """[(label, target key)] offered in the Action drop-down of one row: only what can work for that item."""
        return [(label, key) for key, label in job.available_targets()]

    cso2_ok = False

    def confirm_cso2(self):
        """CSO version 2 plays on a real PSP but not in PPSSPP: say so once, and let the user back out."""
        if self.silent or self.cso2_ok:
            return True
        self.cso2_ok = messagebox.askokcancel(
            APP_NAME, "CSO version 2 is an experimental layout.\n\nIt has been tested on one real PSP, where it plays. The PPSSPP "
                      "emulator refuses it ('CSO version too high'), and other emulators and tools may too.\n\n"
                      "Version 1 (the plain CSO choice) works everywhere. Use version 2 only if you know you need it.\n\nUse version 2?",
            icon="warning")
        return self.cso2_ok

    def pick_action(self, job, choice):
        if choice[1] == "cso2" and not self.confirm_cso2():
            self.close_picker()
            return
        job.set_target(choice[1])
        self.close_picker()
        self.update_action_labels()
        self.update_status_idle()

    GENERIC_TARGETS = (("chd", "→ CHD"), ("cue", "→ CUE/BIN"), ("iso", "→ ISO"), ("pbp", "→ PlayStation PBP"),
                       ("cdi", "→ DiscJuggler CDI"), ("vcd", "→ POPStarter VCD"), ("cso", "→ CSO"), ("cso2", "→ CSO version 2 (real PSP only)"), ("zar", "→ ZAR (Xbox 360)"), ("folder", "→ Game folder (Xbox 360)"), ("zso", "→ ZSO"), ("ecm", "→ ECM"),
                       ("rvz", "→ RVZ (Wii / GameCube)"), ("wbfs", "→ WBFS (Wii)"), ("gcz", "→ GCZ"), ("wux", "→ WUX (Wii U)"), ("wud", "→ WUD (Wii U)"), ("verify", "Verify (CHD files)"),
                       ("recompress", "Recompress (CHD files)"))

    def set_all_targets(self, key, label):
        """From the Action heading: give every file that can do it the same target; the rest keep their own."""
        if key == "cso2" and not self.confirm_cso2():
            return
        done = 0
        for j in self.jobs:
            have = [k for k, _ in j.available_targets()]
            pick = next((k for k in (("cue", "cue_split") if key == "cue" else (key,)) if k in have), None)
            if pick and not j.status.startswith("Done"):
                j.set_target(pick)
                done += 1
        self.update_action_labels()
        self.update_status_idle()
        self.status.config(text=f"{done} of {len(self.jobs)} item(s) set to “{label}”; the others can't do that and keep their own choice.")
        left = [j for j in self.jobs if not j.status.startswith("Done") and j.target not in (key, "cue_split" if key == "cue" else key)]
        if left and done:
            self.report_problems(
                f"{len(left)} item{'s' if len(left) != 1 else ''} can't become “{label}”:",
                [(j.path.name, sentence(f"this kind of file can't be turned into that, so it keeps its own choice ({target_label(j.target).lstrip('→ ')})")) for j in left],
                "The other items were changed as you asked.")
        elif left and not done:
            self.notify(f"None of the items in the list can become “{label}”, so nothing was changed.")

    def close_picker(self):
        pk = getattr(self, "picker", None)
        self.picker = None
        if pk is not None:
            try:
                pk.destroy()
            except tk.TclError:
                pass

    # ---- column widths: sized to their text, until the user drags a divider (double-click a divider to fit it again)
    def tree_font(self):
        from tkinter import font as tkfont
        if getattr(self, "_tf", None) is None:
            self._tf = tkfont.Font(font=ttk.Style().lookup("Treeview", "font") or "TkDefaultFont")
        return self._tf

    def autofit_columns(self, only=None):
        cols = self.tree["displaycolumns"]
        avail = self.tree.winfo_width()
        if avail < 200 or self.running and only is None:
            return
        f, pad = self.tree_font(), 26
        caps = {"file": 520, "result": 520, "status": 260, "action": 330, "platform": 140, "type": 150, "size": 110}
        floor = {"file": 150, "result": 120, "status": 110, "action": 110, "platform": 80, "type": 70, "size": 70}
        want = {}
        for c in cols:
            texts = [self.tree.heading(c, "text")] + [self.tree.set(i, c) for i in self.tree.get_children()]
            want[c] = min(caps[c], max(f.measure(t) for t in texts) + pad)
        free = [c for c in ("file", "result") if c not in self.manual_cols]
        over = sum(want[c] if c not in self.manual_cols else self.tree.column(c, "width") for c in cols) - avail
        for c in sorted(free, key=lambda c: -want[c]):        # too wide for the window: trim the two long columns first
            if over > 0:
                cut = min(over, want[c] - floor[c])
                want[c] -= cut
                over -= cut
        for c in cols:
            if c in self.manual_cols and only != c:
                continue
            self.manual_cols.discard(c)
            self.auto_w[c] = want[c]
            self.tree.column(c, width=want[c])

    def on_tree_press(self, e):
        self.close_picker()
        region = self.tree.identify_region(e.x, e.y)
        if region == "heading" and self.tree.identify_column(e.x) == "#2" and self.jobs and not self.running:
            m = tk.Menu(self.root, tearoff=0)
            m.add_command(label="Set every file that can, to:", state="disabled")
            for key, label in self.GENERIC_TARGETS:
                m.add_command(label=label, command=lambda k=key, lab=label: self.set_all_targets(k, lab))
            m.tk_popup(e.x_root, e.y_root)
        self.sep_drag = region == "separator"
        self.press_w = {c: self.tree.column(c, "width") for c in self.tree["displaycolumns"]}

    def on_tree_double(self, e):
        region = self.tree.identify_region(e.x, e.y)
        if region == "separator":
            col = self.tree.identify_column(e.x)
            self.manual_cols.discard(self.tree["displaycolumns"][int(col[1:]) - 1])
            self.autofit_columns(only=self.tree["displaycolumns"][int(col[1:]) - 1])
        elif not (region == "cell" and self.tree.identify_column(e.x) == "#2"):
            self.info_selected()

    def note_manual_resize(self):
        """After a divider drag, remember which columns the user sized themselves."""
        for c in self.tree["displaycolumns"]:
            if self.tree.column(c, "width") != self.press_w.get(c):
                self.manual_cols.add(c)

    def hide_tip(self):
        if self.tip_after:
            self.root.after_cancel(self.tip_after)
            self.tip_after = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None
        self.tip_cell = None

    def on_tree_motion(self, e):
        cell = (self.tree.identify_row(e.y), self.tree.identify_column(e.x)) if self.tree.identify_region(e.x, e.y) == "cell" else None
        if cell == self.tip_cell:
            return
        self.hide_tip()
        self.tip_cell = cell
        if cell and cell[0]:
            self.tip_after = self.root.after(500, lambda: self.show_tip(cell, e.x_root, e.y_root))

    def show_tip(self, cell, x, y):
        iid, col = cell
        name = self.tree["displaycolumns"][int(col[1:]) - 1]
        text = self.tree.set(iid, name)
        if not text or self.tree_font().measure(text) + 14 <= self.tree.column(name, "width"):
            return
        if name == "file":
            job = self.tree_ids.get(iid)
            text = str(job.path) if job else text
        self.tip = tk.Toplevel(self.root)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x + 14}+{y + 18}")
        tk.Label(self.tip, text=text, bg=self.t["field"], fg=self.t["fg"], relief="solid", borderwidth=1, padx=6, pady=3,
                 wraplength=700, justify="left", font=("Segoe UI", 9)).pack()

    def on_tree_release(self, e):
        if getattr(self, "sep_drag", False):
            self.sep_drag = False
            self.note_manual_resize()
            return
        """A click on a row's Action cell opens a drop-down with every choice that fits that item."""
        if self.tree.identify_region(e.x, e.y) != "cell" or self.tree.identify_column(e.x) != "#2" or e.state & 0x5:
            return
        if self.running or self.adding:
            if not self._busy_told:                       # once per run, so clicking around never turns into a stream of windows
                self._busy_told = True
                self.busy_notice("changing what a file becomes")
            return
        iid = self.tree.identify_row(e.y)
        job = self.tree_ids.get(iid)
        if job is None or not job.can_pick or job.status.startswith("Done"):
            return
        box = self.tree.bbox(iid, "action")
        if not box:
            return
        self.close_picker()
        choices = self.action_choices(job)
        cb = ttk.Combobox(self.tree, state="readonly", values=[c[0] for c in choices])
        cb.place(x=box[0], y=box[1], width=max(box[2], 290), height=box[3])
        cb.bind("<<ComboboxSelected>>", lambda ev: self.pick_action(job, choices[cb.current()]))
        cb.set(job.action_label().replace("  ▾", ""))
        cb.bind("<Escape>", lambda ev: self.close_picker())
        self.picker = cb
        cb.focus_set()
        try:
            cb.tk.call("ttk::combobox::Post", str(cb))
        except tk.TclError:
            pass

    def wants_split(self, job, o=None):
        """One BIN per track: chosen in the Action drop-down."""
        fmt = self.fmt_for(job, o)
        return fmt == "split" or (fmt == "auto" and job.tracks > 1 and job.target != "cue_single")

    def action_text_for(self, job):
        if job.target == "chd" and job.disc_count > 1:
            return f"→ {job.disc_count} CHDs"
        return target_label(job.target or "") or "Extract"

    def update_action_labels(self):
        for j in self.jobs:
            j.action_text = self.action_text_for(j)
            self.refresh_job(j)
        self.refresh_chd_controls()
        self.root.after_idle(self.autofit_columns)

    def plan_text(self):
        c = {}
        for j in self.jobs:
            lab = target_label(j.target or "")
            c[lab] = c.get(lab, 0) + 1
        return "  ·  ".join(f"{n} {lab}" for lab, n in c.items())

    def theme_menu_label(self):
        return "Switch to the light theme" if self.v["theme"].get() == "dark" else "Switch to the dark theme"

    def show_more_menu(self):
        self.more_menu.update_idletasks()
        x = self.more_btn.winfo_rootx() + self.more_btn.winfo_width() - self.more_menu.winfo_reqwidth()
        self.more_menu.tk_popup(max(x, 0), self.more_btn.winfo_rooty() + self.more_btn.winfo_height())

    def open_settings(self, tab="Convert"):
        w = self.settings_win
        if not getattr(self, "_settings_placed", False):
            w.update_idletasks()        # on a scaled display the pages are bigger than 780x700: fit the tallest one
            ww = min(max(780, w.winfo_reqwidth() + 8), w.winfo_screenwidth() - 60)
            hh = min(max(700, w.winfo_reqheight() + 8), w.winfo_screenheight() - 90)
            w.geometry(f"{ww}x{hh}+{max(self.root.winfo_rootx() + 80, 0)}+{max(self.root.winfo_rooty() + 40, 0)}")
            self._settings_placed = True
        w.deiconify()
        w.lift()
        names = {self.nb.tab(i, "text"): i for i in range(self.nb.index("end"))}
        if tab in names:
            self.nb.select(names[tab])

    def show_log(self, show):
        shown = str(self.logf) in self.pw.panes()
        if show and not shown:
            self.pw.add(self.logf, weight=1)
        elif not show and shown:
            self.pw.forget(self.logf)
        self.log_btn.config(text="Hide log" if show else "Show log")

    def toggle_log(self):
        self.show_log(str(self.logf) not in self.pw.panes())

    def summary_text(self):
        o = self.options()
        comp = o["compression"] if o["compression"] != "Custom" else f"custom ({o['custom_comp'].strip() or 'default'})"
        parts = [f"{comp.lower()} compression" if o["compression"] != "Default" else "default compression",
                 f"{o['threads']} threads" if o["threads"] else "all threads"]
        if str(o["hunk"]).strip():
            parts.append(f"hunk {str(o['hunk']).strip()}")
        parts.append("verify after" if o["verify_after"] else "no verify")
        parts.append({"Skip": "skip existing", "Overwrite": "overwrite existing", "Rename": "rename if exists"}.get(o["exists"], "skip existing"))
        parts.append("saved next to the originals" if o["out_mode"] == "same" or not o["out_dir"]
                     else f"saved to {Path(o['out_dir']).name or o['out_dir']}")
        if o["low_priority"]:
            parts.append("low priority")
        if o["delete_src"]:
            parts.append("delete originals for good after verify" if o["delete_forever"] else "recycle originals after verify")
        lead = "Suggested settings:  " if o["auto_suggest"] else "Settings:  "
        return lead + "  ·  ".join(parts)

    def refresh_summary(self):
        try:
            self.summary_label.config(text=self.summary_text())
        except (tk.TclError, AttributeError):
            pass

    def fit_toolbar(self):
        """Keep the window wide enough that no toolbar button is squeezed (the label text changes at runtime)."""
        try:
            self.root.update_idletasks()
            need = self.toolbar.winfo_reqwidth() + 24
            self.root.minsize(max(900, need), 580)
            if self.root.winfo_width() < need:
                self.root.geometry(f"{need}x{self.root.winfo_height()}")
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ settings
    def save_settings(self):
        if self.no_save:
            return
        d = {k: var.get() for k, var in self.v.items()}
        d["chdman"] = self.chd_path_var.get() if hasattr(self, "chd_path_var") else ""
        d["last_dir"] = self.settings.get("last_dir", "")
        d["app_dir"] = str(HERE)                  # lets the next start notice that the program folder was moved
        try:
            d["geometry"] = self.root.geometry()
        except tk.TclError:
            d["geometry"] = ""
        try:
            SETTINGS_FILE.write_text(json.dumps(d, indent=2), "utf-8")
        except OSError:
            pass

    def on_close(self):
        if self.adding:                                   # closing in the middle of reading files would tear the list apart: finish first
            self._close_when_done = True
            self.status.config(text="Closing as soon as the files have been added…")
            return
        if self.running:
            if not self.ask("A job is running. Stop it and quit?"):
                return
            self.stop()
        self.save_settings()
        self.drop_all_arcs()
        self.root.destroy()

    def ask(self, msg):
        return getattr(self, "silent_answer", True) if self.silent else messagebox.askyesno(APP_NAME, msg)

    def on_delete_toggle(self):
        if self.v["delete_src"].get() and not self.silent:
            messagebox.showinfo(APP_NAME, "Source files will go to the Recycle Bin (so you can restore them), "
                                          "and only after the new CHD has been created AND verified. "
                                          "Verification is turned on automatically.")
            self.v["verify_after"].set(True)

    def on_forever_toggle(self):
        """Ticking 'delete permanently' asks once; it also switches on the verified-delete option it belongs to."""
        if not self.v["delete_forever"].get():
            return
        if not self.silent and not messagebox.askyesno(
                APP_NAME, "Permanently delete the source files after each CHD is created and verified?\n\n"
                          "They will NOT go to the Recycle Bin and cannot be restored. A source is only deleted once its "
                          "new CHD has passed verification; a CHD that can't be verified never causes a delete.",
                icon="warning", default="no"):
            self.v["delete_forever"].set(False)
            return
        self.v["delete_src"].set(True)
        self.v["verify_after"].set(True)

    def pick_out_dir(self):
        d = filedialog.askdirectory(title="Output folder")
        if d:
            self.v["out_dir"].set(d)
            self.v["out_mode"].set("custom")

    def pick_chdman(self):
        global CHDMAN
        p = filedialog.askopenfilename(title="Select chdman.exe", filetypes=[("chdman", "chdman.exe"), ("All", "*.*")])
        if p:
            CHDMAN = p
            self.chd_path_var.set(p)
            self.chd_version, self.has_dvd = chdman_info()
            self.set_footer("chdman")
            self.root.after(50, self.fit_toolbar)
            self.log(f"Using {p} (chdman {self.chd_version})", "info")

    # ------------------------------------------------------------------ queue mgmt
    def add_files_dialog(self):
        files = filedialog.askopenfilenames(
            title="Select disc images or CHD files", initialdir=self.settings.get("last_dir") or None,
            filetypes=[("Disc images / CHD", "*.cue *.gdi *.iso *.toc *.bin *.pbp *.cdi *.vcd *.zar *.mds *.nrg *.ccd *.img *.m3u *.chd *.pkg"), ("All files", "*.*")])
        if files:
            self.settings["last_dir"] = str(Path(files[0]).parent)
            self.add_paths(files)

    def add_folder_dialog(self):
        d = filedialog.askdirectory(title="Add a folder (searched recursively)",
                                    initialdir=self.settings.get("last_dir") or None)
        if d:
            self.settings["last_dir"] = d
            self.add_paths([d])

    DUP_ITEM, DUP_ARCHIVE = "This is already in the list.", "This archive is already in the list."

    def refuse(self, name, why, tag="err", logline=None):
        """Something was turned away: the log says so now, and it is collected for one clear window when the batch is done."""
        self.log(logline or f"{name}: {why}", tag)
        self._problems.append((name, why))

    def report_problems(self, headline, rows, outro=""):
        """One window that lists what was refused and why, so nobody has to dig through the log. (The log keeps the details.)
        The self-test and the command line stay quiet and keep a copy in self.notices."""
        if not rows:
            return
        self.notices.append((headline, list(rows), outro))
        if self.silent:
            return
        shown = rows[:200]
        body = "\n\n".join(f"{n}\n    {r}" for n, r in shown) + (f"\n\n…and {len(rows) - 200} more (see the log)." if len(rows) > 200 else "")
        try:
            w, fr = self.themed_dialog(APP_NAME)
            w.resizable(True, True)
            fr.columnconfigure(0, weight=1)
            fr.rowconfigure(1, weight=1)
            ttk.Label(fr, text=headline, justify="left", wraplength=560, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w")
            lines = sum(2 + len(r) // 70 for _n, r in shown) + len(shown)
            box = tk.Text(fr, width=72, height=max(4, min(16, lines)), wrap="word", relief="flat", padx=8, pady=6,
                          bg=self.t["field"], fg=self.t["fg"], font=("Segoe UI", 10))
            sb = ttk.Scrollbar(fr, command=box.yview)
            box.config(yscrollcommand=sb.set)
            box.tag_configure("name", font=("Segoe UI", 10, "bold"))
            for n, r in shown:
                box.insert("end", n + "\n", "name")
                box.insert("end", "    " + r + "\n\n")
            if len(rows) > 200:
                box.insert("end", f"…and {len(rows) - 200} more (see the log).")
            box.config(state="disabled")
            box.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
            sb.grid(row=1, column=1, sticky="ns", pady=(10, 0))
            if outro:
                ttk.Label(fr, text=outro, justify="left", wraplength=560, style="Muted.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
            bt = ttk.Frame(fr)
            bt.grid(row=3, column=0, columnspan=2, sticky="e", pady=(14, 0))

            def copy():
                self.root.clipboard_clear()
                self.root.clipboard_append(headline + "\n\n" + body)
                cb.config(text="Copied")
            cb = ttk.Button(bt, text="Copy", command=copy)
            cb.pack(side="left", padx=(0, 8))
            ok = ttk.Button(bt, text="OK", style="Accent.TButton", command=w.destroy)
            ok.pack(side="left")
            w.bind("<Return>", lambda _e: w.destroy())
            w.bind("<Escape>", lambda _e: w.destroy())
            self.place_dialog(w)
            w.grab_set()
            ok.focus_set()
            w.wait_window()
        except Exception:  # noqa: BLE001  (a fault while drawing the window must still tell the person)
            messagebox.showwarning(APP_NAME, headline + "\n\n" + body[:1500])

    def add_paths(self, paths):
        """Read files, folders and archives into the list. While that works the window shows how far it is and Start waits;
        a second drop that arrives meanwhile is queued and handled right after the first."""
        if self.adding:
            self._add_queue.append(list(paths))
            return
        self.adding, self._busy_told = True, False
        self.start_btn.config(state="disabled")
        todo = [list(paths)]
        try:
            while todo:
                self._add_batch(todo.pop(0))
                todo += self._add_queue
                self._add_queue = []
        finally:
            self.adding = False
            if not self.running:
                self.start_btn.config(state="normal")
                self.cur_bar["value"] = 0
            self.update_status_idle()
        if self._close_when_done:
            self.on_close()

    def add_progress(self, done, total, text):
        """The status line and the big bar while files are being read (the bars belong to the conversion once one runs)."""
        if self.running:
            return
        self.status.config(text=text)
        self.cur_bar["value"] = 100 * done / max(1, total)
        self.root.update()

    def _add_batch(self, paths):
        self._problems = []
        added = 0
        try:
            added = self._add_paths(paths) or 0
        finally:
            rows, self._problems = self._problems, []
        if added:                                          # repeats among new items are harmless: the log mentions them, no window
            rows = [r for r in rows if r[1] not in (self.DUP_ITEM, self.DUP_ARCHIVE)]
        if rows:
            n = len(rows)
            self.report_problems(
                f"{n} item{'s' if n != 1 else ''} could not be added, or can't be converted yet:", rows,
                "Nothing on your disk was changed or deleted. Everything else was added as usual; the log has the details.")

    def _add_paths(self, paths):
        found = []   # (path, root)
        picked = sheet_refs(p for p in map(Path, paths) if not p.is_dir())      # what the .cue/.gdi/.toc files picked here point to
        self.add_progress(0, 1, "Looking at what you added…")
        for p in map(Path, paths):
            if p.is_dir():
                self.add_progress(0, 1, f"Looking through {p.name}…")
                files, ignored = self.scan_folder(p)
                found += [(f, p) for f in files]
                if not files and not ignored:
                    self.refuse(p.name, "No disc images or game folders (Xbox 360, PlayStation 3) were found in this folder (its sub-folders were searched too).", "warn")
                if ignored:
                    self.refuse(p.name, sentence(f"{sum(ignored.values())} {', '.join(sorted(ignored))} file(s) were left out: an .mdf is only the data half "
                                                 "of a disc image, and it needs its .mds file next to it"), "warn",
                                f"Ignored {sum(ignored.values())} file(s) ({', '.join(sorted(ignored))}): keep an .mdf next to its .mds.")
            elif p.suffix.lower() not in (".cue", ".gdi", ".toc") and str(p).lower() in picked:
                self.log(f"{p.name} belongs to {picked[str(p).lower()]}, so it is not added on its own.", "info")
            elif p.suffix.lower() in ARCHIVE_EXTS:
                found.append((p, None))
            elif split_set(p) is not None:                  # one piece of a split PS3 disc image: the first piece stands for the whole set
                if (split_first(p), None) not in found:
                    found.append((split_first(p), None))
            elif p.suffix.lower() in SRC_EXTS | {".chd"}:
                found.append((image_primary(p), None))
            else:
                self.refuse(p.name, sentence(f"CHD Manager doesn't convert {p.suffix.lower() or 'this kind of'} files, so it was not added"), "warn",
                            f"Skipped (unsupported type): {p.name}")
        refs = sheet_refs(f for f, _r in found)          # a disc file that a sheet in this batch already covers is part of that sheet
        mine = [(f, r) for f, r in found if f.suffix.lower() in (".cue", ".gdi", ".toc") or str(f).lower() not in refs]
        for f, _r in found:
            if (f, _r) not in mine:
                self.log(f"{f.name} belongs to {refs[str(f).lower()]}, so it is not added on its own.", "info")
        found = mine
        jobs_from = []   # (file, root, archive, folder it was unpacked into)
        known = {str(j.archive).lower() for j in self.jobs if j.archive}
        for f, root in found:
            if f.suffix.lower() in ARCHIVE_EXTS:
                if str(f).lower() in known:
                    self.refuse(f.name, self.DUP_ARCHIVE, "info")
                    continue
                files, tmp = self.open_archive(f)
                jobs_from += [(x, None, f, tmp) for x in files]
            else:
                jobs_from.append((f, root, None, None))
        have = {str(j.path).lower() for j in self.jobs}
        have |= {str(x).lower() for j in self.jobs if j.container in ("ps3pkg", "swnsp", "swxci") for x in j.src_files}
        listed = set(have)
        was_empty = not self.jobs
        added = 0
        for idx, (f, root, arc, tmp) in enumerate(jobs_from):
            self.add_progress(idx, len(jobs_from), f"Adding {idx + 1} of {len(jobs_from)}: {f.name}")
            if str(f).lower() in have:
                if str(f).lower() in listed:                # a .cue and its .bin picked together are one item: only a real repeat is worth saying
                    self.refuse(f.name, self.DUP_ITEM, "info")
                continue
            try:
                job = Job(f, root)
                job.analyze()
            except Exception as e:  # noqa: BLE001
                self.refuse(f.name, plain_exc(e), "err", f"Could not read {f.name}: {e}")
                continue
            if arc:
                job.archive, job.extract_dir, job.home, job.src_files = arc, tmp, arc.parent / f.name, [arc]
                self.arc_dirs[tmp] += 1
            if job.container in ("swnsp", "swxci") and job.nsps[0]["base"]:
                mate = next((j for j in self.jobs if j.container in ("swnsp", "swxci") and j.nsps[0]["base"] == job.nsps[0]["base"]
                             and not j.status.startswith("Done")), None)
                if mate is not None:
                    new = job.nsps[0]
                    mine = {(t["tid"], t["version"]) for t in new["titles"]}
                    twin = next((i for i in mate.nsps if mine <= {(t["tid"], t["version"]) for t in i["titles"]}), None)
                    if twin is not None:
                        self.refuse(f.name, sentence(f"it is the same package as {twin['path'].name}, which is already in the list"), "warn")
                        continue
                    mate.add_nsp(job)
                    have.add(str(f).lower())
                    added += 1
                    self.refresh_job(mate)
                    self.log(f"{f.name} is for the same game as {mate.nsps[0]['path'].name}, so they are one item: the game, then its newest update, then its extra content. Where the same update or extra content is in both, the newest version is used.", "info")
                    continue
            if job.container == "ps3pkg":
                mate = next((j for j in self.jobs if j.container == "ps3pkg" and j.pkgs[0]["title_id"] == job.pkgs[0]["title_id"]
                             and not j.status.startswith("Done")), None)
                if mate is not None:
                    new = job.pkgs[0]
                    twin = next((i for i in mate.pkgs if i["content_id"] == new["content_id"] and i["install_dir"] == new["install_dir"] and i["patch"] == new["patch"]
                                  and i["version"] == new["version"]), None)
                    if twin is not None:
                        self.refuse(f.name, sentence(f"it is the same package as {twin['path'].name}, which is already in the list"), "warn")
                        continue
                    mate.add_pkg(job)
                    have.add(str(f).lower())
                    added += 1
                    self.refresh_job(mate)
                    self.log(f"{f.name} is for the same game as {mate.pkgs[0]['path'].name}, so they are one item: installed in order, base game first, then its updates.", "info")
                    continue
            have.add(str(f).lower())
            self.jobs.append(job)
            iid = self.tree.insert("", "end", values=job.row(), tags=("odd" if len(self.tree_ids) % 2 else "even",))
            self.tree_ids[iid] = job
            added += 1
            if job.warn:
                self.refuse(job.path.name, sentence("it was added, but " + job.warn), "warn")
            if job.missing:
                self.refuse(job.path.name, sentence("it was added, but it won't convert: the files it points to weren't found next to it ("
                                                   + ", ".join(job.missing) + ")"), "warn",
                            f"{job.path.name}: missing referenced file(s): {', '.join(job.missing)}")
        self.add_progress(len(jobs_from), len(jobs_from), "Finishing up…")
        self.refresh_empty()
        self.log(f"Added {added} item(s).", "info")
        self.update_action_labels()
        self.update_status_idle()
        if added:
            self.log("Click the Action cell of a file to choose what it becomes; click the Action heading to set them all.", "info")
        if added:
            self.refresh_suggestions(first_add=was_empty)
        return added

    def refresh_empty(self):
        if self.jobs:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")

    def selected_jobs(self):
        return [self.tree_ids[i] for i in self.tree.selection() if i in self.tree_ids]

    def busy_notice(self, what):
        if self.adding:
            self.notify(f"Files are still being added, so {what} has to wait a moment. The bar at the bottom shows how far it is.")
        else:
            self.notify(f"A conversion is running, so {what} has to wait. Let it finish, or press Stop first.")

    def remove_selected(self):
        if self.running or self.adding:
            self.busy_notice("removing files")
            return
        for iid in self.tree.selection():
            job = self.tree_ids.pop(iid, None)
            if job in self.jobs:
                self.jobs.remove(job)
                self.drop_arc(job)
            self.tree.delete(iid)
        self.restripe()
        self.refresh_empty()
        self.update_status_idle()
        self.refresh_suggestions()
        self.refresh_chd_controls()
        self.root.after_idle(self.autofit_columns)

    def clear_all(self):
        if self.running or self.adding:
            self.busy_notice("clearing the list")
            return
        self.tree.delete(*self.tree.get_children())
        self.tree_ids.clear()
        self.jobs.clear()
        self.drop_all_arcs()
        self.refresh_empty()
        self.update_status_idle()
        self.refresh_suggestions()
        self.refresh_chd_controls()

    def update_status_idle(self):
        if not self.running:
            n = len(self.jobs)
            total = sum(j.size for j in self.jobs)
            plan = self.plan_text()
            self.status.config(text=(f"{n} item(s), {fmt_size(total)}" + (f"   ·   {plan}" if plan else "")) if n else "Ready")

    def set_override(self, val):
        for j in self.selected_jobs():
            if j.kind == "convert":
                j.override = val
                self.refresh_job(j)

    def on_right_click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
        if self.tree.selection():
            self.menu.tk_popup(e.x_root, e.y_root)

    def open_selected_folder(self):
        for j in self.selected_jobs()[:1]:
            target = j.out_path or j.path
            open_folder(target, select=True)

    def open_output_folder(self):
        o = self.options()
        if o["out_mode"] == "custom" and o["out_dir"] and Path(o["out_dir"]).is_dir():
            open_folder(o["out_dir"])
        else:
            for j in (self.selected_jobs() or self.jobs)[:1]:
                open_folder((j.out_path or j.path).parent)

    def refresh_job(self, job):
        for iid, j in self.tree_ids.items():
            if j is job:
                self.tree.item(iid, values=job.row(), tags=self.tags_for(iid, job))
                break

    # ------------------------------------------------------------------ log
    def log(self, text, tag="info"):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text.rstrip("\n") + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        if self.echo:
            self.echo(text.rstrip("\n"), tag)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.logbox.get("1.0", "end"))

    def save_log(self):
        p = filedialog.asksaveasfilename(defaultextension=".txt", initialfile="chd_manager_log.txt")
        if p:
            Path(p).write_text(self.logbox.get("1.0", "end"), "utf-8")

    def clear_log(self):
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")

    # ------------------------------------------------------------------ run control
    def options(self):
        o = {k: var.get() for k, var in self.v.items()}
        try:
            o["threads"] = int(o["threads"])
        except (ValueError, tk.TclError):
            o["threads"] = 0
        o["base_pbp_file"] = str(find_base_pbp(o.get("base_pbp", "")) or "")
        try:
            o["pbp_level"] = min(9, max(0, int(o["pbp_level"])))
        except (ValueError, tk.TclError):
            o["pbp_level"] = 6
        for k, d in (("rvz_level", 5), ("rvz_chunk", 131072)):
            try:
                o[k] = int(o[k])
            except (ValueError, tk.TclError):
                o[k] = d
        return o

    BASE_PBP_TEXT = ("A PBP needs a small loader program from Sony to run on a real PSP or Vita. CHD Manager doesn't include it, "
                     "but can download a generic PBP that has one from the public GitHub repository julianxhokaxhiu/iPoPS "
                     "(two files, about 1.4 MB, each checked against a fixed checksum) and keep it next to CHD Manager.exe as BASE.PBP and DATA.PSP. "
                     "It is only used to copy the loader, icon and boot logo into the PBPs you make. "
                     "You can also choose any working PlayStation 1 .pbp of your own in Settings, Formats.")

    def pbp_jobs(self, todo):
        return any(j.target == "pbp" for j in todo)

    def offer_base_pbp(self):
        """Before making PBPs without a base: offer the download once. False = don't start."""
        if self.silent or not self.v["base_pbp_ask"].get() or find_base_pbp(self.v["base_pbp"].get()):
            return True
        ans = messagebox.askyesnocancel(
            APP_NAME, self.BASE_PBP_TEXT + "\n\nYes: download it now.\nNo: make PBPs without it (they work in emulators and "
            "frontends, not on a PSP/Vita) and don't ask again.\nCancel: don't start yet.")
        if ans is None:
            return False
        if ans:
            self.download_base_pbp_ui(confirm=False)
        else:
            self.v["base_pbp_ask"].set(False)
        return True

    def download_base_pbp_ui(self, confirm=True):
        if self.silent or (confirm and not messagebox.askyesno(APP_NAME, self.BASE_PBP_TEXT + "\n\nDownload it now?")):
            return False
        self.status.config(text="Downloading the base PBP…")
        self.root.update_idletasks()
        try:
            download_base_pbp(HERE)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not get the base PBP: {e}\n\nYou can still pick a PBP of your own in Settings.")
            self.update_status_idle()
            return False
        self.log(f"Saved BASE.PBP and DATA.PSP next to the program ({HERE}).", "ok")
        if (HERE / "BASE.PBP").is_file():
            self.v["base_pbp"].set(str(HERE / "BASE.PBP"))          # select it, so nothing has to be browsed for
            self.save_settings()
        self.update_status_idle()
        return True

    # ---- archives: unpacked into a hidden folder when added, and removed again when the file is done with
    def scan_folder(self, p, archives=True):
        """The disc files in a folder tree -> ([paths], {ignored extension: count})."""
        found, ignored = [], {}
        allf, games = [], []
        for dp, dn, fns in os.walk(p):
            if any(x.upper() == "PS3_GAME" for x in dn) and ps3_folder_info(Path(dp)) is not None:
                games.append(Path(dp))                          # a PS3 game folder: taken whole
                dn[:] = []
                continue
            if psn_folder_info(Path(dp)) is not None:
                games.append(Path(dp))                          # a PlayStation Store game folder (as RPCS3 keeps it): taken whole
                dn[:] = []
                continue
            if nup_bundle(Path(dp)) is not None:
                games.append(Path(dp))                          # a decrypted Switch game folder (exefs and romfs): taken whole
                dn[:] = []
                continue
            if any(x.lower() == "default.xex" for x in fns):
                games.append(Path(dp))                          # an Xbox 360 game folder: taken whole
                dn[:] = []
                continue
            allf += [Path(dp) / fn for fn in fns]
        referenced = set()
        for f in allf:
            e = f.suffix.lower()
            refs = parse_cue(f) if e == ".cue" else parse_gdi(f) if e == ".gdi" else parse_toc(f) if e == ".toc" else []
            referenced.update(str(r).lower() for r in refs)
        pieces_seen = set()
        for f in sorted(allf):
            e = f.suffix.lower()
            if split_set(f) is not None:
                first = split_first(f)
                if first not in pieces_seen:                # a split image counts once, however many of its pieces are in the folder
                    pieces_seen.add(first)
                    found.append(first)
            elif e == ".chd" or e in (".cue", ".gdi", ".toc", ".iso", ".pbp", ".cdi", ".vcd", ".zar", ".mds", ".nrg", ".ccd", ".cso", ".zso", ".ecm",
                                    ".gcm", ".gcz", ".rvz", ".wia", ".wbfs", ".ciso", ".wux", ".wud", ".pkg", ".nsp", ".xci", ".nup"):
                found.append(f)
            elif e == ".bin" and str(f).lower() not in referenced:
                found.append(f)
            elif e == ".img" and not f.with_suffix(".ccd").exists() and not f.with_suffix(".mds").exists():
                found.append(f)
            elif e == ".mdf" and not f.with_suffix(".mds").exists():
                ignored[e] = ignored.get(e, 0) + 1
            elif e in ARCHIVE_EXTS and archives:
                found.append(f)
        return sorted(games) + found, ignored

    def pumped(self, fn):
        """Run fn in a thread while the window keeps answering; returns the exception it raised, or None."""
        box = {}

        def work():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                box["err"] = e
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            self.root.update()
            time.sleep(0.03)
        return box.get("err")

    def open_archive(self, arc):
        """Unpack an archive next to itself in a hidden folder -> ([the disc files inside], that folder or None)."""
        import gzip
        import tarfile
        import zipfile
        name, ext = arc.name.lower(), arc.suffix.lower()
        try:
            tmp = Path(tempfile.mkdtemp(prefix=".chdm_arc_", dir=str(arc.parent)))
        except OSError:
            tmp = Path(tempfile.mkdtemp(prefix="chdm_arc_"))
        self.status.config(text=f"Unpacking {arc.name}…")
        self.root.update_idletasks()

        def run():
            if ext == ".zip":
                with zipfile.ZipFile(arc) as z:
                    z.extractall(tmp)
            elif ext in (".tar", ".tgz") or name.endswith((".tar.gz", ".tar.bz2", ".tar.xz")):
                with tarfile.open(arc) as t:
                    t.extractall(tmp, filter="data")
            elif ext == ".gz":
                with gzip.open(arc) as src, open(tmp / arc.stem, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
            else:
                z = find_7z()
                if not z:
                    raise RuntimeError("7-Zip is needed for this kind of archive (Settings, Output page, or install it from 7-zip.org)")
                r = subprocess.run([z, "x", "-y", f"-o{tmp}", str(arc), "-bso0", "-bsp0"], capture_output=True,
                                   creationflags=CREATE_NO_WINDOW)
                if r.returncode:
                    raise RuntimeError((r.stderr or r.stdout).decode("utf-8", "replace").strip()[-300:] or f"7-Zip exited with {r.returncode}")
        if ext in (".7z", ".rar") and not find_7z() and not self.silent and not self.ensure_7zip():
            shutil.rmtree(tmp, ignore_errors=True)
            self.refuse(arc.name, "This kind of archive needs 7-Zip, which isn't installed, so it was not opened.", "warn")
            return [], None
        err = self.pumped(run)
        if err:
            self.refuse(arc.name, sentence(f"the archive couldn't be unpacked ({err})"), "err", f"Could not unpack {arc.name}: {err}")
            shutil.rmtree(tmp, ignore_errors=True)
            return [], None
        files, _ = self.scan_folder(tmp, archives=False)
        if not files:
            self.refuse(arc.name, "There are no disc images inside this archive.", "warn", f"{arc.name}: no disc images found inside.")
            shutil.rmtree(tmp, ignore_errors=True)
            return [], None
        self.arc_dirs[tmp] = 0
        return files, tmp

    def drop_arc(self, job):
        """A job that came out of an archive is finished with: delete the unpacked folder once no other job uses it."""
        d = getattr(job, "extract_dir", None)
        if d is None or d not in self.arc_dirs:
            return
        job.extract_dir = None
        self.arc_dirs[d] -= 1
        if self.arc_dirs[d] <= 0:
            del self.arc_dirs[d]
            shutil.rmtree(d, ignore_errors=True)

    def drop_all_arcs(self):
        for d in list(self.arc_dirs):
            shutil.rmtree(d, ignore_errors=True)
        self.arc_dirs.clear()

    def download_gamedb_ui(self):
        if self.silent or not messagebox.askyesno(APP_NAME, "To name files after the game's title, region or disc code, CHD Manager "
                "needs a list of PlayStation game codes and titles: gameInfo.db (about 1 MB) from the public PSXPackager project on "
                "GitHub (MIT license). It is checked against a fixed checksum and kept next to CHD Manager.\n\nDownload it now?"):
            return
        self.status.config(text="Downloading the game list…")
        self.root.update_idletasks()
        try:
            download_gamedb(HERE)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"Could not get the game list: {e}")
        else:
            self.log(f"Saved gameInfo.db next to the program ({HERE}).", "ok")
        self.update_status_idle()

    def pick_art_dir(self):
        d = filedialog.askdirectory(title="Folder with one subfolder of artwork per game")
        if d:
            self.v["art_dir"].set(d)

    def take_pkg_key(self, data=None):
        key = parse_hex_key(self.pkg_key_entry.get() if data is None else data)
        if not key:
            messagebox.showerror(APP_NAME, "That isn't a key. A package key is 32 letters and numbers (0-9 and a-f).", parent=self.settings_win)
            return
        self.v["pkg_key"].set(key.hex())
        self.pkg_key_entry.set("")
        self.save_settings()

    def pick_pkg_key_file(self):
        p = filedialog.askopenfilename(parent=self.settings_win, title="Choose the file that holds your package key",
                                       filetypes=[("Key files", "*.txt *.key *.bin *.hex"), ("All files", "*.*")])
        if p:
            try:
                if Path(p).stat().st_size > 4096:
                    raise OSError("that file is too big to be a key file")
                self.take_pkg_key(Path(p).read_bytes())
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't read that file: {e}", parent=self.settings_win)

    def take_switch_keys(self, data=None):
        keys = parse_switch_keys(self.sw_keys_box.get("1.0", "end") if data is None else data)
        if not keys or "header_key" not in keys:
            messagebox.showerror(APP_NAME, "That doesn't look like a prod.keys file (it has no header_key line). Copy the whole file, or choose it with the button.",
                                 parent=self.settings_win)
            return
        self.v["switch_keys"].set(switch_keys_text(keys))
        self.sw_keys_box.delete("1.0", "end")
        self.save_settings()

    def pick_switch_keys_file(self):
        p = filedialog.askopenfilename(parent=self.settings_win, title="Choose your prod.keys file",
                                       filetypes=[("Key files", "*.keys *.txt"), ("All files", "*.*")])
        if p:
            try:
                if Path(p).stat().st_size > 1 << 20:
                    raise OSError("that file is too big to be a key file")
                self.take_switch_keys(Path(p).read_bytes())
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't read that file: {e}", parent=self.settings_win)

    def pick_rpcs3(self):
        p = filedialog.askopenfilename(parent=self.settings_win, title="Find RPCS3: choose rpcs3.exe",
                                       filetypes=[("RPCS3", "rpcs3.exe"), ("All programs", "*.exe")])
        if p:
            if Path(p).name.lower() != "rpcs3.exe":
                messagebox.showerror(APP_NAME, "That isn't rpcs3.exe. Pick rpcs3.exe from your RPCS3 folder.", parent=self.settings_win)
                return
            self.v["rpcs3"].set(p)
            self.save_settings()

    def pick_base_pbp(self):
        p = filedialog.askopenfilename(title="Choose a working PlayStation 1 .pbp",
                                       filetypes=[("PBP files", "*.pbp"), ("All files", "*.*")])
        if not p:
            return
        try:
            read_base_pbp(p)
        except (UnpackError, OSError) as e:
            messagebox.showerror(APP_NAME, f"That file can't be used as a base PBP: {e}")
            return
        self.v["base_pbp"].set(p)

    def refresh_ctx(self):
        on = ctx_menu_installed()
        self.ctx_btn.config(text="Remove the right-click menu" if on else "Add the right-click menu")
        if os.name != "nt":
            self.ctx_label.config(text=(
                "On. In KDE's Dolphin (the Steam Deck's desktop mode) right-click a disc image or folder and open the 'CHD Manager' menu. "
                "In GNOME Files (Nautilus) look under Scripts. Most file managers also list CHD Manager under 'Open With'. "
                "Select several files and they all go into one window." if on else
                "Off. Turn it on to convert disc images from your file manager's right-click menu (Dolphin, GNOME Files). It only "
                "adds files inside your own ~/.local/share folder, and this button takes them out again."))
            return
        self.ctx_label.config(text=(
            "On. Right-click a disc image or a folder and choose 'Convert with CHD Manager' to convert it at once, 'Convert with "
            "CHD Manager to' to pick the format, or 'Add to CHD Manager' to put it in the list. Select several files and they all go "
            "into one window." if on else
            "Off. Turn it on to right-click a disc image or a folder and convert it from the menu. It only changes your own Windows "
            "account (no administrator needed), and this button takes it out again."))

    def on_ctx_toggle(self):
        try:
            if ctx_menu_installed():
                ctx_menu_remove()
            else:
                where = "your own Windows account only" if os.name == "nt" else "files inside your own ~/.local/share folder only"
                if not messagebox.askyesno(APP_NAME, "Add 'Convert with CHD Manager' to the right-click menu of disc image files?\n\n"
                                           f"This writes to {where}, and nothing about what opens "
                                           "when you double-click changes. If you move this program's folder, remove the menu "
                                           "and add it again.", parent=self.settings_win):
                    return
                ctx_menu_install()
        except (OSError, ValueError) as e:
            messagebox.showerror(APP_NAME, f"The menu could not be changed: {e}", parent=self.settings_win)
        self.refresh_ctx()

    def ipc_serve(self, srv):
        """(thread) another copy of the program handing over files from the right-click menu."""
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            try:
                c.settimeout(5)
                msg = json.loads(c.makefile("rb").readline(1 << 20))
                paths = [x for x in msg.get("paths", []) if isinstance(x, str)]
                to = msg.get("to") if isinstance(msg.get("to"), str) else None
                self.ipc_q.put((paths, bool(msg.get("start")), to))
                c.sendall(IPC_HELLO)
            except (OSError, ValueError, AttributeError):
                pass
            finally:
                c.close()

    def poll_ipc(self):
        try:
            while True:
                self.handle_external(*self.ipc_q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(300, self.poll_ipc)

    def handle_external(self, paths, start=False, to=None):
        """Files from the right-click menu (or dropped on the program): add them, set the format if one was asked for,
        and start once nothing more has arrived for a moment, so a whole selection becomes one batch in one window."""
        if self.adding:
            self.root.after(300, lambda: self.handle_external(paths, start, to))
            return
        before = {id(j) for j in self.jobs}
        self.add_paths(paths)
        new = [j for j in self.jobs if id(j) not in before]
        if to in TARGET_LABELS and new and not (to == "cso2" and not self.confirm_cso2()):
            for j in new:
                have = [k for k, _ in j.available_targets()]
                pick = next((k for k in (("cue", "cue_split") if to == "cue" else (to,)) if k in have), None)
                if pick:
                    j.set_target(pick)
                else:
                    self.log(f"{j.path.name}: can't be made into {TARGET_LABELS[to].replace('→ ', '')}; it keeps its usual action.", "warn")
            self.update_action_labels()
            self.update_status_idle()
        if start and new:
            self.pending_start += new
            self.start_due = time.time() + 1.5
            self.root.after(400, self.tick_start)
        try:
            self.root.deiconify()
            self.root.lift()
        except tk.TclError:
            pass

    def tick_start(self):
        if not self.pending_start:
            return
        if self.running or self.adding or time.time() < self.start_due:
            self.root.after(500, self.tick_start)
            return
        todo = [j for j in self.pending_start if not j.status.startswith("Done")]
        self.pending_start = []
        if todo:
            self.start(todo)

    def start(self, only=None):
        if self.running:
            return
        if self.adding:
            self.notify("Files are still being added. Wait for the bar at the bottom to finish, then press Start.")
            return
        if not CHDMAN:
            self.notify("chdman was not found, so nothing can be converted. Set it in Settings, Output page.", error=True)
            return
        todo = [j for j in (only if only is not None else self.jobs) if not j.status.startswith("Done")]
        if not todo:
            self.status.config(text="Nothing to do - add files first.")
            if not self.jobs:
                self.notify("There is nothing to convert yet. Add files first: drag them onto this window, or click Add Files or Add Folder.")
            else:
                self.notify("Everything in the list is already done. Add more files, or clear the list to start fresh.")
            return
        if self.pbp_jobs(todo) and not self.offer_base_pbp():
            return
        for j in todo:
            j.status, j.result = "Queued", ""
            self.refresh_job(j)
        self.save_settings()
        self.log_mark = self.logbox.index("end-1c")
        self.run_jobs, self._busy_told = list(todo), False
        self.running, self.stop_flag = True, False
        self.set_state("run")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.all_bar["value"] = self.cur_bar["value"] = 0
        o = self.options()
        self.worker = threading.Thread(target=self.run_all, args=(todo, o), daemon=True)
        self.worker.start()

    def stop(self):
        self.stop_flag = True
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except OSError:
                pass

    def run_blocking(self, jobs=None, timeout=600):
        """Used by the self-test: start, pump the UI until finished."""
        self.start(jobs)
        end = time.time() + timeout
        while self.running and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        return not self.running

    # ------------------------------------------------------------------ worker thread
    def post(self, *a):
        self.q.put(a)

    def run_all(self, todo, o):
        self.cur_opts = o
        t0 = time.time()
        stats = {"ok": 0, "fail": 0, "skip": 0, "src": 0, "out": 0}
        total = len(todo)
        for i, job in enumerate(todo):
            if self.stop_flag:
                job.status = "Stopped"
                self.post("job", job)
                continue
            self.post("overall", i, total)
            self.post("label", f"[{i + 1}/{total}] {job.path.name}")
            try:
                why = self.space_check(job, o) if o.get("space_check", True) else None
            except Exception:  # noqa: BLE001  (a fault in the estimate must never block a conversion)
                why = None
            if why:
                job.status, job.result = "Failed (disk space)", why
                self.post("log", f"{job.path.name}: {why}", "err")
                self.post("job", job)
                stats["fail"] += 1
                continue
            job.status = "Working…"
            self.post("job", job)
            started = time.time()
            try:
                {"convert": self.do_convert, "extract": self.do_extract, "verify": self.do_verify,
                 "recompress": self.do_recompress}[job.kind](job, o)
            except Exception:  # noqa: BLE001
                job.status = "Error"
                self.post("log", traceback.format_exc(), "err")
            took = fmt_time(time.time() - started)
            if job.status.startswith("Done"):
                stats["ok"] += 1
                if job.kind in ("convert", "recompress"):
                    stats["src"] += job.size
                    stats["out"] += job.out_size
                    if job.size:
                        pct = 100 * job.out_size / job.size
                        job.result = f"{fmt_size(job.size)} → {fmt_size(job.out_size)} ({pct:.0f}%) · {took}"
                else:
                    job.result = (job.result + " · " if job.result else "") + took
            elif job.status.startswith(("Skipped", "Stopped")):
                stats["skip"] += 1
            else:
                stats["fail"] += 1
            self.post("job", job)
        self.post("overall", total, total)
        if o.get("make_m3u", True):
            try:
                for line in write_m3u_playlists([j.out_path for j in todo if j.status.startswith("Done") and j.out_path], o.get("exists", "Skip")):
                    self.post("log", line, "ok")
            except Exception:  # noqa: BLE001  (a playlist is a convenience; it must never spoil a finished run)
                self.post("log", "Could not write the .m3u playlist:\n" + traceback.format_exc(), "warn")
        saved = stats["src"] - stats["out"]
        msg = (f"Finished in {fmt_time(time.time() - t0)}: {stats['ok']} done, {stats['skip']} skipped, "
               f"{stats['fail']} failed.")
        if stats["src"]:
            msg += f"  Converted {fmt_size(stats['src'])} → {fmt_size(stats['out'])}, saved {fmt_size(saved)} ({100 * saved / stats['src']:.0f}%)."
        self.post("finish", msg, stats["fail"] == 0)

    def out_stem(self, job, o):
        """The output file's name: the source's, or built from the game ID and title when Settings, Formats says so."""
        fmt = (o.get("name_format") or "%FILENAME%").strip()
        stem = job.home.name if job.container in ("xfolder", "ps3folder", "psnfolder", "swfolder") else job.split_stem if job.container == "ps3parts" else job.home.stem
        if fmt == "%FILENAME%":
            return stem
        gid = job.game_id
        dbf = find_gamedb()
        ent = load_gamedb(dbf).get(gid) if gid and dbf else None
        if ent is None and job.ps3_info.get("title") and gid:       # PlayStation 3: the title and region in PARAM.SFO
            ent = dict(title=job.ps3_info["title"], main_title=job.ps3_info["title"], main_id=gid, region=job.ps3_info.get("region", ""))
        if ent is None and job.container == "disc" and gid:        # GameCube / Wii: the name stored on the disc
            di = job.disc_info
            ent = dict(title=di["title"], main_title=di["title"], main_id=gid,
                       region={"E": "USA", "P": "Europe", "J": "Japan", "K": "Korea"}.get(gid[3:4], ""))
        vals = {"%FILENAME%": stem, "%GAMEID%": gid, "%TITLE%": ent["title"] if ent else "",
                "%MAINTITLE%": ent["main_title"] if ent else "", "%MAINGAMEID%": ent["main_id"] if ent else "",
                "%REGION%": ent["region"] if ent else ""}
        if any(k in fmt and not v for k, v in vals.items()):
            if not job.name_warned:
                job.name_warned = True
                why = "no game ID could be read from it" if not gid else "gameInfo.db has no entry for it (Settings, Formats, Get game titles)"
                self.post("log", f"{job.path.name}: kept the original file name, since {why}.", "warn")
            return stem
        for k, v in vals.items():
            fmt = fmt.replace(k, v)
        return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", fmt).strip(" .") or stem

    def output_path(self, job, o, ext, multi=False, make=True):
        stem = self.out_stem(job, o)
        if o["out_mode"] == "custom" and o["out_dir"]:
            base = Path(o["out_dir"])
            if o["keep_structure"] and job.root:
                try:
                    base = base / job.home.parent.relative_to(job.root)
                except ValueError:
                    pass
        else:
            base = job.home.parent
        t = (job.target or "").partition("_d")[0]
        if ext == ".cso" and (t == "cso2" or (t == "cso" and job.container == "cso" and job.path.suffix.lower() == ".cso")):
            stem = re.sub(r" \(CSO v[12]\)$", "", stem) + (" (CSO v2)" if t == "cso2" else " (CSO v1)")      # never the source's own name: it would be skipped as existing
        if job.kind == "extract" and (multi or o["extract_subfolder"]):
            base = base / stem
        if make:
            base.mkdir(parents=True, exist_ok=True)
        return base / (stem + ext)

    def space_needs(self, job, o):
        """[(folder, bytes)] this job will occupy at its peak: the finished file plus any temporary files, which are
        always written next to it. The ECM check also unpacks to the Windows temp folder."""
        t = (job.target or "").partition("_d")[0]
        if t == "verify":
            return []
        raw = job.raw_size()
        ext = job.path.suffix.lower()
        final = job.size if t == "recompress" else int(raw * SPACE_RATIO.get(t, 1.0))
        extra = 0
        if job.container == "disc":
            if t == "wbfs" and job.platform == "Wii" and o["wbfs_scrub"] and not WII_KEYS[0]:
                extra = raw                                  # the scrubbed ISO Dolphin makes first
        elif ext == ".chd":
            if t in ("cso", "cso2", "zso", "ecm", "pbp", "cdi", "vcd", "zar", "folder", "cue_fix") or (t == "iso" and job.disc == "CD"):
                extra = raw                                  # extracted to a temporary folder first
        elif job.kind == "convert":
            extra = raw if job.container else 0              # PBP / CDI / images are unpacked first
        else:
            direct = (t == "cue" and job.container in ("pbp", "cdi", "image", "ecm")) or \
                     (t in ("cso", "cso2", "zso") and (job.container == "cso" or (ext == ".iso" and not job.wrap_cue))) or \
                     (t == "iso" and job.container in ("cso", "zar", "xfolder", "ps3folder", "ps3parts")) or t in ("cue_fix", "art", "zar", "folder")
            if not direct:                                   # through a temporary uncompressed CHD
                extra = raw + (raw if job.container else 0)
        if job.container == "ps3pkg":
            extra = 300 * 1024 * 1024                        # the throwaway copy of RPCS3's program files, if they can't be linked
        out = self.output_path(job, o, SPACE_EXT.get(t, ".x"), make=False).parent
        needs = [(out, final + extra)]
        if t == "ecm" and o["verify_after"]:
            needs.append((Path(tempfile.gettempdir()), raw))
        return needs

    def space_check(self, job, o):
        """None when the job fits; otherwise a plain sentence saying how much is needed and how much is free."""
        needs = self.space_needs(job, o)
        t = (job.target or "").partition("_d")[0]
        ext = SPACE_EXT.get(t)
        if ext and o["exists"] == "Skip" and self.output_path(job, o, ext, make=False).exists():
            return None                                      # it will be skipped, so it needs no room
        drives = {}
        for folder, n in needs:
            near, free = free_space(folder)
            d = drives.setdefault(os.stat(near).st_dev, [near, free, 0])
            d[2] += n
        for near, free, n in drives.values():
            if n > 0 and free < n * 1.03 + SPACE_MARGIN:
                bin_note = " Files in the Recycle Bin still use space until it is emptied." if o["delete_src"] and not o["delete_forever"] else ""
                return (f"Not enough free space on {near.anchor or near}: about {fmt_size(n)} is needed there, "
                        f"but only {fmt_size(free)} is free. Free up some room or choose another Output folder." + bin_note)
        return None

    def handle_exists(self, job, out, o):
        """-> (out, force, skip)"""
        if not out.exists():
            return out, False, False
        mode = o["exists"]
        if mode == "Skip":
            job.status = "Skipped (exists)"
            job.result = f"{out.name} already exists (see 'If output exists' in Settings)"
            self.post("log", f"Skipped {job.path.name}: {out.name} already exists.", "warn")
            return out, False, True
        if mode == "Rename":
            n = 1
            while True:
                cand = out.with_name(f"{out.stem} ({n}){out.suffix}")
                if not cand.exists():
                    return cand, False, False
                n += 1
        return out, True, False

    def comp_args(self, o, typ):
        c = o["compression"]
        if c == "None":
            return ["-c", "none"]
        if c == "Fast":
            return ["-c", "cdzl" if typ == "CD" else "zlib"]
        if c == "Smallest":
            return ["-c", "cdlz" if typ == "CD" else "lzma"]
        if c == "Custom" and o["custom_comp"].strip():
            return ["-c", o["custom_comp"].strip()]
        return []

    def common_args(self, o, typ):
        a = self.comp_args(o, typ)
        h = str(o["hunk"]).strip()
        if h:
            if h.isdigit():
                a += ["-hs", h]
            else:
                self.post("log", f"Ignoring hunk size '{h}' (must be a number).", "warn")
        if o["threads"] > 0:
            a += ["-np", str(o["threads"])]
        return a

    def unpack_job(self, job, base, outdir, tag, prog, iso_ok=False):
        """One disc of a .pbp / .cdi / disc image -> CUE/BIN files in outdir. Returns the .cue (or .iso) path."""
        stopped = lambda: self.stop_flag  # noqa: E731
        if job.container == "pbp":
            return unpack_pbp_disc(job.path, base, outdir, tag, prog, stopped)
        if job.container == "image":
            return unpack_tracks(read_image(job.path), outdir, tag, prog, stopped, iso_ok=iso_ok)
        if job.container == "cso":
            iso = Path(outdir) / f"{tag}.iso"
            cso_to_iso(job.path, iso, prog, stopped)
            return iso
        if job.container == "ps3parts":
            iso = Path(outdir) / f"{tag}.iso"
            join_pieces(job.src_files, iso, prog, stopped)
            return iso
        if job.container == "ps3folder":
            if not iso_ok:
                raise UnpackError("a PS3 disc image is a DVD-style image, and this chdman is too old to make those (get one from MAME 0.255 or later)")
            iso = Path(outdir) / f"{tag}.iso"
            self.ps3_to_iso(job, iso, prog)
            return iso
        if job.container in ("zar", "xfolder"):
            if not iso_ok:
                raise UnpackError("an Xbox 360 disc is a DVD, and this chdman is too old to make those (get one from MAME 0.255 or later)")
            iso = Path(outdir) / f"{tag}.iso"
            self.xbox_to_xiso(job, iso, prog)
            return iso
        if job.container == "ecm":
            out_bin = Path(outdir) / f"{tag}.bin"
            ttype = ecm_decode(job.path, out_bin, prog, stopped) or "MODE1/2352"
            cue = Path(outdir) / f"{tag}.cue"
            cue.write_text(f'FILE "{out_bin.name}" BINARY\n  TRACK 01 {ttype}\n    INDEX 01 00:00:00\n', "utf-8")
            return cue
        return unpack_cdi(job.path, outdir, tag, prog, stopped)

    def do_unpack(self, job, o):
        """.pbp / .cdi / .mds / .nrg / .ccd / .img -> CUE/BIN files in their own folder, no CHD."""
        n = job.disc_count
        stem = self.output_path(job, o, ".cue").stem
        out = self.output_path(job, o, ".cue").parent / stem
        if out.exists() and any(out.iterdir()):
            if o["exists"] == "Skip":
                job.status, job.result = "Skipped (exists)", f"{out.name} already exists (see 'If output exists' in Settings)"
                self.post("log", f"Skipped {job.path.name}: folder {out.name} already exists.", "warn")
                return
            if o["exists"] == "Rename":
                k = 1
                while out.with_name(f"{stem} ({k})").exists():
                    k += 1
                out = out.with_name(f"{stem} ({k})")
        created = not out.exists()
        out.mkdir(parents=True, exist_ok=True)
        bases = pbp_discs(job.path) if job.container == "pbp" else [None]
        cues = []
        try:
            for i in ([job.disc_pick - 1] if job.disc_pick else range(n)):
                label = f" disc {i + 1} of {n}" if n > 1 else ""
                last = [-1]

                def prog(x, label=label):
                    pct = int(100 * x)
                    if pct != last[0]:
                        last[0] = pct
                        job.status = f"Unpacking{label} {pct}%"
                        self.post("job", job)
                        self.post("progress", pct)

                self.post("log", f"\n{job.path.name}{label}  [{'joining the pieces' if job.container == 'ps3parts' else 'unpacking ' + job.path.suffix[1:].upper()}]", "info")
                cues.append(self.unpack_job(job, bases[i], out, f"{stem} (Disc {i + 1})" if n > 1 else stem, prog))
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        else:
            job.out_path = cues[0]
            job.out_size = sum(f.stat().st_size for f in out.iterdir() if f.is_file())
            job.status, job.result = "Done", f"→ {out.name}\\"
            self.post("log", f"OK  {out}", "ok")
        finally:
            if created and not job.status.startswith("Done"):
                shutil.rmtree(out, ignore_errors=True)

    def do_convert_container(self, job, o):
        """.pbp / .cdi -> unpack to CUE/BIN in a temporary folder, then chdman createcd. A multi-disc PBP
        becomes one CHD per disc plus an .m3u playlist."""
        n = job.disc_count
        first = self.output_path(job, o, ".chd")
        stem = first.stem
        discs = [job.disc_pick - 1] if job.disc_pick else list(range(n))
        outs = [first] if n == 1 else [first.with_name(f"{stem} (Disc {i + 1}).chd") for i in discs]
        tmp = first.parent / f".{stem[:40]}.chdm_tmp"
        made, skipped, unverified = [], 0, False
        try:
            bases = pbp_discs(job.path) if job.container == "pbp" else [None]
            for i, out in zip(discs, outs):
                label = f" disc {i + 1} of {n}" if n > 1 else ""
                out, force, skip = self.handle_exists(job, out, o)
                if skip:
                    skipped += 1
                    continue
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir(parents=True, exist_ok=True)
                last = [-1]

                def prog(x, label=label):
                    pct = int(100 * x)
                    if pct != last[0]:
                        last[0] = pct
                        job.status = f"Unpacking{label} {pct}%"
                        self.post("job", job)
                        self.post("progress", pct)

                tag = f"{stem} (Disc {i + 1})" if n > 1 else stem
                self.post("log", f"\n{job.path.name}{label}  [{'joining the pieces' if job.container == 'ps3parts' else 'unpacking ' + job.path.suffix[1:].upper()}]", "info")
                try:
                    cue = self.unpack_job(job, bases[i], tmp, tag, prog, iso_ok=job.type == "DVD" and self.has_dvd)
                except UnpackError as e:
                    if self.stop_flag:
                        job.status = "Stopped"
                    else:
                        job.status, job.result = "Failed (can't unpack)", str(e)
                        self.post("log", f"{job.path.name}: {e}", "err")
                    return
                except OSError as e:
                    job.status, job.result = "Failed (disk)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
                    return
                iso = cue.suffix.lower() == ".iso"
                cmd = [CHDMAN, "createdvd" if iso else "createcd", "-i", str(cue), "-o", str(out)] + (["-f"] if force else []) \
                    + self.common_args(o, "DVD" if iso else "CD")
                job.out_path = out
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, "Compressing")
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    if out.exists():
                        try:
                            out.unlink()
                        except OSError:
                            pass
                    return
                if o["verify_after"] or o["delete_src"]:
                    self.post("log", "Verifying…", "info")
                    _, ok = self.verify_file(out, job, "Verifying")
                    if ok is False:
                        job.status = "Verify failed"
                        self.post("log", f"VERIFY FAILED for {out.name} - the source file was NOT touched.", "err")
                        return
                    unverified = unverified or ok is None
                made.append(out)
                self.post("log", f"OK  {out.name}", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not made:
            return                                  # everything was skipped; handle_exists already said why
        if n > 1 and len(made) == n and not job.disc_pick:
            first.with_name(stem + ".m3u").write_text("".join(p.name + "\n" for p in made), "utf-8")
        job.out_size = sum(p.stat().st_size for p in made)
        job.status = "Done"
        job.result = f"→ {made[0].name}" if len(made) == 1 else f"→ {len(made)} CHDs" + (" + .m3u" if len(made) == n else "")
        if skipped:
            job.result += f" ({skipped} already existed)"
        self.post("log", f"OK  {job.path.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and not skipped and not unverified:
            self.remove_sources(job, o)
        elif o["delete_src"]:
            self.post("log", "The source file was kept (not everything could be verified, or part of it already existed).", "warn")

    def do_convert(self, job, o):
        if job.container:
            return self.do_convert_container(job, o)
        if job.missing:
            job.status = "Failed (missing files)"
            self.post("log", f"{job.path.name}: missing {', '.join(job.missing)}", "err")
            return
        typ = job.type
        src, temp_cue = job.path, None
        if job.lone_bin or job.wrap_cue:
            if not job.wrap_cue and not o["auto_cue"]:
                job.status = "No CUE"
                self.post("log", f"{job.path.name}: .bin without a CUE (enable auto-create in Settings, Convert page).", "err")
                return
            mode = job.wrap_cue or "MODE2/2352"
            temp_cue = job.path.with_name(job.path.stem + ".__chdm_auto__.cue")
            temp_cue.write_text(f'FILE "{job.path.name}" BINARY\n  TRACK 01 {mode}\n    INDEX 01 00:00:00\n', "utf-8")
            src = temp_cue
            typ = "CD"
            self.post("log", f"{job.path.name}: created a temporary CUE ({mode}).", "warn" if job.lone_bin and not job.wrap_cue else "info")
        else:
            temp_cue = case_fixed_sheet(src)
            if temp_cue:
                src = temp_cue
                self.post("log", f"{job.path.name}: file names differ from the sheet only in upper/lower case; using a corrected temporary copy.", "info")
        try:
            out = self.output_path(job, o, ".chd")
            out, force, skip = self.handle_exists(job, out, o)
            if skip:
                return
            verb = "createcd"
            if typ == "DVD":
                if self.has_dvd:
                    verb = "createdvd"
                else:
                    self.post("log", "chdman has no createdvd - using createcd instead.", "warn")
            cmd = [CHDMAN, verb, "-i", str(src), "-o", str(out)] + (["-f"] if force else []) + self.common_args(o, typ)
            job.out_path = out
            self.post("log", f"\n{job.path.name}  [{typ}{' ' + job.platform if job.platform else ''}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Compressing")
        finally:
            if temp_cue and temp_cue.exists():
                try:
                    temp_cue.unlink()
                except OSError:
                    pass
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            return
        job.out_size = out.stat().st_size
        unverified = False
        if o["verify_after"] or o["delete_src"]:
            self.post("log", "Verifying…", "info")
            vrc, ok = self.verify_file(out, job, "Verifying")
            if ok is False:
                job.status = "Verify failed"
                self.post("log", f"VERIFY FAILED for {out.name} - source files were NOT touched.", "err")
                return
            unverified = ok is None
        job.status = "Done"
        self.post("log", f"OK  {out.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and unverified:
            self.post("log", "Source files were kept: an uncompressed CHD can't be verified, so nothing was recycled.", "warn")
        elif o["delete_src"]:
            self.remove_sources(job, o)

    def remove_sources(self, job, o):
        """After a verified CHD: send the source files to the Recycle Bin, or delete them for good when the user chose that."""
        if o.get("delete_forever"):
            if delete_forever(job.src_files):
                self.post("log", f"Deleted {len(job.src_files)} source file(s) permanently; the space is free again.", "ok")
            else:
                self.post("log", "Could not delete every source file.", "warn")
        elif recycle(job.src_files):
            self.post("log", f"Sent {len(job.src_files)} source file(s) to the Recycle Bin.", "ok")
        else:
            self.post("log", "Could not recycle the source files.", "warn")

    def verify_file(self, path, job, label):
        lines = []
        rc = self.run_proc([CHDMAN, "verify", "-i", str(path)], job, label, capture=lines)
        text = "\n".join(lines).lower()
        if "uncompressed" in text and "no verification" in text:
            self.post("log", "This CHD is uncompressed, so chdman has no checksum to verify it against.", "warn")
            return rc, None
        ok = rc == 0 and "verification successful" in text and "does not match" not in text
        if ok:
            self.post("log", "Verification successful (SHA-1 matches).", "ok")
        return rc, ok

    def do_verify(self, job, o):
        self.post("log", f"\nVerify {job.path.name}", "info")
        _, ok = self.verify_file(job.path, job, "Verifying")
        job.status = "Done" if ok is not False else "Verify failed"
        job.result = "uncompressed: nothing to verify" if ok is None else "SHA-1 OK" if ok else "CORRUPT or unreadable"
        if ok is False:
            self.post("log", f"{job.path.name} FAILED verification.", "err")

    def do_extract(self, job, o):
        if job.path.suffix.lower() not in (".chd", ".m3u"):
            return self.export_other(job, o)
        if job.disc in ("", "?"):
            job.disc, job.tracks = chd_info(job.path)
        kind = job.disc
        ext, verb, extra = {"CD": (".cue", "extractcd", True), "GD-ROM": (".gdi", "extractcd", True),
                            "DVD": (".iso", "extractdvd", False), "HD": (".img", "extracthd", False),
                            "LD": (".avi", "extractld", False), "Raw": (".bin", "extractraw", False)}.get(
            kind, (None, None, False))
        if not verb:
            job.status = "Failed (unknown CHD)"
            self.post("log", f"{job.path.name}: cannot read this CHD.", "err")
            return
        fmt = self.fmt_for(job, o)
        if fmt in ("cso", "cso2", "zso"):
            return self.extract_to_cso(job, o, lz4=fmt == "zso")
        if fmt == "ecm":
            return self.extract_to_ecm(job, o)
        if fmt == "zar":
            return self.extract_to_zar(job, o)
        if fmt == "folder":
            return self.extract_to_folder(job, o)
        if fmt == "vcd":
            return self.extract_to_vcd(job, o)
        if fmt == "fix":
            return self.do_fix(job, o)
        if fmt == "pbp" or job.path.suffix.lower() == ".m3u":
            if fmt != "pbp":
                job.status, job.result = "Failed (choose PBP)", "A playlist can only be combined into a PBP: choose PlayStation PBP under 'CHD files'."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            if kind != "CD":
                job.status, job.result = "Failed (not possible)", f"PBP is only possible for PlayStation 1 CDs; this CHD holds a {kind} disc."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            return self.extract_to_pbp(job, o)
        if fmt == "cdi" and kind == "CD":
            return self.extract_to_cdi(job, o)
        if fmt == "cdi":
            job.status, job.result = "Failed (not possible)", f"CDI is only possible for CD discs; this CHD holds a {kind} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        if fmt == "iso" and kind == "CD":
            return self.extract_to_iso(job, o)
        if fmt == "iso" and kind != "DVD":
            job.status = "Failed (not possible)"
            job.result = f"ISO is only possible for CD and DVD discs; this CHD holds a {kind} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        split = verb == "extractcd" and kind == "CD" and self.wants_split(job, o)
        # a disc that comes out as several files (a .cue with several BINs, or a .gdi with its tracks) gets its own folder
        out = self.output_path(job, o, ext, multi=split or kind == "GD-ROM")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        cmd = [CHDMAN, verb, "-i", str(job.path), "-o", str(out)] + (["-f"] if force else [])
        if split:
            cmd += ["-sb"]
            self.post("log", f"{job.tracks} tracks: writing one BIN per track.", "info")
        job.out_path = out
        self.post("log", f"\n{job.path.name}  [{kind}]", "info")
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        rc = self.run_proc(cmd, job, "Extracting")
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return
        job.status = "Done"
        job.result = f"→ {out.name}"
        self.post("log", f"OK  {out}", "ok")

    # ---- sources other than a CHD that become something other than a CHD
    def export_other(self, job, o):
        """A disc image / PBP / CDI / CSO whose target isn't a CHD. Where a direct route exists it is used; every other
        combination goes through a temporary uncompressed CHD, so each target is written by the same code whatever the source."""
        t = (job.target or "").partition("_d")[0]
        if t == "art":
            return self.do_extract_art(job, o)
        if t == "cue_fix":
            return self.do_fix(job, o)
        if job.container == "disc":
            return self.do_disc(job, o)
        if t == "cue" and job.container in ("pbp", "cdi", "image", "ecm"):
            return self.do_unpack(job, o)
        if t in ("cso", "cso2", "zso") and job.path.suffix.lower() == ".iso" and not job.wrap_cue:
            return self.do_iso_to_cso(job, o, lz4=t == "zso")
        if t in ("cso", "cso2", "zso") and job.container == "cso":
            return self.do_cso_convert(job, o, lz4=t == "zso")
        if t == "iso" and job.container == "cso":
            return self.do_cso_to_iso(job, o)
        if t == "iso" and job.container in ("zar", "xfolder"):
            return self.do_zar_to_iso(job, o)
        if t == "iso" and job.container == "ps3folder":
            return self.do_ps3_to_iso(job, o)
        if t == "iso" and job.container == "ps3parts":
            return self.do_join_ps3(job, o)
        if t == "zar":
            return self.do_to_zar(job, o)
        if t == "pkg" and job.container == "psnfolder":
            return self.do_folder_to_pkg(job, o)
        if t == "folder" and job.container == "ps3pkg":
            return self.do_pkg_to_folder(job, o)
        if t == "nup" and job.container == "swfolder":
            return self.do_folder_to_nup(job, o)
        if t == "folder" and job.container == "nup":
            return self.do_nup_to_folder(job, o)
        if t == "xci" and job.container in ("swnsp", "swxci"):
            return self.do_nsp_to_xci(job, o)
        if t == "nsp" and job.container in ("swnsp", "swxci"):
            return self.do_xci_to_nsp(job, o)
        if t == "split" and job.container in ("swnsp", "swxci"):
            return self.do_sw_split(job, o)
        if t == "folder":
            return self.do_to_folder(job, o)
        return self.export_via_chd(job, o)

    def export_via_chd(self, job, o):
        tmp = self.output_path(job, o, ".x").parent / f".{job.home.stem[:40]}.chdm_hub"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        saved = (job.path, job.disc, job.tracks)
        try:
            tchd = tmp / (job.home.stem + ".chd")
            if not self.chd_from_source(job, o, tchd, tmp):
                return
            job.path = tchd
            job.disc, job.tracks = chd_info(tchd)
            self.do_extract(job, o)
        finally:
            job.path, job.disc, job.tracks = saved
            shutil.rmtree(tmp, ignore_errors=True)

    def chd_from_source(self, job, o, tchd, tmp):
        """Any non-CHD source -> an uncompressed CHD at tchd. False (with the job's status set) when that fails."""
        src, typ = job.path, job.type
        if job.missing:
            job.status = "Failed (missing files)"
            self.post("log", f"{job.path.name}: missing {', '.join(job.missing)}", "err")
            return False
        if job.container:
            last = [-1]

            def prog(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"Unpacking {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)

            unp = tmp / "unp"
            unp.mkdir()
            self.post("log", f"\n{job.path.name}  [{'joining the pieces' if job.container == 'ps3parts' else 'unpacking ' + job.path.suffix[1:].upper()}]", "info")
            try:
                base = pbp_discs(job.path)[0] if job.container == "pbp" else None
                src = self.unpack_job(job, base, unp, job.home.stem, prog, iso_ok=True)
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                else:
                    job.status, job.result = "Failed (can't unpack)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
                return False
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
                return False
            typ = "DVD" if src.suffix.lower() == ".iso" else "CD"
        temp_cue = None
        if job.wrap_cue:
            src = temp_cue = job.path.with_name(job.path.stem + ".__chdm_auto__.cue")
            temp_cue.write_text(f'FILE "{job.path.name}" BINARY\n  TRACK 01 {job.wrap_cue}\n    INDEX 01 00:00:00\n', "utf-8")
            typ = "CD"
        elif not job.container and (temp_cue := case_fixed_sheet(src)):
            src = temp_cue
        elif job.target == "pbp" and src.suffix.lower() == ".iso":
            last = [-1]

            def cprog(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"Rebuilding sectors {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)

            self.post("log", f"{job.path.name}: rebuilding raw CD sectors from the plain ISO (a PBP needs them).", "info")
            raw = tmp / "raw.bin"
            try:
                cook_to_raw(src, raw, cprog, lambda: self.stop_flag)
            except UnpackError:
                job.status = "Stopped"
                return False
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
                return False
            src = tmp / "raw.cue"
            src.write_text('FILE "raw.bin" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n', "utf-8")
            typ = "CD"
        cmd = [CHDMAN, "createdvd" if typ == "DVD" and self.has_dvd else "createcd", "-i", str(src), "-o", str(tchd), "-c", "none"]
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        try:
            rc = self.run_proc(cmd, job, "Preparing")
        finally:
            if temp_cue:
                temp_cue.unlink(missing_ok=True)
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return False
        return True

    def cso_progress(self, job, label):
        last = [-1]

        def prog(x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)
        return prog

    def write_cso_file(self, job, o, iso, part, out, lz4=False):
        """iso -> part (a CSO, or a ZSO), compared against the ISO when verifying is on, then moved to out. True on success."""
        try:
            v2 = (job.target or "").partition("_d")[0] == "cso2"
            if v2:
                self.post("log", "CSO version 2: this plays on a real PSP, but the PPSSPP emulator refuses it ('CSO version too high'). "
                                 "Use version 1 for the emulator.", "warn")
            iso_to_cso(iso, part, self.cso_progress(job, "Compressing"), lambda: self.stop_flag, lz4=lz4, v2=v2)
            if o["verify_after"] and not verify_cso(part, iso, self.cso_progress(job, "Verifying"), lambda: self.stop_flag):
                job.status, job.result = "Verify failed", "the finished CSO didn't match the ISO it was made from; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return False
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't write)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return False
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return False
        os.replace(part, out)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        return True

    def do_iso_to_cso(self, job, o, lz4=False):
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [ISO → {'ZSO' if lz4 else 'CSO'}]", "info")
        try:
            self.write_cso_file(job, o, job.path, part, out, lz4)
        finally:
            part.unlink(missing_ok=True)

    def do_cso_convert(self, job, o, lz4=False):
        """CSO <-> ZSO, through a temporary ISO."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [{'CSO → ZSO' if lz4 else 'ZSO → CSO'}]", "info")
        try:
            iso = tmp / "t.iso"
            cso_to_iso(job.path, iso, self.cso_progress(job, "Unpacking"), lambda: self.stop_flag)
            self.write_cso_file(job, o, iso, part, out, lz4)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            part.unlink(missing_ok=True)

    def xbox_source(self, job):
        """(entries, read, close) for the files of an Xbox 360 job: a ZAR, a game folder or an XISO."""
        if job.container == "zar":
            return zar_source(job.path)
        if job.container == "xfolder":
            return folder_source(job.path)
        return iso_source(job.path)

    def verify_wanted(self):
        o = getattr(self, "cur_opts", {})
        return bool(o.get("verify_after") or o.get("delete_src"))

    def xbox_to_xiso(self, job, dest, prog):
        """The files of an Xbox 360 ZAR or game folder -> an XISO at dest. With 'verify' on, a ZAR's own checksum is checked first
        and the finished image is read back against the source."""
        verify = self.verify_wanted()
        stop = lambda: self.stop_flag  # noqa: E731
        if verify and job.container == "zar":
            self.post("log", "Checking the ZAR's own checksum…", "info")
            z = ZarArchive(job.path)
            try:
                if not z.hash_ok(lambda x: prog(x * 0.1), stop):
                    raise UnpackError("the ZAR doesn't match its own checksum, so it is damaged; nothing was made from it")
            finally:
                z.close()
        ents, read, close = self.xbox_source(job)
        try:
            span = 0.45 if verify else 0.9
            build_xiso(ents, dest, read, lambda x: prog(0.1 + x * span), stop)
            if verify:
                self.post("log", "Reading the new disc image back against the source…", "info")
                e2, r2, c2 = iso_source(dest)
                try:
                    if not entries_match((ents, read), (e2, r2), lambda x: prog(0.55 + x * 0.45), stop):
                        raise UnpackError("the disc image that was built didn't match what it came from; it was discarded")
                finally:
                    c2()
        finally:
            close()
        prog(1.0)

    def xbox_to_zar(self, job, dest, prog, iso=None):
        """An Xbox 360 XISO (job's own, or the one just made from a CHD), or a game folder -> a ZAR at dest."""
        verify = self.verify_wanted()
        stop = lambda: self.stop_flag  # noqa: E731
        if iso is not None or self.xbox_job_is_iso(job):
            iso = iso or job.path
            if xbox_iso_kind(iso) != "360":
                raise UnpackError("this isn't an Xbox 360 disc image (it has no default.xex at its top), so it can't become a ZAR")
            ents, read, close = iso_source(iso)
        else:
            ents, read, close = self.xbox_source(job)
        try:
            write_zar(dest, ents, read, lambda x: prog(x * (0.7 if verify else 1.0)), stop)
            if verify:
                self.post("log", "Reading the new ZAR back against the source…", "info")
                z = ZarArchive(dest)
                try:
                    if not z.hash_ok(lambda x: prog(0.7 + x * 0.05), stop) or \
                            not entries_match((ents, read), (list(z.entries()), z.read), lambda x: prog(0.75 + x * 0.25), stop):
                        raise UnpackError("the ZAR that was built didn't match what it came from; it was discarded")
                finally:
                    z.close()
        finally:
            close()
        prog(1.0)

    def xbox_job_is_iso(self, job):
        return job.container not in ("zar", "xfolder")

    def do_zar_to_iso(self, job, o):
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".iso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [Xbox 360 → ISO]  (a rebuilt disc image, for emulators)", "info")
        try:
            self.xbox_to_xiso(job, part, self.cso_progress(job, "Building ISO"))
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"]:
            self.remove_sources(job, o)

    def do_to_zar(self, job, o):
        """An Xbox 360 ISO or game folder -> a ZAR (what Xenia reads)."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zar"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [Xbox 360 → ZAR]", "info")
        try:
            self.xbox_to_zar(job, part, self.cso_progress(job, "Making ZAR"))
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except (OSError, ValueError) as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"]:
            self.remove_sources(job, o)

    def rm_parts(self, base):
        """Delete base and base.0, base.1 ... (the leftovers of a PS3 image that did not finish)."""
        base = Path(base)
        try:
            for e in os.listdir(base.parent):
                if e == base.name or (e.startswith(base.name + ".") and e[len(base.name) + 1:].isdigit()):
                    (base.parent / e).unlink(missing_ok=True)
        except OSError:
            pass

    def ps3_to_iso(self, job, dest, prog, split=False):
        """The files of a PS3 game folder -> a PS3 disc image at dest (or parts dest.0, dest.1 ...). Returns the files written. With
        'verify' on, the finished image is read back against the folder."""
        verify = self.verify_wanted()
        stop = lambda: self.stop_flag  # noqa: E731
        keep = bool(getattr(self, "cur_opts", {}).get("ps3_update"))
        ents, read, close = ps3_folder_source(job.path, keep)
        try:
            sfo = next((e for e in ents if e[1] and [x.upper() for x in e[0]] == [b"PS3_GAME", b"PARAM.SFO"]), None)
            info = ps3_sfo_info(read_sfo(b"".join(read(sfo[2], sfo[3])))) if sfo is not None and sfo[3] < 1 << 20 else {}
            if not info.get("id"):
                self.post("log", f"{job.path.name}: PS3_GAME\\PARAM.SFO is missing or unreadable, so the image is made without a game ID in its header.", "warn")
            if not any(e[1] and [x.upper() for x in e[0]] == [b"PS3_DISC.SFB"] for e in ents):
                self.post("log", f"{job.path.name}: PS3_DISC.SFB is missing. The image is made anyway: RPCS3 doesn't need that file, a real PS3 does.", "warn")
            if not keep and _child(job.path, "PS3_UPDATE") is not None:
                self.post("log", f"{job.path.name}: the files in PS3_UPDATE (a system update that no emulator uses) are left out; the folder is kept, empty. "
                                 "Settings, Formats can keep them.", "info")
            files = build_ps3_iso(ents, dest, read, info.get("id", ""), lambda x: prog(x * (0.55 if verify else 1.0)), stop, split=split)
            if verify:
                self.post("log", "Reading the new disc image back against the folder…", "info")
                e2, r2, c2 = ps3_iso_source(files)
                try:
                    if not entries_match((ents, read), (e2, r2), lambda x: prog(0.55 + x * 0.45), stop):
                        raise UnpackError("the disc image that was built didn't match the folder it came from; it was discarded")
                finally:
                    c2()
        finally:
            close()
        prog(1.0)
        return files

    def do_ps3_to_iso(self, job, o):
        """A PS3 game folder -> an ISO (a whole one, or 4 GB parts when Settings says so)."""
        split = bool(o.get("ps3_split"))
        out0 = self.output_path(job, o, ".iso")
        if o["exists"] == "Skip" and out0.with_name(out0.name + ".0").exists():
            job.status, job.result = "Skipped (exists)", f"{out0.name}.0 already exists (see 'If output exists' in Settings)"
            self.post("log", f"Skipped {job.path.name}: {out0.name}.0 already exists.", "warn")
            return
        out, _, skip = self.handle_exists(job, out0, o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [PS3 game folder → ISO]" + ("  (split into 4 GB parts)" if split else ""), "info")
        try:
            files = self.ps3_to_iso(job, part, self.cso_progress(job, "Building ISO"), split=split)
            for old in [out] + [out.with_name(out.name + f".{i}") for i in range(200)]:      # leftovers of an earlier run that is being replaced
                old.unlink(missing_ok=True)
            if len(files) == 1 and files[0] == part:
                os.replace(part, out)
                finals = [out]
            else:
                finals = []
                for i, f_ in enumerate(files):
                    dst = out.with_name(out.name + f".{i}")
                    os.replace(f_, dst)
                    finals.append(dst)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            self.rm_parts(part)
        job.out_path, job.out_size = finals[0], sum(f_.stat().st_size for f_ in finals)
        job.status = "Done"
        job.result = f"→ {finals[0].name}" + (f" … .{len(finals) - 1}  ({len(finals)} parts)" if len(finals) > 1 else "")
        self.post("log", f"OK  {finals[0]}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"]:
            self.remove_sources(job, o)

    def do_pkg_to_folder(self, job, o):
        """PS3 packages (a game, then its updates) -> the game's folder. The packages are installed by the user's own RPCS3 into a throwaway
        copy of its program files, and the finished game folder is moved out of that copy. The real RPCS3 and its library are not touched."""
        exe = find_rpcs3(o.get("rpcs3", ""))
        key = parse_hex_key(o.get("pkg_key", ""))
        if not exe and not key:
            job.status, job.result = "Failed (setup needed)", ("this needs your own RPCS3 or your own package key: Settings, Formats, PlayStation 3, "
                                                               "and show it where rpcs3.exe is, or give it your key")
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        only_updates = all(i["patch"] for i in job.pkgs)
        if only_updates:                                    # RPCS3 itself installs these without the game, so they are unpacked too
            self.post("log", f"{job.path.name}: only updates were added, so the result holds just the files the updates carry, not the whole game. "
                             "Add the game's own package as well to get the whole game.", "warn")
        out, skip = self.folder_target(job, o)
        if skip:
            return
        if not exe:
            return self.pkg_own_unpack(job, o, out, key)
        work = out.parent / f".{out.name[:40]}.chdm_rpcs3"
        shutil.rmtree(work, ignore_errors=True)
        n = len(job.pkgs)
        self.post("log", f"\n{job.path.name}  [PS3 package{'s' if n != 1 else ''} → game folder, installed by your RPCS3]", "info")
        ok = False
        try:
            wexe = rpcs3_workdir(exe, work)
            seen = []
            for k, info in enumerate(job.pkgs):
                log = work / "log" / "RPCS3.log"
                log.unlink(missing_ok=True)
                cmd = [str(wexe), "--allow-any-location", "--headless", "--installpkg", str(info["path"])]
                self.post("log", f"Installing {k + 1} of {n}: {info['path'].name}" + ("  (update)" if info["patch"] else "  (DLC)" if info["dlc"] else ""), "info")
                si = None
                if os.name == "nt":                         # RPCS3 crashes at start-up with no console at all, so it gets one that is hidden
                    si = subprocess.STARTUPINFO()
                    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                    si.wShowWindow = 0
                proc = subprocess.Popen(cmd, cwd=str(work), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                        **({"startupinfo": si} if si else {}))
                self.proc, last, t0 = proc, -1, time.time()
                while proc.poll() is None:
                    time.sleep(0.2)
                    if self.stop_flag:
                        proc.kill()
                        raise UnpackError("stopped")
                    if time.time() - t0 > 3 * 3600:
                        proc.kill()
                        raise UnpackError("RPCS3 took far too long to install this package, so it was stopped")
                    try:
                        made = log.read_bytes().count(b"PKG: Created file") if log.exists() else 0
                    except OSError:
                        made = 0
                    pct = min(99, int(100 * made / max(1, info["files"])))
                    if pct != last:
                        last = pct
                        job.status = f"Installing {k + 1}/{n} {pct}%" if n > 1 else f"Installing {pct}%"
                        self.post("job", job)
                        self.post("progress", (k * 100 + pct) / n)
                text = log.read_bytes().decode("utf-8", "replace") if log.exists() else ""
                m = re.search(r"Successfully installed .*?\(title_id=(\w+)", text)
                if not m:
                    self.post("log", f"RPCS3 exited with code {proc.returncode}. The end of its own log:" if text else
                                     f"RPCS3 exited with code {proc.returncode} and wrote no log.", "warn")
                    for ln in text.splitlines()[-6:]:
                        self.post("log", "  " + ln[:200], "info")
                    why = re.findall(r"^·[EF] .*$", text, re.M)
                    detail = (why[-1].split("}", 1)[-1].strip() if why else "")[:160]
                    raise UnpackError("your RPCS3 could not install " + info["path"].name + (f": {detail}" if detail else
                                      ". (Is this RPCS3 recent enough, and is the package complete?)"))
                seen.append(m.group(1))
            games = work / "dev_hdd0" / "game"
            ids = sorted(d.name for d in games.iterdir() if d.is_dir() and d.name.replace("＄", "$") != "$locks" and d.name != "TEST12345")
            if not ids:
                raise UnpackError("RPCS3 said it installed the package, but no game folder appeared")
            if len(ids) == 1:
                os.replace(games / ids[0], out)
            else:                                           # the packages put files in more than one place: each gets a folder of its own
                out.mkdir(parents=True)
                for x in ids:
                    os.replace(games / x, out / x)
            ok = True
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not ok:
                shutil.rmtree(out, ignore_errors=True)
        if ok:
            job.note = "; ".join(x for x in (f"{n} packages merged" if n > 1 else "", "updates only: not the whole game" if only_updates else "") if x)
            self.folder_finish(job, out)
            self.post("log", "The package sources were kept (a package has no checksum to prove the copy against). No licence (RAP) is needed to unpack; "
                             "keep yours for playing.", "info")

    def pkg_own_unpack(self, job, o, out, key):
        """The packages (game, then its updates) -> the game folder, opened by this program with the user's own key."""
        work = out.parent / f".{out.name[:40]}.chdm_pkg"
        shutil.rmtree(work, ignore_errors=True)
        n = len(job.pkgs)
        self.post("log", f"\n{job.path.name}  [PS3 package{'s' if n != 1 else ''} → game folder, opened with your own key]", "info")
        base = self.cso_progress(job, "Unpacking")
        ok = False
        try:
            work.mkdir(parents=True)
            used = {}                                       # install folder -> how many packages went into it
            for k, info in enumerate(job.pkgs):
                dest = work / info["install_dir"]
                dest.mkdir(exist_ok=True)
                first = not used.get(info["install_dir"])
                used[info["install_dir"]] = used.get(info["install_dir"], 0) + 1
                self.post("log", f"Package {k + 1} of {n}: {info['path'].name}" + ("  (update)" if info["patch"] else "  (extra content)" if info["dlc"] else "")
                          + (f"  → folder {info['install_dir']}" if info["install_dir"] != info["title_id"] else ""), "info")
                ents, read, close = pkg_source(info, key)
                try:
                    if not first:          # a file that is already there is only replaced when the package says so (as the console does)
                        ents = [e for e in ents if not (e[1] and dest.joinpath(*[x.decode("utf-8", "replace") for x in e[0]]).exists() and not e[2][1])]
                    self.unpack_entries(job, ents, read, dest, lambda x, k=k: base((k + x) / n), "utf-8", lambda r: ps3_folder_source(r, True),
                                        verify=self.verify_wanted() and first)
                finally:
                    close()
            if len(used) == 1:
                os.replace(work / next(iter(used)), out)
                shutil.rmtree(work, ignore_errors=True)
            else:                                           # the packages put files in more than one folder: each keeps a folder of its own
                os.replace(work, out)
            ok = True
        except PkgKeyError as e:
            job.status, job.result = "Failed (wrong key)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not ok:
                shutil.rmtree(out, ignore_errors=True)
        if ok:
            job.note = "; ".join(x for x in (f"{n} packages merged" if n > 1 else "", "updates only: not the whole game" if all(i["patch"] for i in job.pkgs) else "") if x)
            self.folder_finish(job, out)
            if n > 1 and self.verify_wanted():
                self.post("log", "The game package was read back and checked; the updates written over it were not (they change files the check "
                                 "would compare).", "info")
            self.post("log", "The package sources were kept. No licence (RAP) is needed to unpack; keep yours for playing.", "info")

    def do_folder_to_pkg(self, job, o):
        """A PSN game folder -> a package that RPCS3 installs, sealed with the user's own key."""
        key = parse_hex_key(o.get("pkg_key", ""))
        if not key:
            job.status, job.result = "Failed (setup needed)", "making a package needs your own package key (Settings, Formats, PlayStation 3: give it your key)"
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".pkg"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        info = job.ps3_info
        cid = pkg_content_id(info["id"], info.get("title", ""))
        self.post("log", f"\n{job.path.name}  [game folder → PKG, sealed with your key]  (for RPCS3; a real console would also want Sony's signature, "
                         "which nobody can make)", "info")
        verify = self.verify_wanted()
        prog = self.cso_progress(job, "Building PKG")
        stop = lambda: self.stop_flag  # noqa: E731
        ents, read, close = ps3_folder_source(job.path, True)
        try:
            folder = job.path.name
            odd = folder.upper() != info["id"].upper()
            if odd and (not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", folder)):
                raise UnpackError("this folder's name is used as the folder the package installs into, and it has characters that can't be used there "
                                  "(letters, numbers, - and _ only); rename the folder first")
            build_pkg(ents, read, part, key, cid, lambda x: prog(x * (0.6 if verify else 1.0)), stop,
                      content_type=4 if info.get("category") == "GD" else 5, install_dir=folder if odd else "")
            if verify:
                self.post("log", "Reading the new package back against the folder…", "info")
                e2, r2, c2 = pkg_source(pkg_info(part), key)
                try:
                    if not entries_match((ents, read), (e2, r2), lambda x: prog(0.6 + x * 0.4), stop):
                        raise UnpackError("the package that was built didn't match the folder it came from; it was discarded")
                finally:
                    c2()
            os.replace(part, out)
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            close()
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def sw_finish(self, job, out, part, label):
        os.replace(part, out)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def do_nsp_to_xci(self, job, o):
        """Switch packages (the game, its newest update, its extra content) -> one XCI cartridge image, using the user's own keys when a game needs them."""
        keys = parse_switch_keys(o.get("switch_keys", ""))
        infos = job.nsps
        if not any(t["kind"] in ("base", "") for i in infos for t in i["titles"]):
            job.status, job.result = "Failed (setup needed)", "only an update or extra content was added; add the game's own NSP as well, because an XCI needs the game"
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        if any(i["ticketed"] for i in infos) and not keys:
            job.status, job.result = "Failed (setup needed)", ("this game needs your own keys to be converted: Settings, Formats, Nintendo Switch, "
                                                               "and choose your prod.keys file or paste it in")
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".xci"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [Switch package{'s' if len(infos) != 1 else ''} → XCI]  (for emulators and flash carts; a real console would also want "
                         "Nintendo's signature, which nobody can make)", "info")
        verify = self.verify_wanted()
        prog = self.cso_progress(job, "Building XCI")
        stop = lambda: self.stop_flag  # noqa: E731
        try:
            plan = sw_plan(infos, keys)
            for i, t in sw_select(infos)[1]:
                self.post("log", f"{i['path'].name}: the {t['kind'] or 'title'} version {t['version']} was left out, because a newer one for the same game is in the list, and a Switch "
                                 "update is a patch against the game itself, so only the newest is needed.", "info")
            build_xci(plan, part, keys, lambda x: prog(x * (0.7 if verify else 1.0)), stop)
            if verify:
                self.post("log", "Reading the new XCI back against the packages…", "info")
                xci_verify(part, plan, lambda x: prog(0.7 + x * 0.3), stop)
            self.sw_finish(job, out, part, "XCI")
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        if o["delete_src"] and verify:
            self.remove_sources(job, o)

    def do_xci_to_nsp(self, job, o):
        """A Switch XCI -> an NSP holding the same files, unchanged."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".nsp"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [→ NSP]  (the game's files are copied as they are)", "info")
        verify = self.verify_wanted()
        prog = self.cso_progress(job, "Building NSP")
        stop = lambda: self.stop_flag  # noqa: E731
        try:
            if len(job.nsps) > 1 and any(t["kind"] == "" for i in job.nsps for t in i["titles"]):
                raise UnpackError("to put several files together, CHD Manager has to tell the game, its updates and its extra content apart, and that needs your own keys "
                                  "(Settings, Formats, Nintendo Switch)")
            for i, t in sw_select(job.nsps)[1]:
                self.post("log", f"{i['path'].name}: the {t['kind'] or 'title'} version {t['version']} was left out, because a newer one is in the list.", "info")
            files = sw_nsp_files(job.nsps)
            build_nsp(files, part, lambda x: prog(x * (0.6 if verify else 1.0)), stop)
            if verify:
                self.post("log", "Reading the new NSP back against the XCI…", "info")
                nsp_verify(part, files, lambda x: prog(0.6 + x * 0.4), stop)
            self.sw_finish(job, out, part, "NSP")
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        if o["delete_src"] and verify:
            self.remove_sources(job, o)

    def do_folder_to_nup(self, job, o):
        """A decrypted Switch game folder -> a NUP (a community container, zstd compressed, with a checksum for every file). Experimental."""
        pkgs = nup_bundle(job.path) or []
        err = nup_resolve(pkgs, job.path) if pkgs else "it has nothing in it that can go into a NUP"
        if err:
            job.status, job.result = "Failed (can't build)", err
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".nup"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        names = " + ".join({0: "game", 1: f"update {x['version']}", 2: "extra content"}[x["kind"]] for x in pkgs)
        self.post("log", f"\n{job.path.name}  [game folder → NUP: {names}]  (experimental: written from the public draft of the format; not tried in an emulator yet)", "info")
        verify = self.verify_wanted()
        prog = self.cso_progress(job, "Building NUP")
        stop = lambda: self.stop_flag  # noqa: E731
        try:
            build_nup(pkgs, part, lambda x: prog(x * (0.6 if verify else 1.0)), stop)
            if verify:
                self.post("log", "Reading the new NUP back against the folder…", "info")
                nup_verify(part, pkgs, lambda x: prog(0.6 + x * 0.4), stop)
            os.replace(part, out)
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.zar_raw)} → {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and verify:
            self.remove_sources(job, o)

    def do_nup_to_folder(self, job, o):
        """A NUP -> the folder of files it holds (every file checked against its checksum)."""
        out, skip = self.folder_target(job, o)
        if skip:
            return
        work = out.parent / f".{out.name[:40]}.chdm_nup"
        shutil.rmtree(work, ignore_errors=True)
        self.post("log", f"\n{job.path.name}  [NUP → game folder]  (experimental)", "info")
        prog = self.cso_progress(job, "Unpacking")
        ok = False
        try:
            work.mkdir(parents=True)
            nup_extract(job.path, work, prog, lambda: self.stop_flag)
            os.replace(work, out)
            ok = True
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if ok:
            self.folder_finish(job, out)
            self.post("log", f"OK  {out}", "ok")
            if o["delete_src"] and self.verify_wanted():
                self.remove_sources(job, o)

    def do_sw_split(self, job, o):
        """A Switch XCI or NSP that holds several titles -> a folder with one NSP for the game, one for the update and one for each extra content."""
        if any(t["kind"] == "" for i in job.nsps for t in i["titles"]):
            job.status, job.result = "Failed (setup needed)", ("to tell the game, its update and its extra content apart, this needs your own keys: "
                                                               "Settings, Formats, Nintendo Switch")
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, skip = self.folder_target(job, o)
        if skip:
            return
        work = out.parent / f".{out.name[:40]}.chdm_split"
        shutil.rmtree(work, ignore_errors=True)
        chosen, left = sw_select(job.nsps)
        stem = re.sub(r"\s*[\[(][^\])]*[\])]", "", job.home.stem).strip() or job.home.stem
        self.post("log", f"\n{job.path.name}  [→ {len(chosen)} separate NSPs]", "info")
        verify = self.verify_wanted()
        prog = self.cso_progress(job, "Splitting")
        stop = lambda: self.stop_flag  # noqa: E731
        ok = False
        try:
            work.mkdir(parents=True)
            names = set()
            for k, (i, t) in enumerate(chosen):
                name = f"{stem} [{t['tid']:016X}][v{t['version']}].nsp"
                if name in names:
                    name = name[:-4] + f" ({k}).nsp"
                names.add(name)
                files = sw_title_files(i, t)
                self.post("log", f"{name}  ({t['kind']}, {len(files)} files)", "info")
                share = lambda x, k=k: prog((k + x * (0.6 if verify else 1.0)) / len(chosen))  # noqa: E731
                build_nsp(files, work / name, share, stop)
                if verify:
                    nsp_verify(work / name, files, lambda x, k=k: prog((k + 0.6 + 0.4 * x) / len(chosen)), stop)
            os.replace(work, out)
            ok = True
        except (UnpackError, ValueError) as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't build)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not ok:
            return
        self.folder_finish(job, out)
        self.post("log", f"OK  {out}  ({len(chosen)} packages)", "ok")
        if o["delete_src"] and verify:
            self.remove_sources(job, o)

    def do_join_ps3(self, job, o):
        """The pieces of a split PS3 disc image -> one ISO."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".iso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        n = len(job.src_files)
        self.post("log", f"\n{job.path.name}  [joining {n} piece{'s' if n != 1 else ''} → one ISO]", "info")
        try:
            join_pieces(job.src_files, part, self.cso_progress(job, "Joining"), lambda: self.stop_flag, verify=self.verify_wanted())
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't join)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({n} piece{'s' if n != 1 else ''}, {fmt_size(job.out_size)})", "ok")
        if o["delete_src"] and self.verify_wanted():
            self.remove_sources(job, o)

    def unpack_entries(self, job, ents, read, dest, prog, enc, readback, verify=None):
        """Write the files of a disc image or archive into the folder dest. A name this computer can't use is changed (and listed in the
        log and in the finished row) instead of stopping the whole disc. With 'verify' on, what was written is read back against the
        source: readback(folder) -> (entries, read, close) of what is on disk."""
        verify = self.verify_wanted() if verify is None else verify
        stop = lambda: self.stop_flag  # noqa: E731
        root = Path(dest).resolve()
        fixed, renamed = fix_entry_names(ents, enc)
        if renamed:
            shown = "; ".join(f"{a!r} → {b!r}" for a, b in renamed[:6]) + (f"; and {len(renamed) - 6} more" if len(renamed) > 6 else "")
            self.post("log", f"{job.path.name}: {len(renamed)} name(s) can't be used on this computer and were changed: {shown}", "warn")
            job.note = f"{len(renamed)} name(s) changed, see the log"
        total = sum(sz for _p, f, _o, sz in fixed if f) or 1
        done = 0
        for parts, is_file, src, size in fixed:
            target = root.joinpath(*[x.decode(enc) for x in parts])
            if root not in target.resolve().parents:
                raise UnpackError("a name in the disc image points outside the folder it is being unpacked into; nothing was made")
            if not is_file:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(target, "wb") as g:
                for piece in read(src, size):
                    g.write(piece)
                    done += len(piece)
                    prog(done / total * (0.6 if verify else 1.0))
                    if stop():
                        raise UnpackError("stopped")
        if verify:
            self.post("log", "Reading the new folder back against the source…", "info")
            e2, r2, c2 = readback(root)
            try:
                if not entries_match((fixed, read), (e2, r2), lambda x: prog(0.6 + x * 0.4), stop):
                    raise UnpackError("the files that were written didn't match what they came from; they were discarded")
            finally:
                c2()

    def ps3_to_folder(self, job, dest, prog, path):
        """The files of a PS3 disc image (or any plain ISO 9660 disc) -> loose files in the folder dest."""
        info = ps3_iso_kind(path)
        if info and info.get("encrypted"):
            raise UnpackError("this PS3 disc image is still encrypted (its protected parts can't be read without your own disc key), so its files would "
                              "come out unreadable. Decrypt it first, or make a CHD of it, which keeps it exactly as it is")
        ents, read, close = ps3_iso_source(path)
        try:
            self.unpack_entries(job, ents, read, dest, prog, "utf-8", lambda r: ps3_folder_source(r, True))
        finally:
            close()
        prog(1.0)

    def disc_to_folder(self, job, dest, prog, iso=None):
        """The files of a disc image (or a ZAR) -> a game folder: Xbox 360, PlayStation 3, or any other plain ISO 9660 disc."""
        if iso is None and job.container in ("zar", "xfolder"):
            return self.xbox_to_folder(job, dest, prog)
        path = iso or (job.src_files if job.container == "ps3parts" else job.path)
        if ps3_iso_kind(path) is not None:
            return self.ps3_to_folder(job, dest, prog, path)
        if xbox_iso_kind(path) in ("360", "xbox"):
            return self.xbox_to_folder(job, dest, prog, iso=iso)
        if is_iso9660(path):
            self.post("log", f"{job.path.name}: not an Xbox 360 or PS3 disc, so its files are unpacked as a plain ISO 9660 disc.", "info")
            return self.ps3_to_folder(job, dest, prog, path)
        raise UnpackError("this isn't a disc image that can be unpacked to a folder (it is not an Xbox 360 disc, a PlayStation 3 disc or "
                          "a plain ISO 9660 disc; UDF-only images aren't supported)")

    def xbox_to_folder(self, job, dest, prog, iso=None):
        """The files of an Xbox 360 ZAR or disc image -> loose files in the folder dest. With 'verify' on, what was written is
        read back against the source."""
        if iso is not None or self.xbox_job_is_iso(job):
            iso = iso or job.path
            if xbox_iso_kind(iso) not in ("360", "xbox"):
                raise UnpackError("this isn't an Xbox disc image (it has no default.xex at its top), so it can't become a game folder")
            ents, read, close = iso_source(iso)
        else:
            ents, read, close = self.xbox_source(job)
        try:
            self.unpack_entries(job, ents, read, dest, prog, "cp1252", folder_source)
        finally:
            close()
        prog(1.0)

    def folder_target(self, job, o):
        """Where an unpacked game goes: a folder named like the source, next to it (or in the chosen output folder).
        Returns (folder, skip); an existing one is skipped, replaced or numbered as 'If output exists' says."""
        stem = self.output_path(job, o, ".x").stem
        out = self.output_path(job, o, ".x").parent / stem
        if out.exists() and any(out.iterdir()):
            if o["exists"] == "Skip":
                job.status, job.result = "Skipped (exists)", f"{out.name} already exists (see 'If output exists' in Settings)"
                self.post("log", f"Skipped {job.path.name}: folder {out.name} already exists.", "warn")
                return out, True
            if o["exists"] == "Rename":
                k = 1
                while out.with_name(f"{stem} ({k})").exists():
                    k += 1
                out = out.with_name(f"{stem} ({k})")
            else:
                shutil.rmtree(out, ignore_errors=True)
        return out, False

    def folder_finish(self, job, out):
        job.out_path = out
        job.out_size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        job.status, job.result = "Done", f"→ {out.name}\\" + (f"  ({job.note})" if job.note else "")
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def do_to_folder(self, job, o):
        """An Xbox 360 ZAR or ISO, or a PS3 ISO -> a game folder."""
        out, skip = self.folder_target(job, o)
        if skip:
            return
        created = not out.exists()
        self.post("log", f"\n{job.path.name}  [{job.platform or 'disc'} → game folder]", "info")
        ok = False
        try:
            out.mkdir(parents=True, exist_ok=True)
            self.disc_to_folder(job, out, self.cso_progress(job, "Unpacking"))
            ok = True
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            if not ok and created:
                shutil.rmtree(out, ignore_errors=True)
        if ok:
            self.folder_finish(job, out)
            if o["delete_src"]:
                self.remove_sources(job, o)

    def extract_to_folder(self, job, o):
        """A DVD CHD of an Xbox disc -> a game folder (through a temporary ISO)."""
        if job.disc != "DVD":
            job.status, job.result = "Failed (not possible)", f"A game folder is only possible for an Xbox 360 or PlayStation 3 disc image; this CHD holds a {job.disc} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, skip = self.folder_target(job, o)
        if skip:
            return
        created = not out.exists()
        tmp = out.parent / f".{out.name[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        ok = False
        try:
            iso = tmp / "t.iso"
            cmd = [CHDMAN, "extractdvd", "-i", str(job.path), "-o", str(iso), "-f"]
            self.post("log", f"\n{job.path.name}  [DVD CHD → game folder]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            out.mkdir(parents=True, exist_ok=True)
            try:
                self.disc_to_folder(job, out, self.cso_progress(job, "Unpacking"), iso=iso)
                ok = True
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                else:
                    job.status, job.result = "Failed (can't unpack)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if not ok and created:
                shutil.rmtree(out, ignore_errors=True)
        if ok:
            self.folder_finish(job, out)

    def extract_to_zar(self, job, o):
        """A DVD CHD of an Xbox 360 disc -> a ZAR (through a temporary ISO)."""
        if job.disc != "DVD":
            job.status, job.result = "Failed (not possible)", f"A ZAR is only possible for an Xbox 360 DVD; this CHD holds a {job.disc} disc."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zar"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        try:
            iso = tmp / "t.iso"
            cmd = [CHDMAN, "extractdvd", "-i", str(job.path), "-o", str(iso), "-f"]
            self.post("log", f"\n{job.path.name}  [DVD CHD → ZAR]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            try:
                self.xbox_to_zar(job, part, self.cso_progress(job, "Making ZAR"), iso=iso)
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                else:
                    job.status, job.result = "Failed (can't build)", str(e)
                    self.post("log", f"{job.path.name}: {e}", "err")
                return
            except OSError as e:
                job.status, job.result = "Failed (disk)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
                return
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status, job.result = "Done", f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            part.unlink(missing_ok=True)

    def do_cso_to_iso(self, job, o):
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".iso"), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        self.post("log", f"\n{job.path.name}  [CSO → ISO]", "info")
        try:
            cso_to_iso(job.path, part, self.cso_progress(job, "Unpacking"), lambda: self.stop_flag)
            if o["verify_after"] and not verify_cso(job.path, part, self.cso_progress(job, "Verifying"), lambda: self.stop_flag):
                job.status, job.result = "Verify failed", "the unpacked ISO didn't match the CSO; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't unpack)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}", "ok")

    def extract_to_cso(self, job, o, lz4=False):
        """CHD -> CSO (or ZSO): a DVD CHD or a single-data-track CD CHD, through an ISO."""
        if job.disc != "DVD":
            return self.extract_to_iso(job, o, to_cso=True, lz4=lz4)
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".zso" if lz4 else ".cso"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        try:
            iso = tmp / "t.iso"
            cmd = [CHDMAN, "extractdvd", "-i", str(job.path), "-o", str(iso), "-f"]
            self.post("log", f"\n{job.path.name}  [DVD → {'ZSO' if lz4 else 'CSO'}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            self.write_cso_file(job, o, iso, out.with_name(out.name + ".part"), out, lz4)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            out.with_name(out.name + ".part").unlink(missing_ok=True)

    # ---- Wii keys and Dolphin
    def refresh_wii(self):
        RPCS3[0] = find_rpcs3(self.v["rpcs3"].get().strip())
        PKG_KEY[0] = parse_hex_key(self.v["pkg_key"].get())
        SWITCH_KEYS[0] = parse_switch_keys(self.v["switch_keys"].get())
        if hasattr(self, "sw_status"):
            k = SWITCH_KEYS[0]
            self.sw_status.config(text=(f"✓ Keys are set ({len(k)} kept). They open games made for Switch firmware up to {sw_fw(sw_level(k))}." +
                                        ("" if "header_key" in k else " The header_key line is missing, so they can't be used.")) if k
                                  else "Keys: not set. Games without a ticket convert without them.")
        if hasattr(self, "rpcs3_status"):
            self.rpcs3_status.config(text=(f"✓ RPCS3 found: {RPCS3[0]}" if RPCS3[0] else "RPCS3: not set yet.") + "   " +
                                     ("✓ A package key is set." if PKG_KEY[0] else "Package key: not set.") +
                                     ("" if RPCS3[0] or PKG_KEY[0] else "  Without either, .pkg packages can be added but not unpacked."))
        load_wii_keys(self.v["wii_common_key"].get(), self.v["wii_korean_key"].get())
        WII_TOOL[0] = find_dolphintool(self.v["dolphintool"].get().strip())
        WII_SCRUB[0] = bool(self.v["wbfs_scrub"].get())
        if hasattr(self, "wii_status"):
            self.wii_status.config(text=self.wii_status_text())
        for j in getattr(self, "jobs", []):
            if j.container == "disc" and not j.status.startswith("Done") and j.target not in [k for k, _ in j.available_targets()]:
                j.set_target(j.default_target())
        if hasattr(self, "tree"):
            self.update_action_labels()

    def wii_status_text(self):
        if not (WII_KEYS[0] or WII_TOOL[0]):
            return "Not set up yet: needs Dolphin, or your Wii key (use the Wii setup button)"
        return "Ready: " + " and ".join(x for x in ("Dolphin found" if WII_TOOL[0] else "", "Wii key found" if WII_KEYS[0] else "") if x)

    def open_keys_folder(self):
        ensure_keys_dir()
        try:
            os.startfile(KEYS_DIR)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Couldn't open {KEYS_DIR}: {e}")

    def themed_dialog(self, title):
        w = tk.Toplevel(self.root)
        w.title(title)
        w.configure(bg=self.t["bg"])
        w.transient(self.root)
        w.resizable(False, False)
        fr = ttk.Frame(w, padding=18)
        fr.pack(fill="both", expand=True)
        return w, fr

    def place_dialog(self, w):
        w.update_idletasks()
        x = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - w.winfo_reqwidth()) // 2)
        y = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - w.winfo_reqheight()) // 3)
        w.geometry(f"+{x}+{y}")

    def show_wii_setup(self):
        """One window for everything Wii: show where Dolphin is, or give your key (pick the file, or paste it)."""
        old = getattr(self, "wii_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                return
        except tk.TclError:
            pass
        w, fr = self.themed_dialog("Wii setup")
        self.wii_win = w
        ttk.Label(fr, justify="left", wraplength=470, font=("Segoe UI Semibold", 10),
                  text="To turn a Wii disc into RVZ, WIA or WBFS, CHD Manager needs ONE of these two things:").grid(
            row=0, column=0, sticky="w")
        o1 = ttk.LabelFrame(fr, text=" Option 1: Dolphin (the easy way) ", padding=10)
        o1.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(o1, justify="left", wraplength=450, text="If Dolphin is on this PC, just show CHD Manager where it is. "
                  "That is all it needs; no key is required.").grid(row=0, column=0, columnspan=2, sticky="w")
        d_stat = ttk.Label(o1, text="", wraplength=300, justify="left")
        d_stat.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=(8, 0))
        o2 = ttk.LabelFrame(fr, text=" Option 2: your own Wii key ", padding=10)
        o2.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Label(o2, justify="left", wraplength=450, text="Choose the file that holds your key, or paste the key "
                  "(32 letters and numbers). CHD Manager contains no keys and never downloads them.").grid(row=0, column=0, columnspan=3, sticky="w")
        k_stat = ttk.Label(o2, text="", wraplength=300, justify="left")
        k_stat.grid(row=1, column=1, columnspan=2, sticky="w", padx=(10, 0), pady=(8, 0))
        pasted = tk.StringVar()
        ttk.Label(o2, text="Or paste the key here:").grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Entry(o2, textvariable=pasted, width=38).grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Label(fr, justify="left", wraplength=470, style="Muted.TLabel", text="Without either, a Wii disc can still become an ISO, "
                  "GCZ or CISO. GameCube and Wii U discs and every other format never need any of this.").grid(
            row=3, column=0, sticky="w", pady=(10, 0))

        def refresh():
            d_stat.config(text=f"✓ Found: {WII_TOOL[0]}" if WII_TOOL[0] else "Not set yet")
            k_stat.config(text="✓ A key is set" if WII_KEYS[0] else "Not set yet")
            self.wii_status.config(text=self.wii_status_text())

        def find_dolphin():
            p = filedialog.askopenfilename(parent=w, title="Find Dolphin: choose Dolphin.exe (or DolphinTool.exe)",
                                           filetypes=[("Dolphin", "Dolphin.exe DolphinTool.exe"), ("All programs", "*.exe")])
            if not p:
                return
            p = Path(p)
            if p.name.lower() == "dolphin.exe":
                p = p.with_name("DolphinTool.exe")
            if p.name.lower() != "dolphintool.exe" or not p.is_file():
                messagebox.showerror(APP_NAME, "That isn't Dolphin, or this Dolphin has no DolphinTool.exe next to it. "
                                     "Pick Dolphin.exe from the Dolphin folder (a recent Dolphin download).", parent=w)
                return
            self.v["dolphintool"].set(str(p))
            self.save_settings()
            refresh()

        def take_key(data, name=""):
            keys = parse_wii_keys(data, name)
            if not keys[0] and not keys[1]:
                messagebox.showerror(APP_NAME, "No key found there. A key is 32 letters and numbers (0-9 and a-f).", parent=w)
                return
            if keys[0]:
                self.v["wii_common_key"].set(keys[0].hex())
            if keys[1]:
                self.v["wii_korean_key"].set(keys[1].hex())
            self.save_settings()
            pasted.set("")
            refresh()

        def choose_key():
            p = filedialog.askopenfilename(parent=w, title="Choose the file that holds your Wii key",
                                           filetypes=[("Key files", "*.txt *.key *.bin *.hex"), ("All files", "*.*")])
            if not p:
                return
            try:
                if Path(p).stat().st_size > 4096:
                    raise OSError("that file is too big to be a key file")
                take_key(Path(p).read_bytes(), Path(p).name)
            except OSError as e:
                messagebox.showerror(APP_NAME, f"Couldn't read that file: {e}", parent=w)

        ttk.Button(o1, text="Find Dolphin…", style="Accent.TButton", command=find_dolphin).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(o2, text="Choose key file…", style="Accent.TButton", command=choose_key).grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(o2, text="Use this key", command=lambda: take_key(pasted.get().strip().encode("ascii", "ignore"))).grid(
            row=3, column=2, padx=(8, 0), pady=(4, 0))
        bf = ttk.Frame(fr)
        bf.grid(row=4, column=0, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Open the keys folder", command=self.open_keys_folder).pack(side="left", padx=(0, 16))
        ttk.Button(bf, text="Done", command=w.destroy).pack(side="left")
        refresh()
        self.place_dialog(w)

    def wii_warning(self):
        """At start: say that Wii to RVZ/WIA/WBFS is switched off until Dolphin or a key is set up."""
        if WII_KEYS[0] or WII_TOOL[0] or self.v["wii_warn_off"].get():
            return
        w, fr = self.themed_dialog("Wii keys needed")
        ttk.Label(fr, justify="left", wraplength=430, text=(
            "Turning a Wii disc into RVZ, WIA or WBFS needs Dolphin on this PC, or your own Wii key. Neither was found, "
            "so those conversions are switched off for now.\n\n"
            "Everything else works without it: GameCube and Wii U discs and every other format, and Wii discs can still "
            "become ISO, GCZ or CISO.\n\nSet it up now? It takes a moment: just show it where Dolphin is, or give it your key.")
                  ).grid(row=0, column=0, columnspan=2, sticky="w")
        dont = tk.BooleanVar(value=False)
        ttk.Checkbutton(fr, text="Don't show this again", variable=dont).grid(row=1, column=0, sticky="w", pady=(14, 0))

        def close(setup):
            if dont.get():
                self.v["wii_warn_off"].set(True)
                self.save_settings()
            w.destroy()
            if setup:
                self.show_wii_setup()
        bf = ttk.Frame(fr)
        bf.grid(row=1, column=1, sticky="e", pady=(14, 0))
        ttk.Button(bf, text="Not now", command=lambda: close(False)).pack(side="left", padx=4)
        ttk.Button(bf, text="Set up…", style="Accent.TButton", command=lambda: close(True)).pack(side="left")
        w.protocol("WM_DELETE_WINDOW", lambda: close(False))
        self.place_dialog(w)

    def dolphin_convert(self, job, o, t, part, method, scrub=False):
        """Wii disc -> RVZ/WIA (or a scrubbed ISO) with the user's own DolphinTool.exe, used when there is no Wii key. True on success."""
        tool = find_dolphintool(o.get("dolphintool", ""))
        if not tool:
            job.status, job.result = "Failed (setup needed)", "this needs Dolphin or your Wii key (Settings, Formats, Set up…)"
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return False
        udir = Path(tempfile.mkdtemp(prefix="chdm_dolphin_"))
        try:
            cmd = [tool, "convert", "-i", str(job.path), "-o", str(part), "-f", t, "-u", str(udir)]
            if t == "iso":
                cmd += ["-s"] if scrub else []
            else:
                name = {v: k for k, v in self.RVZ_METHODS.items()}[method]
                cmd += ["-b", str(o["rvz_chunk"] if t == "rvz" else 0x200000), "-c", name, "-l", str(o["rvz_level"])]
            self.post("log", "No Wii key, so Dolphin's own tool does the work:", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Converting with Dolphin")
        finally:
            shutil.rmtree(udir, ignore_errors=True)
        if self.stop_flag or rc != 0 or not part.exists():
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            return False
        return True

    # ---- GameCube / Wii discs (Dolphin's formats)
    DISC_EXT = {"iso": ".iso", "rvz": ".rvz", "wia": ".wia", "gcz": ".gcz", "ciso": ".ciso", "wbfs": ".wbfs", "wud": ".wud",
                "wux": ".wux"}
    RVZ_METHODS = {"none": 0, "bzip2": 2, "lzma": 3, "lzma2": 4, "zstd": 5}

    def do_disc(self, job, o):
        t = (job.target or "").partition("_d")[0]
        out, _, skip = self.handle_exists(job, self.output_path(job, o, self.DISC_EXT[t]), o)
        if skip:
            return
        part = out.with_name(out.name + ".part")
        job.out_path = out
        fmt = job.disc_info.get("format", "ISO")
        self.post("log", f"\n{job.path.name}  [{job.platform}: {fmt} → {t.upper()}]", "info")
        prog = self.cso_progress(job, "Converting")
        stop = lambda: self.stop_flag  # noqa: E731
        src = vsrc = tmpiso = None
        try:
            src = open_disc(job.path)
            if t in ("iso", "wud"):
                disc_to_iso(src, part, prog, stop)
            elif t == "wux":
                write_wux(src, part, prog, stop)
            elif t == "gcz":
                write_gcz(src, part, prog, stop)
            elif t == "ciso":
                write_ciso(src, part, prog, stop)
            elif t == "wbfs" and job.platform == "Wii" and o["wbfs_scrub"] and not WII_KEYS[0]:
                tmpiso = out.with_name(f".{out.stem[:40]}.chdm_scrub.iso")
                if not self.dolphin_convert(job, o, "iso", tmpiso, 0, scrub=True):
                    return
                vsrc = open_disc(tmpiso)
                write_wbfs(vsrc, part, prog, stop, scrub=False)
            elif t == "wbfs":
                write_wbfs(src, part, prog, stop, scrub=bool(o["wbfs_scrub"]))
            else:
                method = self.RVZ_METHODS.get(str(o["rvz_method"]).lower(), 5)
                if t == "wia" and method == 5:
                    self.post("log", "WIA can't use zstd; using LZMA2 instead.", "info")
                if job.platform == "Wii" and not WII_KEYS[0]:
                    if not self.dolphin_convert(job, o, t, part, method):
                        return
                else:
                    write_rvz(src, part, prog, stop, rvz=t == "rvz", method=method, level=o["rvz_level"], chunk=o["rvz_chunk"])
            if o["verify_after"] and t not in ("iso", "wud"):
                if not verify_disc_file(part, vsrc or src, t, self.cso_progress(job, "Verifying"), stop):
                    job.status, job.result = "Verify failed", "the finished file didn't match the disc it was made from; it was discarded."
                    self.post("log", f"{job.path.name}: {job.result}", "err")
                    return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't convert)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except (OSError, struct.error, zlib.error, lzma.LZMAError) as e:
            job.status, job.result = "Failed (disk or file)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            for d in (src, vsrc):
                if d:
                    d.close()
            for f in (part, tmpiso):
                if f:
                    f.unlink(missing_ok=True)
        job.out_size = out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def extract_to_ecm(self, job, o):
        """CD CHD with one raw data track -> ECM."""
        out, _, skip = self.handle_exists(job, self.output_path(job, o, ".ecm"), o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        stop = lambda: self.stop_flag  # noqa: E731
        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            self.post("log", f"\n{job.path.name}  [CD → ECM]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            tracks = parse_cue_tracks(cue)
            if len(tracks) != 1 or tracks[0][1] not in ("MODE1/2352", "MODE2/2352"):
                job.status, job.result = "Failed (can't be an ECM)", "ECM needs one track of raw 2352-byte sectors."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            src_bin = parse_cue(cue)[0]
            ecm_encode(src_bin, part, self.cso_progress(job, "Compressing"), stop)
            if o["verify_after"] and not verify_ecm(part, src_bin, self.cso_progress(job, "Verifying"), stop):
                job.status, job.result = "Verify failed", "the finished ECM didn't decode back to the disc; it was discarded."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            os.replace(part, out)
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't write)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
            return
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            part.unlink(missing_ok=True)
        job.out_path, job.out_size = out, out.stat().st_size
        job.status, job.result = "Done", f"→ {out.name}"
        self.post("log", f"OK  {out}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    # ---- artwork for PBPs
    def find_art(self, o, stem, game_id, title):
        """Your artwork for this game, if the artwork folder (Settings, Formats) has a subfolder named after the output file,
        the game ID or the title. Returns a dict for write_pbp, or None."""
        root = o.get("art_dir")
        if not root or not Path(root).is_dir():
            return None
        for name in dict.fromkeys(n for n in (stem, game_id, title) if n):
            art, warn = read_art_folder(Path(root) / name)
            if art:
                self.post("log", f"Using your artwork from {Path(root) / name}: {', '.join(k.upper() for k in art)}.", "info")
                for w in warn:
                    self.post("log", f"Artwork: {w}.", "warn")
                return art
        return None

    def pbp_art(self, o, stem, game_id, title):
        """The artwork for a new PBP: yours where you have it, and for the icon and background you don't have, a plain
        title card with the game's name and code, so the game can be told apart in the PSP menu."""
        art = self.find_art(o, stem, game_id, title) or {}
        if o.get("title_cards"):
            dbf = find_gamedb()
            ent = load_gamedb(dbf).get(game_id) if dbf else None
            shown = (ent["main_title"] or ent["title"]) if ent else title
            made = make_title_art(shown, game_id, art)
            if made:
                self.post("log", f"Drew title cards for the PSP menu ({', '.join(k.upper() for k in made)}): {shown}.", "info")
            art = {**made, **art}
        return art or None

    def do_extract_art(self, job, o):
        """PBP -> its icon, pictures and sound as files (into the artwork folder when one is set, so they are picked up again)."""
        root = o.get("art_dir")
        out = Path(root) / job.home.stem if root and Path(root).is_dir() else self.output_path(job, o, ".x").parent / f"{job.home.stem} (artwork)"
        try:
            made = extract_pbp_art(job.path, out)
        except (UnpackError, OSError) as e:
            job.status, job.result = "Failed (can't read)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
            return
        if not made:
            job.status, job.result = "Nothing to extract", "this PBP has no icon, pictures or sound"
            self.post("log", f"{job.path.name}: {job.result}", "warn")
            return
        job.out_path = out
        job.status, job.result = "Done", f"→ {out.name}\\ ({', '.join(made)})"
        self.post("log", f"OK  {out}: {', '.join(made)}", "ok")

    def do_fix(self, job, o):
        """→ CUE/BIN with the EDC/ECC of every Mode 2 Form 1 sector rebuilt (audio, Form 2 and Mode 1 sectors stay as they are)."""
        stem = self.out_stem(job, o)
        tmp = self.output_path(job, o, ".x").parent / f".{stem[:40]}.chdm_fix"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        ext, made = job.path.suffix.lower(), None
        last = [-1]

        def prog(label):
            def f(x):
                pct = int(100 * x)
                if pct != last[0]:
                    last[0] = pct
                    job.status = f"{label} {pct}%"
                    self.post("job", job)
                    self.post("progress", pct)
            return f
        try:
            self.post("log", f"\n{job.path.name}  [rebuilding EDC/ECC]", "info")
            if ext == ".chd":
                cue = tmp / "t.cue"
                cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, "Extracting")
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    return
            elif job.container:
                cue = self.unpack_job(job, pbp_discs(job.path)[0] if job.container == "pbp" else None, tmp, "t", prog("Unpacking"))
            elif job.wrap_cue:
                cue = tmp / "t.cue"
                cue.write_text(f'FILE "{job.path}" BINARY\n  TRACK 01 {job.wrap_cue}\n    INDEX 01 00:00:00\n', "utf-8")
            else:
                cue = job.path
            files = list(dict.fromkeys(parse_cue(cue)))
            if not files or not all(f.is_file() for f in files):
                job.status, job.result = "Failed (can't read disc)", "the CUE's data files weren't found"
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            multi = len(files) > 1
            out_cue = self.output_path(job, o, ".cue", multi=multi)
            out_cue, force, skip = self.handle_exists(job, out_cue, o)
            if skip:
                return
            names = {f.name.lower(): (f"{out_cue.stem}.bin" if not multi else f"{out_cue.stem} (Track {i + 1}).bin") for i, f in enumerate(files)}
            changed = 0
            part_files = []
            for f in files:
                dst = out_cue.with_name(names[f.name.lower()])
                part_files.append(dst)
                changed += repair_bin(f, dst, prog("Rebuilding"), lambda: self.stop_flag)
            text = cue.read_text("utf-8", "replace")
            text = re.sub(r'(FILE\s+)(?:"([^"]+)"|(\S+))', lambda mt: mt.group(1) + '"' + names.get(Path(mt.group(2) or mt.group(3)).name.lower(), "x.bin") + '"', text, flags=re.I)
            out_cue.write_text(text, "utf-8")
            made = out_cue
            job.out_path, job.out_size = out_cue, sum(p.stat().st_size for p in part_files)
            job.status = "Done"
            job.result = f"→ {out_cue.name}" + (f" ({changed} sectors changed)" if changed else " (nothing needed fixing)")
            self.post("log", f"OK  {out_cue}: {changed} sector(s) changed.", "ok")
        except UnpackError as e:
            if self.stop_flag:
                job.status = "Stopped"
            else:
                job.status, job.result = "Failed (can't read)", str(e)
                self.post("log", f"{job.path.name}: {e}", "err")
        except OSError as e:
            job.status, job.result = "Failed (disk)", str(e)
            self.post("log", f"{job.path.name}: {e}", "err")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def extract_to_pbp(self, job, o):
        """CD CHD (or an .m3u playlist of CHDs, one per disc) -> one .pbp. PlayStation 1 discs only."""
        is_m3u = job.path.suffix.lower() == ".m3u"
        chds = job.m3u_chds if is_m3u else [job.path]
        n = len(chds)
        out = self.output_path(job, o, ".pbp")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        last = [-1]

        def prog(label, x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)

        def fail(status, why):
            job.status, job.result = status, why
            self.post("log", f"{job.path.name}: {why}", "err")

        try:
            self.post("log", f"\n{job.path.name}  [{'playlist, ' + str(n) + ' discs' if n > 1 else 'CD'} → PBP]", "info")
            layouts, ids = [], []
            for i, chd in enumerate(chds):
                label = f"disc {i + 1} of {n}" if n > 1 else ""
                cue = tmp / f"d{i}.cue"
                if chd_kind(chd) != "CD":
                    return fail("Failed (not a CD)", f"{chd.name} is not a CD image; PBP files hold PlayStation 1 CDs only.")
                cmd = [CHDMAN, "extractcd", "-i", str(chd), "-o", str(cue), "-f"]
                self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
                rc = self.run_proc(cmd, job, ("Extracting " + label).strip())
                if self.stop_flag or rc != 0:
                    job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                    return
                try:
                    lay = cue_layout(cue)
                except (UnpackError, OSError) as e:
                    return fail("Failed (can't read disc)", f"{chd.name}: {e}")
                gid = ps1_boot_id(lay["bin"], 24) or ps1_boot_id(lay["bin"], 16)
                if not gid:
                    return fail("Failed (not PS1)", f"{chd.name} doesn't look like a PlayStation 1 game, so it can't go in a PBP.")
                layouts.append(lay)
                ids.append(gid)
            if o["name_format"].strip() != "%FILENAME%":
                job.game_id = ids[0]                    # the code read from the disc names the file
                new = self.output_path(job, o, ".pbp")
                if new != out:
                    out, force, skip = self.handle_exists(job, new, o)
                    if skip:
                        return
                    part = out.with_name(out.name + ".part")
                    job.out_path = out
            donor = None
            if o.get("base_pbp_file"):
                try:
                    donor = read_base_pbp(o["base_pbp_file"])
                except (UnpackError, OSError) as e:
                    self.post("log", f"The base PBP wasn't used: {e}", "warn")
            self.post("log", f"Using base PBP {Path(o['base_pbp_file']).name} (loader, icon and boot logo for a PSP/Vita)." if donor else
                      "No base PBP: this PBP works in emulators and frontends, but a PSP/Vita needs one (Settings, Formats).",
                      "info" if donor else "warn")
            title = re.sub(r"\s*\((?:disc|cd)\s*\d+\)", "", out.stem, flags=re.I)
            title = re.sub(r"\s*\[[^\]]*\]", "", title).strip() or out.stem
            try:
                write_pbp(part, layouts, ids, title, lambda x: prog("Writing PBP", x), lambda: self.stop_flag, donor=donor,
                          level=o["pbp_level"], art=self.pbp_art(o, out.stem, ids[0], title))
                if o["verify_after"]:
                    if not verify_pbp(part, layouts, lambda x: prog("Verifying", x), lambda: self.stop_flag):
                        return fail("Verify failed", "the finished PBP didn't match the disc it was made from; it was discarded.")
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                    return
                return fail("Failed (can't write)", str(e))
            except OSError as e:
                return fail("Failed (disk)", str(e))
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status = "Done"
            job.result = f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def extract_to_cdi(self, job, o):
        """CD CHD -> DiscJuggler .cdi (the reverse of reading one)."""
        out = self.output_path(job, o, ".cdi")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        last = [-1]

        def prog(label, x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)

        def fail(status, why):
            job.status, job.result = status, why
            self.post("log", f"{job.path.name}: {why}", "err")

        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            self.post("log", f"\n{job.path.name}  [CD → CDI]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            try:
                write_cdi(part, cue, lambda x: prog("Writing CDI", x), lambda: self.stop_flag)
                if o["verify_after"] and not verify_cdi(part, cue, lambda x: prog("Verifying", x), lambda: self.stop_flag):
                    return fail("Verify failed", "the finished CDI didn't match the disc it was made from; it was discarded.")
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                    return
                return fail("Failed (can't write)", str(e))
            except OSError as e:
                return fail("Failed (disk)", str(e))
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status = "Done"
            job.result = f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def extract_to_vcd(self, job, o):
        """CD CHD -> POPStarter .vcd (PlayStation 1 discs only)."""
        out = self.output_path(job, o, ".vcd")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        part = out.with_name(out.name + ".part")
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True, exist_ok=True)
        job.out_path = out
        last = [-1]

        def prog(label, x):
            pct = int(100 * x)
            if pct != last[0]:
                last[0] = pct
                job.status = f"{label} {pct}%"
                self.post("job", job)
                self.post("progress", pct)

        def fail(status, why):
            job.status, job.result = status, why
            self.post("log", f"{job.path.name}: {why}", "err")

        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            self.post("log", f"\n{job.path.name}  [CD → VCD]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            try:
                write_vcd(part, cue, lambda x: prog("Writing VCD", x), lambda: self.stop_flag)
                if o["verify_after"] and not verify_vcd(part, cue, lambda x: prog("Verifying", x), lambda: self.stop_flag):
                    return fail("Verify failed", "the finished VCD didn't match the disc it was made from; it was discarded.")
            except UnpackError as e:
                if self.stop_flag:
                    job.status = "Stopped"
                    return
                return fail("Failed (can't write)", str(e))
            except OSError as e:
                return fail("Failed (disk)", str(e))
            os.replace(part, out)
            job.out_size = out.stat().st_size
            job.status = "Done"
            job.result = f"→ {out.name}"
            self.post("log", f"OK  {out}  ({fmt_size(job.out_size)})", "ok")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def extract_to_iso(self, job, o, to_cso=False, lz4=False):
        """CD CHD -> .iso (or .cso / .zso). Only possible when the disc is one data track; anything else would lose data."""
        out = self.output_path(job, o, (".zso" if lz4 else ".cso") if to_cso else ".iso")
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        tmp = out.parent / f".{out.stem[:40]}.chdm_tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        part = out.with_name(out.name + ".part")
        try:
            cue = tmp / "t.cue"
            cmd = [CHDMAN, "extractcd", "-i", str(job.path), "-o", str(cue), "-f"]
            job.out_path = out
            self.post("log", f"\n{job.path.name}  [CD → {('ZSO' if lz4 else 'CSO') if to_cso else 'ISO'}]", "info")
            self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
            rc = self.run_proc(cmd, job, "Extracting")
            if self.stop_flag or rc != 0:
                job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
                return
            tracks = parse_cue_tracks(cue)
            if len(tracks) != 1 or tracks[0][1] not in ("MODE1/2048", "MODE1/2352", "MODE2/2352"):
                what = "audio tracks or several tracks" if len(tracks) != 1 else f"{tracks[0][1]} data"
                job.status = "Failed (can't be an ISO)"
                job.result = f"This disc has {what}; an ISO would lose data. Extract it as CUE/BIN instead."
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            src, ttype = tracks[0]
            form2 = False
            if ttype == "MODE1/2048":
                os.replace(src, part)
            else:                                   # raw 2352-byte sectors: keep the 2048 bytes of user data in each
                mode2 = ttype == "MODE2/2352"
                off = 24 if mode2 else 16           # Mode 2 sectors carry an 8-byte subheader before the data
                total = max(1, src.stat().st_size // 2352)
                done, last = 0, -1
                with open(src, "rb") as f, open(part, "wb") as g:
                    while True:
                        chunk = f.read(2352 * 1024)
                        if not chunk:
                            break
                        mv = memoryview(chunk)
                        if mode2 and any(mv[i + 18] & 0x20 for i in range(0, len(chunk) - 2351, 2352)):
                            form2 = True           # a Form 2 sector (audio/video) cannot fit in an ISO
                            break
                        g.write(b"".join(mv[i + off:i + off + 2048] for i in range(0, len(chunk) - 2351, 2352)))
                        done += len(chunk) // 2352
                        pct = int(100 * done / total)
                        if pct != last:
                            last = pct
                            job.status = f"Making ISO {pct}%"
                            self.post("job", job)
                            self.post("progress", pct)
                        if self.stop_flag:
                            break
            if form2:
                job.status = "Failed (can't be an ISO)"
                job.result = ("This disc has audio/video (Mode 2 Form 2) sectors, common on PS1 discs; an ISO would lose "
                              "them. Extract it as CUE/BIN instead.")
                self.post("log", f"{job.path.name}: {job.result}", "err")
                return
            if self.stop_flag:
                job.status = "Stopped"
                return
            if to_cso:
                self.write_cso_file(job, o, part, out.with_name(out.name + ".part2"), out, lz4)
                return
            os.replace(part, out)
            ok = DiscReader(out, 2048, 0).sector(16)[1:6] == b"CD001"
            job.status = "Done"
            job.result = f"→ {out.name}" + ("" if ok else "  (no ISO 9660 header found - check it)")
            self.post("log", f"OK  {out}", "ok" if ok else "warn")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            out.with_name(out.name + ".part2").unlink(missing_ok=True)
            if part.exists():
                try:
                    part.unlink()
                except OSError:
                    pass

    def do_recompress(self, job, o):
        """CHD -> CHD with different compression / hunk size (chdman 'copy'). The original is never touched."""
        kind = job.disc if job.disc not in ("", "?") else chd_kind(job.path)
        job.disc = kind
        if kind not in ("CD", "GD-ROM", "DVD"):
            job.status = "Failed (not possible)"
            job.result = f"Recompress works for CD, GD-ROM and DVD CHDs; this one is {kind}."
            self.post("log", f"{job.path.name}: {job.result}", "err")
            return
        typ = "DVD" if kind == "DVD" else "CD"
        out = self.output_path(job, o, ".chd")
        try:
            if out.resolve() == job.path.resolve():
                out = out.with_name(out.stem + " (recompressed).chd")
        except OSError:
            pass
        out, force, skip = self.handle_exists(job, out, o)
        if skip:
            return
        cmd = [CHDMAN, "copy", "-i", str(job.path), "-o", str(out)] + (["-f"] if force else []) + self.common_args(o, typ)
        job.out_path = out
        self.post("log", f"\n{job.path.name}  [recompress {kind}]", "info")
        self.post("log", "$ " + " ".join(f'"{c}"' if " " in c else c for c in cmd), "cmd")
        rc = self.run_proc(cmd, job, "Recompressing")
        if self.stop_flag or rc != 0:
            job.status = "Stopped" if self.stop_flag else f"Failed (code {rc})"
            if out.exists():
                try:
                    out.unlink()
                except OSError:
                    pass
            return
        job.out_size = out.stat().st_size
        if o["verify_after"]:
            self.post("log", "Verifying…", "info")
            _, ok = self.verify_file(out, job, "Verifying")
            if ok is False:
                job.status = "Verify failed"
                self.post("log", f"VERIFY FAILED for {out.name} - your original CHD was not touched.", "err")
                return
        job.status = "Done"
        self.post("log", f"OK  {out.name}  ({fmt_size(job.size)} → {fmt_size(job.out_size)})", "ok")

    def run_proc(self, cmd, job, label, capture=None):
        low = bool(getattr(self, "cur_opts", {}).get("low_priority"))
        if os.name == "nt":
            extra = dict(creationflags=CREATE_NO_WINDOW | (0x00004000 if low else 0))          # BELOW_NORMAL
        else:
            extra = dict(preexec_fn=(lambda: os.nice(10)) if low else None)
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     bufsize=0, **extra)
        buf, last = b"", -1
        self.last_msg = self.reason_msg = ""
        while True:
            chunk = self.proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode("utf-8", "replace").strip()
                if not line:
                    continue
                m = re.search(r"([\d.]+)%\s+complete", line)
                if m:
                    pct = float(m.group(1))
                    if int(pct) != last:
                        last = int(pct)
                        job.status = f"{label} {last}%"
                        self.post("job", job)
                        self.post("progress", pct)
                    continue
                if capture is not None:
                    capture.append(line)
                if not line.startswith("chdman - "):
                    self.last_msg = line
                    low = line.lower()
                    bad = any(w in low for w in ("error", "fatal", "not divisible", "cannot", "can't", "failed", "invalid", "unsupported"))
                    if bad and not low.startswith("fatal error occurred"):
                        self.reason_msg = line              # the specific reason, not chdman's generic last line
                    self.post("log", "  " + line, "err" if bad else "info")
        if buf.strip():
            line = buf.decode("utf-8", "replace").strip()
            if capture is not None:
                capture.append(line)
            self.post("log", "  " + line, "info")
        rc = self.proc.wait()
        if rc != 0 and not self.stop_flag and (self.reason_msg or self.last_msg):
            job.result = (self.reason_msg or self.last_msg)[:140]              # the reason, visible in the table
        self.post("progress", 100 if rc == 0 else 0)
        return rc

    # ------------------------------------------------------------------ info / verify buttons
    def info_selected(self):
        sel = self.selected_jobs()
        if not sel:
            return
        job = sel[0]
        if job.path.suffix.lower() == ".chd":
            def work():
                rc, text = run_capture([CHDMAN, "info", "-v", "-i", str(job.path)])
                self.post("info", job.path.name, text)
            threading.Thread(target=work, daemon=True).start()
        else:
            lines = [f"File:       {job.path}", f"Platform:   {job.platform or 'unknown'}",
                     f"Disc type:  {job.type}{'  (forced)' if job.override != 'Auto' else '  (auto)'}",
                     f"Why:        {job.reason}", f"Total size: {fmt_size(job.size)}", "", "Files:"]
            lines += [f"  {'(MISSING) ' if not f.exists() else ''}{f}" for f in job.src_files]
            self.show_info(job.path.name, "\n".join(lines))

    def show_info(self, title, text):
        win = tk.Toplevel(self.root)
        win.title(f"Info - {title}")
        win.geometry("780x520")
        win.configure(bg=self.t["bg"])
        t = tk.Text(win, wrap="none", font=("Consolas", 9), bg=self.t["panel"], fg=self.t["fg"], relief="flat",
                    bd=0, padx=10, pady=8, insertbackground=self.t["fg"], selectbackground=self.t["sel"])
        t.insert("1.0", text)
        t.configure(state="disabled")
        sb = ttk.Scrollbar(win, command=t.yview)
        t.configure(yscrollcommand=sb.set)
        bt = ttk.Frame(win)
        bt.pack(side="bottom", fill="x", pady=6)
        ttk.Button(bt, text="Copy", command=lambda: (self.root.clipboard_clear(), self.root.clipboard_append(text))).pack(side="left", padx=8)
        ttk.Button(bt, text="Close", command=win.destroy).pack(side="right", padx=8)
        sb.pack(side="right", fill="y")
        t.pack(fill="both", expand=True)

    def verify_selected(self):
        chds = [j for j in self.selected_jobs() if j.path.suffix.lower() == ".chd"]
        if not chds:
            self.status.config(text="Select one or more .chd files to verify.")
            self.notify("Nothing to verify: click one or more .chd files in the list first (verifying works on CHD files only), then try again.")
            return
        for j in chds:
            j.set_target("verify")
            self.update_action_labels()
        self.start(chds)

    # ------------------------------------------------------------------ CHD Manager's own updates
    def check_app_update_async(self, manual=False):
        def work():
            try:
                self.post("app_upd_info", fetch_app_release(), "", manual)
            except Exception as e:  # noqa: BLE001
                self.post("app_upd_info", None, str(e), manual)
        threading.Thread(target=work, daemon=True).start()
        if manual:
            self.status.config(text="Checking GitHub for a newer CHD Manager…")
            self.set_footer("app", "checking…")

    def app_update_available(self):
        return bool(getattr(self, "app_latest", None) and ver_tuple(self.app_latest["tag"]) > ver_tuple(APP_VER))

    def diagnostics_text(self):
        """Handy for bug reports. Deliberately leaves out folder paths and user names."""
        ram = total_ram_gb()
        return "\n".join([
            f"CHD Manager {APP_VER} ({'exe' if FROZEN else 'script'})",
            f"chdman {self.chd_version or 'not found'}" + ("" if self.has_dvd else " (no createdvd)"),
            f"Windows: {platform.platform()}",
            f"Python {sys.version.split()[0]}, Tk {tk.TkVersion}",
            f"CPU threads: {os.cpu_count()}" + (f", RAM: {ram:.0f} GB" if ram else ""),
            f"Settings: threads {self.v['threads'].get()}, compression {self.v['compression'].get()}, "
            f"low priority {self.v['low_priority'].get()}, theme {self.v['theme'].get()}",
        ])

    def copy_diagnostics(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.diagnostics_text())
        self.status.config(text="Diagnostics copied to the clipboard.")

    def ask_update_choice(self, msg):
        """Three-way prompt -> 'now' | 'later' | 'skip'."""
        if self.silent:
            return getattr(self, "silent_choice", None) or ("now" if getattr(self, "silent_answer", True) else "later")
        result = {"v": "later"}
        win = tk.Toplevel(self.root)
        win.title("CHD Manager update")
        win.configure(bg=self.t["bg"])
        win.transient(self.root)
        win.resizable(False, False)
        ttk.Label(win, text=msg, wraplength=470, justify="left").pack(padx=22, pady=(20, 12))
        bf = ttk.Frame(win)
        bf.pack(padx=22, pady=(0, 18), anchor="e")

        def pick(v):
            result["v"] = v
            win.destroy()

        ttk.Button(bf, text="Update now", style="Accent.TButton", command=lambda: pick("now")).pack(side="left")
        ttk.Button(bf, text="Remind me next time", command=lambda: pick("later")).pack(side="left", padx=8)
        ttk.Button(bf, text="Skip this version", command=lambda: pick("skip")).pack(side="left")
        win.protocol("WM_DELETE_WINDOW", lambda: pick("later"))
        win.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_width()) // 2
        y = self.root.winfo_rooty() + 120
        win.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        win.grab_set()
        win.wait_window()
        return result["v"]

    def on_app_update_info(self, info, err, manual):
        if err or not info:
            if manual:
                self.status.config(text="Could not check for a CHD Manager update.")
                self.notify("Could not check for a CHD Manager update.\n\nThe reason: " + (err or "no answer from GitHub") +
                            "\n\nCheck your internet connection and try again later.", error=True)
                self.set_footer("app", "couldn't check", revert_after=8000)
            return
        self.app_latest = info
        new_v = ".".join(str(x) for x in ver_tuple(info["tag"]))
        if not self.app_update_available():
            self.draw_header()
            self.app_upd_label.config(text=f"Up to date ({APP_VER}).", style="Muted.TLabel")
            self.set_footer("app", "up to date" if manual else "", revert_after=8000 if manual else 0)
            if manual:
                self.status.config(text=f"CHD Manager {APP_VER} is the latest version.")
                self.log(f"CHD Manager {APP_VER} is up to date.", "ok")
            return
        self.app_upd_label.config(text=f"{new_v} available (you have {APP_VER}).", style="Warn.TLabel")
        self.set_footer("app", f"{new_v} available - click to update", "warn")
        self.draw_header()                      # shows the clickable "update available" badge
        self.log(f"A newer CHD Manager is available: {new_v} (you have {APP_VER}). Click the badge at the top right, "
                 "or use Settings, About page.", "warn")
        # startup: ask unless this exact version was skipped; the button and the badge always ask
        if manual or self.v["skip_version"].get() != info["tag"]:
            self.confirm_app_update()

    def confirm_app_update(self):
        info = self.app_latest
        new_v = ".".join(str(x) for x in ver_tuple(info["tag"]))
        notes = info["notes"].split("\n\n")[0][:300]
        if self.running:
            self.notify("Wait for the current jobs to finish, then click the update badge again.")
            return
        mode = "exe" if FROZEN else "page"
        if FROZEN:
            msg = (f"CHD Manager {new_v} is available (you have {APP_VER}).\n\n{notes}\n\n"
                   f"Update now? It downloads {info['name']} ({fmt_size(info['size'])}) from github.com/{APP_REPO}, "
                   "checks its SHA-256, test-runs it, and restarts into the new version. "
                   "Your settings and chdman stay as they are.")
        elif can_self_update_source() and info.get("kind") == "source" and info.get("url") and os.access(HERE, os.W_OK):
            mode = "source"
            msg = (f"CHD Manager {new_v} is available (you have {APP_VER}).\n\n{notes}\n\n"
                   f"Update now? It downloads {info['name']} ({fmt_size(info['size'])}) from github.com/{APP_REPO}, "
                   "checks its SHA-256, test-runs the new version, then replaces the program files in this folder and restarts. "
                   f"The old files are kept in a '{SOURCE_BACKUP}' folder. Your settings, keys and chdman are not touched.")
        else:
            why = ("This release has no download for this kind of install." if can_self_update_source() and not info.get("url")
                   else "This copy's folder can't be written to." if can_self_update_source() and not os.access(HERE, os.W_OK)
                   else "You're running the script version, so it can't replace itself.")
            msg = (f"CHD Manager {new_v} is available (you have {APP_VER}).\n\n{notes}\n\n"
                   f"{why} 'Update now' opens the download page.")
        choice = self.ask_update_choice(msg)
        if choice == "skip":
            self.v["skip_version"].set(info["tag"])
            self.log(f"Skipping {new_v} at startup. The update badge and Settings, About page still offer it.", "info")
        elif choice == "now":
            if mode == "page":
                if not self.silent:
                    webbrowser.open(info["page"])
                return
            self.app_upd_btn.config(state="disabled")
            self.app_btn.config(state="disabled")
            threading.Thread(target=self.app_update_worker if mode == "exe" else self.source_update_worker, args=(info,), daemon=True).start()
        # "later": nothing recorded, so it asks again next launch

    def app_update_worker(self, info):
        report_dir = None
        try:
            new_exe = stage_app_update(info, HERE, lambda pct, txt: self.post("upd_progress", pct, txt))
            self.post("upd_progress", 100, "Testing the new version…")
            report_dir = Path(tempfile.mkdtemp(prefix="chdm_selfcheck_"))
            report = report_dir / "report.json"
            run_capture([str(new_exe), "--smoke", str(report)], timeout=180)
            r = json.loads(report.read_text("utf-8")) if report.exists() else {}
            if r.get("status") != "Done" or ver_tuple(r.get("app_version", "")) != ver_tuple(info["tag"]):
                try:
                    new_exe.unlink()
                except OSError:
                    pass
                raise RuntimeError("The downloaded version failed its self-test, so it was not installed. "
                                   + (r.get("error", "") or f"(status: {r.get('status')}, version: {r.get('app_version')})"))
            old_exe = Path(sys.executable).resolve()
            self.post("app_upd_ready", str(write_swap_script(new_exe, old_exe)))
        except Exception as e:  # noqa: BLE001
            self.post("app_upd_done", False, str(e))
        finally:
            if report_dir:
                shutil.rmtree(report_dir, ignore_errors=True)

    def source_update_worker(self, info):
        """Linux / macOS: download, verify, test-run the new version from a scratch folder, install it, ask for a restart."""
        stage = None
        try:
            files = fetch_source_update(info, lambda pct, txt: self.post("upd_progress", pct, txt))
            check_source_files(files, info["tag"])
            self.post("upd_progress", 100, "Testing the new version…")
            stage = Path(tempfile.mkdtemp(prefix="chdm_src_test_"))
            (stage / "CHD_Manager.pyw").write_bytes(files["CHD_Manager.pyw"])
            report = stage / "report.json"
            run_capture([sys.executable, str(stage / "CHD_Manager.pyw"), "--smoke", str(report)], timeout=180)
            r = json.loads(report.read_text("utf-8")) if report.exists() else {}
            bad = (not r or r.get("error") or ver_tuple(r.get("app_version", "")) != ver_tuple(info["tag"])
                   or (r.get("chdman") and r.get("status") != "Done"))          # without chdman the conversion part of the test can't run
            if bad:
                raise RuntimeError("The downloaded version failed its self-test, so it was not installed. "
                                   + (r.get("error", "") or f"(status: {r.get('status')}, version: {r.get('app_version')})").strip()[-400:])
            self.post("upd_progress", 100, "Installing…")
            install_source_update(files, HERE)
            self.post("app_upd_restart")
        except Exception as e:  # noqa: BLE001
            self.post("app_upd_done", False, str(e))
        finally:
            if stage:
                shutil.rmtree(stage, ignore_errors=True)

    def on_app_update_restart(self):
        """The new files are in place: start the program again from them (the same process is replaced, so there is one window)."""
        self.status.config(text="Restarting into the new version…")
        self.log("Updated. Restarting into the new version…", "ok")
        self.restart_requested = True
        if self.silent:
            return
        self.save_settings()
        self.drop_all_arcs()
        try:
            os.execv(sys.executable, [sys.executable, str(HERE / "CHD_Manager.pyw")])
        except OSError as e:
            self.app_upd_btn.config(state="normal")
            self.app_btn.config(state="normal")
            messagebox.showinfo(APP_NAME, f"The update was installed, but the program couldn't restart itself ({e}).\n\n"
                                          "Close CHD Manager and start it again to use the new version.")

    def on_app_update_done(self, ok, msg):
        self.app_upd_btn.config(state="normal")
        self.app_btn.config(state="normal")
        self.set_footer("app")
        self.cur_bar["value"] = 0
        self.status.config(text="CHD Manager update failed - nothing was changed.")
        self.log("CHD Manager update failed: " + msg, "err")
        if not self.silent:
            messagebox.showerror(APP_NAME, "The update failed:\n" + msg + "\n\nYour current version was not changed.")

    def on_app_update_ready(self, script):
        self.status.config(text="Restarting into the new version…")
        self.log("Restarting into the new version…", "ok")
        self.app_swap_script = script
        if self.silent:
            return
        spawn_swap(script)
        self.save_settings()
        self.root.destroy()

    # ------------------------------------------------------------------ chdman update
    def check_update_async(self, manual=False):
        if os.name != "nt":
            if manual:
                self.notify("On Linux and macOS chdman comes from your package manager (on Debian, Ubuntu and SteamOS: "
                            "the 'mame-tools' package), so update it there.")
            return

        def work():
            try:
                self.post("upd_info", fetch_latest(), "", manual)
            except Exception as e:  # noqa: BLE001
                self.post("upd_info", None, str(e), manual)
        threading.Thread(target=work, daemon=True).start()
        if manual:
            self.status.config(text="Checking GitHub for the latest chdman…")
            self.set_footer("chdman", "checking…")

    def update_available(self):
        cur = ver_to_int(self.chd_version) if CHDMAN else 0
        return bool(self.latest and self.latest["num"] > cur)

    def update_clicked(self):
        if self.running:
            self.status.config(text="Wait for the current jobs to finish before updating chdman.")
            self.busy_notice("updating chdman")
            return
        if self.update_available():
            self.confirm_install()
        else:
            self.check_update_async(manual=True)

    def on_update_info(self, info, err, manual):
        if err or not info:
            if manual:
                self.status.config(text="Could not check for updates.")
                self.notify("Could not check for a chdman update.\n\nThe reason: " + (err or "no answer from GitHub") +
                            "\n\nCheck your internet connection and try again later.", error=True)
                self.set_footer("chdman", "couldn't check", revert_after=8000)
            return
        self.latest = info
        new_txt = f"0.{info['num']:03d}"
        if self.update_available():
            self.set_footer("chdman", f"{new_txt} available - click to update", "warn")
            self.log(f"A newer chdman is available: {new_txt} (you have {self.chd_version or 'none'}).", "warn")
            if manual:
                self.confirm_install()
        else:
            self.set_footer("chdman", "up to date" if manual else "", revert_after=8000 if manual else 0)
            if manual:
                self.status.config(text=f"chdman is up to date ({self.chd_version}).")
                self.log(f"chdman {self.chd_version} is the latest release.", "ok")

    def notify(self, msg, error=False):
        self.messages = getattr(self, "messages", []) + [msg]
        self.log(msg.replace("\n\n", "  ").replace("\n", " "), "warn")
        if not self.silent:
            (messagebox.showerror if error else messagebox.showinfo)(APP_NAME, msg)

    def ensure_7zip(self):
        """True if 7-Zip is available (offering to install it if not)."""
        if find_7z():
            return True
        if not self.ask("Updating chdman needs 7-Zip, a free file tool that isn't installed on this PC.\n\n"
                        "Install it now? It comes from the official Windows Package Manager (winget), is about 2 MB, "
                        "and Windows will ask for your permission first."):
            self.notify("No problem. Without 7-Zip, CHD Manager can't update chdman automatically, so the update "
                        "button won't work. Everything else in the program works normally.\n\n"
                        "To update by hand, download chdman from github.com/mamedev/mame/releases and pick it with "
                        "Browse in Settings, Output page. You can also install 7-Zip later from 7-zip.org.")
            return False
        self.status.config(text="Installing 7-Zip… (approve the Windows prompt if it appears)")
        self.root.update_idletasks()
        ok, msg = install_7zip()
        if ok:
            self.log("7-Zip installed.", "ok")
            return True
        if msg == "no-winget":
            self.notify("This PC doesn't have the Windows Package Manager (winget), so I can't install 7-Zip "
                        "automatically.\n\nPlease install it from 7-zip.org, then press the update button again. "
                        "Until then the update button won't work, but everything else does.", error=True)
        else:
            self.notify("7-Zip wasn't installed (the permission prompt may have been declined).\n\n"
                        "Without it CHD Manager can't update chdman automatically. Everything else works normally.\n"
                        "You can install 7-Zip from 7-zip.org and try again.\n\nDetails: " + msg, error=True)
        return False

    def confirm_install(self):
        info = self.latest
        name, _, size = info["exe"]
        if not self.ensure_7zip():
            self.status.config(text="Update cancelled (7-Zip not available).")
            return
        cur = self.chd_version or "none"
        if self.ask(f"Update chdman {cur}  →  0.{info['num']:03d}?\n\n"
                    f"Download: {name} ({fmt_size(size)}) from the official MAME release on GitHub.\n"
                    "It is checked against the release's SHA-256, only chdman.exe is taken out of it "
                    "(nothing is run), and your current chdman is backed up in a 'chdman_old' folder.\n\nContinue?"):
            self.upd_btn.config(state="disabled")
            threading.Thread(target=self.install_update, args=(info,), daemon=True).start()

    def install_update(self, info):
        tmp = Path(tempfile.mkdtemp(prefix="chdman_update_"))
        try:
            name, url, size = info["exe"]
            sums = http_get(info["sums"]).read().decode("utf-8", "replace")
            want = None
            for line in sums.splitlines():
                m = re.match(r"([0-9a-fA-F]{64})\s+\*?(.+)$", line.strip())
                if m and m.group(2).strip() == name:
                    want = m.group(1).lower()
            if not want:
                raise RuntimeError(f"no checksum is published for {name}")
            dest, h, got = tmp / name, hashlib.sha256(), 0
            with http_get(url, 60) as r, open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    self.post("upd_progress", 100 * got / max(size, 1),
                              f"Downloading {name}: {fmt_size(got)} / {fmt_size(size)}")
            if h.hexdigest() != want:
                raise RuntimeError("checksum mismatch - the download was discarded")
            self.post("upd_progress", 100, "Checksum OK. Extracting chdman.exe…")
            out = tmp / "x"
            out.mkdir()
            rc, text = run_capture([find_7z(), "e", str(dest), f"-o{out}", "chdman.exe", "-y"], timeout=900)
            newexe = out / "chdman.exe"
            if rc != 0 or not newexe.is_file():
                raise RuntimeError("could not extract chdman.exe: " + text[-200:])
            _, t2 = run_capture([str(newexe)])
            mv = re.search(r"manager ([\d.]+)", t2)
            if not mv or ver_to_int(mv.group(1)) != info["num"]:
                raise RuntimeError("the downloaded chdman reports an unexpected version")
            target = Path(CHDMAN) if CHDMAN else HERE / "chdman.exe"
            backup = ""
            if target.exists():
                bdir = target.parent / "chdman_old"
                bdir.mkdir(exist_ok=True)
                cand = bdir / f"chdman_{self.chd_version or 'old'}.exe"
                n = 1
                while cand.exists():
                    cand = bdir / f"chdman_{self.chd_version or 'old'}_{n}.exe"
                    n += 1
                shutil.copy2(target, cand)
                backup = str(cand)
            staged = target.with_name("chdman.new.exe")
            shutil.copy2(newexe, staged)
            os.replace(staged, target)
            self.post("upd_done", True, str(target), mv.group(1), backup)
        except Exception as e:  # noqa: BLE001
            self.post("upd_done", False, str(e), "", "")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def on_update_done(self, ok, a, b, c):
        global CHDMAN
        self.upd_btn.config(state="normal")
        self.cur_bar["value"] = 0
        if not ok:
            self.status.config(text="chdman update failed - your current chdman was not changed.")
            self.log("chdman update failed: " + a, "err")
            if not self.silent:
                messagebox.showerror(APP_NAME, "chdman update failed:\n" + a + "\n\nYour current chdman was not changed.")
            return
        CHDMAN = a
        self.chd_path_var.set(a)
        self.chd_version, self.has_dvd = chdman_info()
        self.set_footer("chdman", "updated", revert_after=8000)
        self.status.config(text=f"chdman updated to {self.chd_version}.")
        self.log(f"chdman updated to {self.chd_version}: {a}", "ok")
        if c:
            self.log(f"Previous version backed up to {c}", "info")

    def report_run(self):
        """After a run: every item that did not become what was asked gets named, with the reason, in one window."""
        jobs = self.run_jobs
        rows = [(j.path.name, plain_reason(j)) for j in jobs if not j.status.startswith(("Done", "Stopped"))]
        if not rows:
            return
        n, total = len(rows), len(jobs)
        self.report_problems(
            f"{n} of {total} item{'s' if total != 1 else ''} {'was' if n == 1 else 'were'} not converted:", rows,
            "Your original files were not changed or deleted for these. The log has the full details.")

    # ------------------------------------------------------------------ UI pump
    def pump(self):
        try:
            while True:
                ev = self.q.get_nowait()
                kind = ev[0]
                if kind == "job":
                    self.refresh_job(ev[1])
                elif kind == "log":
                    self.log(ev[1], ev[2])
                elif kind == "progress":
                    self.cur_bar["value"] = ev[1]
                elif kind == "overall":
                    self.all_bar["value"] = 100 * ev[1] / max(1, ev[2])
                elif kind == "label":
                    self.status.config(text=ev[1])
                    if self.echo:
                        self.echo(ev[1], "label")
                elif kind == "info":
                    self.show_info(ev[1], ev[2])
                elif kind == "upd_info":
                    self.on_update_info(ev[1], ev[2], ev[3])
                elif kind == "upd_progress":
                    self.cur_bar["value"] = ev[1]
                    self.status.config(text=ev[2])
                elif kind == "upd_done":
                    self.on_update_done(ev[1], ev[2], ev[3], ev[4])
                elif kind == "app_upd_info":
                    self.on_app_update_info(ev[1], ev[2], ev[3])
                elif kind == "app_upd_ready":
                    self.on_app_update_ready(ev[1])
                elif kind == "app_upd_restart":
                    self.on_app_update_restart()
                elif kind == "app_upd_done":
                    self.on_app_update_done(ev[1], ev[2])
                elif kind == "finish":
                    self.running = False
                    if self.v["auto_log"].get():
                        try:
                            with open(HERE / "chd_manager_log.txt", "a", encoding="utf-8") as lf:
                                lf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n"
                                         + self.logbox.get(getattr(self, "log_mark", "1.0"), "end-1c") + "\n")
                        except (OSError, tk.TclError):
                            pass
                    for j in self.jobs:
                        if j.status.startswith("Done"):
                            self.drop_arc(j)
                    self.root.after_idle(self.autofit_columns)
                    self.start_btn.config(state="normal")
                    self.stop_btn.config(state="disabled")
                    self.status.config(text=ev[1])
                    self.log("\n" + ev[1], "ok" if ev[2] else "warn")
                    self.set_state("done" if ev[2] else "fail")
                    if not ev[2]:
                        self.show_log(True)
                    self.cur_bar["value"] = 0
                    self.proc = None
                    if not self.silent:
                        try:
                            import winsound
                            winsound.MessageBeep()
                        except Exception:  # noqa: BLE001
                            pass
                    self.report_run()
        except queue.Empty:
            pass
        try:
            self.root.after(80, self.pump)
        except tk.TclError:
            pass


def smoke_test(outfile):
    """Hidden self-check used to verify the packaged exe: convert + verify a tiny disc, write a JSON report."""
    report = {"frozen": FROZEN, "here": str(HERE), "app_version": APP_VER}
    tmp = Path(tempfile.mkdtemp(prefix="chdm_smoke_"))
    try:
        root = tk.Tk()
        root.withdraw()
        app = App(root, silent=True)
        app.v["theme"].set("light")
        report.update(chdman=CHDMAN, chdman_version=app.chd_version, has_dvd=app.has_dvd)
        iso = tmp / "Smoke Test.iso"
        iso.write_bytes((b"CHD MANAGER SMOKE TEST " * 100)[:2048] * 600)
        app.add_paths([str(iso)])
        app.v["verify_after"].set(True)
        report["finished"] = app.run_blocking(timeout=120)
        report["status"] = app.jobs[0].status if app.jobs else "no job"
        report["chd_made"] = (tmp / "Smoke Test.chd").is_file()
        root.destroy()
    except Exception:  # noqa: BLE001
        report["error"] = traceback.format_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    Path(outfile).write_text(json.dumps(report, indent=2), "utf-8")


CLI_EPILOG = """\
examples:
  CHD Manager cli game.cue --to chd -o D:\\chd
  CHD Manager cli game.chd --to pbp --level 9 -o D:\\psp
  CHD Manager cli D:\\roms --to chd --exists overwrite --log convert.txt
  CHD Manager cli game.chd game2.iso --list

targets (--list shows which ones a given file can become):
""" + "\n".join(f"  {k:<11}{v}" for k, v in TARGET_LABELS.items()) + """
  chd_d2, cue_d2, ...   one disc (here the 2nd) of a multi-disc PBP

exit code: 0 = everything done or skipped, 1 = something failed, 2 = wrong command line
"""


def attach_console():
    """The packaged program is a windowed one, so it has no console of its own; use the one it was started from."""
    ok = False
    try:
        ok = sys.stdout is not None and sys.stdout.writable()
    except (AttributeError, ValueError, OSError):
        pass
    if not ok and os.name == "nt":
        try:
            ctypes.windll.kernel32.AttachConsole(-1)
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = sys.stdout
        except Exception:  # noqa: BLE001
            pass
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def cli_main(argv):
    """chdm cli ...: the same conversions as the window, without the window."""
    import argparse
    attach_console()
    ap = argparse.ArgumentParser(prog="CHD Manager cli", formatter_class=argparse.RawDescriptionHelpFormatter, epilog=CLI_EPILOG,
                                 description=f"{APP_NAME} {APP_VER}: convert disc images from the command line. "
                                             "Takes files, folders and archives; what each one becomes is chosen with --to.")
    ap.add_argument("inputs", nargs="*", help="files, folders or archives")
    ap.add_argument("--to", metavar="TARGET", help="what to make (see below); default: each file's usual action")
    ap.add_argument("--list", action="store_true", help="only show what each input can become")
    ap.add_argument("-o", "--output", metavar="DIR", help="output folder (default: next to each source file)")
    ap.add_argument("--flat", action="store_true", help="with -o: don't recreate the input folders' structure")
    ap.add_argument("--name", metavar="PATTERN", help="output file name, e.g. \"%%GAMEID%% - %%TITLE%%\" (%%FILENAME%% %%GAMEID%% "
                    "%%TITLE%% %%MAINTITLE%% %%MAINGAMEID%% %%REGION%%)")
    ap.add_argument("--exists", choices=("skip", "overwrite", "rename"), default="skip", help="if the output exists (default: skip)")
    ap.add_argument("--no-verify", action="store_true", help="don't verify each result")
    ap.add_argument("--compression", choices=("default", "none", "fast", "smallest"), default="default", help="CHD compression")
    ap.add_argument("--codecs", metavar="LIST", help="custom CHD codecs, e.g. cdlz,cdzl (instead of --compression)")
    ap.add_argument("--hunk", metavar="BYTES", help="CHD hunk size")
    ap.add_argument("--threads", type=int, default=0, metavar="N", help="CHD processors (default: all)")
    ap.add_argument("--level", type=int, choices=range(10), metavar="0-9", help="PBP compression (default 6)")
    ap.add_argument("--wii-key", metavar="HEX", help="your Wii common key (32 hex characters); or put it in the keys folder")
    ap.add_argument("--rpcs3", metavar="EXE", help="your rpcs3.exe, used to unpack PS3 .pkg packages")
    ap.add_argument("--pkg-key", metavar="HEX", help="your own PS3 package key (32 hex characters), used to unpack .pkg packages when there is no RPCS3")
    ap.add_argument("--switch-keys", metavar="FILE", help="your own prod.keys, used to turn Switch NSP files that have tickets into XCI")
    ap.add_argument("--dolphintool", metavar="EXE", help="your DolphinTool.exe, used for Wii RVZ/WIA when there is no key")
    ap.add_argument("--rvz-method", choices=("zstd", "lzma", "lzma2", "bzip2", "none"), help="RVZ/WIA compression (default zstd)")
    ap.add_argument("--rvz-level", type=int, choices=range(1, 10), metavar="1-9", help="RVZ/WIA compression level (default 5)")
    ap.add_argument("--rvz-block", type=int, metavar="KIB", help="RVZ block size in KiB, 32 to 2048 (default 128)")
    ap.add_argument("--no-m3u", action="store_true", help="don't write .m3u playlists for multi-disc games")
    ap.add_argument("--no-space-check", action="store_true", help="don't check for enough free disk space before each conversion")
    ap.add_argument("--ps3-split", action="store_true", help="PS3: split ISOs of more than 4 GB into .iso.0, .iso.1 ... parts (for FAT32 drives)")
    ap.add_argument("--ps3-update", action="store_true", help="PS3: keep the PS3_UPDATE folder when making an ISO")
    ap.add_argument("--no-scrub", action="store_true", help="WBFS: keep all data, don't leave out what nothing uses")
    ap.add_argument("--art", metavar="DIR", help="your PBP artwork folder (one subfolder per game)")
    ap.add_argument("--no-cards", action="store_true", help="don't draw title cards for PBPs without artwork")
    ap.add_argument("--base-pbp", metavar="FILE", help="base PBP for a real PSP/Vita")
    ap.add_argument("--chdman", metavar="EXE", help="use this chdman")
    ap.add_argument("--log", metavar="FILE", help="also write the full log to this file")
    ap.add_argument("-v", "--verbose", action="store_true", help="show the full log, including chdman's output")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {APP_VER}")
    a = ap.parse_args(argv)
    if not a.inputs:
        ap.print_usage()
        print("nothing to do: give at least one file, folder or archive (or --help)")
        return 2
    if a.to and a.to.partition("_d")[0] not in TARGET_LABELS:
        print(f"unknown target '{a.to}'. Choose one of: {', '.join(TARGET_LABELS)}")
        return 2
    logf = None
    if a.log:
        try:
            logf = open(a.log, "a", encoding="utf-8")
            logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {' '.join(argv)} =====\n")
        except OSError as e:
            print(f"can't write the log file: {e}")
            return 2

    def say(text):
        print(text, flush=True)
        if logf:
            logf.write(text + "\n")

    root = tk.Tk()
    root.withdraw()
    app = App(root, silent=True)
    app.no_save = True                                  # a command-line run never changes the window's saved settings
    code = 1
    try:
        for k, d in DEFAULTS.items():                    # only the machine's own paths come from the saved settings
            if k in app.v and k not in ("base_pbp", "art_dir", "dolphintool", "rpcs3", "pkg_key", "switch_keys", "wii_common_key", "wii_korean_key"):
                app.v[k].set(d)
        v = app.v
        if a.output:
            v["out_mode"].set("custom")
            v["out_dir"].set(a.output)
            v["keep_structure"].set(not a.flat)
        if a.name:
            v["name_format"].set(a.name)
        v["exists"].set(a.exists.capitalize())
        v["verify_after"].set(not a.no_verify)
        if a.codecs:
            v["compression"].set("Custom")
            v["custom_comp"].set(a.codecs)
        else:
            v["compression"].set(a.compression.capitalize())
        v["hunk"].set(a.hunk or "")
        v["threads"].set(a.threads)
        if a.level is not None:
            v["pbp_level"].set(a.level)
        if a.wii_key:
            v["wii_common_key"].set(a.wii_key)
        if a.dolphintool:
            v["dolphintool"].set(a.dolphintool)
        if a.rpcs3:
            v["rpcs3"].set(a.rpcs3)
        if a.pkg_key:
            v["pkg_key"].set(a.pkg_key)
        if a.switch_keys:
            try:
                ks = parse_switch_keys(Path(a.switch_keys).read_bytes())
            except OSError as e:
                print(f"Can't read {a.switch_keys}: {e}")
                return 1
            if not ks:
                print(f"{a.switch_keys} doesn't look like a prod.keys file")
                return 1
            v["switch_keys"].set(switch_keys_text(ks))
        if a.rvz_method:
            v["rvz_method"].set(a.rvz_method)
        if a.rvz_level:
            v["rvz_level"].set(a.rvz_level)
        if a.rvz_block:
            v["rvz_chunk"].set(a.rvz_block * 1024)
        if a.no_scrub:
            v["wbfs_scrub"].set(False)
        if a.no_m3u:
            v["make_m3u"].set(False)
        if a.no_space_check:
            v["space_check"].set(False)
        if a.ps3_split:
            v["ps3_split"].set(True)
        if a.ps3_update:
            v["ps3_update"].set(True)
        if a.art:
            v["art_dir"].set(a.art)
        if a.base_pbp:
            v["base_pbp"].set(a.base_pbp)
        if a.no_cards:
            v["title_cards"].set(False)
        if a.chdman:
            global CHDMAN
            found = find_chdman(a.chdman)
            if not found:
                say(f"chdman not found: {a.chdman}")
                return 2
            CHDMAN = found
            app.chd_version, app.has_dvd = chdman_info()

        def echo(text, tag):
            if logf:
                logf.write(text + "\n")
            if text.startswith(("Added ", "Click the Action")):
                return                                  # window hints that mean nothing here
            if a.verbose and tag != "cmd" or tag in ("label", "warn", "err"):
                print(text, flush=True)
        app.echo = echo
        app.add_paths(a.inputs)
        if not app.jobs:
            say("no usable files found in the inputs")
            return 1
        if a.list:
            for j in app.jobs:
                say(f"{j.path}\n  {j.platform or '?'} · {j.type_label()} · {fmt_size(j.size)}")
                for k, label in j.available_targets():
                    say(f"    {k:<11}{label}" + ("   (default)" if k == j.default_target() else ""))
            return 0
        todo, bad = [], 0
        for j in app.jobs:
            ok = [k for k, _ in j.available_targets()]
            if a.to and a.to not in ok:
                hint = ("; RVZ, WIA and WBFS from a Wii disc need your Wii key (--wii-key, or the keys folder) or DolphinTool.exe (--dolphintool)"
                        if j.platform == "Wii" and a.to in ("rvz", "wia", "wbfs") else "")
                say(f"FAIL {j.path.name}: can't become '{a.to}' (it can become: {', '.join(ok) or 'nothing'}){hint}")
                bad += 1
                continue
            if a.to:
                j.set_target(a.to)
            todo.append(j)
        if todo:
            app.start(todo)
            try:
                while app.running:
                    root.update()
                    time.sleep(0.02)
                root.update()
            except KeyboardInterrupt:
                app.stop()
                end = time.time() + 15
                while app.running and time.time() < end:
                    root.update()
                    time.sleep(0.02)
                say("stopped")
                return 130
        for j in todo:
            word = "OK  " if j.status.startswith("Done") else "SKIP" if j.status.startswith("Skipped") else "FAIL"
            if word == "FAIL":
                bad += 1
            say(f"{word} {j.path.name}: " + (j.result if word == "OK  " else j.status if word == "SKIP" else f"{j.status} {j.result}".strip()))
        code = 1 if bad else 0
    finally:
        try:
            app.drop_all_arcs()
            root.destroy()
        except tk.TclError:
            pass
        if logf:
            logf.close()
    return code


def main():
    if len(sys.argv) >= 4 and sys.argv[1] == "--relaunch-test":
        spawn_swap(write_swap_script(Path(sys.argv[2]), Path(sys.argv[3])))
        return
    if len(sys.argv) >= 2 and sys.argv[1].lower() in ("cli", "--cli"):
        sys.exit(cli_main(sys.argv[2:]))
    if len(sys.argv) >= 3 and sys.argv[1] == "--smoke":
        SETTINGS_FILE_OVERRIDE = Path(sys.argv[2]).with_suffix(".settings.json")
        globals()["SETTINGS_FILE"] = SETTINGS_FILE_OVERRIDE      # don't touch the real settings during the check
        smoke_test(sys.argv[2])
        return
    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    files, start, to, args, i = [], False, None, sys.argv[1:], 0
    while i < len(args):
        if args[i] == "--start":
            start = True
        elif args[i] == "--to" and i + 1 < len(args):
            to, i = args[i + 1], i + 1
        elif not args[i].startswith("--"):
            files.append(os.path.abspath(args[i]))
        i += 1
    srv = ipc_listen()                                  # the first copy owns the port; later ones hand their files to it
    if srv is None and files and ipc_send({"paths": files, "start": start, "to": to}):
        return
    root = tk.Tk()
    App(root, initial_paths=files, auto_start=start, target=to, listener=srv)
    root.mainloop()


if __name__ == "__main__":
    main()
