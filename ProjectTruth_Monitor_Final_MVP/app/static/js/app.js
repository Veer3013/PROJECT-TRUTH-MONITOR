/* ProjectTruth Monitor progressive enhancements. Every flow remains usable without JS. */
(() => {
  "use strict";

  const nav = document.querySelector("#site-navigation");
  const navToggle = document.querySelector("[data-nav-toggle]");

  if (nav && navToggle) {
    navToggle.addEventListener("click", () => {
      const open = nav.classList.toggle("is-open");
      navToggle.setAttribute("aria-expanded", String(open));
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && nav.classList.contains("is-open")) {
        nav.classList.remove("is-open");
        navToggle.setAttribute("aria-expanded", "false");
        navToggle.focus();
      }
    });
  }

  document.addEventListener("click", (event) => {
    const dismiss = event.target.closest("[data-dismiss]");
    if (dismiss) dismiss.closest(".flash")?.remove();

    const disclosure = event.target.closest("[data-disclosure]");
    if (disclosure) {
      const target = document.getElementById(disclosure.getAttribute("aria-controls"));
      if (target) {
        const expanded = disclosure.getAttribute("aria-expanded") === "true";
        disclosure.setAttribute("aria-expanded", String(!expanded));
        target.hidden = expanded;
      }
    }
  });

  document.querySelectorAll("[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  document.querySelectorAll("[data-file-input]").forEach((input) => {
    input.addEventListener("change", () => {
      const output = document.querySelector(`[data-file-name="${input.id}"]`);
      if (output) output.textContent = input.files?.[0]?.name || "No file selected";
    });
  });

  document.querySelectorAll("[data-table-filter]").forEach((input) => {
    const table = document.getElementById(input.dataset.tableFilter);
    if (!table) return;
    input.addEventListener("input", () => {
      const query = input.value.trim().toLocaleLowerCase();
      table.querySelectorAll("tbody tr").forEach((row) => {
        row.hidden = query !== "" && !row.textContent.toLocaleLowerCase().includes(query);
      });
    });
  });

  const announce = (message, kind = "info") => {
    let stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      stack.setAttribute("aria-live", "polite");
      document.querySelector("#main-content, #login-form")?.prepend(stack);
    }
    const notice = document.createElement("div");
    notice.className = `flash flash--${kind}`;
    notice.setAttribute("role", kind === "error" ? "alert" : "status");
    const copy = document.createElement("p");
    copy.textContent = message;
    notice.append(copy);
    stack.prepend(notice);
    notice.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  const errorMessage = (payload, fallback) => {
    const structured = payload?.error;
    if (typeof structured === "string") return structured;
    if (structured?.message) return structured.message;
    if (Array.isArray(structured?.fields) && structured.fields[0]?.message) {
      return structured.fields[0].message;
    }
    const detail = payload?.detail;
    if (typeof detail === "string") return detail;
    if (detail?.message) return detail.message;
    if (Array.isArray(detail) && detail[0]?.msg) return detail[0].msg;
    return fallback;
  };

  const formToJSON = (form) => {
    const data = new FormData(form);
    const result = {};
    const names = new Set([...data.keys()]);
    names.forEach((name) => {
      if (name === "csrf_token" || name === "_method") return;
      const controls = [...form.elements].filter((control) => control.name === name);
      const values = data.getAll(name).filter((value) => !(value instanceof File));
      if (controls.length > 1 || controls.some((control) => control.hasAttribute("data-json-list"))) {
        result[name] = values;
        return;
      }
      const control = controls[0];
      if (control?.type === "checkbox") {
        result[name] = control.checked;
      } else if (control?.type === "number" && values[0] !== "") {
        result[name] = Number(values[0]);
      } else if (values[0] !== "") {
        result[name] = values[0];
      }
    });
    return result;
  };

  const sendForm = async (form, { json = false, multipart = false } = {}) => {
    const submit = form.querySelector('[type="submit"]');
    const csrf = form.querySelector('[name="csrf_token"]')?.value || "";
    const override = form.querySelector('[name="_method"]')?.value;
    const method = (override || form.method || "POST").toUpperCase();
    const headers = { Accept: "application/json" };
    if (csrf) headers["X-CSRF-Token"] = csrf;
    let body;
    if (json) {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(formToJSON(form));
    } else {
      body = new FormData(form);
      if (!multipart) {
        body = new URLSearchParams([...body].filter(([, value]) => !(value instanceof File)));
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8";
      }
    }
    form.setAttribute("aria-busy", "true");
    if (submit) submit.disabled = true;
    try {
      const response = await fetch(form.action, { method, headers, body, credentials: "same-origin" });
      const payload = response.status === 204 ? null : await response.json().catch(() => null);
      if (!response.ok) throw new Error(errorMessage(payload, `Request failed (${response.status}).`));
      const destination = form.dataset.successUrl;
      if (destination) window.location.assign(destination);
      else {
        announce(form.dataset.successMessage || "The change was recorded.", "success");
        if (form.dataset.reload === "true") window.location.reload();
      }
      return payload;
    } catch (error) {
      announce(error.message || "The request could not be completed.", "error");
      return null;
    } finally {
      form.removeAttribute("aria-busy");
      if (submit) submit.disabled = false;
    }
  };

  document.querySelectorAll("form[data-api-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      sendForm(form, { json: true });
    });
  });

  document.querySelectorAll("form[data-fetch-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      sendForm(form);
    });
  });

  document.querySelectorAll("form[data-multipart-form]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      sendForm(form, { multipart: true });
    });
  });

  document.querySelectorAll("form[data-csv-preview]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      const input = form.querySelector('input[type="file"]');
      const file = input?.files?.[0];
      if (!file) return announce("Select a CSV file first.", "error");
      const csrf = form.querySelector('[name="csrf_token"]')?.value || "";
      const submit = form.querySelector('[type="submit"]');
      form.setAttribute("aria-busy", "true");
      if (submit) submit.disabled = true;
      try {
        const response = await fetch(form.action, {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf, Accept: "application/json" },
          body: JSON.stringify({ csv_text: await file.text() }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(errorMessage(payload, `Preview failed (${response.status}).`));
        const target = document.querySelector("#csv-preview-live");
        const tbody = target?.querySelector("tbody");
        if (!target || !tbody) return;
        tbody.replaceChildren();
        payload.rows.forEach((row) => {
          const tr = document.createElement("tr");
          const identity = row.normalized.registration_number || row.normalized.project_name || "Identity missing";
          const state = row.errors.length ? row.errors.join("; ") : row.duplicate ? "Duplicate - existing identity will be reused" : "Valid";
          [row.row_number, identity, row.normalized.developer_name || "Not provided", row.normalized.monitoring_cadence || "weekly", state].forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.append(td);
          });
          tbody.append(tr);
        });
        target.querySelector("[data-preview-summary]").textContent = `${payload.valid_count} valid - ${payload.error_count} with errors`;
        const commit = target.querySelector("form[data-csv-commit]");
        commit.querySelector('[name="preview_token"]').value = payload.preview_token;
        commit.querySelector('[name="watchlist_id"]').value = form.querySelector('[name="watchlist_id"]').value;
        commit.querySelector('[name="idempotency_key"]').value = `csv-${crypto.randomUUID()}`;
        commit.querySelector('[type="submit"]').disabled = payload.error_count > 0;
        target.hidden = false;
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (error) {
        announce(error.message || "The CSV preview could not be completed.", "error");
      } finally {
        form.removeAttribute("aria-busy");
        if (submit) submit.disabled = false;
      }
    });
  });
})();
