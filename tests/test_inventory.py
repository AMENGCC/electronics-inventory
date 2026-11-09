import copy
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import GATEWAY_PREFIX, Inventory, ValidationError
from fnos_test_support import GatewayTCPHarness


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inventory = Inventory(Path(self.temp.name) / "inventory.db")

    def tearDown(self):
        self.temp.cleanup()

    def item(self, **values):
        return self.inventory.create({"name": "贴片电阻", "model": "RC0603-10K", "quantity": 20, "minimum_stock": 10, **values})

    def test_stock_persists_with_balanced_ledger(self):
        item = self.item()
        self.inventory.stock(item["id"], {"kind": "in", "quantity": 5, "note": "采购"})
        self.inventory.stock(item["id"], {"kind": "out", "quantity": 7})
        self.inventory.stock(item["id"], {"kind": "adjust", "quantity": 15})
        state = Inventory(self.inventory.path).snapshot()
        self.assertEqual(state["components"][0]["quantity"], 15)
        self.assertEqual(sum(r["delta"] for r in state["movements"]), 15)
        self.assertEqual(len(state["movements"]), 4)

    def test_invalid_stock_never_changes_data(self):
        item = self.item()
        before = self.inventory.snapshot()
        for operation in [{"kind": "out", "quantity": 21}, {"kind": "in", "quantity": -1}, {"kind": "in", "quantity": 1.5}, {"kind": "adjust", "quantity": 20}]:
            with self.assertRaises(ValidationError):
                self.inventory.stock(item["id"], operation)
        with self.assertRaises(ValidationError):
            self.inventory.update(item["id"], {**item, "quantity": 100})
        self.assertEqual(before, self.inventory.snapshot())

    def test_concurrent_outgoing_never_oversells(self):
        item = self.item(quantity=10)

        def take_one(_):
            try:
                self.inventory.stock(item["id"], {"kind": "out", "quantity": 1})
                return True
            except ValidationError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(take_one, range(24)))
        self.assertEqual(sum(results), 10)
        state = self.inventory.snapshot()
        self.assertEqual(state["components"][0]["quantity"], 0)
        self.assertEqual(len(state["movements"]), 11)

    def test_restore_roundtrip_and_invalid_restore_rollback(self):
        item = self.item(notes="中文与特殊字符 <&>")
        self.inventory.stock(item["id"], {"kind": "out", "quantity": 3})
        backup = json.loads(json.dumps(self.inventory.backup(), ensure_ascii=False))
        expected = self.inventory.snapshot()
        self.item(model="STM32F103C8T6")
        self.inventory.restore(backup)
        self.assertEqual(self.inventory.snapshot(), expected)
        for invalid in ["balance", "continuity", "reference", "negative", "type"]:
            bad = copy.deepcopy(backup)
            if invalid == "balance":
                bad["components"][0]["quantity"] += 1
            elif invalid == "continuity":
                bad["movements"][0]["before_quantity"] += 1
                bad["movements"][0]["after_quantity"] += 1
            elif invalid == "reference":
                bad["movements"][0]["component_id"] = 999
            elif invalid == "negative":
                bad["components"][0]["quantity"] = -1
            else:
                bad["movements"][0]["kind"] = "in"
            with self.assertRaises((ValidationError, sqlite3.IntegrityError)):
                self.inventory.restore(bad)
            self.assertEqual(self.inventory.snapshot(), expected)

    def test_delete_retains_history_and_nonzero_cannot_delete(self):
        item = self.item()
        with self.assertRaises(ValidationError):
            self.inventory.delete(item["id"])
        self.inventory.stock(item["id"], {"kind": "adjust", "quantity": 0})
        self.inventory.delete(item["id"])
        state = self.inventory.snapshot()
        self.assertEqual(state["components"], [])
        self.assertEqual(len(state["movements"]), 2)
        self.assertTrue(all(r["component_id"] is None for r in state["movements"]))
        self.inventory.restore(self.inventory.backup())
        self.assertEqual(self.inventory.snapshot(), state)

    def test_full_migration_preserves_all_fields_history_and_numbering(self):
        item = self.item(code="CUSTOM-001", package="0603", value="10k / 1%", manufacturer="制造商",
                         location="A柜/01格", unit="卷", supplier="供应商", datasheet_url="https://example.com/data.pdf",
                         notes="完整资料 <&>", unit_price=0.123456, category="电阻")
        self.inventory.stock(item["id"], {"kind": "in", "quantity": 4, "note": "采购到货"})
        self.inventory.stock(item["id"], {"kind": "out", "quantity": 3})
        removed = self.item(quantity=0)
        self.inventory.stock(removed["id"], {"kind": "in", "quantity": 2})
        self.inventory.stock(removed["id"], {"kind": "out", "quantity": 2})
        self.inventory.delete(removed["id"])
        backup = json.loads(json.dumps(self.inventory.backup(), ensure_ascii=False))
        target = Inventory(Path(self.temp.name) / "new-device" / "inventory.db")
        # The destination has a larger previous sequence; an import must replace it too.
        for _ in range(5):
            target.create({"name": "新设备原有物料", "model": "TEMP", "quantity": 1})
        target.restore(backup)
        self.assertEqual(target.snapshot(), self.inventory.snapshot())
        self.assertEqual(target.backup()["sequences"], backup["sequences"])
        self.assertTrue(any(row["component_id"] is None for row in target.snapshot()["movements"]))
        expected = self.item(quantity=0)
        actual = target.create({"name": "贴片电阻", "model": "RC0603-10K", "quantity": 0})
        self.assertEqual((actual["id"], actual["code"]), (expected["id"], expected["code"]))

    def test_import_preview_is_validated_without_changing_destination(self):
        self.item(location="A柜", category="电阻")
        backup = self.inventory.backup()
        self.item(model="目标设备已有物料")
        before = self.inventory.backup()
        preview = self.inventory.preview_import(backup)
        self.assertEqual(preview["incoming"], {"components": 1, "movements": 1, "categories": 1, "locations": 1, "projects": 0, "bom_items": 0})
        self.assertEqual(preview["current"]["components"], 2)
        self.assertEqual(preview["exported_at"], backup["exported_at"])
        for issue in ("balance", "sequence", "sequence_type", "version"):
            bad = copy.deepcopy(backup)
            if issue == "balance":
                bad["components"][0]["quantity"] += 1
            elif issue == "sequence":
                bad["sequences"]["components"] = 0
            elif issue == "sequence_type":
                bad["sequences"] = []
            else:
                bad["version"] = True
            for operation in (self.inventory.preview_import, self.inventory.restore):
                with self.subTest(issue=issue, operation=operation.__name__):
                    with self.assertRaises(ValidationError):
                        operation(bad)
        after = self.inventory.backup()
        for key in ("components", "movements", "sequences"):
            self.assertEqual(after[key], before[key])

    def test_legacy_backup_import_and_empty_full_migration(self):
        self.item()
        legacy = self.inventory.backup()
        legacy["version"] = 1
        del legacy["projects"], legacy["bom_items"]
        del legacy["sequences"]
        self.item()
        self.inventory.restore(legacy)
        self.assertEqual(self.inventory.backup()["sequences"], {"components": 1, "movements": 1, "projects": 0, "bom_items": 0})
        fresh = Inventory(Path(self.temp.name) / "empty.db")
        empty = fresh.backup()
        self.assertEqual(self.inventory.preview_import(empty)["incoming"]["components"], 0)
        self.inventory.restore(empty)
        self.assertEqual(self.inventory.snapshot(), {"components": [], "movements": []})
        self.assertEqual(self.inventory.backup()["sequences"], {"components": 0, "movements": 0, "projects": 0, "bom_items": 0})

    def test_input_validation_and_unique_code(self):
        for values in [{"quantity": True}, {"quantity": "NaN"}, {"unit_price": "Infinity"}, {"unit_price": -1}, {"datasheet_url": "javascript:alert(1)"}, {"model": ""}]:
            with self.assertRaises(ValidationError):
                self.item(**values)
        item = self.item(code="EC-00002")
        self.assertNotEqual(self.item()["code"], item["code"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.item(code="ec-00002")


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inventory = Inventory(Path(self.temp.name) / "inventory.db")
        self.server = GatewayTCPHarness(self.inventory)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}{GATEWAY_PREFIX}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, method=None, headers=None):
        payload = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.url + path, data=payload, method=method, headers={"Content-Type": "application/json", "X-Trim-Userid": "1000", "X-Trim-Isadmin": "true", **(headers or {})})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as response:
            return response.code, response.read(), response.headers

    def test_http_routes_and_exports(self):
        self.assertEqual(self.request("/")[0], 200)
        self.assertEqual(self.request("/app.js")[0], 200)
        self.assertEqual(self.request("/style.css")[0], 200)
        self.assertEqual(self.request("/../app.py")[0], 404)
        status, content, _ = self.request("/api/components", {"name": "=1+1", "model": "测试型号", "quantity": 5})
        self.assertEqual(status, 201)
        item = json.loads(content)
        status, _, _ = self.request(f"/api/components/{item['id']}/stock", {"kind": "out", "quantity": 6})
        self.assertEqual(status, 400)
        status, content, headers = self.request("/api/export.csv")
        self.assertEqual(status, 200)
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn("'=1+1", content.decode("utf-8-sig"))
        self.assertIn("attachment", headers["Content-Disposition"])
        backup = json.loads(self.request("/api/backup")[1])
        self.assertEqual(self.request("/api/restore", backup)[0], 200)
        self.assertEqual(len(json.loads(self.request("/api/state")[1])["components"]), 1)

    def test_cross_origin_and_bad_json_rejected(self):
        self.assertEqual(self.request("/api/state", headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/state", headers={"Host": "[broken"})[0], 403)
        self.assertEqual(self.request("/api/components", [1, 2])[0], 400)
        self.assertEqual(self.request("/api/components", {"name": "x", "model": "y"}, headers={"Content-Type": "text/plain"})[0], 400)

    def test_whole_export_preview_and_import_routes(self):
        self.inventory.create({"name": "电容", "model": "10uF", "quantity": 10, "location": "B柜"})
        status, content, headers = self.request("/api/export.json")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        exported = json.loads(content)
        self.inventory.create({"name": "目标设备物料", "model": "TEMP", "quantity": 1})
        before = self.inventory.snapshot()
        status, content, _ = self.request("/api/import/preview", exported)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(content)["incoming"]["components"], 1)
        self.assertEqual(self.inventory.snapshot(), before)
        invalid = copy.deepcopy(exported)
        invalid["components"][0]["quantity"] += 1
        self.assertEqual(self.request("/api/import/preview", invalid)[0], 400)
        self.assertEqual(self.request("/api/import", invalid)[0], 400)
        self.assertEqual(self.inventory.snapshot(), before)
        self.assertEqual(self.request("/api/import", exported)[0], 200)
        self.assertEqual(len(self.inventory.snapshot()["components"]), 1)

    def test_project_bom_http_routes(self):
        import base64

        self.inventory.create({"name": "电阻", "model": "MPN-A", "package": "0603", "quantity": 3})
        status, content, _ = self.request("/api/projects", {"name": "采集板", "build_quantity": 2})
        self.assertEqual(status, 201)
        project_id = json.loads(content)["id"]
        payload = {"filename": "bom.csv", "content": base64.b64encode(b"Manufacturer Part,Quantity,Footprint\nMPN-A,2,R0603").decode()}
        base = f"/api/projects/{project_id}"
        self.assertEqual(self.request(base + "/bom/preview", payload)[0], 200)
        self.assertEqual(self.request(base + "/bom", payload)[0], 200)
        status, content, _ = self.request(base)
        self.assertEqual(status, 200)
        report = json.loads(content)
        self.assertEqual(report["rows"][0]["shortage"], 1)
        self.assertEqual(self.request(base + f"/items/{report['rows'][0]['id']}", {"component_id": 1}, "PUT")[0], 200)
        status, content, headers = self.request(base + "/shortages.csv")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn(b"MPN-A", content)
        self.assertEqual(self.request("/api/bom-template.csv")[0], 200)
        self.assertEqual(self.request(base, {"name": "采集板", "build_quantity": 1}, "PUT")[0], 200)
        self.assertTrue(json.loads(self.request(base)[1])["all_ready"])
        self.assertEqual(self.request(base, method="DELETE")[0], 200)
        self.assertEqual(self.request(base)[0], 404)


if __name__ == "__main__":
    unittest.main()
