"use strict";
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

document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.importTarget) return;
  const target = document.getElementById(input.dataset.importTarget);
  const status = document.getElementById("import-status");
  const file = input.files && input.files[0];
  if (!file || !target) return;
  if (file.size > 512 * 1024) { input.setCustomValidity("文件不能超过 512 KiB"); input.reportValidity(); return; }
  try {
    target.value = await file.text();
    if (input.dataset.fileMarker) {
      const marker = document.querySelector(`input[name="${input.dataset.fileMarker}"]`);
      if (marker instanceof HTMLInputElement) marker.value = "1";
    }
    input.setCustomValidity("");
    if (status) status.textContent = `已读取 ${file.name}（${target.value.length} 字），检查内容后点「添加材料」。`;
  }
  catch (_) {
    input.setCustomValidity("无法读取文件，请重新选择"); input.reportValidity();
    if (status) status.textContent = "";
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
