import base64
import copy
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import Inventory, SCHEMA
from bom import BomError, parse_bom


def csv_file(text, encoding="utf-8-sig", filename="bom.csv"):
    return {"filename": filename, "content": base64.b64encode(text.encode(encoding)).decode()}


def xlsx_file(sheets, shared=False):
    """Small OOXML fixture with independently supplied cells and sheet names."""
    output = io.BytesIO()
    strings = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>' + ''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, (name, _) in enumerate(sheets, 1)) + '</sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + ''.join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/>' for i in range(1, len(sheets) + 1)) + '</Relationships>')
        for index, (_, rows) in enumerate(sheets, 1):
            parts = []
            for number, row in enumerate(rows, 1):
                cells = []
                for column, value in enumerate(row):
                    reference = f"{chr(65 + column)}{number}"
                    if isinstance(value, int):
                        cells.append(f'<c r="{reference}"><v>{value}</v></c>')
                    elif shared:
                        strings.append(str(value))
                        cells.append(f'<c r="{reference}" t="s"><v>{len(strings)-1}</v></c>')
                    else:
                        cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
                parts.append(f'<row r="{number}">' + ''.join(cells) + '</row>')
            archive.writestr(f"xl/worksheets/sheet{index}.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(parts) + '</sheetData></worksheet>')
        if shared:
            archive.writestr("xl/sharedStrings.xml", '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">' + ''.join('<si><t>' + escape(value) + '</t></si>' for value in strings) + '</sst>')
    return {"filename": "lceda.xlsx", "content": base64.b64encode(output.getvalue()).decode()}


class BomReaderTest(unittest.TestCase):
    def test_english_csv_and_utf16_lceda_tsv(self):
        for encoding, separator in (("utf-8-sig", ","), ("utf-16", "\t")):
            rows = [["No.", "Quantity", "Comment", "Designator", "Footprint", "Manufacturer Part", "Supplier Part"],
                    ["1", "2", "10k", "R1 R2", "R0603", "0603WAF1002T5E", "C25804"]]
            text = "\n".join(separator.join(row) for row in rows)
            parsed = parse_bom(**{ "filename": "eda.csv", "encoded": csv_file(text, encoding)["content"] })
            self.assertEqual(parsed["error_count"], 0)
            self.assertEqual(parsed["rows"][0]["model"], "0603WAF1002T5E")
            self.assertEqual(parsed["rows"][0]["quantity"], 2)
            self.assertEqual(parsed["rows"][0]["supplier_part"], "C25804")

    def test_chinese_gb_csv_and_invalid_quantities_are_reported(self):
        payload = csv_file("型号,数量,封装,位号,供应商编号\n电阻,2.0,0603,R1 R2,C25804\n电容,-1,0603,C1,C100\nIC,1.5,SOP8,U1,C200", "gb18030")
        parsed = parse_bom(payload["filename"], payload["content"])
        self.assertEqual(len(parsed["rows"]), 1)
        self.assertEqual(parsed["error_count"], 2)
        self.assertIn("第 3 行", parsed["errors"][0])

    def test_tsv_with_title_and_price_only_footer(self):
        payload = csv_file("采集板 BOM\nManufacturer Part\tQuantity\tFootprint\tPrice\nMPN-A\t2\tR0603\t1.5\n\t\t\t3.0", "utf-16")
        parsed = parse_bom(payload["filename"], payload["content"])
        self.assertEqual(parsed["error_count"], 0)
        self.assertEqual(len(parsed["rows"]), 1)
        self.assertTrue(parsed["warnings"])

    def test_xlsx_shared_and_inline_strings_with_cover_and_title(self):
        rows = [["演示项目 BOM"], [], ["Manufacturer Part", "Quantity", "Footprint", "Designator", "Supplier Part"], ["MPN-A", 3, "R0603", "R1,R2,R3", "C25804"]]
        for shared in (False, True):
            payload = xlsx_file([("封面", [["cover"]]), ("BOM", rows)], shared)
            parsed = parse_bom(payload["filename"], payload["content"])
            self.assertEqual(parsed["sheet"], "BOM")
            self.assertEqual(parsed["rows"][0]["quantity"], 3)
            self.assertEqual(parsed["rows"][0]["designators"], "R1,R2,R3")

    def test_rejects_bad_files_and_missing_required_headers(self):
        for filename, data in (("old.xls", b"legacy"), ("bad.xlsx", b"bad zip"), ("data.csv", b"Name,Footprint\nR,0603")):
            with self.subTest(filename=filename), self.assertRaises(BomError):
                parse_bom(filename, base64.b64encode(data).decode())
        with self.assertRaises(BomError):
            parse_bom("bom.csv", "not base64!")


class ProjectBomTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.inventory = Inventory(Path(self.temp.name) / "inventory.db")
        self.project = self.inventory.save_project({"name": "测试电路板", "build_quantity": 1})

    def tearDown(self):
        self.temp.cleanup()

    def item(self, **values):
        return self.inventory.create({"name": "电阻", "model": "MPN-A", "package": "0603", "quantity": 10, **values})

    def import_rows(self, text):
        payload = csv_file("Manufacturer Part,Quantity,Footprint,Designator,Supplier Part\n" + text)
        self.inventory.import_bom(self.project["id"], payload)
        return self.inventory.project_report(self.project["id"])

    def test_repeated_rows_share_inventory_and_build_quantity_multiplies(self):
        self.item(location="A柜")
        self.item(location="B柜", quantity=4)
        before = self.inventory.snapshot()
        report = self.import_rows("MPN-A,8,R0603,R1,\nMPN-A,8,R0603,R2,")
        self.assertEqual([row["status"] for row in report["rows"]], ["ready", "short"])
        self.assertEqual([row["available"] for row in report["rows"]], [14, 6])
        self.assertEqual(report["rows"][1]["shortage"], 2)
        self.assertEqual(self.inventory.snapshot(), before)
        self.inventory.save_project({"name": "测试电路板", "build_quantity": 2}, self.project["id"])
        report = self.inventory.project_report(self.project["id"])
        self.assertEqual([row["required"] for row in report["rows"]], [16, 16])
        self.assertEqual([row["shortage"] for row in report["rows"]], [2, 16])

    def test_lcsc_matching_conflicts_ambiguity_and_units(self):
        item = self.item(lcsc="C25804")
        report = self.import_rows("GENERIC,2,R0603,R1,C25804\nMPN-A,1,R0603,R2,C99999\nUNKNOWN,1,SOP8,U1,")
        self.assertEqual([row["status"] for row in report["rows"]], ["ready", "missing", "missing"])
        self.assertEqual(report["rows"][0]["candidates"], [item["id"]])
        report = self.import_rows("MPN-A,1,0805,R1,")
        self.assertEqual(report["rows"][0]["status"], "review")
        self.item(value="不同精度")
        report = self.import_rows("MPN-A,1,R0603,R1,")
        self.assertEqual(report["rows"][0]["status"], "review")
        self.item(model="ROLL", unit="卷")
        report = self.import_rows("ROLL,1,0603,R1,")
        self.assertEqual(report["rows"][0]["status"], "review")

    def test_manual_match_deleted_component_and_live_stock(self):
        item = self.item()
        report = self.import_rows("ALIAS,7,R0603,R1,")
        row = report["rows"][0]
        self.inventory.match_bom_item(self.project["id"], row["id"], {"component_id": item["id"]})
        self.assertTrue(self.inventory.project_report(self.project["id"])["all_ready"])
        self.inventory.stock(item["id"], {"kind": "out", "quantity": 5})
        self.assertEqual(self.inventory.project_report(self.project["id"])["rows"][0]["shortage"], 2)
        self.inventory.stock(item["id"], {"kind": "adjust", "quantity": 0})
        self.inventory.delete(item["id"])
        self.assertEqual(self.inventory.project_report(self.project["id"])["rows"][0]["status"], "missing")

    def test_bad_reimport_keeps_previous_bom_and_preview_never_changes_data(self):
        report = self.import_rows("MPN-A,2,0603,R1,")
        before = self.inventory.backup()
        bad = csv_file("型号,数量\nMPN-A,not-a-number")
        self.assertEqual(self.inventory.preview_bom(self.project["id"], bad)["error_count"], 1)
        with self.assertRaises(BomError):
            self.inventory.import_bom(self.project["id"], bad)
        self.assertEqual(self.inventory.project_report(self.project["id"])["rows"], report["rows"])
        self.assertEqual(self.inventory.backup()["sequences"], before["sequences"])

    def test_project_bom_and_manual_links_survive_full_migration(self):
        item = self.item(lcsc="C25804")
        report = self.import_rows("ALIAS,2,R0603,R1,")
        self.inventory.match_bom_item(self.project["id"], report["rows"][0]["id"], {"component_id": item["id"]})
        backup = json.loads(json.dumps(self.inventory.backup()))
        target = Inventory(Path(self.temp.name) / "new-device.db")
        preview = target.preview_import(backup)
        self.assertEqual(preview["incoming"]["projects"], 1)
        self.assertEqual(preview["incoming"]["bom_items"], 1)
        target.restore(backup)
        self.assertEqual(target.project_report(self.project["id"]), self.inventory.project_report(self.project["id"]))
        bad = copy.deepcopy(backup)
        bad["bom_items"][0]["project_id"] = 999
        with self.assertRaises(sqlite3.IntegrityError):
            target.restore(bad)
        self.assertEqual(target.backup()["projects"], backup["projects"])
        before = self.inventory.snapshot()
        self.inventory.delete_project(self.project["id"])
        self.assertEqual(self.inventory.list_projects(), [])
        self.assertEqual(self.inventory.snapshot(), before)
        self.assertEqual(self.inventory.backup()["bom_items"], [])

    def test_existing_database_adds_lcsc_column_without_losing_stock(self):
        old_path = Path(self.temp.name) / "old-version.db"
        old_schema = SCHEMA.replace("    lcsc TEXT NOT NULL DEFAULT '',\n", "")
        with sqlite3.connect(old_path) as db:
            db.executescript(old_schema)
            db.execute("INSERT INTO components(code,name,model,category,package,value,manufacturer,location,unit,quantity,minimum_stock,unit_price,supplier,datasheet_url,notes,created_at,updated_at) VALUES('EC-001','old','MPN-A','IC','SOP8','','','A','个',5,1,0,'','','','2026-11-08T10:00:00+08:00','2026-11-08T10:00:00+08:00')")
        migrated = Inventory(old_path)
        item = migrated.snapshot()["components"][0]
        self.assertEqual(item["quantity"], 5)
        self.assertEqual(item["lcsc"], "")
        self.assertEqual(migrated.list_projects(), [])


if __name__ == "__main__":
    unittest.main()
