"""Read-only component lookup with independent providers and partial-failure recovery."""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from catalog_parsers import parse_hqchip, parse_ickey

ENDPOINT = "https://lceda.cn/api/eda/product/search"
RESULT_LIMIT = 12
MAX_RESPONSE = 512 * 1024
MAX_HTML_RESPONSE = 2 * 1024 * 1024
MAX_COMBINED = 24
SOURCES = {
    "lcsc": {"name": "立创商城 · 立创 EDA", "base": "https://lceda.cn", "path": "/api/eda/product/search"},
    "hqchip": {"name": "华秋商城", "base": "https://www.hqchip.com", "path": "/search/"},
    "ickey": {"name": "云汉芯城", "base": "https://search.ickey.cn", "path": "/site/index.html"},
    "easyeda": {"name": "EasyEDA 国际（立创）", "base": "https://easyeda.com", "path": "/api/eda/product/search"},
}
_cache = OrderedDict()
_lock = threading.Lock()
_requests = threading.BoundedSemaphore(3)
_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="catalog")


class CatalogError(Exception):
    pass


def public_url(value, base=""):
    """Return a plain HTTPS URL, never markup, credentials or executable schemes."""
    if not isinstance(value, str) or not value.strip():
        return ""
    value = value.strip()
    if any(c.isspace() or ord(c) < 32 for c in value):
        return ""
    value = urljoin(base, value) if base else value
    try:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            return ""
    except ValueError:
        return ""
    return value if len(value) <= 2000 else ""


def normalize_product(raw):
    if not isinstance(raw, dict):
        return None
    code = raw.get("code", "")
    if not isinstance(code, str) or not re.fullmatch(r"C[0-9]{1,15}", code):
        return None

    def text(key, limit):
        value = raw.get(key)
        return value.strip()[:limit] if isinstance(value, str) else ""

    model = text("model", 160)
    if not model:
        return None
    image = raw.get("image")
    image_url = public_url(image.split("<$>")[0]) if isinstance(image, str) else ""
    price = None
    minimum = None
    # Chinese catalogue prices are CNY. Keep the quantity tier visible; do not
    # silently replace the user's actual purchase price with a volume quote.
    for tier in raw.get("priceList") or []:
        if not isinstance(tier, dict):
            continue
        try:
            number = float(tier["price"])
            start = int(tier["startNumber"])
            if math.isfinite(number) and 0 <= number <= 1_000_000_000 and 0 < start <= 1_000_000_000:
                price, minimum = round(number, 6), start
                break
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
    return {
        "lcsc": code, "model": model,
        "name": text("catalogName", 120) or text("name", 120) or model[:120],
        "category": text("catalogName", 60), "package": text("standard", 60),
        "value": text("name", 120), "manufacturer": text("brandName", 100),
        "unit": text("stockUnit", 16) or "个", "supplier": "立创商城",
        "datasheet_url": public_url(raw.get("pdfFileUrl"), "https://atta.szlcsc.com"),
        "image_url": image_url,
        "source_url": f"https://item.szlcsc.com/{int(raw['id'])}.html" if type(raw.get("id")) is int and raw["id"] > 0 else f"https://so.szlcsc.com/global.html?{urlencode({'k': code})}",
        "reference_price": price, "price_minimum": minimum,
    }


class _CatalogueRedirect(HTTPRedirectHandler):
    def __init__(self, host="lceda.cn"):
        self.hosts = {host}
        if host in ("www.ickey.cn", "search.ickey.cn"):
            self.hosts.update(("www.ickey.cn", "search.ickey.cn"))

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlparse(newurl).scheme != "https" or urlparse(newurl).netloc not in self.hosts:
            raise CatalogError("物料来源返回异常跳转，请稍后重试")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def normalize_international(raw):
    if not isinstance(raw, dict):
        return None
    images = raw.get("image")
    image = images[0].get("224x224", "") if isinstance(images, list) and images and isinstance(images[0], dict) else ""
    return {
        "lcsc": raw.get("number", ""), "supplier_part": raw.get("number", ""),
        "model": raw.get("mpn", ""), "name": raw.get("mpn", ""),
        "category": "", "package": raw.get("package", ""), "value": "",
        "manufacturer": raw.get("manufacturer", ""), "unit": "个",
        "image_url": image, "datasheet_url": "",
        "source_url": public_url(raw.get("url"), "https://www.lcsc.com"),
        # International quotes are not CNY. Do not put a USD quote in an RMB field.
        "reference_price": None, "price_minimum": None,
    }


def _sanitize_product(item, source):
    if not isinstance(item, dict) or not isinstance(item.get("model"), str) or not item["model"].strip():
        return None
    fields = {"lcsc": 64, "supplier_part": 100, "name": 120, "model": 160, "category": 60, "package": 60,
              "value": 120, "manufacturer": 100, "unit": 16}
    result = {key: item.get(key, "").strip()[:limit] if isinstance(item.get(key, ""), str) else "" for key, limit in fields.items()}
    if result["lcsc"] and not re.fullmatch(r"C[0-9]{1,15}", result["lcsc"]):
        return None
    result["supplier_part"] = result["supplier_part"] or result["lcsc"]
    result["name"] = result["name"] or result["model"][:120]
    result["unit"] = result["unit"] or "个"
    for field in ("source_url", "image_url", "datasheet_url"):
        result[field] = public_url(item.get(field), SOURCES[source]["base"])
    if len(result["datasheet_url"]) > 1000:
        result["datasheet_url"] = ""
    result.update(supplier="立创商城" if source in ("lcsc", "easyeda") else SOURCES[source]["name"],
                  provider_id=source, provider_name=SOURCES[source]["name"], reference_price=None, price_minimum=None)
    price, minimum = item.get("reference_price"), item.get("price_minimum")
    if type(price) in (int, float) and math.isfinite(price) and 0 <= price <= 1_000_000_000 and type(minimum) is int and 0 < minimum <= 1_000_000_000:
        result.update(reference_price=round(price, 6), price_minimum=minimum)
    return result


def _fetch_provider(keyword, source):
    config = SOURCES[source]
    url = config["base"] + config["path"]
    if source == "hqchip":
        url += quote(keyword, safe="") + ".html"
    else:
        url += "?" + urlencode({"keyword": keyword, "page": 1, "pageSize": RESULT_LIMIT})
    request = Request(url, headers={"Accept": "application/json,text/html", "User-Agent": "ElectronicsInventory/1.5 (component lookup)"})
    maximum = MAX_HTML_RESPONSE if source in ("hqchip", "ickey") else MAX_RESPONSE
    try:
        with build_opener(_CatalogueRedirect(urlparse(config["base"]).netloc)).open(request, timeout=5) as response:
            raw = response.read(maximum + 1)
        if len(raw) > maximum:
            raise CatalogError("搜索结果过大，请输入更具体的型号或立创编号")
        if source in ("hqchip", "ickey"):
            parser = parse_hqchip if source == "hqchip" else parse_ickey
            items = parser(raw.decode("utf-8"))
        else:
            data = json.loads(raw)
            result = data.get("result") if isinstance(data, dict) else None
            if not isinstance(data, dict) or data.get("code") not in (0, 200) or not isinstance(result, dict) or not isinstance(result.get("productList"), list):
                raise CatalogError("物料来源暂时无法提供搜索结果，请稍后重试")
            normalize = normalize_product if source == "lcsc" else normalize_international
            items = [normalize(item) for item in result["productList"][:RESULT_LIMIT]]
        products = []
        seen = set()
        for raw_item in items[:RESULT_LIMIT]:
            item = _sanitize_product(raw_item, source)
            key = (item["supplier_part"], item["model"], item["source_url"]) if item else None
            if item and key not in seen:
                products.append(item)
                seen.add(key)
        if items and not products:
            raise CatalogError("物料来源的数据格式已变化，请暂时手动录入")
        return products
    except HTTPError as error:
        message = "物料来源请求较多，请稍后重试" if error.code == 429 else "物料来源暂时不可用，请稍后重试或手动录入"
        raise CatalogError(message) from None
    except (URLError, TimeoutError, OSError):
        raise CatalogError("无法连接物料来源，请检查 NAS 外网连接后重试") from None
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise CatalogError("物料来源返回了异常数据，请稍后重试或手动录入") from None


def _search_uncached(keyword, source):
    selected = list(SOURCES) if source == "auto" else [source]
    futures = {name: _executor.submit(_fetch_provider, keyword, name) for name in selected}
    done, _ = wait(futures.values(), timeout=10)
    groups, statuses, warnings = [], [], []
    for name, future in futures.items():
        label = SOURCES[name]["name"]
        try:
            if future not in done:
                future.cancel()
                raise CatalogError("搜索超时，请稍后重试")
            products = future.result()
            groups.append(products)
            statuses.append({"id": name, "name": label, "status": "ok", "count": len(products)})
        except CatalogError as error:
            warnings.append(f"{label}：{error}")
            statuses.append({"id": name, "name": label, "status": "error", "count": 0})
        except Exception:
            # A changed page/parser must not take down the other providers.
            warnings.append(f"{label}：商品资料解析失败，请切换来源或稍后重试")
            statuses.append({"id": name, "name": label, "status": "error", "count": 0})
    if not groups:
        raise CatalogError("；".join(warnings) or "物料来源暂时不可用，请稍后重试")
    # Round-robin keeps an independent provider visible even when the first one
    # returns many results. LCSC's Chinese/international duplicate SKUs collapse.
    merged, seen = [], set()
    for index in range(RESULT_LIMIT):
        for group in groups:
            if index >= len(group):
                continue
            item = group[index]
            key = ("lcsc", item["lcsc"]) if item["lcsc"] else (item["provider_id"], item["supplier_part"], item["model"])
            if key not in seen:
                seen.add(key)
                merged.append(item)
            if len(merged) >= MAX_COMBINED:
                break
        if len(merged) >= MAX_COMBINED:
            break
    return {"source": "综合搜索" if source == "auto" else SOURCES[source]["name"], "products": merged,
            "limit": MAX_COMBINED if source == "auto" else RESULT_LIMIT, "sources": statuses, "warnings": warnings, "stale": False}


def search_products(keyword, source="auto"):
    if not isinstance(keyword, str):
        raise ValueError("请输入型号、规格关键词或立创编号")
    keyword = " ".join(keyword.split())
    if not 2 <= len(keyword) <= 100 or any(ord(c) < 32 for c in keyword):
        raise ValueError("搜索关键词需为 2 至 100 个字")
    try:
        keyword.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("搜索关键词包含无效字符") from None
    if not isinstance(source, str) or source not in ("auto", *SOURCES):
        raise ValueError("请选择有效的物料来源")
    key = (source, keyword.casefold())
    with _lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry[0] < (30 if entry[1].get("warnings") else 300):
            _cache.move_to_end(key)
            return copy.deepcopy(entry[1])
    if not _requests.acquire(blocking=False):
        raise CatalogError("正在搜索的请求较多，请稍后重试")
    try:
        try:
            result = _search_uncached(keyword, source)
        except CatalogError:
            if not entry or not entry[1]["products"] or time.monotonic() - entry[0] > 86400:
                raise
            result = copy.deepcopy(entry[1])
            result["stale"] = True
            for status in result["sources"]:
                status["status"] = "cached"
            result["warnings"] = ["联网来源暂时不可用，正在显示 24 小时内的缓存资料；请核对信息，参考价格已停用。"]
            for item in result["products"]:
                item.update(reference_price=None, price_minimum=None)
            return result
        with _lock:
            _cache[key] = (time.monotonic(), copy.deepcopy(result))
            _cache.move_to_end(key)
            while len(_cache) > 128:
                _cache.popitem(last=False)
        return result
    finally:
        _requests.release()
