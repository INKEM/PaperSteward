"use strict";
document.addEventListener("change", async (event) => {
  const input = event.target;
  if (!(input instanceof HTMLInputElement) || !input.dataset.importTarget) return;
  const target = document.getElementById(input.dataset.importTarget);
  const file = input.files && input.files[0];
  if (!file || !target) return;
  if (file.size > 256 * 1024) { input.setCustomValidity("文件不能超过 256 KiB"); input.reportValidity(); return; }
  try { target.value = await file.text(); input.setCustomValidity(""); }
  catch (_) { input.setCustomValidity("无法读取文件，请重新选择"); input.reportValidity(); }
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
