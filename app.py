"""飞牛 fnOS Native 电子元器件物料管理。仅使用 Python 标准库。"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import re
import secrets
import signal
import socket
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from socketserver import TCPServer, ThreadingMixIn
from urllib.parse import urlparse

from bom import BomError, MAX_ROWS, check_stock, parse_bom, positive, validate_row
from catalog import CatalogError, public_url, search_products

ROOT = Path(__file__).resolve().parent
VERSION = 2
TABLES = ("components", "movements", "projects", "bom_items")
MAX_BODY = 10 * 1024 * 1024
MAX_IMPORT_BODY = 64 * 1024 * 1024
GATEWAY_PREFIX = "/app/electronics-inventory"
TEXT_FIELDS = {
    "code": 64, "name": 120, "model": 160, "category": 60,
    "package": 60, "value": 120, "manufacturer": 100, "location": 100,
    "unit": 16, "supplier": 100, "datasheet_url": 1000, "notes": 2000, "lcsc": 64,
    "image_url": 2000, "source_url": 2000,
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name TEXT NOT NULL, model TEXT NOT NULL, category TEXT NOT NULL,
    package TEXT NOT NULL, value TEXT NOT NULL, manufacturer TEXT NOT NULL,
    location TEXT NOT NULL, unit TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    minimum_stock INTEGER NOT NULL CHECK(minimum_stock >= 0),
    unit_price REAL NOT NULL CHECK(unit_price >= 0),
    supplier TEXT NOT NULL, datasheet_url TEXT NOT NULL, notes TEXT NOT NULL,
    lcsc TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    code TEXT NOT NULL, name TEXT NOT NULL, model TEXT NOT NULL, unit TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('initial','in','out','adjust')),
    delta INTEGER NOT NULL, before_quantity INTEGER NOT NULL CHECK(before_quantity >= 0),
    after_quantity INTEGER NOT NULL CHECK(after_quantity >= 0),
    note TEXT NOT NULL, created_at TEXT NOT NULL,
    CHECK(after_quantity = before_quantity + delta)
);
CREATE INDEX IF NOT EXISTS movements_component ON movements(component_id, id DESC);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL, notes TEXT NOT NULL,
    build_quantity INTEGER NOT NULL CHECK(build_quantity BETWEEN 1 AND 1000000),
    bom_filename TEXT NOT NULL DEFAULT '', bom_sheet TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bom_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    model TEXT NOT NULL, name TEXT NOT NULL, package TEXT NOT NULL,
    supplier_part TEXT NOT NULL, designators TEXT NOT NULL, unit TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK(quantity BETWEEN 1 AND 1000000),
    component_id INTEGER REFERENCES components(id) ON DELETE SET NULL,
    match_mode TEXT NOT NULL DEFAULT 'auto' CHECK(match_mode IN ('auto','manual')),
    UNIQUE(project_id, position)
);
"""


class ValidationError(Exception):
    pass


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def integer(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValidationError(f"{label}必须是整数")
    try:
        number = int(value)
        if float(value) != number or not minimum <= number <= 1_000_000_000:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValidationError(f"{label}必须是 {minimum} 至 1000000000 之间的整数")
    return number


def validate_component(data):
    result = {}
    for field, limit in TEXT_FIELDS.items():
        value = data.get(field, "")
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValidationError(f"字段 {field} 格式错误或超过 {limit} 字")
        result[field] = value.strip()
    for field, label in (("name", "物料名称"), ("model", "型号")):
        if not result[field]:
            raise ValidationError(f"请填写{label}")
    result["category"] = result["category"] or "未分类"
    result["unit"] = result["unit"] or "个"
    for field, label in (("image_url", "图片"), ("source_url", "商品来源")):
        if result[field] and not public_url(result[field]):
            raise ValidationError(f"{label}链接必须是不含账号密码的完整 HTTPS 地址")
    link = result["datasheet_url"]
    if link and (urlparse(link).scheme not in ("http", "https") or not urlparse(link).netloc):
        raise ValidationError("数据手册链接必须是完整的 http 或 https 地址")
    result["minimum_stock"] = integer(data.get("minimum_stock", 0), "安全库存")
    try:
        if isinstance(data.get("unit_price"), bool):
            raise ValueError
        price = float(data.get("unit_price", 0))
        if not math.isfinite(price) or not 0 <= price <= 1_000_000_000:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise ValidationError("单价必须是有效的非负数")
    result["unit_price"] = round(price, 6)
    return result


class Inventory:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            existing_columns = {row[1] for row in db.execute("PRAGMA table_info(components)")}
            for column in ("lcsc", "image_url", "source_url"):
                if column not in existing_columns:
                    db.execute(f"ALTER TABLE components ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def transaction(db):
        db.execute("BEGIN IMMEDIATE")

    @staticmethod
    def movement(db, item, kind, delta, before, note):
        db.execute(
            "INSERT INTO movements(component_id,code,name,model,unit,kind,delta,before_quantity,after_quantity,note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (item["id"], item["code"], item["name"], item["model"], item["unit"], kind, delta, before, before + delta, note, now()),
        )

    def create(self, data):
        item = validate_component(data)
        item["quantity"] = integer(data.get("quantity", 0), "初始库存")
        item["created_at"] = item["updated_at"] = now()
        with self.connect() as db:
            self.transaction(db)
            if not item["code"]:
                sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='components'").fetchone()
                number = (sequence[0] if sequence else 0) + 1
                while db.execute("SELECT 1 FROM components WHERE code=?", (f"EC-{number:05d}",)).fetchone():
                    number += 1
                item["code"] = f"EC-{number:05d}"
            keys = list(item)
            cursor = db.execute(f"INSERT INTO components({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", [item[k] for k in keys])
            item["id"] = cursor.lastrowid
            if item["quantity"]:
                self.movement(db, item, "initial", item["quantity"], 0, "建立物料初始库存")
        return item

    def update(self, item_id, data):
        item = validate_component(data)
        with self.connect() as db:
            self.transaction(db)
            existing = db.execute("SELECT * FROM components WHERE id=?", (item_id,)).fetchone()
            if not existing:
                raise LookupError("物料不存在或已删除")
            # Older clients do not send the optional media fields. An explicit
            # empty value still lets the current editor remove a link.
            for field in ("image_url", "source_url"):
                if field not in data:
                    item[field] = existing[field]
            if "quantity" in data and integer(data["quantity"], "库存") != existing["quantity"]:
                raise ValidationError("请通过出入库或盘点操作修改库存")
            item["code"] = item["code"] or existing["code"]
            item["updated_at"] = now()
            db.execute(f"UPDATE components SET {','.join(k+'=?' for k in item)} WHERE id=?", [*item.values(), item_id])
            return dict(db.execute("SELECT * FROM components WHERE id=?", (item_id,)).fetchone())

    def stock(self, item_id, data):
        kind = data.get("kind")
        if kind not in ("in", "out", "adjust"):
            raise ValidationError("无效的库存操作")
        amount = integer(data.get("quantity"), "数量", 0 if kind == "adjust" else 1)
        note = data.get("note", "")
        if not isinstance(note, str) or len(note.strip()) > 500:
            raise ValidationError("备注不能超过 500 字")
        with self.connect() as db:
            self.transaction(db)
            row = db.execute("SELECT * FROM components WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise LookupError("物料不存在或已删除")
            item = dict(row)
            before = item["quantity"]
            delta = amount - before if kind == "adjust" else amount * (1 if kind == "in" else -1)
            after = before + delta
            if after < 0:
                raise ValidationError(f"库存不足，当前仅有 {before} {item['unit']}")
            if after > 1_000_000_000:
                raise ValidationError("库存数量超过上限")
            if delta == 0:
                raise ValidationError("盘点数量与当前库存相同，无需调整")
            db.execute("UPDATE components SET quantity=?,updated_at=? WHERE id=?", (after, now(), item_id))
            self.movement(db, item, kind, delta, before, note.strip())
            return dict(db.execute("SELECT * FROM components WHERE id=?", (item_id,)).fetchone())

    def delete(self, item_id):
        with self.connect() as db:
            self.transaction(db)
            row = db.execute("SELECT quantity FROM components WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise LookupError("物料不存在或已删除")
            if row[0] != 0:
                raise ValidationError("请先将库存出库或盘点为 0，再删除物料；历史流水会保留")
            db.execute("DELETE FROM components WHERE id=?", (item_id,))

    def snapshot(self):
        with self.connect() as db:
            db.execute("BEGIN")
            return self.read_snapshot(db)

    @staticmethod
    def read_snapshot(db):
        return {
            "components": [dict(r) for r in db.execute("SELECT * FROM components ORDER BY id DESC")],
            "movements": [dict(r) for r in db.execute("SELECT * FROM movements ORDER BY id DESC")],
        }

    def backup(self):
        with self.connect() as db:
            # Export records and numbering sequences from the same transaction.
            db.execute("BEGIN")
            sequences = {table: 0 for table in TABLES}
            sequences.update({name: seq for name, seq in db.execute("SELECT name,seq FROM sqlite_sequence") if name in TABLES})
            return {"format": "electronics-inventory", "version": VERSION, "exported_at": now(),
                    "sequences": sequences, **self.read_snapshot(db),
                    "projects": [dict(row) for row in db.execute("SELECT * FROM projects ORDER BY id")],
                    "bom_items": [dict(row) for row in db.execute("SELECT * FROM bom_items ORDER BY id")]}

    @contextmanager
    def validated_backup(self, data):
        if not isinstance(data, dict) or data.get("format") != "electronics-inventory" or type(data.get("version")) is not int or data["version"] not in (1, VERSION):
            raise ValidationError("不是受支持的物料管理备份文件")
        if data.get("exported_at") is not None:
            self.valid_time(data["exported_at"])
        items, logs = data.get("components"), data.get("movements")
        if not isinstance(items, list) or not isinstance(logs, list):
            raise ValidationError("备份内容不完整")
        # Validate in a separate database before replacing any live records.
        staging = sqlite3.connect(":memory:")
        staging.execute("PRAGMA foreign_keys=ON")
        staging.executescript(SCHEMA)
        try:
            for raw in items:
                if not isinstance(raw, dict):
                    raise ValidationError("备份物料格式错误")
                item = validate_component(raw)
                if not item["code"]:
                    raise ValidationError("备份物料缺少编号")
                item.update(id=integer(raw.get("id"), "物料 ID", 1), quantity=integer(raw.get("quantity"), "库存"))
                for key in ("created_at", "updated_at"):
                    item[key] = self.valid_time(raw.get(key))
                self.insert_record(staging, "components", item)
            for raw in logs:
                if not isinstance(raw, dict):
                    raise ValidationError("备份流水格式错误")
                record = {key: raw.get(key) for key in ("id", "component_id", "code", "name", "model", "unit", "kind", "delta", "before_quantity", "after_quantity", "note", "created_at")}
                for key in ("id", "before_quantity", "after_quantity"):
                    record[key] = integer(record[key], key, 1 if key == "id" else 0)
                if record["component_id"] is not None:
                    record["component_id"] = integer(record["component_id"], "关联物料 ID", 1)
                delta = record["after_quantity"] - record["before_quantity"]
                if isinstance(record["delta"], bool) or record["delta"] != delta or not delta:
                    raise ValidationError("备份流水数量不一致")
                kind = record["kind"]
                if kind not in ("initial", "in", "out", "adjust") or (kind in ("initial", "in") and delta <= 0) or (kind == "out" and delta >= 0) or (kind == "initial" and record["before_quantity"] != 0):
                    raise ValidationError("备份流水类型与数量不一致")
                for key in ("code", "name", "model", "unit", "note"):
                    if not isinstance(record[key], str) or len(record[key]) > (500 if key == "note" else TEXT_FIELDS[key]):
                        raise ValidationError("备份流水文字格式错误")
                record["created_at"] = self.valid_time(record["created_at"])
                self.insert_record(staging, "movements", record)
            for item_id, quantity in staging.execute("SELECT id,quantity FROM components"):
                balance = 0
                for before, after in staging.execute("SELECT before_quantity,after_quantity FROM movements WHERE component_id=? ORDER BY id", (item_id,)):
                    if before != balance:
                        raise ValidationError("备份中的库存流水不连续")
                    balance = after
                if balance != quantity:
                    raise ValidationError("备份库存与流水余额不一致")
            projects, bom_items = data.get("projects", []), data.get("bom_items", [])
            if (data["version"] == VERSION and ("projects" not in data or "bom_items" not in data)) or not isinstance(projects, list) or not isinstance(bom_items, list):
                raise ValidationError("备份缺少完整项目及 BOM 数据")
            if data["version"] == 1 and (projects or bom_items):
                raise ValidationError("项目备份需要使用新版备份格式")
            for raw in projects:
                project = self.validate_project(raw)
                project.update(id=integer(raw.get("id"), "项目 ID", 1))
                for key in ("created_at", "updated_at"):
                    project[key] = self.valid_time(raw.get(key))
                for key in ("bom_filename", "bom_sheet"):
                    value = raw.get(key, "")
                    if not isinstance(value, str) or len(value) > 200:
                        raise ValidationError("项目 BOM 文件信息无效")
                    project[key] = value
                self.insert_record(staging, "projects", project)
            for raw in bom_items:
                row = validate_row(raw)
                row.update(id=integer(raw.get("id"), "BOM ID", 1), project_id=integer(raw.get("project_id"), "项目 ID", 1), position=integer(raw.get("position"), "BOM 行号", 1))
                row["component_id"] = None if raw.get("component_id") is None else integer(raw["component_id"], "物料 ID", 1)
                row["match_mode"] = raw.get("match_mode", "auto")
                if row["match_mode"] not in ("auto", "manual") or (row["match_mode"] == "auto" and row["component_id"] is not None):
                    raise ValidationError("BOM 匹配方式无效")
                self.insert_record(staging, "bom_items", row)
            if staging.execute("SELECT 1 FROM bom_items GROUP BY project_id HAVING COUNT(*)>?", (MAX_ROWS,)).fetchone():
                raise ValidationError("单个项目的 BOM 超过 10000 行")
            # Older JSON backups have no sequences; infer their minimum safe values.
            sequences = data.get("sequences", {table: staging.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0] for table in TABLES})
            if data["version"] == 1 and isinstance(sequences, dict):
                sequences = {"projects": 0, "bom_items": 0, **sequences}
            if not isinstance(sequences, dict) or set(sequences) != set(TABLES):
                raise ValidationError("自动编号序列格式错误")
            for table in TABLES:
                sequence = integer(sequences[table], "自动编号序列")
                maximum = staging.execute(f"SELECT COALESCE(MAX(id),0) FROM {table}").fetchone()[0]
                if sequence < maximum:
                    raise ValidationError("自动编号序列小于已有记录编号")
                staging.execute("DELETE FROM sqlite_sequence WHERE name=?", (table,))
                staging.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, sequence))
            yield staging
        finally:
            staging.close()

    @staticmethod
    def data_summary(db):
        return {
            "components": db.execute("SELECT COUNT(*) FROM components").fetchone()[0],
            "movements": db.execute("SELECT COUNT(*) FROM movements").fetchone()[0],
            "categories": db.execute("SELECT COUNT(DISTINCT category) FROM components").fetchone()[0],
            "locations": db.execute("SELECT COUNT(DISTINCT location) FROM components WHERE location<>''").fetchone()[0],
            "projects": db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
            "bom_items": db.execute("SELECT COUNT(*) FROM bom_items").fetchone()[0],
        }

    def preview_import(self, data):
        with self.validated_backup(data) as staging:
            incoming = self.data_summary(staging)
        with self.connect() as db:
            db.execute("BEGIN")
            current = self.data_summary(db)
        return {"incoming": incoming, "current": current, "exported_at": data.get("exported_at")}

    def restore(self, data):
        with self.validated_backup(data) as staging:
            with self.connect() as db:
                self.transaction(db)
                db.execute("DELETE FROM bom_items")
                db.execute("DELETE FROM projects")
                db.execute("DELETE FROM movements")
                db.execute("DELETE FROM components")
                for table in TABLES:
                    rows = staging.execute(f"SELECT * FROM {table}")
                    keys = [c[0] for c in rows.description]
                    for row in rows:
                        self.insert_record(db, table, dict(zip(keys, row)))
                db.execute("DELETE FROM sqlite_sequence")
                db.executemany("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", staging.execute("SELECT name,seq FROM sqlite_sequence"))
        return {table: len(data.get(table, [])) for table in TABLES}

    @staticmethod
    def validate_project(data):
        if not isinstance(data, dict):
            raise ValidationError("项目格式错误")
        result = {}
        for key, limit in (("name", 120), ("notes", 2000)):
            value = data.get(key, "")
            if not isinstance(value, str) or len(value.strip()) > limit:
                raise ValidationError("项目名称或备注无效")
            result[key] = value.strip()
        if not result["name"]:
            raise ValidationError("请填写项目名称")
        result["build_quantity"] = positive(data.get("build_quantity", 1), "计划制作数量")
        return result

    def list_projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT p.*, (SELECT COUNT(*) FROM bom_items b WHERE b.project_id=p.id) AS bom_count FROM projects p ORDER BY p.id DESC")]

    def save_project(self, data, project_id=None):
        project = self.validate_project(data)
        project["updated_at"] = now()
        with self.connect() as db:
            self.transaction(db)
            if project_id is None:
                project["created_at"] = project["updated_at"]
                keys = list(project)
                project_id = db.execute(f"INSERT INTO projects({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", list(project.values())).lastrowid
            else:
                self.require_project(db, project_id)
                db.execute(f"UPDATE projects SET {','.join(key+'=?' for key in project)} WHERE id=?", [*project.values(), project_id])
            return dict(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    @staticmethod
    def require_project(db, project_id):
        row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if row is None:
            raise LookupError("项目不存在或已删除")
        return dict(row)

    def project_report(self, project_id):
        with self.connect() as db:
            db.execute("BEGIN")
            project = self.require_project(db, project_id)
            rows = [dict(row) for row in db.execute("SELECT * FROM bom_items WHERE project_id=? ORDER BY position,id", (project_id,))]
            components = [dict(row) for row in db.execute("SELECT * FROM components")]
        return {"project": project, **check_stock(rows, components, project["build_quantity"])}

    def preview_bom(self, project_id, data):
        with self.connect() as db:
            self.require_project(db, project_id)
        return parse_bom(data.get("filename"), data.get("content"), data.get("sheet"))

    def import_bom(self, project_id, data):
        parsed = self.preview_bom(project_id, data)
        if parsed["error_count"]:
            raise BomError(f"BOM 有 {parsed['error_count']} 行错误，请修正后重新导入：{parsed['errors'][0]}")
        with self.connect() as db:
            self.transaction(db)
            self.require_project(db, project_id)
            db.execute("DELETE FROM bom_items WHERE project_id=?", (project_id,))
            for position, raw in enumerate(parsed["rows"], 1):
                self.insert_record(db, "bom_items", {**raw, "project_id": project_id, "position": position})
            db.execute("UPDATE projects SET bom_filename=?,bom_sheet=?,updated_at=? WHERE id=?", (parsed["filename"], parsed["sheet"], now(), project_id))
        return {"rows": len(parsed["rows"])}

    def match_bom_item(self, project_id, item_id, data):
        component_id = data.get("component_id")
        if component_id is not None:
            component_id = integer(component_id, "物料 ID", 1)
        with self.connect() as db:
            self.transaction(db)
            self.require_project(db, project_id)
            if component_id is not None and not db.execute("SELECT 1 FROM components WHERE id=?", (component_id,)).fetchone():
                raise LookupError("所选库存物料不存在")
            cursor = db.execute("UPDATE bom_items SET component_id=?,match_mode=? WHERE id=? AND project_id=?", (component_id, "manual" if component_id is not None else "auto", item_id, project_id))
            if cursor.rowcount != 1:
                raise LookupError("BOM 行不存在")
        return {"ok": True}

    def delete_project(self, project_id):
        with self.connect() as db:
            self.transaction(db)
            self.require_project(db, project_id)
            db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        return {"ok": True}

    @staticmethod
    def valid_time(value):
        try:
            if not isinstance(value, str):
                raise ValueError
            datetime.fromisoformat(value)
        except ValueError:
            raise ValidationError("备份时间格式错误")
        return value

    @staticmethod
    def insert_record(db, table, record):
        keys = list(record)
        db.execute(f"INSERT INTO {table}({','.join(keys)}) VALUES({','.join('?' for _ in keys)})", list(record.values()))


class GatewayServer(ThreadingMixIn, TCPServer):
    """Serve fnOS requests over a local Unix socket without a public TCP port."""

    address_family = getattr(socket, "AF_UNIX", None)
    daemon_threads = False

    def __init__(self, address, inventory, base_path=GATEWAY_PREFIX):
        if self.address_family is None:
            raise RuntimeError("当前 Python 不支持 Unix Socket；飞牛模式需在 Linux/fnOS 上运行")
        if not re.fullmatch(r"/app/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", base_path):
            raise ValueError("网关前缀必须为 /app/应用名称，可包含字母、数字、下划线及短横线")
        self.base_path = base_path
        self.inventory = inventory
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(str(address), Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def address_string(self):
        return "fnOS-gateway"

    def send(self, status, data, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: https:; connect-src 'self'; base-uri 'none'; frame-ancestors 'self'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def body(self, limit=MAX_BODY):
        if self.headers.get("Content-Type", "").split(";")[0].lower() != "application/json":
            raise ValidationError("请求必须使用 application/json 格式")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= limit:
                raise ValidationError(f"请求为空或超过 {limit // (1024 * 1024)} MB")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError
            return data
        except (ValueError, UnicodeDecodeError):
            raise ValidationError("请求 JSON 格式不正确")

    def safe_request(self):
        host = self.headers.get("Host", "")
        try:
            parsed = urlparse("//" + host)
            valid_host = parsed.hostname and not parsed.username and not parsed.password and not parsed.path and not parsed.query and not parsed.fragment
            parsed.port  # Validate malformed port numbers too.
        except ValueError:
            valid_host = False
        if not valid_host:
            self.send(403, {"error": "无效的访问地址"})
            return False
        # Production only listens on the Unix socket used by the fnOS gateway.
        user_ids = self.headers.get_all("X-Trim-Userid", [])
        admins = self.headers.get_all("X-Trim-Isadmin", [])
        if len(user_ids) != 1 or not re.fullmatch(r"[0-9]+", user_ids[0]):
            self.send(401, {"error": "请通过飞牛桌面登录后访问元件仓"})
            return False
        if admins != ["true"]:
            self.send(403, {"error": "元件仓仅允许 NAS 管理员访问"})
            return False
        tokens = self.headers.get_all("X-Inventory-CSRF", [])
        if tokens:
            # A secret from the authenticated, non-cacheable page remains valid
            # when the gateway rewrites Host or browser Fetch Metadata headers.
            if len(tokens) == 1 and secrets.compare_digest(tokens[0].encode("utf-8"), self.server.csrf_token.encode("ascii")):
                return True
            self.send(403, {"error": "页面会话已更新，请关闭并重新打开元件仓后重试"})
            return False
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            self.send(403, {"error": "不允许跨站请求"})
            return False
        origin = self.headers.get("Origin")
        hosts = {host}
        # Some gateway deployments preserve the external host in this header.
        forwarded_host = self.headers.get("X-Forwarded-Host", "")
        if forwarded_host and not any(c in forwarded_host for c in ",/\\@ \t\r\n"):
            hosts.add(forwarded_host)
        if origin and origin not in {f"{scheme}://{name}" for name in hosts for scheme in ("http", "https")}:
            self.send(403, {"error": "不允许跨站请求"})
            return False
        return True

    def route_path(self):
        path = urlparse(self.path).path
        prefix = self.server.base_path
        if prefix:
            if path == prefix:
                self.send_response(302)
                self.send_header("Location", prefix + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if not path.startswith(prefix + "/"):
                self.send(404, {"error": "地址不在应用网关路径内"})
                return None
            path = path[len(prefix):]
        return path

    def do_GET(self):
        if not self.safe_request():
            return
        path = self.route_path()
        if path is None:
            return
        if path == "/api/health":
            self.send(200, {"ok": True, "mode": "fnos"})
        elif path == "/api/state":
            self.send(200, self.server.inventory.snapshot())
        elif path in ("/api/backup", "/api/export.json"):
            self.send(200, self.server.inventory.backup(), filename=f"inventory-full-{datetime.now():%Y%m%d-%H%M%S}.json")
        elif path == "/api/projects":
            self.send(200, self.server.inventory.list_projects())
        elif path == "/api/bom-template.csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(["Comment", "Designator", "Footprint", "Quantity", "Manufacturer Part", "Supplier Part"])
            writer.writerow(["10k电阻", "R1,R2", "R0603", 2, "0603WAF1002T5E", "C25804"])
            self.send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "bom-template.csv")
        elif re.fullmatch(r"/api/projects/[0-9]+(?:/shortages.csv)?", path):
            try:
                report = self.server.inventory.project_report(int(path.split("/")[3]))
                if path.endswith("/shortages.csv"):
                    output = io.StringIO(newline="")
                    writer = csv.writer(output)
                    writer.writerow(["型号", "立创/供应商编号", "封装", "位号", "总需求", "可用库存", "缺口", "状态"])
                    labels = {"short": "数量不足", "missing": "未找到", "review": "待确认"}
                    for row in report["rows"]:
                        if row["status"] == "ready":
                            continue
                        values = [row["model"], row["supplier_part"], row["package"], row["designators"], row["required"], row["available"], row["shortage"] if row["status"] != "review" else "待确认", labels[row["status"]]]
                        writer.writerow(["'" + value if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
                    self.send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "bom-shortages.csv")
                else:
                    self.send(200, report)
            except (LookupError, ValidationError, BomError) as error:
                self.send(404 if isinstance(error, LookupError) else 400, {"error": str(error)})
        elif path == "/api/export.csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            columns = {"code": "物料编号", "name": "名称", "model": "型号", "lcsc": "立创编号", "category": "分类", "package": "封装", "value": "参数", "manufacturer": "制造商", "location": "存放位置", "quantity": "库存数量", "unit": "单位", "minimum_stock": "安全库存", "unit_price": "参考单价", "supplier": "供应商", "datasheet_url": "数据手册", "image_url": "图片链接", "source_url": "商品来源", "notes": "备注"}
            writer.writerow(columns.values())
            for item in self.server.inventory.snapshot()["components"]:
                # Prevent spreadsheet formula interpretation of user-entered strings.
                writer.writerow([("'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v) for v in (item[k] for k in columns)])
            self.send(200, b"\xef\xbb\xbf" + output.getvalue().encode("utf-8"), "text/csv; charset=utf-8", "inventory.csv")
        else:
            assets = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"), "/catalog.js": ("catalog.js", "text/javascript; charset=utf-8"), "/projects.js": ("projects.js", "text/javascript; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path not in assets:
                self.send(404, {"error": "地址不存在"})
                return
            filename, content_type = assets[path]
            content = (ROOT / "static" / filename).read_bytes()
            if filename == "index.html":
                content = content.replace(b"__INVENTORY_BASE_PATH__", self.server.base_path.encode("ascii"))
                content = content.replace(b"__INVENTORY_CSRF_TOKEN__", self.server.csrf_token.encode("ascii"))
            self.send(200, content, content_type)

    def mutate(self):
        if not self.safe_request():
            return
        route = self.route_path()
        if route is None:
            return
        try:
            path = route.strip("/").split("/")
            inventory = self.server.inventory
            if self.command == "POST" and path == ["api", "catalog", "search"]:
                data = self.body()
                try:
                    result = search_products(data.get("keyword"), data.get("source", "auto"))
                except ValueError as error:
                    raise ValidationError(str(error)) from None
                self.send(200, result)
                return
            if self.command == "POST" and path == ["api", "components"]:
                result = inventory.create(self.body())
                self.send(201, result)
                return
            if self.command == "POST" and path == ["api", "projects"]:
                self.send(201, inventory.save_project(self.body()))
                return
            if len(path) >= 3 and path[:2] == ["api", "projects"] and path[2].isdigit():
                project_id = int(path[2])
                if len(path) == 3 and self.command == "PUT":
                    self.send(200, inventory.save_project(self.body(), project_id))
                    return
                if len(path) == 3 and self.command == "DELETE":
                    self.send(200, inventory.delete_project(project_id))
                    return
                if self.command == "POST" and path[3:] in (["bom"], ["bom", "preview"]):
                    method = inventory.preview_bom if len(path) == 5 else inventory.import_bom
                    self.send(200, method(project_id, self.body()))
                    return
                if len(path) == 5 and path[3] == "items" and path[4].isdigit() and self.command == "PUT":
                    self.send(200, inventory.match_bom_item(project_id, int(path[4]), self.body()))
                    return
            if self.command == "POST" and path == ["api", "import", "preview"]:
                self.send(200, inventory.preview_import(self.body(MAX_IMPORT_BODY)))
                return
            if self.command == "POST" and path in (["api", "restore"], ["api", "import"]):
                self.send(200, inventory.restore(self.body(MAX_IMPORT_BODY)))
                return
            if len(path) in (3, 4) and path[:2] == ["api", "components"] and path[2].isdigit():
                item_id = int(path[2])
                if self.command == "PUT" and len(path) == 3:
                    self.send(200, inventory.update(item_id, self.body()))
                    return
                if self.command == "DELETE" and len(path) == 3:
                    inventory.delete(item_id)
                    self.send(200, {"ok": True})
                    return
                if self.command == "POST" and path[3:] == ["stock"]:
                    self.send(200, inventory.stock(item_id, self.body()))
                    return
            self.send(404, {"error": "接口不存在"})
        except CatalogError as error:
            self.send(502, {"error": str(error)})
        except (ValidationError, BomError) as error:
            self.send(400, {"error": str(error)})
        except LookupError as error:
            self.send(404, {"error": str(error)})
        except sqlite3.IntegrityError as error:
            message = "物料编号已存在，请使用其他编号" if "components.code" in str(error) else "数据约束校验失败，请检查备份文件"
            self.send(409, {"error": message})
        except sqlite3.OperationalError:
            self.send(503, {"error": "数据库暂时不可用，请稍后重试并检查目录写入权限"})

    do_POST = mutate
    do_PUT = mutate
    do_DELETE = mutate


def main():
    parser = argparse.ArgumentParser(description="飞牛 fnOS Native 电子元器件物料管理")
    parser.add_argument("--db", type=Path, required=True, help="飞牛应用持久化数据库路径")
    parser.add_argument("--socket", type=Path, required=True, help="飞牛网关 Unix Socket 路径")
    parser.add_argument("--gateway-prefix", default=GATEWAY_PREFIX)
    args = parser.parse_args()
    inventory = Inventory(args.db)
    try:
        server = GatewayServer(args.socket, inventory, args.gateway_prefix)
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"启动失败：{error}\n")
    print(f"Electronic Inventory: Unix Socket {args.socket} ({args.gateway_prefix}/)\nDatabase: {args.db.resolve()}", flush=True)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
