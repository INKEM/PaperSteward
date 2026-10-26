"use strict";
// 网络失败可恢复（UXR-20）：htmx 请求发不出去或服务出错时给出可见提示，
// 成功请求后自动收起；页面内容不丢失，用户可重试。
document.addEventListener("htmx:sendError", () => {
  showNetworkAlert("网络请求失败——服务可能未在运行。页面上的内容没有丢失，恢复后可直接重试。");
});
document.addEventListener("htmx:responseError", (event) => {
  const status = event.detail.xhr ? event.detail.xhr.status : 0;
  if (status >= 500) {
    showNetworkAlert("服务暂时出错（" + status + "）。页面上的内容没有丢失，可稍后重试。");
  }
});
document.addEventListener("htmx:afterRequest", (event) => {
  const alert = document.getElementById("network-alert");
  if (alert && !alert.hidden && event.detail.successful) alert.hidden = true;
});

function showNetworkAlert(message) {
  const alert = document.getElementById("network-alert");
  if (!alert) return;
  alert.textContent = message;
  alert.hidden = false;
  alert.focus();
}

// 采集参数浮层：点击面板外收起（summary 内的点击交给原生 toggle）。
document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) return;
  for (const details of document.querySelectorAll(".collect-params[open]")) {
    if (!details.contains(event.target)) details.removeAttribute("open");
  }
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  for (const details of document.querySelectorAll(".collect-params[open]")) {
    details.removeAttribute("open");
  }
});

// 模型连接表单：不提交表单，用表单当前值即时测试连通性。
document.addEventListener("click", async (event) => {
  const button = event.target;
  if (!(button instanceof HTMLButtonElement) || !button.dataset.testLlm) return;
  const formEl = document.getElementById(button.dataset.testLlm);
  if (!(formEl instanceof HTMLFormElement)) return;
  const result = document.getElementById("llm-test-result");
  const payload = {
    base_url: formEl.elements.namedItem("base_url")?.value || "",
    api_key: formEl.elements.namedItem("api_key")?.value || "",
    model: formEl.elements.namedItem("model")?.value || "",
    timeout_s: Number(formEl.elements.namedItem("timeout_s")?.value || "30") || 30,
  };
  button.disabled = true;
  if (result) result.textContent = "正在测试连接…";
  try {
    const response = await fetch("/settings/llm/test", {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-papersteward-ui": "1" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    let message = data.ok ? data.message : (data.error ? data.error.message : data.message);
    const detail = data.error && Array.isArray(data.error.details) && data.error.details[0];
    if (detail) message += `（${detail.field || ""}：${detail.message || ""}）`;
    if (result) result.textContent = message || "测试完成，但服务返回了无法识别的结果";
  } catch (_) {
    if (result) result.textContent = "测试请求失败，请重试";
  } finally {
    button.disabled = false;
  }
});

// 文件导入（UXR-01）：导入目标、状态栏与 from_file 标记都限定在当前表单内查找，
// data-import-target 的值是同表单内的字段名（如 content），不是全局元素 id。
document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.importTarget) return;
  const form = input.closest("form");
  const target = form ? form.elements.namedItem(input.dataset.importTarget) : null;
  const status = form ? form.querySelector("[data-import-status]") : null;
  const file = input.files && input.files[0];
  // 失败路径统一清空文件选择：既保住已有正文，也允许原样重选同一个文件。
  const fail = (message) => {
    input.value = "";
    if (status) status.textContent = message;
  };
  if (!(target instanceof HTMLTextAreaElement) || !file) return;
  if (file.size > 512 * 1024) {
    fail(`文件 ${file.name} 超过 512 KiB 上限，请拆分后再导入；已有正文未受影响。`);
    return;
  }
  if (target.value && !window.confirm("正文已有内容，导入文件将替换现有正文。是否继续？")) {
    fail("已取消导入，原有正文保留。");
    return;
  }
  try {
    const text = await file.text();
    target.value = text;
    if (input.dataset.fileMarker) {
      const marker = form.querySelector(`input[name="${input.dataset.fileMarker}"]`);
      if (marker instanceof HTMLInputElement) marker.value = "1";
    }
    input.value = "";
    if (status) {
      status.textContent = `已读取 ${file.name}（${text.length} 字），尚未添加——检查内容后点「添加材料」。`;
    }
  } catch (_) {
    fail(`无法读取文件 ${file.name}，请重新选择；已有正文未受影响。`);
  }
});

// Native forms navigate away; HTMX forms restore their buttons when the request ends.
function busy(form, enabled) {
  if (enabled) form.setAttribute("aria-busy", "true");
  else form.removeAttribute("aria-busy");
  for (const button of form.querySelectorAll("button")) {
    if (enabled) { button.dataset.originalLabel = button.textContent; button.textContent = "正在提交…"; }
    else if (button.dataset.originalLabel) button.textContent = button.dataset.originalLabel;
    button.disabled = enabled;
  }
}
// 破坏性操作确认：capture 阶段拦截，原生提交与 htmx 提交都在弹确认后才继续。
document.addEventListener("submit", (event) => {
  const form = event.target;
  if (form instanceof HTMLFormElement && form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
    event.preventDefault();
    event.stopPropagation();
  }
}, true);
document.addEventListener("submit", (event) => {
  if (!event.defaultPrevented && !event.target.hasAttribute("hx-post")) busy(event.target, true);
});
document.addEventListener("htmx:beforeRequest", (event) => {
  const form = event.detail.elt.closest("form");
  if (form) busy(form, true);
});
document.addEventListener("htmx:afterRequest", (event) => {
  const form = event.detail.elt.closest("form");
  if (form) busy(form, false);
});
window.addEventListener("pageshow", () => document.querySelectorAll("form[aria-busy]").forEach((form) => busy(form, false)));

// Ctrl/⌘+Enter 直接提交所在表单（textarea 内也生效）；htmx 表单经 requestSubmit 触发其 hx 处理。
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const form = target.closest("form");
  if (!form) return;
  event.preventDefault();
  if (typeof form.requestSubmit === "function") form.requestSubmit();
  else form.submit();
});

// 阅读回顾切换周期时，不提交另一个周期遗留的日期字段。
function syncInsightPeriod(form) {
  const kind = form.elements.namedItem("kind")?.value;
  for (const field of ["anchor_date", "start_date", "end_date"]) {
    const input = form.elements.namedItem(field);
    if (!input) continue;
    input.disabled = field === "anchor_date" ? !["week", "month"].includes(kind) : kind !== "custom";
  }
}
document.querySelectorAll("form[data-insight-period]").forEach(syncInsightPeriod);
document.addEventListener("change", (event) => {
  const form = event.target.closest?.("form[data-insight-period]");
  if (form) syncInsightPeriod(form);
});
