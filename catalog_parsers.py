"""Extract public product markup without executing any remote scripts."""
import json
import re
from html.parser import HTMLParser


class Node:
    def __init__(self, tag, attrs):
        self.tag, self.attrs, self.children = tag, dict(attrs), []

    def text(self):
        return " ".join("".join(child.text() if isinstance(child, Node) else child for child in self.children).split())

    def find(self, class_name):
        result = [self] if class_name in self.attrs.get("class", "").split() else []
        for child in self.children:
            if isinstance(child, Node):
                result.extend(child.find(class_name))
        return result


class ProductMarkup(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.cards, self.scripts, self.stack = [], [], []
        self.script = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "script" and attributes.get("type", "").lower() == "application/ld+json":
            self.script = []
        if not self.stack and tag == "article" and "product-card" in attributes.get("class", "").split() and len(self.cards) < 12:
            self.stack = [Node(tag, attrs)]
        elif self.stack:
            node = Node(tag, attrs)
            self.stack[-1].children.append(node)
            if tag not in self.VOID:
                self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.script is not None:
            self.scripts.append("".join(self.script))
            self.script = None
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index].tag == tag:
                if index == 0:
                    self.cards.append(self.stack[0])
                del self.stack[index:]
                break

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
        if self.stack:
            self.stack[-1].children.append(data)


def parse_hqchip(text):
    markup = ProductMarkup()
    markup.feed(text)
    products = []
    for card in markup.cards:
        def content(class_name):
            nodes = card.find(class_name)
            return nodes[0].text() if nodes else ""

        def attribute(class_name, name):
            nodes = card.find(class_name)
            return nodes[0].attrs.get(name, "") if nodes else ""

        model = attribute("product-model", "title") or content("product-model")
        if not model:
            continue
        info = {}
        for row in card.find("info-row"):
            labels, values = row.find("info-label"), row.find("info-text")
            if labels and values:
                info[labels[0].text().rstrip("：:")] = values[0].attrs.get("title") or values[0].text()
        price, minimum = None, None
        tables = card.find("price-table")
        if tables:
            amounts, tiers = tables[0].find("price-cell"), tables[0].find("moq-cell")
            if amounts and tiers:
                price_match = re.fullmatch(r"[¥￥]\s*(\d+(?:\.\d+)?)", amounts[0].text())
                tier_match = re.fullmatch(r"(\d+)\s*\+", tiers[0].text())
                if price_match and tier_match:
                    price, minimum = float(price_match[1]), int(tier_match[1])
        products.append({
            "model": model, "name": content("category-link") or model,
            "category": content("category-link"), "package": info.get("封装", ""),
            "manufacturer": content("brand-link"), "value": content("desc-value"),
            "supplier_part": info.get("编号", ""), "lcsc": "", "unit": "个",
            "image_url": attribute("product-image", "src"),
            "source_url": attribute("model-link", "href"),
            "datasheet_url": attribute("datasheet-link", "href"),
            "reference_price": price, "price_minimum": minimum,
        })
    if not products and not ("搜索结果" in text and any(marker in text for marker in ("暂无", "没有找到", "empty-result", "no-result"))):
        raise ValueError("华秋商品页面格式变化")
    return products


def parse_ickey(text):
    markup = ProductMarkup()
    markup.feed(text)
    products, collection = [], False

    def visit(value, depth=0):
        nonlocal collection
        if depth > 24 or len(products) >= 12:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            kind = value.get("@type")
            if kind in ("CollectionPage", "ItemList"):
                collection = True
            if kind == "Product":
                products.append(value)
            else:
                for item in value.values():
                    visit(item, depth + 1)

    for script in markup.scripts:
        try:
            visit(json.loads(script))
        except (ValueError, RecursionError):
            continue
    if not collection and not products:
        raise ValueError("云汉商品页面格式变化")
    result = []
    for raw in products:
        brand = raw.get("brand")
        documentation = raw.get("documentation")
        model, name = raw.get("mpn") or raw.get("sku"), raw.get("name", "")
        if not isinstance(model, str) or not isinstance(name, str):
            continue
        # Use clear product metadata only. Some descriptions contain substituted
        # glyphs; never try to decode them or infer technical specifications.
        name = name if "&#" not in name else model
        image = raw.get("image", "")
        if isinstance(image, list):
            image = image[0] if image else ""
        result.append({
            "model": model, "name": name, "value": name, "category": "", "package": "",
            "manufacturer": brand.get("name", "") if isinstance(brand, dict) else "",
            "supplier_part": model, "lcsc": "", "unit": "个", "image_url": image,
            "source_url": raw.get("url", ""),
            "datasheet_url": documentation.get("url", "") if isinstance(documentation, dict) else "",
            # The page's offer does not reliably describe a quantity tier.
            "reference_price": None, "price_minimum": None,
        })
    if products and not result:
        raise ValueError("云汉商品资料格式变化")
    return result
