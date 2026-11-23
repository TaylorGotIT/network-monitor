#!/usr/bin/env python3
"""Build a Debian/Ubuntu package for the monitor (pure-Python, arch: all)."""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import re
import shutil
import tarfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "deb"
PKG = "network-monitor"
ARCH = "all"
MAINTAINER = "Taylor.Gao <TaylorGotIT@users.noreply.github.com>"
EPOCH = 1763856000  # 2026-11-23, keeps the .deb reproducible


def version() -> str:
    text = (ROOT / "netmon" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("cannot find __version__")
    return match.group(1)


def copy_file(src: Path, dst: Path, mode: int = 0o644) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    dst.chmod(mode)


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content.encode("utf-8"))
    path.chmod(mode)


def stage_tree(version: str) -> Path:
    stage = BUILD / "root"
    shutil.rmtree(stage, ignore_errors=True)
    (stage / "opt" / PKG).mkdir(parents=True)

    copy_file(ROOT / "app.py", stage / "opt" / PKG / "app.py")
    copy_file(ROOT / "requirements.txt", stage / "opt" / PKG / "requirements.txt")
    copy_file(ROOT / "README.md", stage / "opt" / PKG / "README.md")
    copy_file(ROOT / "LICENSE", stage / "opt" / PKG / "LICENSE")
    for source in sorted((ROOT / "netmon").glob("*.py")):
        copy_file(source, stage / "opt" / PKG / "netmon" / source.name)

    for source in sorted((ROOT / "static").rglob("*")):
        if source.is_file():
            copy_file(source, stage / "opt" / PKG / "static" / source.relative_to(ROOT / "static"))

    copy_file(PACKAGING / "config.default.json", stage / "usr" / "share" / PKG / "config.json")
    write_file(
        stage / "lib" / "systemd" / "system" / "network-monitor.service",
        (PACKAGING / "network-monitor.service").read_text(encoding="utf-8"),
    )
    write_file(
        stage / "usr" / "share" / "doc" / PKG / "copyright",
        (PACKAGING / "deb" / "copyright").read_text(encoding="utf-8"),
    )

    wrapper = (
        "#!/bin/sh\n"
        "exec /usr/bin/python3 /opt/network-monitor/app.py \\\n"
        "    --config /var/lib/network-monitor/config.json \\\n"
        "    --db /var/lib/network-monitor/monitor.db \\\n"
        "    --static /opt/network-monitor/static \"$@\"\n"
    )
    write_file(stage / "usr" / "bin" / "network-monitor", wrapper, 0o755)

    changelog = (
        "%s (%s) unstable; urgency=medium\n\n"
        "  * Release %s: ICMP/TCP/UDP probes, history charts, automatic MTR fault localization.\n\n"
        " -- Taylor.Gao <TaylorGotIT@users.noreply.github.com>  Mon, 23 Nov 2026 22:00:00 +0800\n"
    ) % (PKG, version, version)
    changelog_path = stage / "usr" / "share" / "doc" / PKG / "changelog.gz"
    with open(changelog_path, "wb") as raw:
        with gzip.GzipFile(filename="changelog.gz", mode="wb", fileobj=raw, mtime=EPOCH) as handle:
            handle.write(changelog.encode("utf-8"))

    return stage


def control_fields(version: str, installed_kb: int) -> str:
    return (
        "Package: %s\n"
        "Version: %s\n"
        "Architecture: %s\n"
        "Maintainer: %s\n"
        "Installed-Size: %d\n"
        "Depends: python3 (>= 3.9), python3-tornado (>= 6.1)\n"
        "Recommends: iputils-ping, mtr-tiny, traceroute\n"
        "Section: net\n"
        "Priority: optional\n"
        "Homepage: https://github.com/TaylorGotIT/network-monitor\n"
        "Description: Network quality and outage monitor\n"
        " Real-time ICMP/TCP/UDP probes with latency, loss and jitter history,\n"
        " automatic MTR/traceroute fault localization, SQLite storage and a\n"
        " dependency-free web dashboard.\n"
    ) % (PKG, version, ARCH, MAINTAINER, installed_kb)


def make_tar_gz(path: Path, entries: list[tuple[Path, str, int]]) -> None:
    """Create a deterministic tar.gz from (source, arcname, mode) entries."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for source, arcname, mode in entries:
            info = tar.gettarinfo(str(source), arcname=arcname)
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            info.mtime = EPOCH
            if info.isdir():
                info.mode = 0o755
                tar.addfile(info)
            else:
                info.mode = mode
                info.size = source.stat().st_size
                with source.open("rb") as handle:
                    tar.addfile(info, handle)
    raw = open(path, "wb")
    try:
        with gzip.GzipFile(filename=str(path), mode="wb", fileobj=raw, mtime=EPOCH) as handle:
            handle.write(buffer.getvalue())
    finally:
        raw.close()
def ar_archive(path: Path, members: list[tuple[str, bytes]]) -> None:
    output = bytearray(b"!<arch>\n")
    for name, data in members:
        size = len(data)
        header = "%-16s%-12d%-6d%-6d%-8s%-10d`\n" % (name, EPOCH, 0, 0, "100644", size)
        output.extend(header.encode("ascii"))
        output.extend(data)
        if size % 2:
            output.extend(b"\n")
    path.write_bytes(bytes(output))


def md5sums(stage: Path) -> str:
    lines = []
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            digest = hashlib.md5(path.read_bytes()).hexdigest()
            lines.append("%s  %s\n" % (digest, path.relative_to(stage).as_posix()))
    return "".join(lines)


def main() -> None:
    ver = version()
    DIST.mkdir(exist_ok=True)
    shutil.rmtree(BUILD, ignore_errors=True)
    BUILD.mkdir(parents=True)

    stage = stage_tree(ver)
    installed_kb = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file()) // 1024 + 1

    control = BUILD / "control"
    shutil.rmtree(control, ignore_errors=True)
    control.mkdir()
    write_file(control / "control", control_fields(ver, installed_kb))
    for name in ("postinst", "prerm", "postrm"):
        write_file(control / name, (PACKAGING / "deb" / name).read_text(encoding="utf-8"), 0o755)
    write_file(control / "md5sums", md5sums(stage))

    data_entries: list[tuple[Path, str, int]] = [(stage, "./", 0o755)]
    executables = {"usr/bin/network-monitor"}
    for path in sorted(stage.rglob("*")):
        relative = path.relative_to(stage).as_posix()
        arcname = "./" + relative + ("/" if path.is_dir() else "")
        mode = 0o755 if (path.is_dir() or relative in executables) else 0o644
        data_entries.append((path, arcname, mode))

    data_tar = BUILD / "data.tar.gz"
    control_tar = BUILD / "control.tar.gz"
    make_tar_gz(data_tar, data_entries)
    make_tar_gz(
        control_tar,
        [
            (control / "control", "./control", 0o644),
            (control / "postinst", "./postinst", 0o755),
            (control / "prerm", "./prerm", 0o755),
            (control / "postrm", "./postrm", 0o755),
            (control / "md5sums", "./md5sums", 0o644),
        ],
    )

    deb = DIST / ("%s_%s_%s.deb" % (PKG, ver, ARCH))
    ar_archive(
        deb,
        [
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", control_tar.read_bytes()),
            ("data.tar.gz", data_tar.read_bytes()),
        ],
    )

    # sanity checks
    assert deb.read_bytes().startswith(b"!<arch>\n")
    with tarfile.open(data_tar, "r:gz") as tar:
        names = tar.getnames()
    required = [
        "./opt/network-monitor/app.py",
        "./opt/network-monitor/static/index.html",
        "./usr/bin/network-monitor",
        "./lib/systemd/system/network-monitor.service",
        "./usr/share/network-monitor/config.json",
    ]
    missing = [name for name in required if name not in names]
    if missing:
        raise SystemExit("missing from package: %s" % missing)

    print("built: %s" % deb)
    print("size: %.1f KB, installed: %d KB, files: %d" % (deb.stat().st_size / 1024, installed_kb, len(names)))
    print("version: %s" % ver)


if __name__ == "__main__":
    main()
