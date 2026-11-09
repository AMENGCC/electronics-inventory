"use strict";

const $ = (id) => document.getElementById(id);
const basePath = document.querySelector('meta[name="inventory-base-path"]').content;
const csrfToken = document.querySelector('meta[name="inventory-csrf-token"]')?.content || "";
const gatewayHeaders = () => ({ "X-Inventory-CSRF": csrfToken });
const serviceError = "无法连接服务，请检查飞牛应用中心的运行状态，或重新登录飞牛桌面。";
const sessionError = "飞牛登录状态可能已过期，请重新打开应用；若仍无法访问，请重新登录飞牛桌面。";
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const icon = (name) => `<svg aria-hidden="true"><use href="#i-${name}"/></svg>`;
const number = (n) => Number(n).toLocaleString("zh-CN");
const money = (n) => "¥" + Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const unitPrice = (n) => "¥" + Number(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 6 });
const date = (value) => new Date(value).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
const kindNames = { initial: "初始库存", in: "采购入库", out: "领用出库", adjust: "库存盘点" };
const state = { components: [], movements: [], projects: [], view: "inventory", page: 1, historyPage: 1, location: null };
let confirmTask = null;
let toastTimer;
let loadRequest = 0;

function httpsUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

function componentImage(url, label, size = "") {
  const source = httpsUrl(url);
  if (!source) return `<span class="component-image ${size}">${icon("box")}</span>`;
  return `<button type="button" class="component-image ${size}" data-image-preview="${e(source)}" data-image-label="${e(label)}" aria-label="查看${e(label)}大图" aria-haspopup="dialog" aria-controls="image-dialog" title="点击查看大图">${icon("box")}<img src="${e(source)}" alt="${e(label)}" loading="lazy" referrerpolicy="no-referrer"></button>`;
}

// Use an event listener instead of inline handlers so the page keeps its CSP.
document.addEventListener("error", (event) => {
  if (event.target instanceof HTMLImageElement && event.target.closest(".component-image")) {
    const thumbnail = event.target.closest(".component-image");
    thumbnail.disabled = true;
    thumbnail.removeAttribute("data-image-preview");
    thumbnail.title = "图片暂时无法加载";
    event.target.remove();
  }
}, true);

function openImagePreview(source, label) {
  source = httpsUrl(source);
  if (!source) return;
  const dialog = $("image-dialog");
  $("image-title").textContent = label || "元器件图片";
  $("image-full").alt = label || "元器件图片";
  $("image-full").hidden = true;
  $("image-status").textContent = "正在加载图片…";
  $("image-status").hidden = false;
  $("image-original").href = source;
  if (!dialog.open) dialog.showModal();
  $("image-full").src = source;
}

$("image-full").addEventListener("load", () => {
  if (!$("image-dialog").open) return;
  $("image-full").hidden = false;
  $("image-status").hidden = true;
});
$("image-full").addEventListener("error", () => {
  if (!$("image-dialog").open) return;
  $("image-full").hidden = true;
  $("image-status").textContent = "图片暂时无法加载，可尝试打开原图。";
  $("image-status").hidden = false;
});
$("image-dialog").addEventListener("close", () => {
  if ($("image-dialog").open) return;
  $("image-full").removeAttribute("src");
  $("image-full").hidden = true;
  $("image-original").removeAttribute("href");
});
// Keep Escape scoped to this preview when it sits above another dialog.
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !$("image-dialog").open) return;
  event.preventDefault();
  event.stopPropagation();
  $("image-dialog").close();
}, true);
let imageBackdropPressed = false;
function outsideImageDialog(event) {
  const rect = $("image-dialog").getBoundingClientRect();
  return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
}
$("image-dialog").addEventListener("pointerdown", (event) => {
  imageBackdropPressed = event.target === $("image-dialog") && outsideImageDialog(event);
});
$("image-dialog").addEventListener("click", (event) => {
  if (imageBackdropPressed && event.target === $("image-dialog") && outsideImageDialog(event)) $("image-dialog").close();
  imageBackdropPressed = false;
});

function updateComponentImage() {
  const form = $("component-form");
  $("component-image-preview").innerHTML = componentImage(form.elements.image_url.value, "元器件图片", "large");
}

function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").className = error ? "error" : "";
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, error ? 6000 : 3500);
}

async function api(path, method = "GET", body) {
  let response;
  try {
    response = await fetch(basePath + path, { method, headers: { ...gatewayHeaders(), ...(body === undefined ? {} : { "Content-Type": "application/json" }) }, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch {
    throw new Error(serviceError);
  }
  return readApiResponse(response);
}

async function readApiResponse(response) {
  if (response.redirected) throw new Error(sessionError);
  let data;
  try { data = await response.json(); }
  catch {
    const loginPage = response.headers.get("Content-Type")?.includes("text/html") || [401, 403].includes(response.status);
    throw new Error(loginPage ? sessionError : "服务返回了异常内容，请稍后重试或重新打开应用。");
  }
  if (!response.ok) throw new Error(typeof data?.error === "string" ? data.error : "操作失败，请重试");
  if (data === null || typeof data !== "object") throw new Error("服务返回的数据格式异常，请重新打开应用。");
  return data;
}

async function load(showError = true) {
  const request = ++loadRequest;
  try {
    const [data, projects] = await Promise.all([api("/api/state"), api("/api/projects")]);
    if (request !== loadRequest) return false;
    state.components = data.components;
    state.movements = data.movements;
    state.projects = projects;
    $("connection-error").hidden = true;
    $("connection-error-text").textContent = serviceError;
    updateOptions();
    render();
    return true;
  } catch (error) {
    if (request !== loadRequest) return false;
    $("connection-error-text").textContent = error.message;
    $("connection-error").hidden = false;
    if (showError) toast(error.message, true);
    return false;
  }
}

function updateOptions() {
  const select = $("category-filter");
  const selected = select.value;
  const categories = [...new Set(state.components.map((i) => i.category))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.innerHTML = '<option value="">全部分类</option>' + categories.map((c) => `<option value="${e(c)}">${e(c)}</option>`).join("");
  select.value = categories.includes(selected) ? selected : "";
  const defaults = ["电阻", "电容", "电感", "二极管", "三极管 / MOS管", "集成电路", "连接器", "模块", "开关", "晶振", "其他"];
  $("category-options").innerHTML = [...new Set([...defaults, ...categories])].map((c) => `<option value="${e(c)}"></option>`).join("");
  $("location-options").innerHTML = [...new Set(state.components.map((i) => i.location).filter(Boolean))].sort().map((c) => `<option value="${e(c)}"></option>`).join("");
}

function status(item) {
  return item.quantity === 0 ? "empty" : item.quantity < item.minimum_stock ? "low" : "healthy";
}
const needsStock = (item) => item.quantity === 0 || item.quantity < item.minimum_stock;

function render() {
  const items = state.components;
  const low = items.filter(needsStock).length;
  const categories = new Set(items.map((i) => i.category)).size;
  const locations = new Set(items.map((i) => i.location).filter(Boolean)).size;
  $("nav-total").textContent = number(items.length);
  $("nav-low").textContent = number(low);
  $("stat-total").innerHTML = `${number(items.length)}<span>种</span>`;
  $("stat-low").innerHTML = `${number(low)}<span>种</span>`;
  $("stat-locations").innerHTML = `${number(locations)}<span>处</span>`;
  $("stat-value").textContent = money(items.reduce((sum, item) => sum + item.quantity * item.unit_price, 0));
  $("stat-categories").textContent = categories ? `分布在 ${categories} 个物料分类中` : "从第一件物料开始";
  $("stat-low-note").textContent = low ? "含零库存物料，点击查看待补货清单 →" : "库存状态良好";
  $("stat-low-note").classList.toggle("warn", low > 0);
  $("migration-summary").textContent = `当前设备：${number(items.length)} 种物料 · ${number(state.movements.length)} 条流水 · ${number(state.projects.length)} 个项目`;
  if (state.view === "inventory" || state.view === "low") renderInventory();
  if (state.view === "history") renderHistory();
  if (state.view === "locations") renderLocations();
  if (state.view === "projects") renderProjects();
}

function setView(view) {
  const titles = {
    inventory: ["全部物料", "物料一目了然", "管理你的电子元器件，让每一次制作都有备而来。"],
    low: ["低库存提醒", "备好下一次灵感", "查看零库存与低于安全库存的元器件，及时安排补货。"],
    history: ["出入库记录", "每一次变化都有记录", "采购、领用与盘点，清晰追溯每一笔库存变化。"],
    locations: ["存放位置", "需要时，随手找到", "按存放位置整理物料，点击位置查看对应元器件。"],
    projects: ["项目与 BOM", "开工之前，先看齐料", "导入嘉立创 EDA 的 BOM，按计划制作数量核对库存与缺料。"],
    backup: ["数据迁移与备份", "把全部数据带到新设备", "在旧设备整体导出，再在新设备整体导入，物料与历史记录一起迁移。"],
  };
  if (!titles[view]) return;
  state.view = view;
  document.querySelector(".stats").hidden = !["inventory", "low"].includes(view);
  $("inventory-actions").hidden = view !== "inventory";
  state.page = state.historyPage = 1;
  state.location = null;
  $("search").value = "";
  $("category-filter").value = "";
  $("status-filter").value = "";
  const [label, title, subtitle] = titles[view];
  $("breadcrumb-label").textContent = label;
  $("page-title").innerHTML = `${e(title)}<span class="heading-dot">.</span>`;
  $("page-subtitle").textContent = subtitle;
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".nav-item").forEach(button => {
    if (button.dataset.view === view) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  for (const key of ["inventory", "history", "locations", "projects", "backup"]) $(key + "-view").hidden = key !== (view === "low" ? "inventory" : view);
  render();
}

function renderInventory() {
  const query = $("search").value.trim().toLowerCase();
  let items = state.components.filter((item) => {
    const matches = !query || [item.code, item.name, item.model, item.lcsc, item.value, item.manufacturer, item.location, item.notes, item.supplier, item.package, item.category].some((v) => v.toLowerCase().includes(query));
    return matches && (!$("category-filter").value || item.category === $("category-filter").value)
      && (!$("status-filter").value || status(item) === $("status-filter").value)
      && (state.view !== "low" || needsStock(item))
      && (state.location === null || item.location === state.location);
  });
  const order = $("sort-order").value;
  items.sort((a, b) => order === "quantity" ? a.quantity - b.quantity || b.id - a.id : order === "model" ? a.model.localeCompare(b.model, "zh-CN") : order === "updated" ? b.updated_at.localeCompare(a.updated_at) || b.id - a.id : b.id - a.id);
  const size = Number($("page-size").value);
  const pages = Math.max(1, Math.ceil(items.length / size));
  state.page = Math.max(1, Math.min(state.page, pages));
  const start = (state.page - 1) * size;
  $("list-title").innerHTML = `${state.location !== null ? e(state.location || "未设置位置") + ' <button class="text-button" data-action="clear-location">清除位置筛选</button>' : state.view === "low" ? "待补货清单" : "物料清单"} <span id="list-count" class="count-pill">${number(items.length)}</span>`;
  $("inventory-rows").innerHTML = items.slice(start, start + size).map((item) => {
    const level = status(item);
    const stockClass = level === "healthy" ? "" : level;
    return `<tr><td><div class="inventory-component">${componentImage(item.image_url, item.model, "small")}<div><button class="item-name" data-action="detail" data-id="${item.id}">${e(item.name)}</button><div class="model-line">${e(item.model)}</div></div></div></td><td><span class="category-badge">${e(item.category)}</span><span class="cell-secondary">${e(item.package || "未设置封装")}</span></td><td>${e(item.value || "—")}</td><td><span class="stock-value ${stockClass}"><span class="stock-indicator ${stockClass}"></span>${number(item.quantity)}<small>${e(item.unit)}</small></span><span class="cell-secondary">安全库存 ${number(item.minimum_stock)}${needsStock(item) ? " · 待补货" : ""}</span></td><td class="location-cell">${e(item.location || "未设置")}</td><td><div class="row-actions"><button class="action-mini" data-action="in" data-id="${item.id}" aria-label="${e(item.name)}入库">入库</button><button class="action-mini out" data-action="out" data-id="${item.id}" aria-label="${e(item.name)}出库">出库</button><button class="icon-button" data-action="edit" data-id="${item.id}" title="编辑物料" aria-label="编辑${e(item.name)}">${icon("edit")}</button></div></td></tr>`;
  }).join("");
  $("inventory-empty").hidden = items.length > 0;
  if (!items.length) {
    const initial = state.components.length === 0;
    $("inventory-empty").querySelector("h3").textContent = initial ? "从第一件元器件开始" : state.view === "low" && !query && !$("category-filter").value && !$("status-filter").value ? "所有物料库存充足" : "没有符合条件的物料";
    $("inventory-empty").querySelector("p").textContent = initial ? "记下型号、数量与存放位置，下次需要时轻松找到。" : "可以调整搜索关键词或筛选条件，查看其他物料。";
    $("inventory-empty").querySelector("button").hidden = !initial;
    $("inventory-clear-filters").hidden = initial;
    $("inventory-clear-filters").textContent = state.view === "low" ? "查看全部物料" : "清除筛选";
    $("inventory-empty").querySelector(".empty-steps").hidden = !initial;
  }
  $("inventory-summary").textContent = items.length ? `共 ${number(items.length)} 种物料，显示 ${start + 1}–${Math.min(start + size, items.length)} 条` : "共 0 种物料";
  $("page-label").textContent = `${state.page} / ${pages}`;
  document.querySelector('[data-action="prev"]').disabled = state.page <= 1;
  document.querySelector('[data-action="next"]').disabled = state.page >= pages;
}

function renderHistory() {
  const query = $("history-search").value.trim().toLowerCase();
  const kind = $("history-kind").value;
  const records = state.movements.filter((r) => (!kind || r.kind === kind) && (!query || [r.code, r.name, r.model, r.note].some((v) => v.toLowerCase().includes(query))));
  const pages = Math.max(1, Math.ceil(records.length / 15));
  state.historyPage = Math.max(1, Math.min(state.historyPage, pages));
  $("history-count").textContent = number(records.length);
  $("history-rows").innerHTML = records.slice((state.historyPage - 1) * 15, state.historyPage * 15).map((r) => `<tr><td>${e(date(r.created_at))}</td><td><strong>${e(r.name)}</strong><span class="cell-secondary model-line">${e(r.model)} · ${e(r.code)}${r.component_id === null ? " · 已删除" : ""}</span></td><td><span class="kind-badge ${r.kind}">${kindNames[r.kind]}</span></td><td class="${r.delta > 0 ? "positive" : "negative"}">${r.delta > 0 ? "+" : ""}${number(r.delta)} ${e(r.unit)}</td><td>${number(r.before_quantity)} → ${number(r.after_quantity)}</td><td>${e(r.note || "—")}</td></tr>`).join("");
  $("history-empty").hidden = records.length > 0;
  $("history-empty-title").textContent = state.movements.length ? "没有符合条件的流水记录" : "暂无出入库记录";
  $("history-empty-description").textContent = state.movements.length ? "调整搜索关键词或操作类型，可查看其他记录。" : "新增初始库存、入库、出库或盘点后，记录会显示在这里。";
  $("history-clear-filters").hidden = !state.movements.length;
  $("history-summary").textContent = `共 ${number(records.length)} 条记录`;
  $("history-page-label").textContent = `${state.historyPage} / ${pages}`;
  document.querySelector('[data-action="history-prev"]').disabled = state.historyPage <= 1;
  document.querySelector('[data-action="history-next"]').disabled = state.historyPage >= pages;
}

function renderLocations() {
  const groups = new Map();
  for (const item of state.components) {
    if (!groups.has(item.location)) groups.set(item.location, []);
    groups.get(item.location).push(item);
  }
  $("locations-empty").hidden = groups.size > 0;
  $("location-grid").innerHTML = [...groups.entries()].sort(([a], [b]) => a.localeCompare(b, "zh-CN")).map(([location, items]) => {
    const units = new Map();
    items.forEach((item) => units.set(item.unit, (units.get(item.unit) || 0) + item.quantity));
    return `<button class="location-card" data-location="${e(location)}">${icon("pin")}<h3>${e(location || "未设置位置")}</h3><p>${items.length} 种物料 · ${items.filter(needsStock).length} 种待补货<br>${[...units].map(([unit, amount]) => `${number(amount)} ${e(unit)}`).join(" / ")}</p><div class="location-models">${items.slice(0, 3).map((item) => e(item.model)).join(" · ")}</div></button>`;
  }).join("");
}

function openComponent(item = null) {
  const form = $("component-form");
  form.reset();
  $("component-error").hidden = true;
  $("component-title").textContent = item ? "编辑物料" : "新增物料";
  form.elements.id.value = item ? item.id : "";
  if (item) for (const [key, value] of Object.entries(item)) if (form.elements.namedItem(key)) form.elements.namedItem(key).value = value;
  $("initial-quantity-field").hidden = Boolean(item);
  form.elements.quantity.disabled = Boolean(item);
  $("edit-quantity-hint").hidden = !item;
  updateComponentImage();
  $("component-dialog").showModal();
  form.elements.namedItem("name").focus();
}

function openStock(item, kind) {
  const form = $("stock-form");
  form.reset();
  form.elements.id.value = item.id;
  form.elements.kind.value = kind;
  const label = { in: "入库", out: "出库", adjust: "盘点" }[kind];
  $("stock-title").textContent = label;
  $("stock-submit").textContent = `确认${label}`;
  $("stock-quantity-label").textContent = kind === "adjust" ? "实际库存数量" : `${label}数量`;
  form.elements.quantity.min = kind === "adjust" ? "0" : "1";
  if (kind === "adjust") form.elements.quantity.value = item.quantity;
  $("stock-item").innerHTML = `${e(item.name)}<small>${e(item.model)} · 当前库存 ${number(item.quantity)} ${e(item.unit)}</small>`;
  $("stock-error").hidden = true;
  stockPreview();
  $("stock-dialog").showModal();
  form.elements.quantity.focus();
}

function stockPreview() {
  const form = $("stock-form");
  const item = state.components.find((i) => i.id === Number(form.elements.id.value));
  if (!item) return;
  const amount = Number(form.elements.quantity.value);
  const kind = form.elements.kind.value;
  const after = kind === "adjust" ? amount : item.quantity + (kind === "in" ? amount : -amount);
  $("stock-preview").textContent = form.elements.quantity.value === "" ? "数量请输入整数，确认后会自动记录库存流水。" : after < 0 ? "出库数量超过当前库存，请检查。" : `操作后预计库存：${number(after)} ${item.unit}${kind === "adjust" ? "（填写实际剩余总数）" : ""}`;
}

function openDetail(item) {
  $("detail-title").textContent = item.name;
  const fields = [["物料编号", item.code], ["分类 / 封装", `${item.category} / ${item.package || "未设置"}`], ["规格参数", item.value || "—"], ["制造商", item.manufacturer || "—"], ["存放位置", item.location || "未设置"], ["安全库存", `${number(item.minimum_stock)} ${item.unit}`], ["参考单价", unitPrice(item.unit_price)], ["立创编号", item.lcsc || "—"], ["供应商", item.supplier || "—"], ["创建时间", date(item.created_at)], ["更新时间", date(item.updated_at)]];
  const recent = state.movements.filter((r) => r.component_id === item.id).slice(0, 5);
  $("detail-body").innerHTML = `<div class="detail-top">${componentImage(item.image_url, item.model, "large")}<strong>${e(item.model)}</strong><span class="stock-value ${needsStock(item) ? "low" : ""}">${number(item.quantity)}<small>${e(item.unit)}</small></span></div><dl class="detail-grid">${fields.map(([label, value]) => `<div><dt>${label}</dt><dd>${e(value)}</dd></div>`).join("")}<div class="full"><dt>数据手册</dt><dd>${item.datasheet_url ? `<a href="${e(item.datasheet_url)}" target="_blank" rel="noopener noreferrer">打开数据手册 ↗</a>` : "—"}</dd></div><div class="full"><dt>商品来源</dt><dd>${httpsUrl(item.source_url) ? `<a href="${e(httpsUrl(item.source_url))}" target="_blank" rel="noopener noreferrer">查看原商品 ↗</a>` : "—"}</dd></div><div class="full"><dt>备注</dt><dd>${e(item.notes || "—")}</dd></div></dl><div class="form-section-label">最近 5 条库存流水</div>${recent.length ? recent.map((r) => `<div class="detail-record"><span class="kind-badge ${r.kind}">${kindNames[r.kind]}</span><strong class="${r.delta > 0 ? "positive" : "negative"}">${r.delta > 0 ? "+" : ""}${number(r.delta)} ${e(r.unit)}</strong><span>${number(r.before_quantity)} → ${number(r.after_quantity)}</span><span class="subtle">${e(date(r.created_at))}</span></div>`).join("") : '<p class="subtle">暂无库存流水</p>'}`;
  $("detail-actions").innerHTML = `<button class="delete-button" data-action="delete" data-id="${item.id}">删除物料</button><button class="button secondary" data-action="adjust" data-id="${item.id}">盘点</button><button class="button secondary" data-action="edit" data-id="${item.id}">编辑</button><button class="button primary" data-action="in" data-id="${item.id}">入库</button><button class="button secondary" data-action="out" data-id="${item.id}">出库</button>`;
  $("detail-dialog").showModal();
}

async function submitForm(form, errorId, task) {
  const submit = form.querySelector('[type="submit"]');
  submit.disabled = true;
  $(errorId).hidden = true;
  try { await task(); } catch (error) {
    $(errorId).textContent = error.message;
    $(errorId).hidden = false;
  } finally { submit.disabled = false; }
}

function askConfirm(title, message, label, task) {
  $("confirm-title").textContent = title;
  $("confirm-message").textContent = message;
  $("confirm-submit").textContent = label;
  $("confirm-error").hidden = true;
  confirmTask = task;
  $("confirm-dialog").showModal();
}

async function download(path, filename) {
  try {
    const response = await fetch(basePath + path, { headers: gatewayHeaders() });
    if (!response.ok) await readApiResponse(response);
    if (response.redirected) throw new Error(sessionError);
    const contentType = response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
    const expectedType = path.endsWith(".csv") ? "text/csv" : "application/json";
    if (contentType !== expectedType) throw new Error(contentType === "text/html" ? sessionError : "导出文件格式异常，请重新打开应用后重试。");
    const url = URL.createObjectURL(await response.blob());
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    toast("文件已生成，请查看浏览器下载记录");
  } catch (error) { toast(error.message, true); }
}

document.addEventListener("click", async (event) => {
  const preview = event.target.closest("[data-image-preview]");
  if (preview && !preview.disabled) {
    openImagePreview(preview.dataset.imagePreview, preview.dataset.imageLabel);
    return;
  }
  const close = event.target.closest("[data-close]");
  if (close) { $(close.dataset.close).close(); return; }
  const nav = event.target.closest("[data-view]");
  if (nav) { setView(nav.dataset.view); return; }
  const location = event.target.closest("[data-location]");
  if (location) { setView("inventory"); state.location = location.dataset.location; renderInventory(); return; }
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.action;
  if (action === "new") { openComponent(); return; }
  if (action === "reload-app") { window.location.reload(); return; }
  if (action === "clear-filters") { setView("inventory"); return; }
  if (action === "clear-history-filters") { $("history-search").value = ""; $("history-kind").value = ""; state.historyPage = 1; renderHistory(); return; }
  if (action === "refresh") { button.disabled = true; if (await load()) toast("库存数据已更新"); button.disabled = false; return; }
  if (action === "prev" || action === "next") { state.page += action === "next" ? 1 : -1; renderInventory(); return; }
  if (action === "history-prev" || action === "history-next") { state.historyPage += action === "history-next" ? 1 : -1; renderHistory(); return; }
  if (action === "clear-location") { state.location = null; state.page = 1; renderInventory(); return; }
  if (action === "export") { await download("/api/export.csv", "电子元器件物料清单.csv"); return; }
  if (action === "export-all") {
    button.disabled = true;
    try { await download("/api/export.json", `元件仓整体数据-${new Date().toISOString().replace(/[:.]/g, "-")}.json`); }
    finally { button.disabled = false; }
    return;
  }
  if (action === "import-all") { $("import-file").click(); return; }
  const item = state.components.find((i) => i.id === Number(button.dataset.id));
  if (!item) return;
  if (action === "detail") { openDetail(item); return; }
  if (["edit", "in", "out", "adjust", "delete"].includes(action)) $("detail-dialog").close();
  if (action === "edit") openComponent(item);
  if (["in", "out", "adjust"].includes(action)) openStock(item, action);
  if (action === "delete") {
    if (item.quantity !== 0) { toast("请先出库或盘点至 0，再删除物料。历史流水会保留。", true); return; }
    askConfirm("删除这件物料？", `将删除「${item.name} / ${item.model}」的物料档案。已有出入库记录仍会保留。`, "确认删除", async () => {
      await api(`/api/components/${item.id}`, "DELETE");
      $("confirm-dialog").close();
      if (await load()) toast("物料已删除，历史记录已保留");
    });
  }
});

$("component-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, "component-error", async () => {
    const data = Object.fromEntries(new FormData(form));
    const id = data.id;
    delete data.id;
    await api(id ? `/api/components/${id}` : "/api/components", id ? "PUT" : "POST", data);
    $("component-dialog").close();
    if (await load()) toast(id ? "物料资料已更新" : "物料已添加");
  });
});
$("stock-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, "stock-error", async () => {
    const data = Object.fromEntries(new FormData(form));
    const result = await api(`/api/components/${data.id}/stock`, "POST", data);
    $("stock-dialog").close();
    if (await load()) toast(`操作完成，当前库存 ${number(result.quantity)} ${result.unit}`);
  });
});
$("stock-form").elements.quantity.addEventListener("input", stockPreview);
$("confirm-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (confirmTask) submitForm(event.currentTarget, "confirm-error", confirmTask);
});
$("confirm-dialog").addEventListener("close", () => { confirmTask = null; });
$("import-file").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  event.target.value = "";
  if (!file) return;
  try {
    if (file.size > 64 * 1024 * 1024) throw new Error("整体导入文件不能超过 64 MB");
    let backup;
    try { backup = JSON.parse((await file.text()).replace(/^\uFEFF/, "")); } catch { throw new Error("文件不是有效的 JSON 整体数据文件"); }
    if (!backup || backup.format !== "electronics-inventory" || ![1, 2].includes(backup.version) || !Array.isArray(backup.components) || !Array.isArray(backup.movements)) throw new Error("请选择元件仓整体导出的 JSON 文件，或旧版完整 JSON 备份");
    const preview = await api("/api/import/preview", "POST", backup);
    const incoming = preview.incoming;
    const current = preview.current;
    const exportedAt = preview.exported_at ? date(preview.exported_at) : "旧版文件未记录";
    askConfirm("整体导入并替换当前数据？", `文件：${file.name}\n导出时间：${exportedAt}\n\n将导入 ${incoming.components} 种物料、${incoming.movements} 条流水\n包含 ${incoming.categories} 个分类、${incoming.locations} 处存放位置\n以及 ${incoming.projects} 个项目、${incoming.bom_items} 行 BOM\n\n当前设备有 ${current.components} 种物料、${current.movements} 条流水、${current.projects} 个项目。\n确认后将整体替换，不会合并。请先整体导出需要保留的当前数据。`, "确认整体导入", async () => {
      const result = await api("/api/import", "POST", backup);
      $("confirm-dialog").close();
      if (await load()) toast(`整体导入完成：${result.components} 种物料、${result.movements} 条流水`);
    });
  } catch (error) { toast(error.message, true); }
});

$("search").addEventListener("input", () => { state.page = 1; renderInventory(); });
for (const id of ["category-filter", "status-filter", "sort-order", "page-size"]) $(id).addEventListener("change", () => { state.page = 1; renderInventory(); });
$("history-search").addEventListener("input", () => { state.historyPage = 1; renderHistory(); });
$("history-kind").addEventListener("change", () => { state.historyPage = 1; renderHistory(); });
window.addEventListener("focus", () => { if (!document.querySelector("dialog[open]")) load(false); });
const currentDate = new Date();
$("today").textContent = currentDate.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" });
$("today").dateTime = `${currentDate.getFullYear()}-${String(currentDate.getMonth() + 1).padStart(2, "0")}-${String(currentDate.getDate()).padStart(2, "0")}`;
$("connection-error-text").textContent = serviceError;
load();
