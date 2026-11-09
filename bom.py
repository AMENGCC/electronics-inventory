"""Bounded, offline BOM readers and stock matching for LCEDA CSV/XLSX exports."""
from __future__ import annotations

import base64
import binascii
import csv
import io
import posixpath
import re
import unicodedata
import zipfile
from collections import defaultdict
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET

MAX_FILE = 5 * 1024 * 1024
MAX_ROWS = 10000
MAX_COLUMNS = 128
ALIASES = {
    "model": ("manufacturerpart", "manufacturerpartnumber", "mpn", "制造商编号", "制造商料号", "制造商型号", "model", "型号", "comment", "元件名称", "名称", "value", "值"),
    "quantity": ("quantity", "qty", "数量", "用量", "单板数量"),
    "package": ("footprint", "package", "封装"),
    "supplier_part": ("supplierpart", "supplierpartnumber", "lcscpart", "lcscpartnumber", "lcsc", "立创编号", "立创商城编号", "供应商编号", "供应商料号", "嘉立创编号"),
    "designators": ("designator", "designators", "reference", "references", "refdes", "位号"),
    "name": ("comment", "name", "元件名称", "名称", "description", "描述", "value", "值"),
    "unit": ("unit", "单位", "计量单位"),
}


class BomError(ValueError):
    pass


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold()


def header_key(value):
    return re.sub(r"[^\w\u4e00-\u9fff]", "", normalized(value)).replace("_", "")


def package_key(value):
    key = normalized(value)
    # LCEDA commonly names passive footprints R0603/C0603; inventory uses 0603.
    return re.sub(r"^[rcl](0201|0402|0603|0805|1206|1210|1812|2010|2512)$", r"\1", key)


def unit_key(value):
    key = normalized(value)
    return "piece" if key in {"个", "只", "片", "颗", "件", "pcs", "pc", "piece", "pieces"} else key


def positive(value, label):
    try:
        if isinstance(value, bool) or not re.fullmatch(r"\d+(?:\.0+)?", str(value).strip()):
            raise ValueError
        number = int(float(value))
        if not 1 <= number <= 1_000_000:
            raise ValueError
        return number
    except (ValueError, TypeError, OverflowError):
        raise BomError(f"{label}必须是 1 至 1000000 之间的整数")


def validate_row(raw):
    if not isinstance(raw, dict):
        raise BomError("BOM 行格式错误")
    result = {}
    for key, limit in (("model", 160), ("name", 120), ("package", 100), ("supplier_part", 100), ("designators", 4000), ("unit", 16)):
        value = raw.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise BomError(f"BOM 字段 {key} 无效或过长")
        result[key] = value.strip()
    if not result["model"] and not result["supplier_part"]:
        raise BomError("每行至少需要型号或立创/供应商编号")
    result["unit"] = result["unit"] or "个"
    result["quantity"] = positive(raw.get("quantity"), "BOM 数量")
    return result


def read_csv(data):
    encodings = ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    text = None
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None or "\x00" in text:
        raise BomError("CSV 编码无法识别，请另存为 UTF-8 CSV")
    if text.startswith("sep="):
        text = text.split("\n", 1)[-1]
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t")
        reader = csv.reader(io.StringIO(text), dialect)
    except csv.Error:
        delimiter = ","
        for line in text.splitlines()[:50]:
            found = False
            for candidate in ("\t", ",", ";"):
                try:
                    keys = {header_key(value) for value in next(csv.reader([line], delimiter=candidate))}
                except csv.Error:
                    continue
                if keys.intersection(ALIASES["quantity"]) and (keys.intersection(ALIASES["model"]) or keys.intersection(ALIASES["supplier_part"])):
                    delimiter, found = candidate, True
                    break
            if found:
                break
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = []
    try:
        for row in reader:
            if len(rows) >= MAX_ROWS + 60 or len(row) > MAX_COLUMNS:
                raise BomError("BOM 超过 10000 行或 128 列")
            rows.append(row)
    except csv.Error as error:
        raise BomError("CSV 格式错误或字段过长") from error
    return {"CSV": rows}


def read_xlsx(data):
    def xml(archive, name):
        raw = archive.read(name)
        if b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
            raise BomError("不支持包含 DTD 的工作簿")
        return ET.fromstring(raw)

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if len(archive.infolist()) > 500 or sum(m.file_size for m in archive.infolist()) > 32 * 1024 * 1024:
                raise BomError("工作簿解压后过大，请只导出 BOM 工作表")
            strings = []
            if "xl/sharedStrings.xml" in archive.namelist():
                strings = ["".join(si.itertext()) for si in xml(archive, "xl/sharedStrings.xml").findall("{*}si")]
            relationships = {r.get("Id"): r.get("Target") for r in xml(archive, "xl/_rels/workbook.xml.rels") if r.get("TargetMode") != "External"}
            result = {}
            workbook = xml(archive, "xl/workbook.xml")
            for sheet in workbook.findall("{*}sheets/{*}sheet"):
                if sheet.get("state", "visible") != "visible":
                    continue
                relationship = next((v for k, v in sheet.attrib.items() if k.endswith("}id")), None)
                target = relationships.get(relationship, "")
                name = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if not name.startswith("xl/") or ".." in PurePosixPath(name).parts:
                    raise BomError("工作表路径无效")
                rows = []
                for row in xml(archive, name).findall("{*}sheetData/{*}row"):
                    if len(rows) >= MAX_ROWS + 60:
                        raise BomError("BOM 超过 10000 行")
                    values = []
                    for cell in row.findall("{*}c"):
                        reference = re.match(r"([A-Z]+)", cell.get("r", ""))
                        column = 0
                        for letter in reference[1] if reference else "":
                            column = column * 26 + ord(letter) - 64
                        column = column or len(values) + 1
                        if column > MAX_COLUMNS:
                            raise BomError("BOM 超过 128 列")
                        value = cell.findtext("{*}v", "")
                        if cell.get("t") == "s":
                            value = strings[int(value)]
                        elif cell.get("t") == "inlineStr":
                            value = "".join(cell.find("{*}is").itertext())
                        while len(values) < column:
                            values.append("")
                        values[column - 1] = value
                    rows.append(values)
                result[sheet.get("name", "Sheet")] = rows
            return result
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError, TypeError, ValueError, AttributeError, RuntimeError, NotImplementedError) as error:
        if isinstance(error, BomError):
            raise
        raise BomError("XLSX 文件损坏或格式不受支持，请从 EDA 重新导出") from error


def parse_bom(filename, encoded, sheet_name=None):
    if not isinstance(filename, str) or len(filename) > 200 or not isinstance(encoded, str) or len(encoded) > (MAX_FILE * 4 // 3 + 8):
        raise BomError("BOM 文件无效或超过 5 MB")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise BomError("BOM 文件内容无效") from error
    if not data or len(data) > MAX_FILE:
        raise BomError("BOM 文件为空或超过 5 MB")
    extension = filename.rsplit(".", 1)[-1].lower()
    if extension not in ("csv", "tsv", "xlsx"):
        raise BomError("请导入 EDA 导出的 .xlsx 或 .csv 文件；旧 .xls 请另存为 .xlsx")
    sheets = read_xlsx(data) if extension == "xlsx" else read_csv(data)
    candidates = []
    for name, rows in sheets.items():
        if sheet_name and name != sheet_name:
            continue
        for position, row in enumerate(rows[:50]):
            keys = [header_key(value) for value in row]
            mapping = {}
            for field, aliases in ALIASES.items():
                mapping[field] = [keys.index(alias) for alias in aliases if alias in keys]
            if mapping["quantity"] and (mapping["model"] or mapping["supplier_part"]):
                candidates.append((name, rows, position, mapping))
                break
    if not candidates:
        raise BomError("未找到 BOM 表头：请在 EDA 导出中包含 Quantity/数量，以及 Manufacturer Part/型号或 Supplier Part/供应商编号")
    name, table, header, mapping = candidates[0]
    rows, errors, warnings = [], [], []
    for number, cells in enumerate(table[header + 1:], header + 2):
        if not any(str(cell).strip() for cell in cells):
            continue
        raw = {}
        for field, columns in mapping.items():
            raw[field] = next((str(cells[c]).strip() for c in columns if c < len(cells) and str(cells[c]).strip()), "")
        if not any(raw.values()):
            warnings.append(f"第 {number} 行未包含 BOM 字段，已跳过")
            continue
        # Ignore a trailing totals row, never an actual component missing its quantity.
        if raw["model"] in ("合计", "总计", "Total", "TOTAL") and not raw["supplier_part"]:
            warnings.append(f"第 {number} 行合计已跳过")
            continue
        try:
            rows.append(validate_row(raw))
        except BomError as error:
            errors.append(f"第 {number} 行：{error}")
    if len(rows) > MAX_ROWS:
        raise BomError("BOM 超过 10000 行")
    if not rows and not errors:
        raise BomError("BOM 没有物料行")
    if len(candidates) > 1 and not sheet_name:
        warnings.append("包含多个 BOM 工作表，请确认所选工作表")
    return {"filename": filename, "sheet": name, "sheets": list(sheets), "rows": rows,
            "errors": errors[:50], "error_count": len(errors), "warnings": warnings,
            "columns": {field: [table[header][c] for c in columns] for field, columns in mapping.items()}}


def check_stock(rows, components, build_quantity):
    remaining = {item["id"]: item["quantity"] for item in components}
    by_id = {item["id"]: item for item in components}
    by_model, by_part = defaultdict(list), defaultdict(list)
    for item in components:
        by_model[normalized(item["model"])].append(item)
        parts = {normalized(item.get("lcsc", ""))}
        if not item.get("lcsc"):
            parts.add(normalized(item["code"]))
        for part in parts - {""}:
            by_part[part].append(item)
    results = []
    for row in rows:
        needed = row["quantity"] * build_quantity
        candidates, reason = [], "未找到匹配的型号或立创编号"
        if row.get("match_mode") == "manual":
            item = by_id.get(row.get("component_id"))
            candidates = [item] if item else []
            reason = "手动匹配" if item else "原手动匹配物料已删除，请重新选择"
        else:
            if row["supplier_part"]:
                candidates = by_part.get(normalized(row["supplier_part"]), [])
            if not candidates and row["model"]:
                candidates = by_model.get(normalized(row["model"]), [])
                if row["supplier_part"]:
                    candidates = [item for item in candidates if not item.get("lcsc") or normalized(item["lcsc"]) == normalized(row["supplier_part"])]
            if candidates:
                reason = "型号 / 立创编号匹配"
        compatible = [item for item in candidates if unit_key(item["unit"]) == unit_key(row["unit"])]
        if candidates and not compatible:
            results.append({**row, "required": needed, "available": 0, "shortage": needed, "status": "review", "reason": "计量单位不同，请先统一库存单位", "candidates": [item["id"] for item in candidates]})
            continue
        candidates = compatible
        if candidates and row.get("match_mode", "auto") == "auto":
            if row["package"]:
                exact = [item for item in candidates if package_key(item["package"]) == package_key(row["package"])]
                if not exact:
                    results.append({**row, "required": needed, "available": 0, "shortage": needed, "status": "review", "reason": "封装不一致或库存未填写封装，请手动确认", "candidates": [item["id"] for item in candidates]})
                    continue
                candidates = exact
            identities = {(normalized(item["model"]), package_key(item["package"]), normalized(item["value"]), normalized(item["manufacturer"]), normalized(item.get("lcsc", ""))) for item in candidates}
            if len(identities) > 1:
                results.append({**row, "required": needed, "available": 0, "shortage": needed, "status": "review", "reason": "存在多个规格的候选物料，请手动选择", "candidates": [item["id"] for item in candidates]})
                continue
        available = sum(remaining[item["id"]] for item in candidates)
        used = min(needed, available)
        to_allocate = used
        for item in sorted(candidates, key=lambda item: item["id"]):
            take = min(to_allocate, remaining[item["id"]])
            remaining[item["id"]] -= take
            to_allocate -= take
        results.append({**row, "required": needed, "available": available, "shortage": needed - used,
                        "status": "ready" if used == needed else "short" if candidates else "missing",
                        "reason": reason, "candidates": [item["id"] for item in candidates]})
    counts = {status: sum(row["status"] == status for row in results) for status in ("ready", "short", "missing", "review")}
    return {"rows": results, "summary": counts, "all_ready": bool(results) and counts["ready"] == len(results)}
