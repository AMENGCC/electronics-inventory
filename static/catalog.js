"use strict";

let catalogResults = [];
let catalogRequest = 0;

function updateCatalogLinks() {
  const keyword = $("catalog-query").value.trim();
  const link = $("catalog-taobao");
  link.hidden = !keyword;
  link.href = "https://s.taobao.com/search?" + new URLSearchParams({ q: keyword });
}

function openCatalog() {
  catalogRequest++;
  catalogResults = [];
  $("catalog-results").replaceChildren();
  $("catalog-search").disabled = false;
  $("catalog-error").hidden = true;
  $("catalog-warning").hidden = true;
  $("catalog-status").textContent = "输入型号、规格关键词或立创编号，搜索后选择一件物料。";
  const form = $("component-form");
  $("catalog-query").value = form.elements.lcsc.value || form.elements.model.value;
  $("catalog-copy-price").checked = false;
  $("catalog-copy-price").disabled = false;
  updateCatalogLinks();
  $("catalog-dialog").showModal();
  $("catalog-query").focus();
}

$("catalog-open").addEventListener("click", openCatalog);
$("catalog-query").addEventListener("input", updateCatalogLinks);
$("catalog-source").addEventListener("change", () => {
  catalogRequest++;
  catalogResults = [];
  $("catalog-results").replaceChildren();
  $("catalog-search").disabled = false;
  $("catalog-error").hidden = true;
  $("catalog-warning").hidden = true;
  $("catalog-status").textContent = "来源已切换，请点击搜索。";
});
$("component-form").elements.image_url.addEventListener("change", updateComponentImage);
$("catalog-dialog").addEventListener("close", () => { catalogRequest++; });

$("catalog-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("catalog-search").disabled) return;
  const keyword = $("catalog-query").value.trim();
  if (keyword.length < 2) {
    $("catalog-error").textContent = "请输入至少 2 个字的关键词";
    $("catalog-error").hidden = false;
    return;
  }
  const request = ++catalogRequest;
  $("catalog-search").disabled = true;
  $("catalog-error").hidden = true;
  $("catalog-warning").hidden = true;
  $("catalog-status").textContent = `正在搜索「${keyword}」…`;
  $("catalog-results").replaceChildren();
  catalogResults = [];
  try {
    const result = await api("/api/catalog/search", "POST", { keyword, source: $("catalog-source").value });
    if (request !== catalogRequest) return;
    catalogResults = result.products;
    $("catalog-status").textContent = catalogResults.length ? `「${keyword}」· ${result.source} · 显示 ${catalogResults.length} 个候选；可补充关键词缩小范围。` : `没有找到「${keyword}」，可尝试完整型号、切换来源，或前往淘宝搜索。`;
    $("catalog-warning").textContent = (result.warnings || []).join("\n");
    $("catalog-warning").hidden = !result.warnings?.length;
    $("catalog-copy-price").disabled = Boolean(result.stale) || !catalogResults.some((item) => item.reference_price !== null);
    if ($("catalog-copy-price").disabled) $("catalog-copy-price").checked = false;
    $("catalog-results").innerHTML = catalogResults.map((item, index) => {
      const price = item.reference_price === null ? "" : `<span class="catalog-price">${e(unitPrice(item.reference_price))} / ${e(item.unit)} · ${number(item.price_minimum)} ${e(item.unit)}起</span>`;
      const source = httpsUrl(item.source_url);
      return `<article class="catalog-card"><div class="catalog-photo">${componentImage(item.image_url, item.model)}<span class="catalog-provider">${e(item.provider_name || item.supplier)}</span></div><div class="catalog-info"><strong>${e(item.model)}</strong><span>${e(item.manufacturer)} · ${e(item.package || "未标注封装")}</span><p>${e(item.value)}</p><span class="catalog-code">${e(item.supplier_part || item.lcsc || "未提供编号")}${item.category ? " · " + e(item.category) : ""}</span>${price}</div><div class="catalog-actions"><button type="button" class="button primary" data-catalog-index="${index}">选用此物料</button>${source ? `<a href="${e(source)}" target="_blank" rel="noopener noreferrer">商品详情 ↗</a>` : ""}</div></article>`;
    }).join("");
  } catch (error) {
    if (request !== catalogRequest) return;
    $("catalog-status").textContent = "搜索未完成，可重试或关闭后手动录入。";
    $("catalog-error").textContent = error.message;
    $("catalog-error").hidden = false;
  } finally {
    if (request === catalogRequest) $("catalog-search").disabled = false;
  }
});

$("catalog-results").addEventListener("click", (event) => {
  const button = event.target.closest("[data-catalog-index]");
  if (!button) return;
  const item = catalogResults[Number(button.dataset.catalogIndex)];
  if (!item) return;
  const form = $("component-form");
  for (const field of ["name", "model", "category", "package", "value", "manufacturer", "lcsc", "supplier", "datasheet_url", "image_url", "source_url"]) form.elements.namedItem(field).value = item[field] || "";
  if ($("catalog-copy-price").checked && item.reference_price !== null) form.elements.unit_price.value = item.reference_price;
  updateComponentImage();
  $("catalog-dialog").close();
  $("component-error").hidden = true;
  toast("资料已填入，请核对型号、封装、计价单位及库存后保存");
  form.elements.namedItem(form.elements.id.value ? "model" : "quantity").focus();
});
