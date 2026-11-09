import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import catalog
from catalog_parsers import parse_hqchip, parse_ickey
from app import Inventory, ValidationError


def product(**changes):
    return {"id": 26547, "code": "C25804", "model": "0603WAF1002T5E", "name": "厚膜电阻 10kΩ ±1% 100mW",
            "catalogName": "贴片电阻", "standard": "0603", "brandName": "UNI-ROYAL(厚声)", "stockUnit": "个",
            "image": "https://alimg.szlcsc.com/front.jpg<$>https://alimg.szlcsc.com/back.jpg",
            "pdfFileUrl": "/upload/public/pdf/resistor.pdf",
            "priceList": [{"startNumber": 100, "price": 0.0133}], **changes}


class Response:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, size):
        return self.data[:size]


class CatalogTest(unittest.TestCase):
    def setUp(self):
        with catalog._lock:
            catalog._cache.clear()

    def test_normalizes_catalogue_fields_without_importing_stock_or_unmarked_tier_price(self):
        item = catalog.normalize_product(product(stockNumber=50000, hidden_key="not-a-component-field"))
        self.assertEqual(item["lcsc"], "C25804")
        self.assertEqual(item["package"], "0603")
        self.assertEqual(item["image_url"], "https://alimg.szlcsc.com/front.jpg")
        self.assertEqual(item["datasheet_url"], "https://atta.szlcsc.com/upload/public/pdf/resistor.pdf")
        self.assertEqual((item["reference_price"], item["price_minimum"]), (0.0133, 100))
        self.assertNotIn("quantity", item)
        self.assertNotIn("unit_price", item)
        self.assertNotIn("hidden_key", item)

    def test_untrusted_urls_and_malformed_products(self):
        for url in ("javascript:alert(1)", "data:image/svg+xml,test", "https://name:password@site.example/p.jpg", "http://site.example/p.jpg", "https://site.example:bad/", "https://site.example/a\nb"):
            self.assertEqual(catalog.public_url(url), "")
        self.assertIsNone(catalog.normalize_product(product(code="../../../bad")))
        self.assertIsNone(catalog.normalize_product(product(model=None)))
        item = catalog.normalize_product(product(image="javascript:alert(1)", pdfFileUrl="//user:pass@evil.example/x", priceList=[{"price": "nan", "startNumber": 100}]))
        self.assertEqual(item["image_url"], "")
        self.assertEqual(item["datasheet_url"], "")
        self.assertIsNone(item["reference_price"])

    def test_search_limits_payload_and_caches_without_shared_mutable_results(self):
        response = Response({"code": 200, "result": {"productList": [product(), product()]}})
        with patch("catalog.build_opener") as opener:
            opener.return_value.open.return_value = response
            first = catalog.search_products("  C25804  ", "lcsc")
            self.assertEqual(len(first["products"]), 1)
            first["products"].clear()
            second = catalog.search_products("c25804", "lcsc")
            self.assertEqual(len(second["products"]), 1)
            opener.return_value.open.assert_called_once()
            request = opener.return_value.open.call_args.args[0]
            self.assertTrue(request.full_url.startswith(catalog.ENDPOINT + "?"))
        for keyword in (None, [], "x", "x" * 101, "ab\0cd"):
            with self.assertRaises(ValueError):
                catalog.search_products(keyword)

    def test_network_and_upstream_failures_are_not_empty_success_or_raw_errors(self):
        for error in (URLError("private upstream detail"), TimeoutError(), HTTPError(catalog.ENDPOINT, 429, "rate limit", {}, None)):
            with patch("catalog.build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(catalog.CatalogError) as caught:
                    catalog.search_products("STM32F103", "lcsc")
                self.assertNotIn("private upstream detail", str(caught.exception))
        for data in (b"<html>login</html>", b"x" * (catalog.MAX_RESPONSE + 1), [], {"code": 200, "result": {"productList": [None]}}, {"code": 403}):
            with patch("catalog.build_opener") as opener:
                opener.return_value.open.return_value = Response(data)
                with self.assertRaises(catalog.CatalogError):
                    catalog.search_products("STM32F103", "lcsc")

    def test_independent_sources_survive_lcsc_failure_and_keep_supplier_codes_separate(self):
        def fetch(keyword, source):
            if source in ("lcsc", "easyeda"):
                raise catalog.CatalogError("来源不可用")
            return [catalog._sanitize_product({"model": "PART-123", "name": "测试 MCU", "supplier_part": "G123" if source == "hqchip" else "PART-123", "reference_price": 3.5, "price_minimum": 1}, source)]
        with patch("catalog._fetch_provider", side_effect=fetch):
            result = catalog.search_products("PART-123")
        self.assertEqual({p["provider_id"] for p in result["products"]}, {"hqchip", "ickey"})
        self.assertTrue(all(p["lcsc"] == "" for p in result["products"]))
        self.assertEqual(len(result["warnings"]), 2)
        self.assertEqual(sum(s["status"] == "ok" for s in result["sources"]), 2)

    def test_international_duplicate_is_removed_but_independent_supplier_offers_remain(self):
        def fetch(keyword, source):
            item = {"model": "PART-123", "lcsc": "C123" if source in ("lcsc", "easyeda") else "", "supplier_part": "P123"}
            return [catalog._sanitize_product(item, source)]
        with patch("catalog._fetch_provider", side_effect=fetch):
            result = catalog.search_products("PART-123")
            manual = catalog.search_products("PART-123", "hqchip")
        self.assertEqual(len(result["products"]), 3)
        self.assertEqual(len(manual["products"]), 1)
        self.assertEqual(manual["products"][0]["provider_id"], "hqchip")
        with self.assertRaises(ValueError):
            catalog.search_products("PART-123", "https://internal.example")

    def test_all_sources_unavailable_uses_recent_stale_cache_without_prices(self):
        item = catalog._sanitize_product(catalog.normalize_product(product()), "lcsc")
        with patch("catalog._fetch_provider", return_value=[item]):
            catalog.search_products("C25804", "lcsc")
        key = ("lcsc", "c25804")
        entry = catalog._cache[key]
        catalog._cache[key] = (time.monotonic() - 400, entry[1])
        with patch("catalog._fetch_provider", side_effect=catalog.CatalogError("断网")):
            stale = catalog.search_products("C25804", "lcsc")
            self.assertTrue(stale["stale"])
            self.assertIsNone(stale["products"][0]["reference_price"])
            self.assertEqual(stale["sources"][0]["status"], "cached")
            self.assertEqual(catalog._cache[key][1]["products"][0]["reference_price"], 0.0133)
            catalog._cache[key] = (time.monotonic() - 86401, entry[1])
            with self.assertRaises(catalog.CatalogError):
                catalog.search_products("C25804", "lcsc")

    def test_international_prices_are_never_interpreted_as_rmb(self):
        item = catalog.normalize_international({"mpn": "TEST", "number": "C123", "manufacturer": "Brand", "price": [[1, "12.34"]], "image": [{"224x224": "https://assets.lcsc.com/test.jpg"}]})
        self.assertIsNone(item["reference_price"])
        self.assertEqual(item["image_url"], "https://assets.lcsc.com/test.jpg")

    def test_unexpected_provider_parser_failure_is_isolated(self):
        def fetch(keyword, source):
            if source == "lcsc":
                raise RuntimeError("internal parser details")
            return [catalog._sanitize_product({"model": "TEST123", "supplier_part": "PART123"}, source)]
        with patch("catalog._fetch_provider", side_effect=fetch):
            result = catalog.search_products("TEST123")
        self.assertEqual(len(result["products"]), 3)
        self.assertNotIn("internal parser details", " ".join(result["warnings"]))
        self.assertTrue(any("解析失败" in warning for warning in result["warnings"]))


class PublicMarkupTest(unittest.TestCase):
    def test_hqchip_extracts_only_product_card_and_preserves_explicit_cny_tier(self):
        html = '''<html><a class="model-link">HOT-ADVERT</a><article class="product-card">
        <a class="model-link" href="https://item.hqchip.com/123.html"><h3 class="product-model" title="TEST123"><em>TEST</em>123</h3></a>
        <a class="brand-link">测试品牌</a><a class="category-link"><span>微控制器</span></a>
        <div class="info-row"><span class="info-label">封装：</span><span class="info-text">LQFP-48</span></div>
        <div class="info-row"><span class="info-label">编号：</span><span class="info-text">G123</span></div>
        <span class="desc-value">32bit MCU</span><img class="product-image" src="https://file.huaqiu.com/image.jpg">
        <a class="datasheet-link" href="//file.huaqiu.com/test.pdf">手册</a>
        <table class="price-table"><tr><td class="moq-cell">10+</td><td class="price-cell">¥2.35</td></tr></table>
        </article></html>'''
        result = parse_hqchip(html)
        self.assertEqual(len(result), 1)
        self.assertEqual((result[0]["model"], result[0]["package"], result[0]["supplier_part"]), ("TEST123", "LQFP-48", "G123"))
        self.assertEqual((result[0]["reference_price"], result[0]["price_minimum"]), (2.35, 10))
        self.assertEqual(catalog._sanitize_product(result[0], "hqchip")["datasheet_url"], "https://file.huaqiu.com/test.pdf")
        self.assertEqual(parse_hqchip("<title>搜索结果 | 华秋商城</title><div>没有找到商品</div>"), [])
        with self.assertRaises(ValueError):
            parse_hqchip("<html>请登录或验证</html>")

    def test_ickey_uses_jsonld_metadata_and_does_not_decode_substituted_descriptions(self):
        data = {"@type": "CollectionPage", "mainEntity": {"@type": "ItemList", "itemListElement": [{"@type": "Product", "sku": "TEST123", "name": "测试品牌 MCU TEST123", "description": "&#x21550;", "brand": {"name": "测试品牌"}, "url": "https://www.ickey.cn/detail/123/TEST123.html", "image": ["https://www.ickey.cn/image.jpg"], "documentation": {"url": "https://www.ickey.cn/manual.pdf"}, "offers": [{"price": "123", "priceCurrency": "USD"}]}]}}
        html = '<script type="application/ld+json">' + json.dumps(data) + '</script>'
        result = parse_ickey(html)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["value"], "测试品牌 MCU TEST123")
        self.assertEqual(result[0]["lcsc"], "")
        self.assertIsNone(result[0]["reference_price"])
        self.assertEqual(parse_ickey('<script type="application/ld+json">{"@type":"CollectionPage","itemListElement":[]}</script>'), [])
        with self.assertRaises(ValueError):
            parse_ickey("<html>验证页面</html>")


class ComponentMediaTest(unittest.TestCase):
    def test_old_database_migration_and_json_roundtrip_preserve_media(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "old.db"
            inventory = Inventory(path)
            item = inventory.create({"name": "电阻", "model": "0603", "quantity": 3})
            # Recreate the pre-image columns to exercise an actual older database.
            with inventory.connect() as db:
                db.execute("ALTER TABLE components DROP COLUMN image_url")
                db.execute("ALTER TABLE components DROP COLUMN source_url")
            inventory = Inventory(path)
            migrated = inventory.snapshot()["components"][0]
            self.assertEqual(migrated["quantity"], 3)
            self.assertEqual(migrated["image_url"], "")
            self.assertEqual(len(inventory.snapshot()["movements"]), 1)
            updated = inventory.update(item["id"], {**migrated, "image_url": "https://alimg.szlcsc.com/image.jpg", "source_url": "https://item.szlcsc.com/26547.html"})
            legacy_update = {k: v for k, v in updated.items() if k not in ("image_url", "source_url")}
            self.assertEqual(inventory.update(item["id"], legacy_update)["image_url"], updated["image_url"])
            backup = inventory.backup()
            target = Inventory(Path(folder) / "new.db")
            target.restore(backup)
            self.assertEqual(target.snapshot(), inventory.snapshot())
            self.assertEqual(updated["image_url"], target.snapshot()["components"][0]["image_url"])
            old_backup = copy.deepcopy(backup)
            for component in old_backup["components"]:
                component.pop("image_url")
                component.pop("source_url")
            target.restore(old_backup)
            self.assertEqual(target.snapshot()["components"][0]["source_url"], "")

    def test_invalid_media_never_changes_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / "inventory.db")
            for key in ("image_url", "source_url"):
                with self.assertRaises(ValidationError):
                    inventory.create({"name": "电阻", "model": "0603", key: "javascript:alert(1)"})
            self.assertEqual(inventory.snapshot(), {"components": [], "movements": []})


if __name__ == "__main__":
    unittest.main()
