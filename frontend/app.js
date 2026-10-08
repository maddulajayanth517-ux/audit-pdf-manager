/* Audit PDF Volume & Annexure Manager - frontend (no build step, no external dependencies). */
(function () {
  "use strict";

  const state = {
    doc: null,          // DocumentInfo
    analysis: null,     // AnalysisResponse
    sections: [],       // current sections (with size_bytes, protected)
    sectionSource: "bookmarks",
    plan: null,
    jobId: null,
    pollTimer: null,
    pendingFile: null,
    info: null,
  };

  // ------------------------------------------------------------------ helpers
  const $ = (sel) => document.querySelector(sel);

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === "class") el.className = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else if (k === "text") el.textContent = v;
        else el.setAttribute(k, v === true ? "" : v);
      }
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
    }
    return el;
  }

  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

  // Decimal units, identical to the backend: 1 MB = 1,000,000 bytes.
  function fmtSize(b) {
    if (b === null || b === undefined) return "–";
    if (b >= 1e9) return (b / 1e9).toFixed(2) + " GB";
    if (b >= 1e6) return (b / 1e6).toFixed(1) + " MB";
    if (b >= 1e3) return Math.round(b / 1e3) + " KB";
    return b + " bytes";
  }
  const fmtBytes = (b) => (b || 0).toLocaleString("en-US") + " bytes";

  function errorText(detail, fallback) {
    if (!detail) return fallback;
    if (typeof detail === "string") return detail;
    if (detail.message) return detail.message;
    return fallback;
  }

  async function api(method, url, body) {
    const opts = { method, headers: {} };
    if (body instanceof FormData) opts.body = body;
    else if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, opts);
    } catch (e) {
      throw Object.assign(new Error("Cannot reach the server. Is the application still running?"), { status: 0 });
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* non-JSON */ }
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(errorText(d, `Request failed (HTTP ${res.status}).`));
      err.status = res.status;
      err.code = d && d.code;
      throw err;
    }
    return data;
  }

  function alertBox(kind, text, title) {
    return h("div", { class: "alert " + kind }, title ? h("strong", null, title) : null, text);
  }

  function setStatus(el, text, kind) {
    el.textContent = text || "";
    el.className = "status-line" + (kind ? " " + kind : "");
  }

  function show(id, visible = true) { $(id).hidden = !visible; }

  function resetFrom(step) {
    // Hide everything after a changed step so stale plans/results are never shown.
    const order = ["#step-bookmarks", "#step-requirements", "#step-plan", "#step-progress", "#step-results"];
    const idx = order.indexOf(step);
    order.slice(idx).forEach((id) => show(id, false));
    if (idx <= order.indexOf("#step-plan")) state.plan = null;
  }

  // ------------------------------------------------------------------ 1. upload
  const dropzone = $("#dropzone");
  const fileInput = $("#file-input");

  dropzone.addEventListener("click", () => fileInput.click());
  dropzone.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
  });
  dropzone.addEventListener("dragover", (e) => { e.preventDefault(); dropzone.classList.add("drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("drag"));
  dropzone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
    if (e.dataTransfer.files.length) startUpload(e.dataTransfer.files[0]);
  });
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) startUpload(fileInput.files[0]);
    fileInput.value = "";
  });
  $("#password-btn").addEventListener("click", () => {
    if (state.pendingFile) startUpload(state.pendingFile, $("#password-input").value);
  });
  $("#password-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#password-btn").click(); });

  async function startUpload(file, password) {
    const status = $("#upload-status");
    if (!/\.pdf$/i.test(file.name)) {
      setStatus(status, "Please choose a PDF file (.pdf).", "error");
      return;
    }
    const maxMb = state.info ? state.info.max_upload_mb : null;
    if (maxMb && file.size > maxMb * 1e6) {
      setStatus(status, `The file is larger than the maximum upload size (${maxMb} MB).`, "error");
      return;
    }
    if (state.doc) await discardDocument();
    resetFrom("#step-bookmarks");
    show("#doc-summary", false);
    dropzone.classList.add("busy");
    setStatus(status, `Uploading and analysing “${file.name}” (${fmtSize(file.size)})…`);
    const form = new FormData();
    form.append("file", file);
    if (password) form.append("password", password);
    try {
      const res = await api("POST", "/api/upload", form);
      state.pendingFile = null;
      show("#password-row", false);
      $("#password-input").value = "";
      state.doc = res.document;
      renderDocSummary();
      setStatus(status, "✓ PDF loaded. Reading bookmarks…", "ok");
      await analyze(null);
      setStatus(status, "✓ PDF loaded and analysed.", "ok");
    } catch (err) {
      if (err.code === "password_required" || err.code === "password_incorrect") {
        state.pendingFile = file;
        show("#password-row", true);
        $("#password-input").focus();
      }
      setStatus(status, err.message, "error");
    } finally {
      dropzone.classList.remove("busy");
    }
  }

  function renderDocSummary() {
    const d = state.doc;
    $("#sum-filename").textContent = d.filename;
    $("#sum-size").textContent = `${fmtSize(d.size_bytes)} (${fmtBytes(d.size_bytes)})`;
    $("#sum-pages").textContent = d.page_count.toLocaleString("en-US");
    $("#sum-bookmarks").textContent = d.bookmark_count.toLocaleString("en-US");
    show("#doc-summary", true);
    const client = d.filename.replace(/\.pdf$/i, "").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
    $("#client-name").value = client || "Client";
    updateFilenamePreview();
  }

  // ------------------------------------------------------------------ 2. analysis
  async function analyze(level) {
    const res = await api("POST", "/api/analyze", { document_id: state.doc.document_id, level });
    state.analysis = res;
    state.sections = res.sections;
    state.sectionSource = res.has_usable_bookmarks ? "bookmarks" : "whole";
    renderAnalysis();
    show("#step-bookmarks", true);
    show("#step-requirements", true);
    if (!res.has_usable_bookmarks) $("#manual-editor").open = true;
  }

  function renderAnalysis() {
    const a = state.analysis;
    const warn = clear($("#analysis-warnings"));
    a.warnings.forEach((w, i) => warn.appendChild(alertBox(i === 0 && !a.has_usable_bookmarks ? "error" : "warn", w)));

    const sel = clear($("#level-select"));
    if (!a.levels.length) {
      sel.appendChild(h("option", { value: "" }, "No bookmarks"));
      sel.disabled = true;
    } else {
      sel.disabled = false;
      a.levels.forEach((l) => {
        const label = `Level ${l.level} — ${l.valid} bookmark${l.valid === 1 ? "" : "s"}` +
          (l.keyword_hits ? `, ${l.keyword_hits} Annexure-like` : "") +
          (l.level === a.suggested_level ? " (suggested)" : "");
        sel.appendChild(h("option", { value: l.level, selected: l.level === a.selected_level }, label));
      });
    }
    renderTree();
    renderSections();
    renderManualEditor(state.sections);
  }

  $("#level-select").addEventListener("change", async (e) => {
    const level = parseInt(e.target.value, 10);
    if (!level) return;
    resetFrom("#step-plan");
    setStatus($("#upload-status"), "Re-analysing sections for the selected level…");
    try {
      await analyze(level);
      setStatus($("#upload-status"), "✓ Sections updated.", "ok");
    } catch (err) {
      setStatus($("#upload-status"), err.message, "error");
    }
  });

  function renderTree() {
    const a = state.analysis;
    const root = clear($("#bookmark-tree"));
    if (!a.bookmarks.length) {
      root.appendChild(h("div", { class: "empty" }, "This PDF has no bookmarks."));
      return;
    }
    const unitLevel = a.selected_level;
    const build = (nodes) => h("ul", null, nodes.map((n) => h("li", null,
      h("div", { class: "node" + (unitLevel && n.level <= unitLevel && n.page ? " unit" : ""), title: n.title },
        h("span", { class: "lvl" }, "L" + n.level),
        h("span", { class: "title" }, n.title),
        h("span", { class: "pg" + (n.page ? "" : " bad") }, n.page ? "p. " + n.page : "no page")),
      n.children && n.children.length ? build(n.children) : null)));
    root.appendChild(build(a.bookmarks));
  }

  function renderSections() {
    const tbody = clear($("#sections-table tbody"));
    let pages = 0, bytes = 0;
    state.sections.forEach((s, i) => {
      const n = s.end_page - s.start_page + 1;
      pages += n; bytes += s.size_bytes || 0;
      const cb = h("input", { type: "checkbox", checked: s.protected, "aria-label": "Protected" });
      cb.addEventListener("change", () => {
        state.sections[i].protected = cb.checked;
        resetFrom("#step-plan");
        renderSections();
      });
      tbody.appendChild(h("tr", { class: s.protected ? "" : "unprotected" },
        h("td", null, s.title, s.path ? h("div", { class: "sub" }, s.path) : null,
          !s.protected ? h("div", { class: "sub" }, "Not protected — may be split at a page boundary") : null),
        h("td", { class: "r" }, s.start_page),
        h("td", { class: "r" }, s.end_page),
        h("td", { class: "r" }, n),
        h("td", { class: "r", title: fmtBytes(s.size_bytes) }, fmtSize(s.size_bytes)),
        h("td", { class: "c" }, cb)));
    });
    const tfoot = clear($("#sections-table tfoot"));
    tfoot.appendChild(h("tr", null, h("td", null, "Total"), h("td"), h("td"), h("td", { class: "r" }, pages),
      h("td", { class: "r", title: "Sum of individually extracted sections" }, fmtSize(bytes)), h("td")));
    const prot = state.sections.filter((s) => s.protected).length;
    $("#sections-count").textContent = `(${state.sections.length} sections, ${prot} protected)`;
  }

  // ------------------------------------------------------------------ manual sections
  function manualRow(s) {
    const del = h("button", { class: "btn small link", title: "Remove row" }, "Remove");
    const tr = h("tr", null,
      h("td", null, h("input", { type: "text", value: s.title || "", "data-k": "title", placeholder: "e.g. Annexure A" })),
      h("td", { class: "r" }, h("input", { type: "number", min: 1, value: s.start_page || "", "data-k": "start" })),
      h("td", { class: "r" }, h("input", { type: "number", min: 1, value: s.end_page || "", "data-k": "end" })),
      h("td", { class: "c" }, h("input", { type: "checkbox", checked: s.protected !== false, "data-k": "protected" })),
      h("td", null, del));
    del.addEventListener("click", () => tr.remove());
    return tr;
  }

  function renderManualEditor(sections) {
    const tbody = clear($("#manual-table tbody"));
    sections.filter((s) => s.source !== "whole-document").forEach((s) => tbody.appendChild(manualRow(s)));
    if (!tbody.children.length) tbody.appendChild(manualRow({ title: "Main Report", start_page: 1, end_page: "" }));
  }

  $("#manual-add").addEventListener("click", () => {
    const rows = $("#manual-table tbody").children;
    let next = 1;
    if (rows.length) {
      const lastEnd = parseInt(rows[rows.length - 1].querySelector('[data-k="end"]').value, 10);
      if (lastEnd) next = lastEnd + 1;
    }
    $("#manual-table tbody").appendChild(manualRow({ title: "", start_page: next, end_page: "" }));
  });
  $("#manual-copy").addEventListener("click", () => renderManualEditor(state.sections));
  $("#manual-reset").addEventListener("click", async () => {
    resetFrom("#step-plan");
    try {
      await analyze(state.analysis.selected_level);
      setStatus($("#manual-status"), "✓ Bookmark sections restored.", "ok");
    } catch (err) { setStatus($("#manual-status"), err.message, "error"); }
  });
  $("#manual-apply").addEventListener("click", async () => {
    const status = $("#manual-status");
    const rows = Array.from($("#manual-table tbody").children);
    const sections = [];
    for (const [i, tr] of rows.entries()) {
      const title = tr.querySelector('[data-k="title"]').value.trim();
      const start = parseInt(tr.querySelector('[data-k="start"]').value, 10);
      const end = parseInt(tr.querySelector('[data-k="end"]').value, 10);
      if (!start && !end && !title) continue;
      if (!start || !end) { setStatus(status, `Row ${i + 1}: enter both a start and an end page.`, "error"); return; }
      sections.push({ title: title || `Pages ${start}–${end}`, start_page: start, end_page: end,
        protected: tr.querySelector('[data-k="protected"]').checked, source: "manual" });
    }
    if (!sections.length) { setStatus(status, "Add at least one section.", "error"); return; }
    setStatus(status, "Validating and measuring sections…");
    try {
      const res = await api("POST", "/api/sections/measure", { document_id: state.doc.document_id, sections });
      state.sections = res.sections;
      state.sectionSource = "manual";
      resetFrom("#step-plan");
      renderSections();
      setStatus(status, "✓ Manual sections applied." + (res.warnings.length ? " " + res.warnings.join(" ") : ""), "ok");
    } catch (err) { setStatus(status, err.message, "error"); }
  });

  // ------------------------------------------------------------------ 3. requirements
  function currentMode() { return document.querySelector('input[name="mode"]:checked').value; }

  function updateModeFields() {
    const m = currentMode();
    show("#field-max-size", m !== "num_volumes");
    show("#field-volumes", m !== "max_size");
  }
  document.querySelectorAll('input[name="mode"]').forEach((r) => r.addEventListener("change", () => {
    updateModeFields(); resetFrom("#step-plan");
  }));
  ["#max-size", "#size-unit", "#num-volumes"].forEach((id) => $(id).addEventListener("input", () => resetFrom("#step-plan")));
  // Compression settings affect the size check of the plan, so a changed setting needs a new plan.
  $("#compression-mode").addEventListener("change", () => resetFrom("#step-plan"));

  function updateFilenamePreview() {
    const c = ($("#client-name").value || "Client").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "") || "Client";
    $("#filename-preview").textContent = `Files: ${c}_Audit_Report_Volume_01.pdf … · ZIP: ${c}_Audit_Report_Volumes.zip`;
  }
  $("#client-name").addEventListener("input", updateFilenamePreview);

  function compressionSettings() {
    // Only the mode is chosen by the user. Everything else uses the automatic defaults on the
    // server, which compress as far as needed to meet the required size.
    return { mode: $("#compression-mode").value };
  }

  $("#plan-btn").addEventListener("click", async () => {
    const btn = $("#plan-btn");
    const mode = currentMode();
    const body = {
      document_id: state.doc.document_id,
      sections: state.sections.map((s) => ({ title: s.title, start_page: s.start_page, end_page: s.end_page,
        protected: s.protected, path: s.path || "", source: s.source || "bookmark" })),
      mode,
      size_unit: $("#size-unit").value,
      compression: compressionSettings(),
    };
    if (mode !== "num_volumes") body.max_size = parseFloat($("#max-size").value);
    if (mode !== "max_size") body.num_volumes = parseInt($("#num-volumes").value, 10);
    if (mode !== "num_volumes" && !(body.max_size > 0)) { alert("Enter a maximum size greater than zero."); return; }
    if (mode !== "max_size" && !(body.num_volumes >= 1)) { alert("Enter the number of volumes (1 or more)."); return; }
    btn.disabled = true; btn.textContent = "PLANNING…";
    resetFrom("#step-plan");
    try {
      state.planRequest = body;
      state.plan = await api("POST", "/api/plan", body);
      renderPlan();
      show("#step-plan", true);
      $("#step-plan").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false; btn.textContent = "PLAN VOLUMES";
    }
  });

  // Manual adjustment: move a whole section across the boundary between two neighbouring volumes.
  // Only the boundary moves, so the order of sections can never change; the server re-validates.
  async function adjustPlan(volumeStarts) {
    try {
      const body = Object.assign({}, state.planRequest);
      if (volumeStarts) body.volume_starts = volumeStarts; else delete body.volume_starts;
      state.plan = await api("POST", "/api/plan", body);
      renderPlan();
    } catch (err) { alert(err.message); }
  }

  function moveBoundary(volIndex, direction) {
    const vols = state.plan.volumes;
    const starts = vols.map((v) => v.start_page);
    const v = vols[volIndex];
    if (direction === "prev") starts[volIndex] = v.sections[1].start_page;           // first section → previous volume
    else starts[volIndex + 1] = v.sections[v.sections.length - 1].start_page;        // last section → next volume
    adjustPlan(starts);
  }

  // ------------------------------------------------------------------ 4. plan
  function renderPlan() {
    const p = state.plan;
    const warn = clear($("#plan-warnings"));
    if (p.constraints_satisfied && !p.warnings.length) {
      warn.appendChild(alertBox("ok", "All constraints can be met at original size. No protected section is split."));
    }
    p.warnings.forEach((w) => warn.appendChild(alertBox("warn", w)));
    if (p.adjusted) {
      warn.appendChild(h("div", { class: "alert info" }, "Plan adjusted manually. Section order is unchanged and no protected section is split. ",
        h("button", { class: "btn small", onclick: () => adjustPlan(null) }, "Reset to automatic plan")));
    }
    const grid = clear($("#plan-volumes"));
    const last = p.volumes.length - 1;
    p.volumes.forEach((v, i) => {
      const canSplit = v.sections.length > 1;
      const moves = h("div", { class: "move-row" },
        i > 0 ? h("button", { class: "btn small", disabled: !canSplit, onclick: () => moveBoundary(i, "prev"),
          title: canSplit ? `Move “${v.sections[0].title}” to Volume ${v.index - 1}` : "A volume must keep at least one section" },
          `◀ Move first section to Vol ${v.index - 1}`) : null,
        i < last ? h("button", { class: "btn small", disabled: !canSplit, onclick: () => moveBoundary(i, "next"),
          title: canSplit ? `Move “${v.sections[v.sections.length - 1].title}” to Volume ${v.index + 1}` : "A volume must keep at least one section" },
          `Move last section to Vol ${v.index + 1} ▶`) : null);
      grid.appendChild(h("div", { class: "volume-card" + (v.over_limit ? " over" : "") },
        h("h4", null, `Volume ${v.index}`,
          !v.achievable ? h("span", { class: "badge err" }, "✗ Limit not reachable")
            : v.over_limit ? h("span", { class: "badge warn" }, "⚠ Will be compressed") : h("span", { class: "badge ok" }, "✓ Within limit")),
        h("ul", null, v.sections.map((s) => h("li", null, s.title,
          !s.protected ? h("span", { class: "tag unprot" }, "unprotected") : null,
          s.partial ? h("span", { class: "tag partial" }, `part: p. ${s.start_page}–${s.end_page}`) : null))),
        h("div", { class: "meta" }, `Pages ${v.start_page}–${v.end_page} (${v.page_count})`),
        h("div", { class: "meta" }, "Estimated size: ", h("span", { class: "size", title: fmtBytes(v.estimated_bytes) }, fmtSize(v.estimated_bytes)),
          p.max_bytes ? ` of ${fmtSize(p.max_bytes)} limit` : ""),
        p.max_bytes && v.over_limit && v.min_estimated_bytes != null ? h("div", { class: "meta" }, "Smallest reachable (max. compression): ≈ ",
          h("span", { class: "size" }, fmtSize(v.min_estimated_bytes))) : null,
        moves));
    });
    $("#plan-totals").textContent = `${p.volumes.length} volume(s) planned · original total ${fmtSize(p.total_estimated_bytes)}` +
      " · the actual size of every volume is checked during generation.";
  }

  $("#generate-btn").addEventListener("click", async () => {
    const btn = $("#generate-btn");
    btn.disabled = true;
    try {
      const res = await api("POST", "/api/generate", {
        plan_id: state.plan.plan_id,
        client_name: $("#client-name").value,
        compression: compressionSettings(),
      });
      state.jobId = res.job_id;
      show("#step-results", false);
      clear($("#progress-log"));
      show("#step-progress", true);
      $("#cancel-btn").disabled = false;
      $("#step-progress").scrollIntoView({ behavior: "smooth", block: "start" });
      startPolling();
    } catch (err) {
      alert(err.message);
    } finally {
      btn.disabled = false;
    }
  });

  // ------------------------------------------------------------------ 5. progress
  function startPolling() {
    stopPolling();
    const tick = async () => {
      try {
        const st = await api("GET", `/api/status/${state.jobId}`);
        renderProgress(st);
        if (["completed", "failed", "cancelled"].includes(st.state) && !st.active) {
          stopPolling();
          onJobFinished(st);
          return;
        }
      } catch (err) {
        $("#progress-label").textContent = err.message;
        if (err.status === 404) { stopPolling(); return; }
      }
      state.pollTimer = setTimeout(tick, 1000);
    };
    tick();
  }
  function stopPolling() { if (state.pollTimer) clearTimeout(state.pollTimer); state.pollTimer = null; }

  function renderProgress(st) {
    $("#progress-bar").style.width = Math.round((st.progress || 0) * 100) + "%";
    const labels = { queued: "Waiting to start…", running: "Processing…", completed: "Finished", failed: "Failed", cancelled: "Cancelled" };
    $("#progress-label").textContent = `${labels[st.state] || st.state} · ${Math.round((st.progress || 0) * 100)}%`;
    const log = clear($("#progress-log"));
    const icons = { ok: "✓", warn: "!", error: "✗", info: "·" };
    st.events.forEach((e) => log.appendChild(h("li", { class: e.level }, h("span", { class: "ic" }, icons[e.level] || "·"), h("span", null, e.msg))));
    log.scrollTop = log.scrollHeight;
  }

  $("#cancel-btn").addEventListener("click", async () => {
    if (!state.jobId) return;
    if (!confirm("Cancel processing? Partially generated files will be deleted.")) return;
    $("#cancel-btn").disabled = true;
    try { await api("POST", `/api/cancel/${state.jobId}`); } catch (err) { alert(err.message); }
  });

  function onJobFinished(st) {
    $("#cancel-btn").disabled = true;
    if (st.state === "completed") {
      renderResults(st);
      show("#step-results", true);
      $("#step-results").scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }

  // ------------------------------------------------------------------ 6. results
  const STATUS_BADGE = {
    PASS: ["ok", "PASS"],
    OVER_LIMIT: ["warn", "OVER LIMIT"],
    ACCEPTED_OVER_LIMIT: ["info", "PASS (over limit accepted)"],
    FAIL: ["err", "FAIL"],
  };

  function renderResults(st) {
    const s = st.summary || {};
    const banner = $("#result-banner");
    banner.textContent = s.message || "";
    banner.className = "banner " + (s.validation_passed === false ? "err" : s.ready ? "ok" : "warn");

    // Conflicts: volumes still over the limit after all automatic compression.
    const conflicts = clear($("#conflicts"));
    st.volumes.filter((v) => v.status === "OVER_LIMIT").forEach((v) => {
      const names = v.sections.map((x) => x.title).join(", ");
      const box = h("div", { class: "conflict" },
        h("p", null, h("strong", null, `${names} cannot currently be reduced below ${fmtSize(st.max_bytes)} without splitting the protected section.`)),
        h("p", null, `Smallest size achieved: ${fmtSize(v.smallest_bytes)}. Original quality: ${fmtSize(v.original_quality_bytes)}. ` +
          "The section has NOT been split. Choose how to proceed:"));
      const row = h("div", { class: "btn-row" });
      row.appendChild(h("button", { class: "btn primary", onclick: () => resolve(v.index, "stronger"),
        title: "Rasterise pages at 30–24 DPI greyscale. Text will be barely legible." }, "Try stronger compression"));
      row.appendChild(h("button", { class: "btn", onclick: () => resolve(v.index, "accept_smallest") },
        `Allow this volume to exceed the limit (smallest, ${fmtSize(v.smallest_bytes)})`));
      if (v.original_quality_bytes) {
        row.appendChild(h("button", { class: "btn", onclick: () => resolve(v.index, "accept_original") },
          `Allow exceed — keep original quality (${fmtSize(v.original_quality_bytes)})`));
      }
      row.appendChild(h("button", { class: "btn link", onclick: reviewSections }, "Review sections"));
      row.appendChild(h("button", { class: "btn link", onclick: finishAndDelete }, "Cancel"));
      box.appendChild(row);
      conflicts.appendChild(box);
    });

    const tbody = clear($("#results-table tbody"));
    st.volumes.forEach((v) => {
      const [cls, label] = STATUS_BADGE[v.status] || ["err", v.status];
      const canDownload = v.status !== "FAIL";
      tbody.appendChild(h("tr", null,
        h("td", null, h("strong", null, String(v.index)), h("div", { class: "sub" }, v.filename)),
        h("td", null, v.sections.map((x) => x.title).join(" + ")),
        h("td", { class: "r" }, `${v.start_page}–${v.end_page}`, h("div", { class: "sub" }, `${v.page_count} pages`)),
        h("td", { class: "r", title: fmtBytes(v.size_bytes) }, fmtSize(v.size_bytes), h("div", { class: "sub" }, fmtBytes(v.size_bytes))),
        h("td", null, v.compression_level === 0 ? "None" : `Level ${v.compression_level}`,
          h("div", { class: "sub" }, v.compression_label || ""),
          v.extreme ? h("div", { class: "extreme-note" }, "Extreme compression applied — document quality may be significantly reduced.") : null,
          v.text_preserved === false ? h("div", { class: "sub" }, "Text not searchable (pages rasterised)") : null),
        h("td", { class: "c" }, h("span", { class: "badge " + cls }, label)),
        h("td", null, canDownload
          ? h("a", { class: "btn small", href: `/api/download/${v.volume_id}`, download: v.filename }, `Download Volume ${v.index}`)
          : h("span", { class: "sub" }, "Not available"))));
    });

    const totals = clear($("#result-totals"));
    const item = (k, val) => h("div", null, h("dt", null, k), h("dd", null, val));
    totals.appendChild(item("Original PDF", fmtSize(s.original_bytes)));
    totals.appendChild(item("Final total", fmtSize(s.total_bytes)));
    totals.appendChild(item("Volumes", String(s.volume_count)));
    totals.appendChild(item("Validation", s.validation_passed ? "PASS" : "FAIL"));

    const cov = clear($("#coverage-checks"));
    cov.appendChild(h("h3", null, "Whole-document checks"));
    cov.appendChild(checklist(s.coverage || []));

    const details = clear($("#volume-details"));
    details.appendChild(h("h3", null, "Validation per volume"));
    st.volumes.forEach((v) => {
      const d = h("details", { class: "vol-detail" },
        h("summary", null, `Volume ${v.index} — pages ${v.start_page}–${v.end_page} · ${fmtSize(v.size_bytes)} · ` +
          `${v.protected_count} protected section(s) · Validation: ${(v.validation || {}).passed ? "PASS" : "FAIL"}`),
        checklist((v.validation || {}).checks || []));
      details.appendChild(d);
    });

    const zip = $("#zip-btn");
    zip.href = `/api/download-zip/${st.job_id}`;
    zip.classList.toggle("disabled", !s.ready);
    zip.title = s.ready ? (st.zip_name || "") : "Resolve the volumes above first";
    $("#report-btn").href = `/api/report/${st.job_id}`;
  }

  function checklist(checks) {
    return h("ul", { class: "checklist" }, checks.map((c) => {
      const kind = c.ok ? "ok" : (c.severity === "error" ? "err" : "warn");
      const label = c.ok ? "PASS" : (c.severity === "error" ? "FAIL" : "WARN");
      return h("li", null, h("span", { class: "mark " + kind }, label), h("span", null, h("strong", null, c.name + ": "), c.detail));
    }));
  }

  async function resolve(index, action) {
    if (action === "stronger" &&
        !confirm("Stronger compression goes below the automatic floor: every page is rasterised at 30–24 DPI greyscale. Text will not be searchable and may be hard to read. Continue?")) return;
    try {
      const st = await api("POST", `/api/jobs/${state.jobId}/volumes/${index}/resolve`, { action });
      if (action === "stronger") {
        show("#step-progress", true);
        $("#step-progress").scrollIntoView({ behavior: "smooth", block: "start" });
        startPolling();
      } else {
        renderResults(st);
      }
    } catch (err) { alert(err.message); }
  }

  function reviewSections() {
    show("#step-results", false);
    show("#step-progress", false);
    show("#step-plan", false);
    $("#step-bookmarks").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  $("#new-plan-btn").addEventListener("click", () => {
    show("#step-results", false);
    show("#step-progress", false);
    $("#step-requirements").scrollIntoView({ behavior: "smooth", block: "start" });
  });

  async function discardDocument() {
    stopPolling();
    if (state.doc) {
      try { await api("DELETE", `/api/documents/${state.doc.document_id}`); } catch (e) { /* already gone */ }
    }
    state.doc = null; state.analysis = null; state.sections = []; state.plan = null; state.jobId = null;
  }

  async function finishAndDelete() {
    if (!confirm("Delete the uploaded PDF and all generated volumes from the server now?")) return;
    await discardDocument();
    resetFrom("#step-bookmarks");
    show("#doc-summary", false);
    setStatus($("#upload-status"), "✓ All temporary files for this document were deleted.", "ok");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  $("#finish-btn").addEventListener("click", finishAndDelete);

  // ------------------------------------------------------------------ init
  async function init() {
    updateModeFields();
    try {
      const info = await api("GET", "/api/compression/info");
      state.info = info;
      $("#unit-note").textContent = info.unit_note;
    } catch (e) { /* non-critical */ }
  }
  init();
})();
