#!/usr/bin/env python3
"""Private, resumable file hub. Python 3.10+, standard library only.

For internet access use start_public.py or an HTTPS reverse proxy.
All file operations require the shared access code.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import math
import os
import re
import secrets
import shutil
import socket
import stat
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import formatdate, parsedate_to_datetime
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO

APP_NAME = "File Hub"
APP_DIR = Path(__file__).resolve().parent
DEFAULT_PORT = 8765
DEFAULT_CLEANUP_TIME = "08:00"
CHUNK_SIZE = 8 * 1024 * 1024
DEFAULT_MAX_FILE_SIZE = 2 * 1024**3
DEFAULT_STORAGE_LIMIT = 20 * 1024**3
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
CONTROL_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]+')


class HubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def now() -> float:
    return time.time()


def clean_name(name: str, limit: int = 180) -> str:
    return CONTROL_RE.sub("_", str(name)).strip(" .")[:limit] or "file"


def is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def regular_file(path: Path) -> bool:
    try:
        return not is_link(path) and stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def open_regular(path: Path, writable: bool = False) -> BinaryIO:
    """Refuse links, directories and a file replaced between lstat and open."""
    before = path.lstat()
    if is_link(path) or not stat.S_ISREG(before.st_mode):
        raise OSError("unsafe file")
    flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        after = os.fstat(descriptor)
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise OSError("file changed while opening")
        return os.fdopen(descriptor, "r+b" if writable else "rb")
    except Exception:
        os.close(descriptor)
        raise


def get_lan_ip() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def parse_daily_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("time must use HH:MM, for example 08:00")
    return int(match.group(1)), int(match.group(2))


def seconds_until(hour: int, minute: int) -> float:
    current = datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


class DataDirectoryLock:
    """Keep a second CLI process from clearing active uploads on startup."""
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().absolute()
        self.source: BinaryIO | None = None

    def __enter__(self) -> DataDirectoryLock:
        if is_link(self.root):
            raise ValueError("data directory cannot be a symbolic link")
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / ".server.lock"
        if path.exists() or is_link(path):
            self.source = open_regular(path, writable=True)
        else:
            try:
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
                self.source = os.fdopen(descriptor, "r+b")
            except FileExistsError:
                self.source = open_regular(path, writable=True)
        try:
            if os.fstat(self.source.fileno()).st_size == 0:
                self.source.write(b"0")
                self.source.flush()
            self.source.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.source.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.source.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.source.close()
            self.source = None
            raise ValueError("this data directory is already in use by another File Hub process") from exc
        return self

    def __exit__(self, *args: Any) -> None:
        if self.source is not None:
            # Closing releases the OS lock, including on process termination.
            # Keep the file itself so subsequent processes lock the same inode.
            self.source.close()
            self.source = None


@dataclass
class Upload:
    id: str
    path: Path
    name: str
    size: int
    owner: str
    generation: int
    offset: int = 0
    touched: float = field(default_factory=time.monotonic)
    hasher: Any = field(default_factory=hashlib.sha256)
    lock: Any = field(default_factory=threading.Lock)


class Store:
    def __init__(self, root: Path, *, max_file_size: int = DEFAULT_MAX_FILE_SIZE,
                 storage_limit: int = DEFAULT_STORAGE_LIMIT, upload_ttl: int = 7200) -> None:
        root = root.expanduser().absolute()
        if is_link(root):
            raise ValueError("data directory cannot be a symbolic link")
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.files_dir = self.root / "files"
        self.uploads_dir = self.root / ".uploads"
        self.meta_path = self.root / "metadata.json"
        self.deletions_path = self.root / ".deletions.json"
        self.max_file_size, self.storage_limit, self.upload_ttl = max_file_size, storage_limit, upload_ttl
        if max_file_size < 0 or storage_limit <= 0 or max_file_size > storage_limit or upload_ttl <= 0:
            raise ValueError("invalid file/storage limits or upload expiry")
        for path in (self.files_dir, self.uploads_dir):
            if is_link(path):
                raise ValueError("storage subdirectory cannot be a symbolic link")
            path.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.uploads: dict[str, Upload] = {}
        self.downloads: dict[str, int] = {}
        self.pending_deletes: set[str] = set()
        self.generation = 0
        self.meta: dict[str, dict[str, Any]] = self._load_meta()
        if self.deletions_path.exists() or is_link(self.deletions_path):
            try:
                with open_regular(self.deletions_path) as source:
                    deleted = json.load(source)
                if not isinstance(deleted, list) or any(not isinstance(value, str) or not ID_RE.fullmatch(value) for value in deleted):
                    raise ValueError("invalid deletion journal")
            except (OSError, ValueError) as exc:
                raise ValueError("deletion journal cannot be read; restore it from a backup") from exc
            self.pending_deletes = set(deleted)
            # The journal is written before the index changes. Replay committed
            # deletion intent even after a crash between those two atomic writes.
            for file_id in self.pending_deletes:
                self.meta.pop(file_id, None)
            if self.pending_deletes:
                self._save_meta()
            for file_id in list(self.pending_deletes):
                self._remove_pending(file_id)
        # Uploads survive a network failure, but not a server restart.
        for path in self.uploads_dir.glob("*.part"):
            if re.fullmatch(r"[0-9a-f]{32}\.part", path.name) and regular_file(path):
                path.unlink()

    def _check_dirs(self) -> None:
        for path in (self.root, self.files_dir, self.uploads_dir):
            if is_link(path) or not path.is_dir():
                raise HubError(500, "存储目录不可用，请联系管理员。")

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        if not self.meta_path.exists() and not is_link(self.meta_path):
            return {}
        if not regular_file(self.meta_path):
            raise ValueError("metadata must be a regular file")
        try:
            with open_regular(self.meta_path) as source:
                data = json.load(source)
        except (OSError, ValueError) as exc:
            # Never silently overwrite damaged metadata or lose the file index.
            raise ValueError("metadata.json cannot be read; restore it from a backup") from exc
        if not isinstance(data, dict):
            raise ValueError("metadata.json must contain an object")
        valid = {}
        for file_id, item in data.items():
            if not isinstance(file_id, str) or not ID_RE.fullmatch(file_id) or not isinstance(item, dict):
                continue
            path = self.files_dir / file_id
            if not regular_file(path):
                continue
            created = item.get("created_at", now())
            valid[file_id] = {
                "original_name": clean_name(item.get("original_name", "file")),
                "owner": clean_name(item.get("owner", "匿名设备"), 80),
                "created_at": created if isinstance(created, (int, float)) and math.isfinite(created) else now(),
                "size": path.stat().st_size,
                "sha256": item.get("sha256") if re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256", ""))) else None,
            }
        return valid

    def _save_meta(self) -> None:
        self._check_dirs()
        temporary = self.root / (".metadata-" + secrets.token_hex(12) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(self.meta, output, ensure_ascii=False, indent=2, allow_nan=False)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.meta_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _save_pending(self) -> None:
        self._check_dirs()
        temporary = self.root / (".deletions-" + secrets.token_hex(12) + ".tmp")
        try:
            with temporary.open("x", encoding="utf-8") as output:
                json.dump(sorted(self.pending_deletes), output)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.deletions_path)
        finally:
            temporary.unlink(missing_ok=True)

    def used_bytes(self) -> int:
        with self.lock:
            self._check_dirs()
            # Include orphan files and files waiting for active downloads to finish.
            return sum(path.stat().st_size for path in self.files_dir.iterdir() if regular_file(path))

    def list_files(self) -> list[dict[str, Any]]:
        with self.lock:
            self._check_dirs()
            result = []
            for file_id, item in self.meta.items():
                path = self.files_dir / file_id
                if regular_file(path):
                    result.append({"id": file_id, **item, "size": path.stat().st_size})
            return sorted(result, key=lambda item: item["created_at"], reverse=True)

    def new_upload(self, name: Any, size: Any, owner: Any) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip() or not isinstance(owner, str):
            raise HubError(400, "文件名和设备名称格式不正确。")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise HubError(400, "文件大小必须是非负整数。")
        if len(name) > 2048 or len(owner) > 256:
            raise HubError(400, "文件名或设备名称过长。")
        if size > self.max_file_size:
            raise HubError(413, "文件超过单个文件大小限制。")
        with self.lock:
            self._check_dirs()
            if len(self.uploads) >= 128 or len(self.meta) + len(self.uploads) >= 10000:
                raise HubError(429, "文件或传输任务过多，请先删除一些文件。")
            reserved = sum(upload.size for upload in self.uploads.values())
            if self.used_bytes() + reserved + size > self.storage_limit:
                raise HubError(413, "剩余存储空间不足，请删除文件或取消未完成的上传。")
            if size + reserved + 16 * 1024**2 > shutil.disk_usage(self.root).free:
                raise HubError(413, "服务器磁盘剩余空间不足。")
            file_id = secrets.token_hex(16)
            path = self.uploads_dir / (file_id + ".part")
            with path.open("xb"):
                pass
            self.uploads[file_id] = Upload(file_id, path, clean_name(name), size, clean_name(owner or "匿名设备", 80), self.generation)
            return {"id": file_id, "offset": 0, "size": size, "chunk_size": CHUNK_SIZE}

    def get_upload(self, file_id: str) -> Upload:
        if not ID_RE.fullmatch(file_id):
            raise HubError(404, "上传任务不存在或已过期。")
        with self.lock:
            self._check_dirs()
            upload = self.uploads.get(file_id)
            if upload is None or upload.generation != self.generation:
                raise HubError(404, "上传任务不存在或已过期。")
            return upload

    def _active_upload(self, upload: Upload) -> None:
        with self.lock:
            self._check_dirs()
            if self.uploads.get(upload.id) is not upload or upload.generation != self.generation:
                raise HubError(410, "上传任务已取消或已被自动清理。")

    def upload_status(self, file_id: str) -> dict[str, Any]:
        with self.lock:
            item = self.meta.get(file_id)
            if item and regular_file(self.files_dir / file_id):
                return {"id": file_id, "offset": item["size"], "size": item["size"],
                        "completed": True, "file": {"id": file_id, **item}}
        upload = self.get_upload(file_id)
        with upload.lock:
            self._active_upload(upload)
            upload.touched = time.monotonic()
            return {"id": upload.id, "offset": upload.offset, "size": upload.size}

    def write_chunk(self, file_id: str, offset: int, length: int, source: BinaryIO) -> dict[str, Any]:
        upload = self.get_upload(file_id)
        with upload.lock:
            self._active_upload(upload)
            if offset != upload.offset:
                raise HubError(409, "上传进度已变化，请查询进度后重试。")
            if length <= 0 or length > CHUNK_SIZE or upload.offset + length > upload.size:
                raise HubError(413, "分块大小不正确或超过文件剩余大小。")
            hasher = upload.hasher.copy()
            with open_regular(upload.path, writable=True) as output:
                output.seek(offset)
                try:
                    remaining = length
                    while remaining:
                        chunk = source.read(min(1024**2, remaining))
                        if not chunk:
                            raise HubError(400, "上传中断，可重新连接后继续。")
                        output.write(chunk)
                        hasher.update(chunk)
                        remaining -= len(chunk)
                    output.flush()
                    self._active_upload(upload)
                except Exception:
                    output.truncate(offset)
                    raise
            upload.offset += length
            upload.hasher = hasher
            upload.touched = time.monotonic()
            return {"id": file_id, "offset": upload.offset}

    def complete_upload(self, file_id: str) -> dict[str, Any]:
        # Repeating completion is safe if the reply was lost in transit.
        with self.lock:
            if file_id in self.meta:
                return {"id": file_id, **self.meta[file_id]}
        upload = self.get_upload(file_id)
        with upload.lock, self.lock:
            self._active_upload(upload)
            if upload.offset != upload.size:
                raise HubError(409, "文件尚未上传完整，请继续上传。")
            if not regular_file(upload.path) or upload.path.stat().st_size != upload.size:
                raise HubError(409, "临时文件校验失败，请重新上传。")
            destination = self.files_dir / file_id
            if destination.exists() or is_link(destination):
                raise HubError(409, "文件标识冲突，请重新上传。")
            item = {"original_name": upload.name, "owner": upload.owner, "created_at": now(),
                    "size": upload.size, "sha256": upload.hasher.hexdigest()}
            upload.path.replace(destination)
            self.meta[file_id] = item
            try:
                self._save_meta()
            except Exception:
                self.meta.pop(file_id, None)
                destination.replace(upload.path)
                raise
            self.uploads.pop(file_id)
            return {"id": file_id, **item}

    def cancel_upload(self, file_id: str) -> bool:
        try:
            upload = self.get_upload(file_id)
        except HubError as exc:
            if exc.status == 404:
                return False
            raise
        with upload.lock, self.lock:
            if self.uploads.get(file_id) is not upload:
                return False
            if regular_file(upload.path):
                upload.path.unlink()
            self.uploads.pop(file_id, None)
            return True

    def open_download(self, file_id: str) -> tuple[BinaryIO, dict[str, Any]]:
        with self.lock:
            self._check_dirs()
            if not ID_RE.fullmatch(file_id) or file_id not in self.meta:
                raise HubError(404, "文件不存在或已被清理。")
            try:
                source = open_regular(self.files_dir / file_id)
            except OSError:
                raise HubError(404, "文件不存在或不可读取。") from None
            self.downloads[file_id] = self.downloads.get(file_id, 0) + 1
            return source, {"id": file_id, **self.meta[file_id]}

    def close_download(self, file_id: str, source: BinaryIO) -> None:
        source.close()
        with self.lock:
            count = self.downloads.get(file_id, 1) - 1
            if count:
                self.downloads[file_id] = count
            else:
                self.downloads.pop(file_id, None)
                self._remove_pending(file_id)

    def _remove_pending(self, file_id: str) -> None:
        if file_id not in self.pending_deletes or self.downloads.get(file_id):
            return
        try:
            self._check_dirs()
            path = self.files_dir / file_id
            if regular_file(path):
                path.unlink()
            self.pending_deletes.discard(file_id)
            try:
                self._save_pending()
            except OSError:
                self.pending_deletes.add(file_id)
                raise
        except (OSError, HubError):
            pass  # Windows may hold a handle; the janitor retries later.

    def delete(self, file_id: str) -> bool:
        with self.lock:
            self._check_dirs()
            if not ID_RE.fullmatch(file_id) or file_id not in self.meta:
                return False
            self.pending_deletes.add(file_id)
            self._save_pending()
            item = self.meta.pop(file_id)
            try:
                self._save_meta()
            except Exception:
                self.meta[file_id] = item
                raise
            self._remove_pending(file_id)
            return True

    def clear_all(self) -> int:
        with self.lock:
            self._check_dirs()
            count, previous = len(self.meta), self.meta
            self.pending_deletes.update(previous)
            self._save_pending()
            self.meta = {}
            try:
                self._save_meta()
            except Exception:
                self.meta = previous
                raise
            self.generation += 1
            for file_id in list(self.pending_deletes):
                self._remove_pending(file_id)
        self.expire_uploads()
        return count

    def expire_uploads(self) -> None:
        with self.lock:
            candidates = list(self.uploads.values())
            for file_id in list(self.pending_deletes):
                self._remove_pending(file_id)
        for upload in candidates:
            if not upload.lock.acquire(blocking=False):
                continue
            try:
                with self.lock:
                    if upload.generation != self.generation or time.monotonic() - upload.touched > self.upload_ttl:
                        if regular_file(upload.path):
                            upload.path.unlink()
                        self.uploads.pop(upload.id, None)
            except OSError:
                pass
            finally:
                upload.lock.release()


class HubServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64

    def __init__(self, address: tuple[str, int], store: Store, access_code: str, *,
                 trust_proxy: bool = False, cleanup_time: str | None = DEFAULT_CLEANUP_TIME,
                 public_url: str | None = None, web_dir: Path | None = None) -> None:
        self.store, self.access_code = store, access_code
        self.trust_proxy, self.cleanup_time, self.public_url = trust_proxy, cleanup_time, public_url
        self.web_dir = web_dir or APP_DIR / "web"
        self.sessions: dict[str, float] = {}
        self.failed_logins: dict[str, deque[float]] = {}
        self.auth_lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(address, Handler)

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self.slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\nRetry-After: 5\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version, sys_version = "FileHub/2", ""
    server: HubServer

    @property
    def store(self) -> Store:
        return self.server.store

    def setup(self) -> None:
        self.request.settimeout(45)
        super().setup()

    def log_message(self, fmt: str, *args: Any) -> None:
        # Request targets/headers can contain secrets; never log them.
        if len(args) >= 2 and str(args[1]).isdigit():
            print(f"{self.client_address[0]} {getattr(self, 'command', '?')} {args[1]}", flush=True)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        super().end_headers()

    def send_json(self, payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _secure_request(self) -> bool:
        if not self.server.trust_proxy:
            return False
        try:
            trusted = ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            trusted = False
        return trusted and self.headers.get("X-Forwarded-Proto", "").lower() == "https"

    def _origin_check(self) -> None:
        if self.headers.get("Sec-Fetch-Site", "") == "cross-site":
            raise HubError(403, "请从本站页面执行此操作。")
        origin = self.headers.get("Origin")
        if origin is None:
            return  # Non-browser clients still need a valid session cookie.
        expected = ("https" if self._secure_request() else "http") + "://" + self.headers.get("Host", "")
        if origin != expected:
            raise HubError(403, "请求来源不匹配，请刷新本站页面。")

    def _token(self) -> str:
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            value = cookie.get("filehub_session")
            return value.value if value else ""
        except CookieError:
            return ""

    def authenticated(self) -> bool:
        token = self._token()
        with self.server.auth_lock:
            return bool(token and self.server.sessions.get(token, 0) > time.monotonic())

    def _require_auth(self) -> None:
        if not self.authenticated():
            raise HubError(401, "请输入访问码以解锁文件空间。")

    def _length(self, maximum: int) -> int:
        if self.headers.get("Transfer-Encoding") is not None:
            raise HubError(400, "不支持此请求编码。")
        values = self.headers.get_all("Content-Length", [])
        if not values:
            raise HubError(411, "请求缺少内容长度。")
        if len(values) != 1 or not re.fullmatch(r"\d{1,20}", values[0]):
            raise HubError(400, "请求内容长度不正确。")
        length = int(values[0])
        if length > maximum:
            raise HubError(413, "请求内容过大。")
        return length

    def _json(self) -> dict[str, Any]:
        if self.headers.get_content_type() != "application/json":
            raise HubError(415, "请使用 JSON 格式提交请求。")
        length = self._length(16 * 1024)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HubError(400, "请求内容不完整。")
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise HubError(400, "JSON 格式不正确。") from None
        if not isinstance(result, dict):
            raise HubError(400, "请求必须是 JSON 对象。")
        return result

    def _cookie(self, token: str, age: int = 86400) -> str:
        return f"filehub_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={age}" + ("; Secure" if self._secure_request() else "")

    def _login(self) -> None:
        payload = self._json()
        code = payload.get("code", "")
        if not isinstance(code, str) or len(code) > 1024:
            raise HubError(400, "访问码格式不正确。")
        address = self.client_address[0]  # Never trust client-supplied X-Forwarded-For.
        current = time.monotonic()
        with self.server.auth_lock:
            for key in list(self.server.failed_logins):
                entries = self.server.failed_logins[key]
                while entries and current - entries[0] >= 60:
                    entries.popleft()
                if not entries:
                    del self.server.failed_logins[key]
            failures = self.server.failed_logins.get(address, deque())
            valid_code = hmac.compare_digest(code.encode("utf-8"), self.server.access_code.encode("utf-8"))
            if not valid_code:
                # A tunnel shares one loopback peer across all its users. An
                # attacker must not lock out users who have the correct code.
                if len(failures) >= 8 or (address not in self.server.failed_logins and len(self.server.failed_logins) >= 4096):
                    self.send_json({"error": "尝试次数过多，请一分钟后重试。"}, 429, {"Retry-After": "60"})
                    return
                failures.append(current)
                self.server.failed_logins[address] = failures
                raise HubError(401, "访问码不正确，请重试。")
            self.server.failed_logins.pop(address, None)
            self.server.sessions = {key: expiry for key, expiry in self.server.sessions.items() if expiry > current}
            if len(self.server.sessions) >= 1024:
                self.server.sessions.pop(next(iter(self.server.sessions)))
            self.server.sessions.pop(self._token(), None)
            token = secrets.token_urlsafe(32)
            self.server.sessions[token] = current + 86400
        self.send_json({"ok": True}, headers={"Set-Cookie": self._cookie(token)})

    def _static(self, name: str) -> None:
        path = self.server.web_dir / name
        if not regular_file(path):
            raise HubError(404, "页面资源不存在。")
        content_type = {"index.html": "text/html; charset=utf-8", "app.css": "text/css; charset=utf-8", "app.js": "text/javascript; charset=utf-8"}[name]
        with open_regular(path) as source:
            data = source.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def handle_download(self, file_id: str) -> None:
        source, metadata = self.store.open_download(file_id)
        try:
            info = os.fstat(source.fileno())
            size = info.st_size
            etag = '"' + (metadata.get("sha256") or f"{file_id}-{size}-{info.st_mtime_ns}") + '"'
            start, end, status = 0, size - 1, 200
            range_header = self.headers.get("Range")
            if_range = self.headers.get("If-Range")
            if if_range and if_range != etag:
                try:
                    if parsedate_to_datetime(if_range).timestamp() < int(info.st_mtime):
                        range_header = None
                except (ValueError, TypeError, OverflowError):
                    range_header = None
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                try:
                    if not match or not any(match.groups()) or size == 0:
                        raise ValueError
                    first, last = match.groups()
                    if not first:
                        suffix = int(last)
                        if suffix <= 0:
                            raise ValueError
                        start = max(0, size - suffix)
                    else:
                        start = int(first)
                        end = min(int(last), size - 1) if last else size - 1
                    if start >= size or end < start:
                        raise ValueError
                except ValueError:
                    self.send_json({"error": "下载范围超出文件大小。"}, 416, {"Content-Range": f"bytes */{size}"})
                    return
                status = 206
            self.send_response(status)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(max(0, end - start + 1)))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", formatdate(info.st_mtime, usegmt=True))
            quoted = urllib.parse.quote(metadata["original_name"], safe="")
            self.send_header("Content-Disposition", f"attachment; filename=\"download\"; filename*=UTF-8''{quoted}")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            source.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = source.read(min(1024**2, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        finally:
            self.store.close_download(file_id, source)

    def _dispatch(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        method = self.command
        if method in ("POST", "PUT", "DELETE"):
            self._origin_check()
        if method in ("GET", "HEAD"):
            static = {"/": "index.html", "/index.html": "index.html", "/app.css": "app.css", "/app.js": "app.js"}
            if path in static:
                self._static(static[path])
                return
            if path == "/api/status":
                self.send_json({"authenticated": self.authenticated(), "auth_required": True,
                                "public_url": self.server.public_url, "cleanup_time": self.server.cleanup_time,
                                "max_file_size": self.store.max_file_size, "chunk_size": CHUNK_SIZE,
                                "storage_limit": self.store.storage_limit})
                return
            if path == "/healthz":
                self.send_json({"ok": True})
                return
        if method == "POST" and path == "/api/login":
            self._login()
            return
        self._require_auth()
        if method == "POST" and path == "/api/logout":
            with self.server.auth_lock:
                self.server.sessions.pop(self._token(), None)
            self.send_json({"ok": True}, headers={"Set-Cookie": self._cookie("", 0)})
            return
        if method in ("GET", "HEAD") and path == "/api/files":
            self.send_json({"files": self.store.list_files(), "used_bytes": self.store.used_bytes(), "storage_limit": self.store.storage_limit})
            return
        if method == "POST" and path == "/api/uploads":
            payload = self._json()
            self.send_json(self.store.new_upload(payload.get("name"), payload.get("size"), payload.get("owner", "匿名设备")), 201)
            return
        match = re.fullmatch(r"/api/uploads/([A-Za-z0-9_-]+)(/complete)?", path)
        if match:
            file_id, complete = match.groups()
            if complete and method == "POST":
                self.send_json({"ok": True, "file": self.store.complete_upload(file_id)})
                return
            if not complete and method in ("GET", "HEAD"):
                self.send_json(self.store.upload_status(file_id))
                return
            if not complete and method == "DELETE":
                self.store.cancel_upload(file_id)
                self.send_json({"ok": True})
                return
            if not complete and method == "PUT":
                length = self._length(CHUNK_SIZE)
                offset = self.headers.get("Upload-Offset", "")
                if not re.fullmatch(r"\d{1,20}", offset) or len(self.headers.get_all("Upload-Offset", [])) != 1:
                    raise HubError(400, "请求缺少有效的上传偏移量。")
                self.send_json(self.store.write_chunk(file_id, int(offset), length, self.rfile))
                return
        match = re.fullmatch(r"/api/files/([A-Za-z0-9_-]+)", path)
        if method == "DELETE" and match:
            ok = self.store.delete(match.group(1))
            self.send_json({"ok": ok} if ok else {"error": "文件不存在或已被清理。"}, 200 if ok else 404)
            return
        match = re.fullmatch(r"/download/([A-Za-z0-9_-]+)", path)
        if method in ("GET", "HEAD") and match:
            self.handle_download(match.group(1))
            return
        raise HubError(404, "请求的资源不存在。")

    def _handle(self) -> None:
        self.close_connection = True
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except HubError as exc:
            try:
                self.send_json({"error": str(exc)}, exc.status)
            except OSError:
                pass
        except (OSError, ValueError):
            try:
                self.send_json({"error": "服务器暂时无法完成操作，请稍后重试。"}, 500)
            except OSError:
                pass

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_DELETE = _handle


def make_server(host: str, port: int, store: Store, access_code: str, **options: Any) -> HubServer:
    if not access_code or len(access_code) < 16:
        raise ValueError("access code must contain at least 16 characters")
    return HubServer((host, port), store, access_code, **options)


def start_daily_cleanup(store: Store, cleanup_time: str, stop_event: threading.Event) -> threading.Thread:
    hour, minute = parse_daily_time(cleanup_time)

    def run() -> None:
        while not stop_event.wait(seconds_until(hour, minute)):
            try:
                deleted = store.clear_all()
                print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] Daily cleanup: {deleted} file(s).", flush=True)
            except (OSError, HubError):
                print("Daily cleanup failed; storage will be checked on the next run.", flush=True)

    thread = threading.Thread(target=run, name="daily-cleanup", daemon=True)
    thread.start()
    return thread


def load_access_code(path: Path) -> str:
    configured = os.environ.get("FILE_HUB_ACCESS_CODE")
    if configured:
        code = configured
    else:
        path = path.expanduser().absolute()
        if is_link(path) or is_link(path.parent):
            raise ValueError("access code file cannot be a symbolic link")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open_regular(path) as source:
                code = source.read(4096).decode("utf-8").strip()
        else:
            code = secrets.token_urlsafe(18)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(code + "\n")
    if len(code) < 16 or len(code) > 1024 or any(ord(char) < 32 for char in code):
        raise ValueError("access code must contain 16 to 1024 printable characters")
    return code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Private file transfer with resumable uploads.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address (127.0.0.1 behind a local tunnel)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--data-dir", default=os.environ.get("FILE_HUB_DATA_DIR", str(APP_DIR / "shared_files")))
    parser.add_argument("--access-code-file", type=Path, default=APP_DIR / ".filehub" / "access-code")
    parser.add_argument("--max-file-size", type=int, default=int(os.environ.get("FILE_HUB_MAX_FILE_SIZE", DEFAULT_MAX_FILE_SIZE)), help="Maximum file size in bytes")
    parser.add_argument("--storage-limit", type=int, default=int(os.environ.get("FILE_HUB_STORAGE_LIMIT", DEFAULT_STORAGE_LIMIT)), help="Storage quota including unfinished uploads, in bytes")
    parser.add_argument("--upload-ttl", type=int, default=7200, help="Unfinished upload idle expiry, in seconds")
    parser.add_argument("--cleanup-time", default=os.environ.get("FILE_HUB_CLEANUP_TIME", DEFAULT_CLEANUP_TIME), type=lambda value: value if parse_daily_time(value) else value)
    parser.add_argument("--no-auto-cleanup", action="store_true", help="Disable daily cleanup of completed files")
    parser.add_argument("--trust-proxy", action="store_true", help="Trust X-Forwarded-Proto only from loopback")
    return parser.parse_args()


def run(args: argparse.Namespace) -> None:
    store = Store(Path(args.data_dir), max_file_size=args.max_file_size, storage_limit=args.storage_limit, upload_ttl=args.upload_ttl)
    code = load_access_code(args.access_code_file)
    cleanup_time = None if args.no_auto_cleanup else args.cleanup_time
    server = make_server(args.host, args.port, store, code, trust_proxy=args.trust_proxy,
                         cleanup_time=cleanup_time, public_url=os.environ.get("FILE_HUB_PUBLIC_URL"))
    stop_event = threading.Event()
    if cleanup_time:
        start_daily_cleanup(store, cleanup_time, stop_event)

    def janitor() -> None:
        while not stop_event.wait(30):
            store.expire_uploads()
            with server.auth_lock:
                server.sessions = {token: expiry for token, expiry in server.sessions.items() if expiry > time.monotonic()}

    threading.Thread(target=janitor, name="upload-cleanup", daemon=True).start()
    print(f"\n{APP_NAME} is running.\nFiles: {store.root}\nAccess code: {code}", flush=True)
    print(f"Open on this computer: http://127.0.0.1:{server.server_port}", flush=True)
    if args.host == "0.0.0.0":
        print(f"Open on your LAN: http://{get_lan_ip()}:{server.server_port}", flush=True)
    print(f"Daily cleanup: {cleanup_time or 'disabled'} (server local time).", flush=True)
    print("Keep this process running. Press Ctrl+C to stop.\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", flush=True)
    finally:
        stop_event.set()
        server.server_close()


def main() -> None:
    args = parse_args()
    with DataDirectoryLock(Path(args.data_dir)):
        run(args)


if __name__ == "__main__":
    main()
