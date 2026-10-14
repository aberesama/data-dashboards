"""Find or download Oracle Instant Client 19c (Basic Light).

Thick mode needs these libraries to talk to Oracle databases older than 12.1.
The app uses this module automatically. You can also run it directly:

    python oracle_client.py            download and unpack into ./oracle_client
    python oracle_client.py file.zip   unpack a zip you downloaded yourself (offline machines)
"""
import os
import platform
import shutil
import stat
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# One refresh at a time per app process, so tabs and reruns can never open extra database sessions.
REFRESH_LOCK = threading.Lock()

BASE = Path(__file__).resolve().parent / "oracle_client"
# Newest first. Oracle keeps older point releases, so fall through if one is missing.
VERSIONS = [("1929000", "19.29"), ("1928000", "19.28"), ("1927000", "19.27"), ("1926000", "19.26"),
            ("1925000", "19.25"), ("1924000", "19.24"), ("1923000", "19.23")]


def platform_names():
    """Return (url folder, file label, library file) for this machine, or raise."""
    machine = platform.machine().lower()
    is_64 = sys.maxsize > 2**32 and machine in ("amd64", "x86_64")
    system = platform.system()
    if not is_64 or system not in ("Windows", "Linux"):
        raise RuntimeError(
            f"Automatic setup supports 64 bit Windows and Linux only (this is {system} {machine}, "
            f"Python {'64' if sys.maxsize > 2**32 else '32'} bit). Install Instant Client 19c manually."
        )
    if system == "Windows":
        return "nt", "windows", "oci.dll"
    return "linux", "linux", "libclntsh.so"


def find_client(base=BASE):
    """Path of an unpacked Instant Client under base, or None."""
    lib = platform_names()[2]
    for d in sorted(Path(base).glob("instantclient_*"), reverse=True):
        if (d / lib).exists():
            return str(d)
    return None


def candidate_urls():
    folder, label, _ = platform_names()
    return [
        f"https://download.oracle.com/otn_software/{folder}/instantclient/{code}/"
        f"instantclient-basiclite-{label}.x64-{ver}.0.0.0dbru.zip"
        for code, ver in VERSIONS
    ]


def _download(url, dest, log):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(dest, "wb") as out:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        next_mark = 5 * 1024 * 1024
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            if done >= next_mark:
                log(f"  downloaded {done / 1e6:.0f} of {total / 1e6:.0f} MB" if total else f"  downloaded {done / 1e6:.0f} MB")
                next_mark += 10 * 1024 * 1024


def _safe_extract(zf, base):
    root = Path(base).resolve()
    for info in zf.infolist():
        target = root / info.filename
        if not target.resolve().is_relative_to(root):
            raise RuntimeError(f"Unsafe path in zip: {info.filename}")
        if stat.S_ISLNK(info.external_attr >> 16):  # Linux zips store some libraries as symlinks
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            os.symlink(zf.read(info).decode(), target)
        else:
            zf.extract(info, root)
            mode = info.external_attr >> 16
            if mode and not info.is_dir():
                target.chmod(mode & 0o777)


def install(base=BASE, zip_path=None, log=print):
    """Download (or unpack zip_path) into base. Returns the Instant Client folder."""
    existing = find_client(base)
    if existing:
        log(f"Already installed: {existing}")
        return existing
    Path(base).mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        if zip_path:
            archive = Path(zip_path)
        else:
            tmp = Path(tempfile.mkdtemp(dir=base))
            archive = tmp / "instantclient.zip"
            last_error = None
            for url in candidate_urls():
                log(f"Downloading {url}")
                try:
                    _download(url, archive, log)
                    last_error = None
                    break
                except urllib.error.HTTPError as e:
                    last_error = f"HTTP {e.code}"
                    if e.code != 404:  # 404 just means that point release is gone, try the next
                        break
                except Exception as e:
                    last_error = str(e)
                    break
            if last_error:
                raise RuntimeError(
                    f"Download failed ({last_error}). If this laptop cannot reach download.oracle.com, "
                    "download the Instant Client 19c Basic Light zip elsewhere and run: "
                    "python oracle_client.py path\\to\\file.zip"
                )
        log("Unpacking...")
        with zipfile.ZipFile(archive) as zf:
            _safe_extract(zf, base)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    found = find_client(base)
    if not found:
        raise RuntimeError("Unpacked the zip but found no Oracle client library in it. Wrong file or wrong platform?")
    log(f"Installed: {found}")
    return found


if __name__ == "__main__":
    try:
        install(zip_path=sys.argv[1] if len(sys.argv) > 1 else None)
    except Exception as exc:
        print(f"Setup failed: {exc}")
        sys.exit(1)
