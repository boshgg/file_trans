"""Behavioral tests: real HTTP, resumability, authorization and storage races."""
import hashlib
import http.client
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import lan_file_hub as hub

CODE = "test-access-code-123456789"


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.web = self.root / "web"
        self.web.mkdir()
        for name, text in [("index.html", "<html>File Hub</html>"), ("app.js", "'use strict';"), ("app.css", "body{}")]:
            (self.web / name).write_text(text, encoding="utf-8")
        self.store = hub.Store(self.root / "data", max_file_size=32, storage_limit=48)
        self.server = hub.make_server("127.0.0.1", 0, self.store, CODE, web_dir=self.web, cleanup_time=None)
        self.log_patch = patch.object(hub.Handler, "log_message", lambda *args: None)
        self.log_patch.start()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
        self.thread.start()
        self.cookie = ""
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.log_patch.stop()
        self.temporary.cleanup()

    def request(self, method, path, data=None, *, headers=None, auth=True, raw=None):
        outgoing = dict(headers or {})
        if auth and self.cookie:
            outgoing["Cookie"] = self.cookie
        body = raw
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=body, headers=outgoing)
            response = connection.getresponse()
            content = response.read()
            response_headers = dict(response.getheaders())
            if content and response_headers.get("Content-Type", "").startswith("application/json"):
                content = json.loads(content)
            return response.status, response_headers, content
        finally:
            connection.close()

    def login(self, **kwargs):
        status, headers, data = self.request("POST", "/api/login", {"code": CODE}, **kwargs)
        self.assertEqual(status, 200, data)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        return headers

    def new_upload(self, content=b"0123456789", name="example.txt"):
        status, _, data = self.request("POST", "/api/uploads", {"name": name, "size": len(content), "owner": "测试设备"})
        self.assertEqual(status, 201, data)
        return data["id"]

    def complete_file(self, content=b"0123456789", name="example.txt"):
        file_id = self.new_upload(content, name)
        if content:
            status, _, result = self.request("PUT", f"/api/uploads/{file_id}", raw=content, headers={"Upload-Offset": "0"})
            self.assertEqual(status, 200, result)
        status, _, result = self.request("POST", f"/api/uploads/{file_id}/complete")
        self.assertEqual(status, 200, result)
        return file_id, result["file"]

    def test_public_status_but_all_file_routes_require_authentication(self):
        status, _, data = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertFalse(data["authenticated"])
        self.assertEqual(data["chunk_size"], 8 * 1024 * 1024)
        for method, path in [("GET", "/api/files"), ("GET", "/download/abc"), ("HEAD", "/download/abc"),
                             ("POST", "/api/uploads"), ("PUT", "/api/uploads/abc"), ("DELETE", "/api/files/abc")]:
            with self.subTest(method=method, path=path):
                self.assertEqual(self.request(method, path)[0], 401)

    def test_login_logout_and_secure_cookie(self):
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        self.assertNotIn("; Secure", headers["Set-Cookie"])
        self.assertTrue(self.request("GET", "/api/status")[2]["authenticated"])
        self.assertEqual(self.request("POST", "/api/logout")[0], 200)
        self.assertEqual(self.request("GET", "/api/files")[0], 401)
        self.server.trust_proxy = True
        headers = self.login(headers={"X-Forwarded-Proto": "https", "Host": "files.example", "Origin": "https://files.example"})
        self.assertIn("; Secure", headers["Set-Cookie"])

    def test_proxy_headers_only_trusted_on_actual_loopback(self):
        handler = object.__new__(hub.Handler)
        handler.server = SimpleNamespace(trust_proxy=True)
        handler.client_address = ("198.51.100.1", 1234)
        handler.headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "127.0.0.1"}
        self.assertFalse(handler._secure_request())
        handler.client_address = ("127.0.0.1", 1234)
        self.assertTrue(handler._secure_request())

    def test_cross_origin_mutations_rejected_and_login_throttled(self):
        self.assertEqual(self.request("POST", "/api/login", {"code": CODE}, headers={"Origin": "https://attacker.example"})[0], 403)
        self.login()
        self.assertEqual(self.request("DELETE", "/api/files/abc", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        for _ in range(8):
            self.assertEqual(self.request("POST", "/api/login", {"code": "wrong"})[0], 401)
        result = self.request("POST", "/api/login", {"code": "still wrong"})
        self.assertEqual(result[0], 429)
        self.assertEqual(result[1]["Retry-After"], "60")
        # All tunnel clients may share one loopback peer; a bad client cannot
        # prevent somebody with the correct code from signing in.
        self.assertEqual(self.request("POST", "/api/login", {"code": CODE})[0], 200)

    def test_resumable_offsets_hash_and_idempotent_completion(self):
        self.login()
        file_id = self.new_upload(name="中文 100%2F.txt")
        url = f"/api/uploads/{file_id}"
        self.assertEqual(self.request("PUT", url, raw=b"01234", headers={"Upload-Offset": "0"})[2]["offset"], 5)
        self.assertEqual(self.request("PUT", url, raw=b"wrong", headers={"Upload-Offset": "0"})[0], 409)
        self.assertEqual(self.request("GET", url)[2]["offset"], 5)
        self.assertEqual(self.request("POST", url + "/complete")[0], 409)
        self.assertEqual(self.request("PUT", url, raw=b"56789", headers={"Upload-Offset": "5"})[0], 200)
        result = self.request("POST", url + "/complete")[2]
        self.assertEqual(result["file"]["sha256"], hashlib.sha256(b"0123456789").hexdigest())
        self.assertEqual(result["file"]["original_name"], "中文 100%2F.txt")
        self.assertEqual(self.request("POST", url + "/complete")[2], result)
        self.assertTrue(self.request("GET", url)[2]["completed"])
        self.assertEqual(self.request("GET", "/api/files")[2]["used_bytes"], 10)
        self.assertEqual(self.request("GET", "/download/" + file_id)[2], b"0123456789")
        self.assertEqual(self.request("DELETE", "/api/files/" + file_id)[0], 200)
        self.assertEqual(self.request("POST", url + "/complete")[0], 404)

    def test_range_and_head_downloads(self):
        self.login()
        file_id, _ = self.complete_file()
        url = "/download/" + file_id
        for value, expected in [("bytes=2-5", b"2345"), ("bytes=7-", b"789"), ("bytes=-3", b"789"), ("bytes=0-99", b"0123456789")]:
            with self.subTest(value=value):
                status, headers, body = self.request("GET", url, headers={"Range": value})
                self.assertEqual(status, 206)
                self.assertEqual(body, expected)
                self.assertEqual(int(headers["Content-Length"]), len(expected))
        for value in ["bytes=20-30", "bytes=5-2", "bytes=-0", "bytes=", "bytes=1-2,4-5"]:
            status, headers, _ = self.request("GET", url, headers={"Range": value})
            self.assertEqual(status, 416)
            self.assertEqual(headers["Content-Range"], "bytes */10")
        status, headers, body = self.request("HEAD", url)
        self.assertEqual((status, body, headers["Content-Length"]), (200, b"", "10"))
        self.assertEqual(self.request("GET", url, headers={"Range": "bytes=1-2", "If-Range": '"old"'})[0], 200)
        self.assertEqual(self.request("GET", url, headers={"Range": "bytes=1-2", "If-Range": headers["ETag"]})[0], 206)

    def test_empty_file_and_missing_file(self):
        self.login()
        file_id, item = self.complete_file(b"")
        self.assertEqual(item["sha256"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(self.request("GET", "/download/" + file_id)[2], b"")
        self.assertEqual(self.request("GET", "/download/" + file_id, headers={"Range": "bytes=0-"})[0], 416)
        self.assertEqual(self.request("GET", "/download/missing")[0], 404)

    def test_reservations_enforce_storage_limit_before_uploading(self):
        self.login()
        first = self.new_upload(b"a" * 30)
        result = self.request("POST", "/api/uploads", {"name": "other", "size": 19})
        self.assertEqual(result[0], 413)
        self.assertEqual(self.request("DELETE", "/api/uploads/" + first)[0], 200)
        self.assertEqual(self.request("POST", "/api/uploads", {"name": "other", "size": 19})[0], 201)
        for size in [33, -1, True, "10"]:
            self.assertIn(self.request("POST", "/api/uploads", {"name": "invalid", "size": size})[0], (400, 413))

    def test_bad_lengths_and_offsets_cannot_write(self):
        self.login()
        file_id = self.new_upload()
        url = "/api/uploads/" + file_id
        for headers, expected in [({"Content-Length": "-1", "Upload-Offset": "0"}, 400),
                                  ({"Content-Length": str(hub.CHUNK_SIZE + 1), "Upload-Offset": "0"}, 413),
                                  ({"Content-Length": "0", "Upload-Offset": "-1"}, 400),
                                  ({"Content-Length": "0", "Transfer-Encoding": "chunked", "Upload-Offset": "0"}, 400)]:
            self.assertEqual(self.request("PUT", url, raw=b"", headers=headers)[0], expected)
        self.assertEqual(self.request("GET", url)[2]["offset"], 0)

    def test_interrupted_chunk_rolls_back_then_resumes(self):
        self.login()
        file_id = self.new_upload()
        url = "/api/uploads/" + file_id
        self.assertEqual(self.request("PUT", url, raw=b"01234", headers={"Upload-Offset": "0"})[0], 200)
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            request = f"PUT {url} HTTP/1.1\r\nHost: localhost\r\nCookie: {self.cookie}\r\nContent-Length: 5\r\nUpload-Offset: 5\r\nConnection: close\r\n\r\n"
            connection.sendall(request.encode("ascii") + b"56")
            connection.shutdown(socket.SHUT_WR)
            while connection.recv(4096):
                pass
        finally:
            connection.close()
        self.assertEqual(self.request("GET", url)[2]["offset"], 5)
        self.assertEqual(self.store.uploads[file_id].path.stat().st_size, 5)
        self.assertEqual(self.request("PUT", url, raw=b"56789", headers={"Upload-Offset": "5"})[0], 200)
        item = self.request("POST", url + "/complete")[2]["file"]
        self.assertEqual(item["sha256"], hashlib.sha256(b"0123456789").hexdigest())

    def test_static_allowlist_and_traversal_delete(self):
        self.login()
        sentinel = self.root / "data" / "outside.txt"
        sentinel.write_text("must survive", encoding="utf-8")
        for path in ["/api/files/../outside.txt", "/api/files/%2e%2e%2foutside.txt", "/download/../metadata.json"]:
            self.assertEqual(self.request("DELETE" if path.startswith("/api") else "GET", path)[0], 404)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "must survive")
        for path in ["/", "/app.js", "/app.css"]:
            result = self.request("GET", path, auth=False)
            self.assertEqual(result[0], 200)
            self.assertEqual(result[1]["X-Content-Type-Options"], "nosniff")
        for path in ["/../lan_file_hub.py", "/shared_files/metadata.json", "/.filehub/access-code"]:
            self.assertEqual(self.request("GET", path)[0], 404)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = hub.Store(self.root / "data", max_file_size=32, storage_limit=48)

    def tearDown(self):
        self.temporary.cleanup()

    def complete(self, content=b"example"):
        file_id = self.store.new_upload("file.txt", len(content), "device")["id"]
        if content:
            self.store.write_chunk(file_id, 0, len(content), io.BytesIO(content))
        return self.store.complete_upload(file_id)

    def test_daily_cleanup_allows_active_download_to_finish(self):
        item = self.complete()
        source, _ = self.store.open_download(item["id"])
        try:
            self.assertEqual(self.store.clear_all(), 1)
            self.assertEqual(self.store.list_files(), [])
            self.assertEqual(source.read(), b"example")
            self.assertEqual(self.store.used_bytes(), 7)
        finally:
            self.store.close_download(item["id"], source)
        self.assertEqual(self.store.used_bytes(), 0)

    def test_cleanup_during_chunk_does_not_deadlock_or_restore_deleted_file(self):
        file_id = self.store.new_upload("file.txt", 3, "device")["id"]
        entered, release = threading.Event(), threading.Event()
        class SlowSource(io.BytesIO):
            def read(self, count=-1):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("test timed out")
                return super().read(count)
        errors = []
        def upload():
            try:
                self.store.write_chunk(file_id, 0, 3, SlowSource(b"abc"))
            except hub.HubError as error:
                errors.append(error.status)
        thread = threading.Thread(target=upload, daemon=True)
        thread.start()
        self.assertTrue(entered.wait(2))
        try:
            self.store.clear_all()
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [410])
        self.store.expire_uploads()
        self.assertFalse(self.store.uploads)
        self.assertEqual(self.store.list_files(), [])

    def test_expiry_releases_reserved_space_and_restart_drops_partial_uploads(self):
        file_id = self.store.new_upload("first", 30, "device")["id"]
        self.store.uploads[file_id].touched -= 7201
        self.store.expire_uploads()
        self.assertFalse(self.store.uploads)
        file_id = self.store.new_upload("second", 30, "device")["id"]
        self.store.write_chunk(file_id, 0, 3, io.BytesIO(b"abc"))
        replacement = hub.Store(self.store.root, max_file_size=32, storage_limit=48)
        self.assertFalse(list(replacement.uploads_dir.iterdir()))
        self.assertFalse(replacement.uploads)

    def test_restart_replays_pending_deletion_after_active_download(self):
        item = self.complete()
        source, _ = self.store.open_download(item["id"])
        self.assertTrue(self.store.delete(item["id"]))
        self.assertEqual(self.store.used_bytes(), 7)
        # Simulate process termination: OS closes file handles without running
        # the normal download callback, then a new process opens the same store.
        source.close()
        replacement = hub.Store(self.store.root)
        self.assertEqual(replacement.list_files(), [])
        self.assertEqual(replacement.used_bytes(), 0)
        self.assertFalse(replacement.pending_deletes)

    def test_restart_replays_deletion_journal_written_before_metadata(self):
        item = self.complete()
        self.store.pending_deletes.add(item["id"])
        self.store._save_pending()
        replacement = hub.Store(self.store.root)
        self.assertEqual(replacement.list_files(), [])
        self.assertEqual(replacement.used_bytes(), 0)

    def test_legacy_metadata_and_untrusted_metadata_paths(self):
        files = self.store.files_dir
        file_id = "1700000000000-abcd1234"
        (files / file_id).write_bytes(b"legacy")
        (self.store.root / "metadata.json").write_text(json.dumps({
            file_id: {"original_name": "old.txt", "owner": "old", "created_at": 123, "size": 6},
            "../outside": {"original_name": "bad"}, "bad-row": []}), encoding="utf-8")
        reloaded = hub.Store(self.store.root)
        self.assertEqual(len(reloaded.list_files()), 1)
        self.assertEqual(reloaded.list_files()[0]["id"], file_id)
        self.assertIsNone(reloaded.list_files()[0]["sha256"])
        self.assertFalse(reloaded.delete("../outside"))

    def test_symlink_files_cannot_be_downloaded_or_delete_the_target(self):
        item = self.complete()
        target = self.root / "private.txt"
        target.write_bytes(b"private")
        path = self.store.files_dir / item["id"]
        path.unlink()
        try:
            path.symlink_to(target)
        except (OSError, NotImplementedError):
            self.skipTest("Creating symbolic links requires additional OS privileges")
        with self.assertRaises(hub.HubError):
            self.store.open_download(item["id"])
        self.store.delete(item["id"])
        self.assertEqual(target.read_bytes(), b"private")

    def test_invalid_metadata_does_not_get_silently_overwritten(self):
        self.store.meta_path.write_text("broken {", encoding="utf-8")
        with self.assertRaises(ValueError):
            hub.Store(self.store.root)
        self.assertEqual(self.store.meta_path.read_text(encoding="utf-8"), "broken {")

    def test_generated_access_code_is_persisted_and_env_override_is_private(self):
        path = self.root / "settings" / "access-code"
        with patch.dict(os.environ, {}, clear=True):
            code = hub.load_access_code(path)
            self.assertGreaterEqual(len(code), 16)
            self.assertEqual(hub.load_access_code(path), code)
        with patch.dict(os.environ, {"FILE_HUB_ACCESS_CODE": CODE}):
            self.assertEqual(hub.load_access_code(path), CODE)
            self.assertEqual(path.read_text(encoding="utf-8").strip(), code)

    def test_second_cli_instance_cannot_share_data_directory(self):
        with hub.DataDirectoryLock(self.store.root):
            with self.assertRaisesRegex(ValueError, "already in use"):
                with hub.DataDirectoryLock(self.store.root):
                    self.fail("second instance must not get ownership")
        with hub.DataDirectoryLock(self.store.root):
            self.assertTrue((self.store.root / ".server.lock").exists())


if __name__ == "__main__":
    unittest.main()
