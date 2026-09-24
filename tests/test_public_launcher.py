import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import start_public


class PublicLauncherTests(unittest.TestCase):
    def test_redirected_ansi_console_can_display_chinese(self):
        output = io.BytesIO()
        console = io.TextIOWrapper(output, encoding="cp1252")
        with patch.object(start_public.sys, "stdout", console):
            start_public.configure_console()
            print("文件传输", flush=True)
        self.assertEqual(output.getvalue().decode("utf-8"), "文件传输\n")
        console.detach()

    def test_download_is_verified_before_executing(self):
        with tempfile.TemporaryDirectory() as folder:
            payload = b"verified executable"
            assets = {("Windows", "amd64"): ("cloudflared.exe", hashlib.sha256(payload).hexdigest())}
            with patch.object(start_public, "ROOT", Path(folder)), patch.object(start_public, "ASSETS", assets), patch.object(start_public, "asset_key", return_value=("Windows", "amd64")), patch.object(start_public.urllib.request, "urlopen", return_value=io.BytesIO(payload)) as download:
                result = start_public.install_cloudflared()
                self.assertEqual(result.read_bytes(), payload)
                self.assertEqual(start_public.install_cloudflared(), result)
                self.assertEqual(download.call_count, 1)

    def test_checksum_failure_never_installs_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            assets = {("Windows", "amd64"): ("cloudflared.exe", "0" * 64)}
            with patch.object(start_public, "ROOT", Path(folder)), patch.object(start_public, "ASSETS", assets), patch.object(start_public, "asset_key", return_value=("Windows", "amd64")), patch.object(start_public.urllib.request, "urlopen", return_value=io.BytesIO(b"tampered")):
                with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                    start_public.install_cloudflared()
                self.assertFalse(any(Path(folder).rglob("*.exe")))
                self.assertFalse(any(Path(folder).rglob("*.part")))

    def test_failed_server_never_gets_public_tunnel(self):
        process = Mock()
        process.poll.return_value = 1
        with self.assertRaises(RuntimeError):
            start_public.wait_for_server(process, 8765)

    def test_forced_stop_reaps_unresponsive_child(self):
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired("cloudflared", 8), 0]
        start_public.stop(process)
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)

    def test_tunnel_url_has_expected_https_host(self):
        self.assertEqual(start_public.URL_PATTERN.search("INF https://sample-sharing.trycloudflare.com connected").group(), "https://sample-sharing.trycloudflare.com")
        self.assertIsNone(start_public.URL_PATTERN.search("http://sample-sharing.trycloudflare.com"))


if __name__ == "__main__":
    unittest.main()
