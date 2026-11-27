"""Verify and atomically replace a stored Minekuai panel ZIP."""

import hashlib
import logging
import os
import re
import shutil
from pathlib import PurePosixPath
from zipfile import BadZipFile, ZipFile

from minekuai import APIError

LOG = logging.getLogger("minekuai-cold")
ARCHIVE = re.compile(r"archive-\d{4}-\d{2}-\d{2}T\d{6}[+-]\d{4}\.zip")
RESERVE_BYTES = 1024**3


def archive_name(name):
    return isinstance(name, str) and ARCHIVE.fullmatch(name) is not None


def verify(path, size, roots):
    if path.stat().st_size != size:
        raise APIError("ZIP 大小与麦块报告不一致")
    try:
        with ZipFile(path) as archive:
            included = set()
            paths = set()
            for entry in archive.infolist():
                raw = entry.filename.rstrip("/")
                key = PurePosixPath(raw)
                if (not raw or raw.startswith("/") or "\\" in raw or ".." in key.parts
                        or not key.parts or key in paths):
                    raise APIError("ZIP 包含无效或重复路径")
                paths.add(key)
                included.add(key.parts[0])
            if set(roots) != included:
                raise APIError("ZIP 顶层文件与提交给麦块的整服清单不一致")
            if archive.testzip() is not None:
                raise APIError("ZIP 有文件未通过完整性校验")
    except (BadZipFile, OSError, RuntimeError, NotImplementedError):
        raise APIError("ZIP 无法完整读取") from None
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
        os.fsync(stream.fileno())
    return checksum


def available(directory, size):
    if size <= 0 or shutil.disk_usage(directory).free < size + RESERVE_BYTES:
        raise APIError("VPS 剩余空间不足以接收新 ZIP 并保留现有备份")


def publish(directory, temporary, filename):
    """Only called with a fully verified staging file in the same directory."""
    if not archive_name(filename) or temporary.parent != directory:
        raise ValueError("无效的压缩包路径")
    final = directory / filename
    temporary.replace(final)
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return final


def prune_previous(directory, final):
    for entry in directory.iterdir():
        if entry != final and archive_name(entry.name) and entry.is_file() and not entry.is_symlink():
            entry.unlink()


def stored_matches(path, record):
    if (not path.is_file() or path.is_symlink() or path.stat().st_size != record.get("size")
            or not isinstance(record.get("sha256"), str)):
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == record["sha256"]
