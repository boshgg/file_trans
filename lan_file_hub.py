#!/usr/bin/env python3
"""
LAN File Hub

Run this on one computer, then open the printed address from other computers
on the same network to upload and download files.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_NAME = "LAN File Hub"
DEFAULT_PORT = 8765
DEFAULT_CLEANUP_TIME = "08:00"
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>局域网文件传输</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #172033;
      --muted: #637083;
      --line: #d9e0ea;
      --accent: #1477d4;
      --accent-strong: #095eb1;
      --danger: #b3261e;
      --ok: #1b7f45;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #11151b;
        --panel: #1a2029;
        --text: #e8edf5;
        --muted: #9ca8b8;
        --line: #303946;
        --accent: #5aa9ff;
        --accent-strong: #8cc2ff;
      }
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
    }
    main {
      width: min(980px, calc(100vw - 28px));
      margin: 0 auto;
      padding: 22px 0 36px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-end;
      gap: 16px;
      margin-bottom: 18px;
    }
    h1 {
      font-size: clamp(24px, 4vw, 36px);
      margin: 0 0 6px;
      letter-spacing: 0;
    }
    .sub { color: var(--muted); font-size: 14px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 14px;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
    }
    .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    input[type="text"] {
      min-width: min(280px, 100%);
      flex: 1;
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: transparent;
      color: var(--text);
      font-size: 15px;
    }
    button, .button {
      border: 0;
      border-radius: 6px;
      background: var(--accent);
      color: white;
      padding: 10px 13px;
      font-weight: 650;
      cursor: pointer;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 40px;
      white-space: nowrap;
    }
    button:hover, .button:hover { background: var(--accent-strong); }
    button.secondary {
      background: transparent;
      color: var(--accent);
      border: 1px solid var(--line);
    }
    button.danger {
      background: transparent;
      color: var(--danger);
      border: 1px solid var(--line);
    }
    #drop {
      border: 2px dashed var(--line);
      border-radius: 8px;
      padding: 28px 16px;
      text-align: center;
      cursor: pointer;
      transition: border-color .15s, background .15s;
    }
    #drop.drag {
      border-color: var(--accent);
      background: color-mix(in srgb, var(--accent) 12%, transparent);
    }
    #fileInput { display: none; }
    .progress {
      height: 8px;
      border-radius: 99px;
      background: color-mix(in srgb, var(--line) 70%, transparent);
      overflow: hidden;
      margin-top: 10px;
    }
    .bar {
      height: 100%;
      width: 0%;
      background: var(--ok);
      transition: width .12s linear;
    }
    .status { margin-top: 8px; color: var(--muted); font-size: 14px; min-height: 20px; }
    table {
      width: 100%;
      border-collapse: collapse;
    }
    th, td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 11px 8px;
      vertical-align: middle;
      font-size: 14px;
    }
    th { color: var(--muted); font-weight: 700; }
    td.name {
      word-break: break-word;
      font-weight: 650;
    }
    td.actions { width: 170px; text-align: right; }
    .empty {
      color: var(--muted);
      text-align: center;
      padding: 24px 0;
    }
    .pill {
      color: var(--muted);
      font-size: 13px;
    }
    @media (max-width: 640px) {
      header { display: block; }
      th:nth-child(3), td:nth-child(3) { display: none; }
      td.actions { width: 120px; }
      .button, button { width: auto; }
    }
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>局域网文件传输</h1>
        <div class="sub" id="address"></div>
      </div>
      <button class="secondary" id="refreshBtn" title="刷新文件列表">刷新</button>
    </header>

    <section class="panel">
      <div class="row">
        <input id="nameInput" type="text" maxlength="40" autocomplete="off" placeholder="这台电脑的名字">
      </div>
      <div id="drop" style="margin-top: 14px;">
        <strong>点击选择文件，或把文件拖到这里</strong>
        <div class="sub" style="margin-top: 8px;">支持大文件；上传期间请保持这个页面打开。</div>
      </div>
      <input id="fileInput" type="file" multiple>
      <div class="progress"><div class="bar" id="bar"></div></div>
      <div class="status" id="status"></div>
    </section>

    <section class="panel">
      <div class="row" style="justify-content: space-between; margin-bottom: 6px;">
        <strong>可下载文件</strong>
        <span class="pill" id="count"></span>
      </div>
      <div style="overflow-x:auto;">
        <table>
          <thead>
            <tr>
              <th>文件名</th>
              <th>大小</th>
              <th>来自</th>
              <th>时间</th>
              <th></th>
            </tr>
          </thead>
          <tbody id="files"></tbody>
        </table>
      </div>
    </section>
  </main>

  <script>
    const drop = document.getElementById('drop');
    const input = document.getElementById('fileInput');
    const filesBody = document.getElementById('files');
    const statusEl = document.getElementById('status');
    const bar = document.getElementById('bar');
    const countEl = document.getElementById('count');
    const nameInput = document.getElementById('nameInput');
    const refreshBtn = document.getElementById('refreshBtn');

    const savedName = localStorage.getItem('lanHubName') || guessName();
    nameInput.value = savedName;
    document.getElementById('address').textContent = location.href;
    nameInput.addEventListener('input', () => localStorage.setItem('lanHubName', nameInput.value.trim()));
    refreshBtn.addEventListener('click', loadFiles);

    function guessName() {
      const ua = navigator.userAgent;
      if (ua.includes('Windows')) return 'Windows电脑';
      if (ua.includes('Mac')) return 'Mac电脑';
      if (ua.includes('iPhone') || ua.includes('Android')) return '手机';
      return '我的电脑';
    }

    function fmtBytes(n) {
      const units = ['B','KB','MB','GB','TB'];
      let i = 0, v = n;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
    }

    function fmtTime(ts) {
      return new Date(ts * 1000).toLocaleString();
    }

    async function loadFiles() {
      const res = await fetch('/api/files', {cache: 'no-store'});
      const data = await res.json();
      countEl.textContent = data.files.length ? `${data.files.length} 个文件` : '';
      if (!data.files.length) {
        filesBody.innerHTML = `<tr><td class="empty" colspan="5">还没有文件</td></tr>`;
        return;
      }
      filesBody.innerHTML = data.files.map(f => `
        <tr>
          <td class="name">${escapeHtml(f.original_name)}</td>
          <td>${fmtBytes(f.size)}</td>
          <td>${escapeHtml(f.owner || '-')}</td>
          <td>${fmtTime(f.created_at)}</td>
          <td class="actions">
            <a class="button" href="/download/${encodeURIComponent(f.id)}">下载</a>
            <button class="danger" data-delete="${f.id}" title="删除文件">删除</button>
          </td>
        </tr>`).join('');
      for (const btn of filesBody.querySelectorAll('[data-delete]')) {
        btn.addEventListener('click', async () => {
          if (!confirm('删除这个文件？')) return;
          await fetch(`/api/files/${encodeURIComponent(btn.dataset.delete)}`, {method: 'DELETE'});
          loadFiles();
        });
      }
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[c]));
    }

    drop.addEventListener('click', () => input.click());
    input.addEventListener('change', () => uploadFiles(input.files));
    for (const eventName of ['dragenter', 'dragover']) {
      drop.addEventListener(eventName, e => {
        e.preventDefault();
        drop.classList.add('drag');
      });
    }
    for (const eventName of ['dragleave', 'drop']) {
      drop.addEventListener(eventName, e => {
        e.preventDefault();
        drop.classList.remove('drag');
      });
    }
    drop.addEventListener('drop', e => uploadFiles(e.dataTransfer.files));

    async function uploadFiles(list) {
      const files = Array.from(list || []);
      if (!files.length) return;
      for (let i = 0; i < files.length; i++) {
        await uploadOne(files[i], i + 1, files.length);
      }
      input.value = '';
      bar.style.width = '0%';
      await loadFiles();
    }

    function uploadOne(file, index, total) {
      return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        xhr.setRequestHeader('X-Filename', encodeURIComponent(file.name));
        xhr.setRequestHeader('X-Owner', encodeURIComponent(nameInput.value.trim() || '匿名电脑'));
        xhr.setRequestHeader('Content-Type', 'application/octet-stream');
        xhr.upload.onprogress = e => {
          if (e.lengthComputable) {
            const pct = Math.round((e.loaded / e.total) * 100);
            bar.style.width = `${pct}%`;
            statusEl.textContent = `正在上传 ${index}/${total}: ${file.name} · ${pct}%`;
          }
        };
        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            statusEl.textContent = `已上传: ${file.name}`;
            resolve();
          } else {
            statusEl.textContent = `上传失败: ${file.name}`;
            reject(new Error(xhr.responseText || xhr.statusText));
          }
        };
        xhr.onerror = () => {
          statusEl.textContent = `网络中断: ${file.name}`;
          reject(new Error('network error'));
        };
        xhr.send(file);
      });
    }

    loadFiles();
    setInterval(loadFiles, 5000);
  </script>
</body>
</html>
"""


def now() -> float:
    return time.time()


def clean_name(name: str) -> str:
    name = urllib.parse.unquote(name).strip().replace("\\", "_").replace("/", "_")
    name = CONTROL_RE.sub("_", name)
    name = name.strip(" .")
    return name[:180] or "file"


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def parse_daily_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("time must use HH:MM format, for example 08:00")
    return int(match.group(1)), int(match.group(2))


def seconds_until(hour: int, minute: int) -> float:
    current = datetime.now()
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= current:
        target += timedelta(days=1)
    return (target - current).total_seconds()


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.files_dir = root / "files"
        self.meta_path = root / "metadata.json"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.meta: dict[str, dict[str, Any]] = self._load_meta()

    def _load_meta(self) -> dict[str, dict[str, Any]]:
        if not self.meta_path.exists():
            return {}
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_meta(self) -> None:
        tmp = self.meta_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.meta_path)

    def list_files(self) -> list[dict[str, Any]]:
        with self.lock:
            result = []
            for file_id, item in list(self.meta.items()):
                path = self.files_dir / file_id
                if not path.exists():
                    self.meta.pop(file_id, None)
                    continue
                row = dict(item)
                row["id"] = file_id
                row["size"] = path.stat().st_size
                result.append(row)
            result.sort(key=lambda item: item.get("created_at", 0), reverse=True)
            self._save_meta()
            return result

    def add_file(self, source: Path, original_name: str, owner: str) -> dict[str, Any]:
        with self.lock:
            stem = f"{int(now() * 1000)}-{os.urandom(4).hex()}"
            path = self.files_dir / stem
            source.replace(path)
            item = {
                "original_name": original_name,
                "owner": owner[:80],
                "created_at": now(),
                "size": path.stat().st_size,
            }
            self.meta[stem] = item
            self._save_meta()
            return {"id": stem, **item}

    def get_path(self, file_id: str) -> Path | None:
        with self.lock:
            if file_id not in self.meta:
                return None
            path = self.files_dir / file_id
            if not path.exists():
                return None
            return path

    def get_meta(self, file_id: str) -> dict[str, Any] | None:
        with self.lock:
            meta = self.meta.get(file_id)
            return dict(meta) if meta else None

    def delete(self, file_id: str) -> bool:
        with self.lock:
            existed = file_id in self.meta
            self.meta.pop(file_id, None)
            path = self.files_dir / file_id
            if path.exists():
                path.unlink()
                existed = True
            self._save_meta()
            return existed

    def clear_all(self) -> int:
        with self.lock:
            count = 0
            for path in self.files_dir.iterdir():
                if path.is_file():
                    path.unlink()
                    count += 1
            count = max(count, len(self.meta))
            self.meta.clear()
            self._save_meta()
            return count


def start_daily_cleanup(store: Store, cleanup_time: str, stop_event: threading.Event) -> threading.Thread:
    hour, minute = parse_daily_time(cleanup_time)

    def run() -> None:
        while not stop_event.wait(seconds_until(hour, minute)):
            deleted = store.clear_all()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[{stamp}] Daily cleanup finished. Deleted {deleted} file(s).")

    thread = threading.Thread(target=run, name="daily-cleanup", daemon=True)
    thread.start()
    return thread



class Handler(BaseHTTPRequestHandler):
    server_version = f"{APP_NAME}/1.0"

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_text(self, text: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            self.send_text(INDEX_HTML, content_type="text/html; charset=utf-8")
            return
        if parsed.path == "/api/files":
            self.send_json({"files": self.store.list_files()})
            return
        if parsed.path.startswith("/download/"):
            self.handle_download(parsed.path.removeprefix("/download/"))
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            data = INDEX_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_DELETE(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/files/"):
            file_id = parsed.path.removeprefix("/api/files/")
            ok = self.store.delete(file_id)
            self.send_json({"ok": ok}, status=200 if ok else 404)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def handle_upload(self) -> None:
        raw_name = self.headers.get("X-Filename", "file")
        raw_owner = self.headers.get("X-Owner", "匿名电脑")
        original_name = clean_name(raw_name)
        owner = clean_name(raw_owner)
        length_header = self.headers.get("Content-Length")
        if not length_header:
            self.send_json({"error": "missing Content-Length"}, status=411)
            return
        try:
            remaining = int(length_header)
        except ValueError:
            self.send_json({"error": "bad Content-Length"}, status=400)
            return

        fd, tmp_name = tempfile.mkstemp(prefix="upload-", dir=str(self.store.root))
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("client disconnected")
                    out.write(chunk)
                    remaining -= len(chunk)
            item = self.store.add_file(tmp_path, original_name, owner)
            self.send_json({"ok": True, "file": item}, status=201)
        except Exception as exc:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            self.send_json({"ok": False, "error": str(exc)}, status=500)

    def handle_download(self, file_id: str) -> None:
        file_id = urllib.parse.unquote(file_id)
        path = self.store.get_path(file_id)
        meta = self.store.get_meta(file_id)
        if path is None or meta is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        original_name = str(meta.get("original_name") or "download")
        content_type = mimetypes.guess_type(original_name)[0] or "application/octet-stream"
        quoted = urllib.parse.quote(original_name)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.end_headers()
        with path.open("rb") as src:
            shutil.copyfileobj(src, self.wfile, length=1024 * 1024)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simple cross-platform LAN file transfer hub.")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address, default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Listen port, default: {DEFAULT_PORT}")
    parser.add_argument("--data-dir", default="shared_files", help="Folder for uploaded files and metadata")
    parser.add_argument(
        "--cleanup-time",
        default=DEFAULT_CLEANUP_TIME,
        type=lambda value: value if parse_daily_time(value) else value,
        help=f"Daily cleanup time in HH:MM, default: {DEFAULT_CLEANUP_TIME}",
    )
    parser.add_argument("--no-auto-cleanup", action="store_true", help="Disable daily automatic cleanup")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir).expanduser().resolve()
    store = Store(data_dir)
    stop_event = threading.Event()
    if not args.no_auto_cleanup:
        start_daily_cleanup(store, args.cleanup_time, stop_event)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.store = store  # type: ignore[attr-defined]
    lan_ip = get_lan_ip()
    print(f"\n{APP_NAME} is running.")
    print(f"Files are saved in: {data_dir}")
    if args.no_auto_cleanup:
        print("Daily cleanup is disabled.")
    else:
        print(f"Daily cleanup time: {args.cleanup_time}")
    print(f"Open on this computer: http://127.0.0.1:{args.port}")
    print(f"Open from other computers: http://{lan_ip}:{args.port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
