import http.client
import json
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

from app import GATEWAY_PREFIX, GatewayServer, Inventory
from fnos_test_support import GatewayTCPHarness


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inventory = Inventory(Path(self.temp.name) / "inventory.db")
        self.server = GatewayTCPHarness(self.inventory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, method="GET", headers=None, authenticated=True):
        defaults = {"Host": "nas.example:5667", "Content-Type": "application/json"}
        if authenticated:
            defaults.update({"X-Trim-Userid": "1000", "X-Trim-Isadmin": "true"})
        defaults.update(headers or {})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None, defaults)
            response = connection.getresponse()
            return response.status, response.read(), response.headers
        finally:
            connection.close()

    def test_requires_gateway_admin_for_pages_and_every_api(self):
        for path, method in (("/", "GET"), ("/api/state", "GET"), ("/api/backup", "GET"),
                             ("/api/export.csv", "GET"), ("/api/components", "POST"),
                             ("/api/export.json", "GET"), ("/api/import/preview", "POST"), ("/api/import", "POST"),
                             ("/api/projects", "GET"), ("/api/projects", "POST"), ("/api/projects/1", "PUT"),
                             ("/api/projects/1/bom", "POST"), ("/api/projects/1/bom/preview", "POST"), ("/api/projects/1/shortages.csv", "GET"),
                             ("/api/catalog/search", "POST"), ("/api/restore", "POST"), ("/api/components/1", "DELETE")):
            with self.subTest(path=path, method=method):
                full_path = GATEWAY_PREFIX + path
                self.assertEqual(self.request(full_path, method=method, authenticated=False)[0], 401)
                self.assertEqual(self.request(full_path, method=method, headers={"X-Trim-Isadmin": "false"})[0], 403)
        self.assertEqual(self.request(GATEWAY_PREFIX + "/", headers={"X-Trim-Userid": "spoof,1000"})[0], 401)
        self.assertEqual(self.inventory.snapshot(), {"components": [], "movements": []})

    def test_prefixed_page_assets_redirect_and_frame_policy(self):
        status, _, headers = self.request(GATEWAY_PREFIX)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], GATEWAY_PREFIX + "/")
        status, content, headers = self.request(GATEWAY_PREFIX + "/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__INVENTORY_", content)
        self.assertIn("飞牛 NAS".encode(), content)
        for name in ("app.js", "projects.js", "style.css", "favicon.svg"):
            self.assertIn((GATEWAY_PREFIX + "/" + name).encode(), content)
            self.assertEqual(self.request(GATEWAY_PREFIX + "/" + name)[0], 200)
        self.assertIn("frame-ancestors 'self'", headers["Content-Security-Policy"])
        for path in ("/api/state", "/app/electronics-inventory-fake/api/state",
                     GATEWAY_PREFIX + "/../server/app.py", GATEWAY_PREFIX + "/data/inventory.db"):
            self.assertEqual(self.request(path)[0], 404)

    def test_same_origin_and_forwarded_host(self):
        path = GATEWAY_PREFIX + "/api/state"
        self.assertEqual(self.request(path, headers={"Origin": "https://nas.example:5667"})[0], 200)
        for headers in ({"Origin": "https://evil.example"}, {"Origin": "null"},
                        {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
                        {"Host": "[broken"}, {"Host": "nas.example:bad-port"}):
            self.assertEqual(self.request(path, headers=headers)[0], 403)
        self.assertEqual(self.request(path, headers={"Host": "localhost", "X-Forwarded-Host": "nas.example:5667", "Origin": "https://nas.example:5667"})[0], 200)

    def page_token(self):
        status, content, headers = self.request(GATEWAY_PREFIX + "/", headers={"Host": "localhost"})
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        return re.search(rb'name="inventory-csrf-token" content="([A-Za-z0-9_-]+)"', content)[1].decode()

    def test_page_token_handles_proxy_host_and_metadata_rewrites(self):
        token = self.page_token()
        path = GATEWAY_PREFIX + "/api/projects"
        for fetch_site in ("same-origin", "same-site", "cross-site"):
            headers = {"Host": "localhost", "Origin": "https://nas.example:5667", "Sec-Fetch-Site": fetch_site, "X-Inventory-CSRF": token}
            self.assertEqual(self.request(path, {"name": "proxy-project", "build_quantity": 1}, "POST", headers)[0], 201)
            self.assertEqual(self.request(path, headers=headers)[0], 200)
            self.assertEqual(self.request(path, headers={**headers, "X-Trim-Isadmin": "false"})[0], 403)
            self.assertEqual(self.request(path, headers=headers, authenticated=False)[0], 401)

    def test_wrong_missing_and_stale_tokens_cannot_bypass_origin_checks(self):
        path = GATEWAY_PREFIX + "/api/projects"
        headers = {"Host": "localhost", "Origin": "https://other.example", "Sec-Fetch-Site": "same-origin"}
        self.assertEqual(self.request(path, {"name": "blocked"}, "POST", headers)[0], 403)
        self.assertEqual(self.request(path, {"name": "blocked"}, "POST", {**headers, "X-Inventory-CSRF": "wrong"})[0], 403)
        old = self.page_token()
        self.server.csrf_token = secrets.token_urlsafe(32)  # Same rotation as a service restart.
        self.assertEqual(self.request(path, {"name": "stale"}, "POST", {**headers, "X-Inventory-CSRF": old})[0], 403)
        current = self.page_token()
        self.assertNotEqual(old, current)
        self.assertEqual(self.request(path, {"name": "current"}, "POST", {**headers, "X-Inventory-CSRF": current})[0], 201)
        self.assertEqual(len(self.inventory.list_projects()), 1)

    def test_prefixed_inventory_backup_restore_and_persistence(self):
        base = GATEWAY_PREFIX + "/api"
        origin = {"Origin": "https://nas.example:5667"}
        status, content, _ = self.request(base + "/components", {"name": "电阻", "model": "0603-10K", "quantity": 5}, "POST", origin)
        self.assertEqual(status, 201)
        item = json.loads(content)
        self.assertEqual(self.request(f"{base}/components/{item['id']}/stock", {"kind": "out", "quantity": 2}, "POST", origin)[0], 200)
        status, content, _ = self.request(base + "/export.json")
        self.assertEqual(status, 200)
        backup = json.loads(content)
        self.assertEqual(backup["components"][0]["quantity"], 3)
        status, content, headers = self.request(base + "/export.csv")
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(self.request(base + "/import/preview", backup, "POST", origin)[0], 200)
        self.assertEqual(self.request(base + "/import", backup, "POST", origin)[0], 200)
        persisted = Inventory(self.inventory.path).snapshot()
        self.assertEqual(persisted["components"][0]["quantity"], 3)
        self.assertEqual(len(persisted["movements"]), 2)

    def test_cli_has_no_local_tcp_startup(self):
        app = Path(__file__).resolve().parents[1] / "app.py"
        result = subprocess.run([sys.executable, str(app), "--port", "8765"], cwd=self.temp.name, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--socket", result.stderr)
        self.assertIn("--db", result.stderr)
        self.assertFalse((Path(self.temp.name) / "data").exists())

    def test_catalog_search_is_authenticated_read_only_and_handles_upstream_failure(self):
        from catalog import CatalogError
        path = GATEWAY_PREFIX + "/api/catalog/search"
        result = {"source": "立创商城 · 立创 EDA", "products": [], "limit": 12}
        with patch("app.search_products", return_value=result) as search:
            status, content, _ = self.request(path, {"keyword": "STM32F103"}, "POST")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(content), result)
            search.assert_called_once_with("STM32F103", "auto")
        with patch("app.search_products", return_value=result) as search:
            self.assertEqual(self.request(path, {"keyword": "STM32F103", "source": "hqchip"}, "POST")[0], 200)
            search.assert_called_once_with("STM32F103", "hqchip")
        self.assertEqual(self.request(path, {"keyword": "STM32F103", "source": "unknown"}, "POST")[0], 400)
        with patch("app.search_products", side_effect=CatalogError("连接失败")):
            self.assertEqual(self.request(path, {"keyword": "C25804"}, "POST")[0], 502)
        self.assertEqual(self.request(path, {"keyword": "x"}, "POST")[0], 400)
        self.assertEqual(self.inventory.snapshot(), {"components": [], "movements": []})
        status, _, headers = self.request(GATEWAY_PREFIX + "/")
        self.assertEqual(status, 200)
        self.assertIn("img-src 'self' data: https:", headers["Content-Security-Policy"])
        self.assertEqual(self.request(GATEWAY_PREFIX + "/catalog.js")[0], 200)


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets require Linux/fnOS on this Python build")
class UnixGatewayTest(unittest.TestCase):
    def test_real_unix_socket_http_and_healthcheck(self):
        from fnos.healthcheck import check

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "app.sock"
            server = GatewayServer(path, Inventory(Path(folder) / "inventory.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertTrue(check(str(path), GATEWAY_PREFIX))
                self.assertFalse(check(str(path), "/app/wrong-app"))
                connection = http.client.HTTPConnection("localhost", timeout=5)
                connection.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.sock.connect(str(path))
                connection.request("GET", GATEWAY_PREFIX + "/api/state")
                response = connection.getresponse()
                self.assertEqual(response.status, 401)
                response.read()
                connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
