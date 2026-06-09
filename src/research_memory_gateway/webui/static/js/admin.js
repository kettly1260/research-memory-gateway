document.addEventListener("DOMContentLoaded", () => {
  const themeKey = "research-memory-theme";
  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const applyTheme = (value) => {
    if (value && value !== "system") document.documentElement.dataset.theme = value;
    else delete document.documentElement.dataset.theme;
  };
  applyTheme(localStorage.getItem(themeKey) || "system");
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const current = localStorage.getItem(themeKey) || "system";
      const next = current === "system" ? "light" : current === "light" ? "dark" : "system";
      localStorage.setItem(themeKey, next);
      applyTheme(next);
    });
  });
  if (csrf) {
    document.body.addEventListener("htmx:configRequest", (event) => {
      event.detail.headers["x-csrf-token"] = csrf;
    });
  }

  document.querySelectorAll("[data-json-form], [data-secret-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const output = form.querySelector("[data-form-output]");
      const payload = form.hasAttribute("data-secret-form") ? formPayload(form, true) : formPayload(form, false);
      const method = (form.getAttribute("method") || "post").toUpperCase();
      const response = await fetch(form.action, {
        method,
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (output) output.textContent = JSON.stringify(data, null, 2);
      const success = form.getAttribute("data-success");
      if (response.ok && success) window.location.href = success;
    });
  });

  document.querySelectorAll("[data-json-import]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submitter = event.submitter;
      const output = form.querySelector("[data-form-output]");
      let memories = [];
      try { memories = JSON.parse(form.elements.memories.value || "[]"); }
      catch (error) { if (output) output.textContent = `Invalid JSON: ${error.message}`; return; }
      const payload = {
        memories,
        policy: form.elements.policy.value,
        confirmed: form.elements.confirmed.value === "true",
      };
      const response = await fetch(submitter.formAction, {
        method: "POST",
        headers: { "content-type": "application/json", "x-csrf-token": csrf },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (output) output.textContent = JSON.stringify(data, null, 2);
    });
  });
});

function formPayload(form, skipEmpty) {
  const payload = {};
  const jsonField = form.querySelector('[name="__json"]');
  if (jsonField) return JSON.parse(jsonField.value || "{}");
  Array.from(form.elements).forEach((element) => {
    if (!element.name || element.tagName === "BUTTON") return;
    if (element.name === "csrf_token") return;
    if (skipEmpty && !element.value) return;
    setDotted(payload, element.name, coerceValue(element));
  });
  return payload;
}

function coerceValue(element) {
  if (element.hasAttribute("data-list")) return element.value.split(",").map((v) => v.trim()).filter(Boolean);
  if (element.hasAttribute("data-bool")) return element.value === "true";
  if (element.type === "number") return Number(element.value);
  if (element.value === "true") return true;
  if (element.value === "false") return false;
  return element.value;
}

function setDotted(target, dotted, value) {
  const parts = dotted.split(".");
  let current = target;
  while (parts.length > 1) {
    const part = parts.shift();
    current[part] = current[part] || {};
    current = current[part];
  }
  current[parts[0]] = value;
}
