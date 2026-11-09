"use strict";

const projectState = { id: null, report: null, request: 0, page: 1, draft: null, previewRequest: 0, matchRow: null, matchSaving: false };
const bomLabels = { ready: "齐料", short: "数量不足", missing: "未找到", review: "待确认" };

function renderProjects() {
  const projects = state.projects;
  if (!projects.some(p => p.id === projectState.id)) projectState.id = projects[0]?.id ?? null;
  $("project-select").innerHTML = projects.length ? projects.map(p => `<option value="${p.id}">${e(p.name)} · ${p.build_quantity} 套 · ${p.bom_count} 行 BOM</option>`).join("") : '<option value="">暂无项目</option>';
  $("project-select").value = projectState.id ?? "";
  $("project-edit").disabled = $("project-delete").disabled = !projectState.id;
  $("project-empty").hidden = !!projectState.id;
  $("project-detail").hidden = !projectState.id;
  if (projectState.id) refreshProjectReport();
}

async function refreshProjectReport() {
  const id = projectState.id;
  const request = ++projectState.request;
  $("bom-check-error").hidden = true;
  $("bom-result-title").textContent = "正在检查库存…";
  $("bom-rows").innerHTML = "";
  $("bom-summary").innerHTML = "";
  $("bom-empty").hidden = $("bom-filter-empty").hidden = true;
  $("bom-prev").disabled = $("bom-next").disabled = true;
  projectState.report = null;
  const selected = state.projects.find(p => p.id === id);
  if (selected) {
    $("project-name").textContent = selected.name;
    $("project-description").textContent = `计划制作 ${selected.build_quantity} 套${selected.notes ? " · " + selected.notes : ""}`;
    $("project-file").textContent = selected.bom_filename ? `BOM：${selected.bom_filename} · ${selected.bom_sheet}` : "尚未导入 BOM";
  }
  try {
    const report = await api(`/api/projects/${id}`);
    if (request !== projectState.request || id !== projectState.id) return;
    projectState.report = report;
    drawBomReport();
  } catch (error) {
    if (request !== projectState.request) return;
    $("bom-check-error").textContent = error.message;
    $("bom-check-error").hidden = false;
    $("bom-result-title").textContent = "库存检查失败，请重试";
  }
}

function drawBomReport() {
  const report = projectState.report;
  if (!report) return;
  $("bom-summary").innerHTML = Object.entries(bomLabels).map(([key, label]) => `<div class="bom-stat ${key}"><span>${label}</span><strong>${number(report.summary[key])}<small>行</small></strong></div>`).join("");
  $("bom-result-title").textContent = !report.rows.length ? "BOM 检查结果" : report.all_ready ? "物料齐备，可以安排制作" : "存在缺料或待确认物料";
  $("bom-empty").hidden = report.rows.length > 0;
  const rows = report.rows.filter(row => !$("bom-issues-only").checked || row.status !== "ready");
  $("bom-filter-empty").hidden = !report.rows.length || rows.length > 0;
  const pages = Math.max(1, Math.ceil(rows.length / 50));
  projectState.page = Math.min(projectState.page, pages);
  $("bom-page-label").textContent = `${projectState.page} / ${pages} · 共 ${rows.length} 行`;
  $("bom-prev").disabled = projectState.page <= 1;
  $("bom-next").disabled = projectState.page >= pages;
  $("bom-rows").innerHTML = rows.slice((projectState.page - 1) * 50, projectState.page * 50).map(row => {
    const matches = row.candidates.map(id => state.components.find(item => item.id === id)).filter(Boolean);
    const sources = matches.slice(0, 2).map(item => `${item.code} · ${item.location || "未设置位置"}`).join("；");
    return `<tr><td><strong>${e(row.model || row.supplier_part)}</strong><span class="cell-secondary">${e(row.designators || "未填写位号")}</span><span class="cell-secondary">${e(row.name)}</span></td><td>${e(row.package || "未填写")}<span class="cell-secondary">${e(row.supplier_part)}</span></td><td>${number(row.quantity)} ${e(row.unit)}</td><td>${number(row.required)}</td><td>${number(row.available)}</td><td class="${row.shortage ? "negative" : "positive"}">${row.status === "review" ? "待确认" : number(row.shortage)}</td><td><span class="bom-status ${row.status}">${bomLabels[row.status]}</span><span class="cell-secondary">${e(row.reason)}</span><span class="cell-secondary">${e(sources)}${matches.length > 2 ? ` 等 ${matches.length} 个库存档案` : ""}</span><button class="text-button bom-match-button" data-project-action="match" data-bom-id="${row.id}">选择库存 / 修改匹配</button></td></tr>`;
  }).join("");
}

function openProjectDialog(project) {
  const form = $("project-form");
  form.reset();
  form.elements.id.value = project?.id ?? "";
  for (const key of ["name", "notes", "build_quantity"]) if (project) form.elements[key].value = project[key];
  $("project-title").textContent = project ? "编辑项目" : "新建项目";
  $("project-form-error").hidden = true;
  $("project-dialog").showModal();
  form.elements.name.focus();
}

async function previewProjectBom(sheet) {
  const draft = projectState.draft;
  if (!draft) return;
  const request = ++projectState.previewRequest;
  $("bom-import-confirm").disabled = true;
  $("bom-import-errors").hidden = true;
  $("bom-import-summary").textContent = "正在解析与校验…";
  try {
    const preview = await api(`/api/projects/${draft.projectId}/bom/preview`, "POST", { filename: draft.filename, content: draft.content, sheet });
    if (request !== projectState.previewRequest) return;
    draft.sheet = preview.sheet;
    $("bom-import-file").textContent = `项目：${draft.projectName} · 文件：${preview.filename}`;
    $("bom-sheet").innerHTML = preview.sheets.map(name => `<option value="${e(name)}">${e(name)}</option>`).join("");
    $("bom-sheet").value = preview.sheet;
    $("bom-sheet").disabled = preview.sheets.length < 2;
    $("bom-import-columns").textContent = `型号列：${preview.columns.model.join(" / ") || "使用供应商编号"}；数量列：${preview.columns.quantity.join(" / ")}；封装列：${preview.columns.package.join(" / ") || "未提供"}`;
    $("bom-import-summary").textContent = `${preview.rows.length} 行物料可导入，${preview.error_count} 行需要修正。`;
    $("bom-import-errors").textContent = preview.errors.join("\n");
    $("bom-import-errors").hidden = !preview.error_count;
    $("bom-import-warnings").textContent = preview.warnings.join("；");
    $("bom-import-rows").innerHTML = preview.rows.slice(0, 20).map(row => `<tr><td>${e(row.model)}</td><td>${e(row.designators)}</td><td>${e(row.package)}</td><td>${number(row.quantity)}</td><td>${e(row.supplier_part)}</td></tr>`).join("");
    $("bom-import-confirm").disabled = preview.error_count > 0 || preview.rows.length === 0;
  } catch (error) {
    if (request !== projectState.previewRequest) return;
    $("bom-import-errors").textContent = error.message;
    $("bom-import-errors").hidden = false;
    $("bom-import-summary").textContent = "无法导入，请检查文件或选择其他工作表。";
  }
}

function drawMatchChoices() {
  const query = $("bom-match-query").value.trim().toLowerCase();
  const row = projectState.matchRow;
  const items = state.components.filter(item => !query || [item.code, item.model, item.name, item.package, item.lcsc, item.location].some(value => String(value || "").toLowerCase().includes(query)));
  items.sort((a, b) => Number(row.candidates.includes(b.id)) - Number(row.candidates.includes(a.id)) || a.id - b.id);
  $("bom-match-select").innerHTML = items.slice(0, 100).map(item => `<option value="${item.id}">${e(item.model)} · ${e(item.package || "未填封装")} · ${number(item.quantity)} ${e(item.unit)} · ${e(item.code)} · ${e(item.location || "未填位置")}</option>`).join("");
  if (row.component_id && items.some(item => item.id === row.component_id)) $("bom-match-select").value = row.component_id;
  else $("bom-match-select").selectedIndex = -1;
  $("bom-match-count").textContent = `找到 ${items.length} 个库存档案，最多显示 100 个。请核对型号、封装和计量单位。`;
}

document.addEventListener("click", async event => {
  const button = event.target.closest("[data-project-action]");
  if (!button || button.disabled) return;
  const action = button.dataset.projectAction;
  try {
    if (action === "new") { openProjectDialog(); return; }
    if (action === "template") { await download("/api/bom-template.csv", "嘉立创BOM模板.csv"); return; }
    const project = state.projects.find(p => p.id === projectState.id);
    if (!project) return;
    if (action === "edit") { openProjectDialog(project); return; }
    if (action === "delete") {
      askConfirm("删除这个项目？", `将删除「${project.name}」及其 BOM。库存物料和出入库记录会保留。`, "确认删除项目", async () => {
        await api(`/api/projects/${project.id}`, "DELETE");
        $("confirm-dialog").close();
        await load();
      });
      return;
    }
    if (action === "check") { await load(); return; }
    if (action === "import") { projectState.importTarget = project; $("bom-file").click(); return; }
    if (action === "shortages") { await download(`/api/projects/${project.id}/shortages.csv`, `${project.name.replace(/[\\/:*?"<>|]/g, "_")}-缺料清单.csv`); return; }
    if (action === "prev" || action === "next") { projectState.page += action === "next" ? 1 : -1; drawBomReport(); return; }
    if (action === "match") {
      const row = projectState.report?.rows.find(item => item.id === Number(button.dataset.bomId));
      if (!row) return;
      projectState.matchRow = row;
      $("bom-match-title").textContent = `匹配库存：${row.model || row.supplier_part}`;
      $("bom-match-query").value = "";
      $("bom-match-error").hidden = true;
      drawMatchChoices();
      $("bom-match-dialog").showModal();
    }
  } catch (error) { toast(error.message, true); }
});

$("project-select").addEventListener("change", event => { projectState.id = Number(event.target.value); projectState.page = 1; renderProjects(); });
$("bom-issues-only").addEventListener("change", () => { projectState.page = 1; drawBomReport(); });
$("project-form").addEventListener("submit", event => {
  event.preventDefault();
  const form = event.currentTarget;
  submitForm(form, "project-form-error", async () => {
    const data = Object.fromEntries(new FormData(form));
    const id = data.id;
    delete data.id;
    const project = await api(id ? `/api/projects/${id}` : "/api/projects", id ? "PUT" : "POST", data);
    projectState.id = project.id;
    $("project-dialog").close();
    await load();
    toast("项目已保存");
  });
});

$("bom-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  event.target.value = "";
  const target = projectState.importTarget;
  if (!file || !target) return;
  try {
    if (file.size > 5 * 1024 * 1024) throw new Error("BOM 文件不能超过 5 MB");
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = () => reject(new Error("无法读取 BOM 文件"));
      reader.readAsDataURL(file);
    });
    projectState.draft = { projectId: target.id, projectName: target.name, filename: file.name, content };
    $("bom-import-rows").innerHTML = "";
    $("bom-import-dialog").showModal();
    await previewProjectBom();
  } catch (error) { toast(error.message, true); }
});
$("bom-sheet").addEventListener("change", event => previewProjectBom(event.target.value));
$("bom-import-dialog").addEventListener("close", () => { projectState.previewRequest++; });
$("bom-import-confirm").addEventListener("click", async event => {
  const draft = projectState.draft;
  if (!draft) return;
  event.currentTarget.disabled = true;
  try {
    await api(`/api/projects/${draft.projectId}/bom`, "POST", { filename: draft.filename, content: draft.content, sheet: draft.sheet });
    projectState.id = draft.projectId;
    projectState.page = 1;
    // Reimport replaces BOM row IDs. Remove the previous report before closing
    // the modal so its stale matching buttons cannot be used during refresh.
    projectState.report = null;
    $("bom-rows").innerHTML = "";
    $("bom-summary").innerHTML = "";
    $("bom-result-title").textContent = "正在检查库存…";
    $("bom-import-dialog").close();
    const refreshed = await load();
    toast(refreshed ? "BOM 已导入，正在检查当前库存" : "BOM 已导入，库存检查尚未刷新，请重新打开应用", !refreshed);
  } catch (error) {
    $("bom-import-errors").textContent = error.message;
    $("bom-import-errors").hidden = false;
    $("bom-import-confirm").disabled = false;
  }
});
$("bom-match-query").addEventListener("input", drawMatchChoices);
async function saveBomMatch(componentId) {
  const row = projectState.matchRow;
  if (!row || projectState.matchSaving) return;
  projectState.matchSaving = true;
  $("bom-match-save").disabled = $("bom-match-auto").disabled = true;
  try {
    await api(`/api/projects/${row.project_id}/items/${row.id}`, "PUT", { component_id: componentId });
    $("bom-match-dialog").close();
    await refreshProjectReport();
  } catch (error) {
    $("bom-match-error").textContent = error.message;
    $("bom-match-error").hidden = false;
  } finally {
    projectState.matchSaving = false;
    $("bom-match-save").disabled = $("bom-match-auto").disabled = false;
  }
}
$("bom-match-save").addEventListener("click", () => {
  const value = $("bom-match-select").value;
  if (!value) { $("bom-match-error").textContent = "请先选择一个库存档案"; $("bom-match-error").hidden = false; return; }
  saveBomMatch(Number(value));
});
$("bom-match-auto").addEventListener("click", () => saveBomMatch(null));
