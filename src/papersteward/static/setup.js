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
