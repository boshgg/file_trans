#!/usr/bin/env python3
"""Start File Hub behind an authenticated, temporary Cloudflare HTTPS tunnel."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import queue
import re
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
VERSION = "2026.9.1"
# Official GitHub release asset SHA-256 digests; upgrades are intentional.
ASSETS = {
    ("Windows", "amd64"): ("cloudflared-windows-amd64.exe", "2837888cc0f5d58f15b6dc478376de90b4d3ba5241c7947455d1e0a0df429712"),
    ("Windows", "386"): ("cloudflared-windows-386.exe", "11b6e4b2d306950bd87e7caa4deee8e80a32d71ffee555a96237a76651eeae4c"),
    ("Darwin", "amd64"): ("cloudflared-darwin-amd64.tgz", "ff0d3b51d5ff70eceef89d6b32145fee985018a2174596a5dbe405e2766e2ac4"),
    ("Darwin", "arm64"): ("cloudflared-darwin-arm64.tgz", "c27ab8fd0aa489449e3d201eb02f957ef460a13b613662928b1b23394bf1bcfe"),
    ("Linux", "amd64"): ("cloudflared-linux-amd64", "03f1f25d1cc93b9ad6c60569d44060bc4f17ed97075760ed8cfca4b12dcd68cc"),
    ("Linux", "arm64"): ("cloudflared-linux-arm64", "3d97437c71848bd8df68041e12436b484a661d95073ea1937f01a845ce88faa3"),
}
URL_PATTERN = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com\b")


def configure_console() -> None:
    # Redirected Windows output otherwise inherits a legacy ANSI code page.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def asset_key() -> tuple[str, str]:
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "i386": "386", "i686": "386", "x86": "386"}.get(machine, machine)
    if platform.system() == "Windows" and arch == "arm64":
        arch = "amd64"  # Windows 11 ARM runs the official x64 build.
    return platform.system(), arch


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def install_cloudflared(custom: str | None = None) -> Path:
    configure_console()
    if custom:
        binary = Path(custom).expanduser().resolve()
        if not binary.is_file():
            raise RuntimeError(f"找不到指定程序：{binary}")
        return binary
    key = asset_key()
    if key not in ASSETS:
        raise RuntimeError("当前平台没有内置下载配置。请安装官方 cloudflared 后使用 --cloudflared 路径。")
    name, expected = ASSETS[key]
    cache = ROOT / ".filehub" / "bin" / VERSION
    cache.mkdir(parents=True, exist_ok=True)
    asset = cache / name
    if not asset.exists() or digest(asset) != expected:
        print(f"首次准备公网组件 cloudflared {VERSION}，正在从官方 GitHub 下载…", flush=True)
        url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{name}"
        temporary = cache / (name + f".{os.getpid()}.part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "File-Hub/2"})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as target:
                total = 0
                for block in iter(lambda: response.read(1024 * 1024), b""):
                    total += len(block)
                    if total > 150 * 1024 * 1024:
                        raise RuntimeError("下载大小超出预期，已停止。")
                    target.write(block)
            if digest(temporary) != expected:
                raise RuntimeError("cloudflared SHA-256 校验失败，已拒绝运行。")
            temporary.replace(asset)
        finally:
            temporary.unlink(missing_ok=True)
    if name.endswith(".tgz"):
        binary = cache / "cloudflared"
        # Extract only the expected executable, never archive-controlled paths.
        with tarfile.open(asset, "r:gz") as archive:
            member = archive.getmember("cloudflared")
            if not member.isfile() or member.size > 150 * 1024 * 1024:
                raise RuntimeError("压缩包中的 cloudflared 文件不符合预期。")
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError("无法读取 cloudflared。")
            with source, binary.open("wb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
    else:
        binary = asset
    if os.name != "nt":
        binary.chmod(0o700)
    return binary


def stop(process: subprocess.Popen | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def wait_for_server(process: subprocess.Popen, port: int) -> None:
    # Bypass system proxies for the loopback health check.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(80):
        if process.poll() is not None:
            raise RuntimeError("文件服务器启动失败，请查看上方提示。")
        try:
            with opener.open(f"http://127.0.0.1:{port}/api/status", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.15)
    raise RuntimeError("文件服务器没有及时就绪。")


def main(argv: list[str] | None = None) -> int:
    configure_console()
    parser = argparse.ArgumentParser(description="启动临时公网 HTTPS 文件中转站（电脑须保持开机）。其余参数传给文件服务器。")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cloudflared", help="使用指定的可信 cloudflared 可执行文件")
    parser.add_argument("--protocol", choices=("auto", "quic", "http2"), default="auto", help="公网连接协议，默认自动选择并回退")
    parser.add_argument("--install-only", action="store_true", help="仅下载并校验公网组件")
    args, server_args = parser.parse_known_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间。")
    if any(arg == "--host" or arg.startswith("--host=") for arg in server_args):
        parser.error("公网启动器固定绑定 127.0.0.1，请不要指定 --host。")
    server = tunnel = None
    try:
        binary = install_cloudflared(args.cloudflared)
        if args.install_only:
            print(f"公网组件已就绪：{binary}")
            return 0
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", args.port))
            except OSError as exc:
                raise RuntimeError(f"端口 {args.port} 已被占用，请关闭旧服务或使用 --port 8888。") from exc
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        server = subprocess.Popen([sys.executable, "-B", str(ROOT / "lan_file_hub.py"), "--host", "127.0.0.1", "--port", str(args.port), "--trust-proxy", *server_args], cwd=ROOT, env=env)
        wait_for_server(server, args.port)
        state = ROOT / ".filehub"
        state.mkdir(exist_ok=True)
        config = state / "quick-tunnel.yml"
        config.write_text("# Isolated Quick Tunnel configuration.\n{}\n", encoding="utf-8")
        command = [str(binary), "tunnel", "--config", str(config), "--no-autoupdate", "--url", f"http://127.0.0.1:{args.port}", "--protocol", args.protocol]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        tunnel = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", creationflags=flags)
        events: queue.Queue[str | None] = queue.Queue()

        def collect() -> None:
            assert tunnel is not None and tunnel.stdout is not None
            with (state / "tunnel.log").open("w", encoding="utf-8") as log:
                for line in tunnel.stdout:
                    log.write(line)
                    log.flush()
                    events.put(line)
            events.put(None)

        threading.Thread(target=collect, daemon=True, name="tunnel-output").start()
        deadline = time.monotonic() + 90
        url = None
        registered = False
        announced = False
        print("正在建立公网连接，请稍候…", flush=True)
        while True:
            if server.poll() is not None:
                raise RuntimeError("文件服务器已退出，公网入口将同时关闭。")
            if tunnel.poll() is not None:
                raise RuntimeError("公网连接已退出。详情见 .filehub/tunnel.log；检查网络是否允许 Cloudflare Tunnel 后重新启动。")
            try:
                line = events.get(timeout=0.5)
            except queue.Empty:
                line = ""
            if line:
                match = URL_PATTERN.search(line)
                if match:
                    url = match.group(0)
                if "Registered tunnel connection" in line:
                    registered = True
            if url and registered and not announced:
                announced = True
                (state / "public-url.txt").write_text(url + "\n", encoding="utf-8")
                print(f"\n跨网络访问地址：{url}\n将此链接和上方访问密码交给接收者。\n这是临时链接：请保持此窗口打开、电脑开机并联网；重启后链接会改变。\n按 Ctrl+C 停止公网分享。\n", flush=True)
            if not announced and time.monotonic() > deadline:
                raise RuntimeError("90 秒内未能建立公网连接。请查看 .filehub/tunnel.log，或使用局域网启动器。")
    except KeyboardInterrupt:
        print("\n正在关闭文件服务与公网入口…", flush=True)
        return 0
    except (OSError, RuntimeError, tarfile.TarError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr, flush=True)
        return 1
    finally:
        stop(tunnel)
        stop(server)
        if server is not None:
            (ROOT / ".filehub" / "public-url.txt").unlink(missing_ok=True)


if __name__ == "__main__":
    # SIGTERM also closes child processes instead of leaving a public tunnel.
    def interrupted(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
