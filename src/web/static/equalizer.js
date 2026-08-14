// Equalizer worksheet grid — row edits/assignments autosave via small JSON
// endpoints (see routes_equalizer.py's module docstring for why this page
// deviates from the dashboard's usual full-page-reload-per-action pattern).
// Settings/Tax Rates saves are the exception: those PATCH then reload the
// page, since a naming-mode change touches column headers, the PDF, and
// every modal at once — simpler to let the server-rendered page redraw all
// of it than to patch each place in JS.

const RATE_LABELS = { none: "None", ordinary: "Ordinary", lt: "LT Gain", st: "ST Gain" };
const RATE_FIELDS = ["fed_rate_a", "fed_rate_b", "state_rate_a", "state_rate_b", "lt_rate_a", "lt_rate_b", "st_rate_a", "st_rate_b"];
const moneyFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function fmt(n) {
  return moneyFmt.format(n || 0);
}

function initEqualizerGrid(config) {
  const tbody = document.querySelector("#eq-grid tbody");
  const taxModal = document.getElementById("eq-tax-rates-modal");
  const taxRatesHint = document.getElementById("eq-tax-rates-hint");
  let selectedId = null;
  let currentRates = RATE_FIELDS.reduce((acc, f) => ({ ...acc, [f]: config.worksheet[f] || 0 }), {});

  function ratesAreUnset() {
    return RATE_FIELDS.every((f) => !currentRates[f]);
  }

  function openTaxRatesModal(hintText) {
    if (hintText) {
      taxRatesHint.textContent = hintText;
      taxRatesHint.style.display = "block";
    } else {
      taxRatesHint.style.display = "none";
    }
    taxModal.style.display = "flex";
  }

  async function api(method, path, body) {
    const resp = await fetch(path, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (!resp.ok) {
      alert("Save failed: " + (await resp.text()));
      throw new Error("save failed");
    }
    return resp.status === 204 ? null : resp.json();
  }

  function numOrNull(input) {
    if (input.value === "") return null;
    return parseFloat(input.value);
  }

  function numOrZero(input) {
    return input.value === "" ? 0 : parseFloat(input.value);
  }

  // An After-Tax cell shows the auto-computed figure (muted, with a
  // disabled reset button) until someone actually edits it — at which
  // point it becomes a manual override (normal styling, reset button
  // enabled) that always wins over the computed figure. Previously this
  // cell just showed a bare "auto" placeholder with no number behind it,
  // which told you nothing.
  function afterTaxCellHtml(item, side) {
    const raw = item[`after_tax_${side}`];
    const computed = item[`after_tax_${side}_computed`] ?? 0;
    const isAuto = raw === null || raw === undefined;
    const value = isAuto ? computed : raw;
    return `<div class="eq-after-tax">
      <input type="number" step="0.01" data-field="after_tax_${side}" value="${value}" class="${isAuto ? "eq-auto" : ""}" title="${isAuto ? "Auto-computed — edit to override" : "Manual override"}">
      <button type="button" class="eq-reset-auto" data-field="after_tax_${side}" title="Reset to auto-computed" ${isAuto ? "disabled" : ""}>&#8635;</button>
    </div>`;
  }

  function buildRow(item) {
    const tr = document.createElement("tr");
    tr.dataset.itemId = item.id;

    const cell = (html) => {
      const td = document.createElement("td");
      td.innerHTML = html;
      return td;
    };

    tr.appendChild(cell(`<span class="eq-row-num"></span>`));
    tr.appendChild(cell(`<input type="text" data-field="description" value="${(item.description || "").replace(/"/g, "&quot;")}">`));
    tr.appendChild(cell(`<input type="number" step="0.01" data-field="fmv" value="${item.fmv}">`));
    tr.appendChild(cell(`<input type="number" step="0.01" data-field="debt" value="${item.debt}">`));
    tr.appendChild(cell(`<span class="eq-equity"></span>`));
    tr.appendChild(cell(`<input type="number" step="0.01" data-field="before_tax_a" value="${item.before_tax_a}">`));
    tr.appendChild(cell(`<input type="number" step="0.01" data-field="before_tax_b" value="${item.before_tax_b}">`));
    tr.appendChild(cell(`<input type="number" step="0.01" data-field="tax_basis" placeholder="FMV" value="${item.tax_basis ?? ""}">`));

    const rateSelect = document.createElement("select");
    rateSelect.dataset.field = "rate_type";
    for (const [value, label] of Object.entries(RATE_LABELS)) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      if (item.rate_type === value) opt.selected = true;
      rateSelect.appendChild(opt);
    }
    const rateTd = document.createElement("td");
    rateTd.appendChild(rateSelect);
    tr.appendChild(rateTd);

    tr.appendChild(cell(`<input type="checkbox" data-field="gain_loss" ${item.gain_loss ? "checked" : ""}>`));
    tr.appendChild(cell(afterTaxCellHtml(item, "a")));
    tr.appendChild(cell(afterTaxCellHtml(item, "b")));
    tr.appendChild(cell(config.readonly ? "" : `<button type="button" class="secondary eq-delete-btn">&times;</button>`));

    if (config.readonly) {
      tr.querySelectorAll("input, select, button").forEach((el) => (el.disabled = true));
    }
    refreshComputedCells(tr, item);
    return tr;
  }

  // Equity display, plus a visual flag for a row that has real equity but
  // hasn't actually been split yet (Before-Tax A and B both still $0) — the
  // easy-to-miss trap where someone fills in FMV/Debt and assumes that's
  // enough, when the equalization math only ever reads the Before-Tax
  // columns. Renumbering happens separately (renumberRows()) since it needs
  // to run across the whole table, not just one row.
  function refreshComputedCells(tr, item) {
    const equityVal = (Number(item.fmv) || 0) - (Number(item.debt) || 0);
    tr.querySelector(".eq-equity").textContent = fmt(equityVal);

    const beforeA = Number(item.before_tax_a) || 0;
    const beforeB = Number(item.before_tax_b) || 0;
    const unassigned = equityVal !== 0 && beforeA === 0 && beforeB === 0;
    tr.classList.toggle("eq-unassigned", unassigned);
    const numEl = tr.querySelector(".eq-row-num");
    numEl.title = unassigned
      ? "Equity not yet assigned to either party — select this row and click H, W, or =, or type into the Assigned columns."
      : "";
  }

  function renumberRows() {
    Array.from(tbody.children).forEach((tr, idx) => {
      tr.querySelector(".eq-row-num").textContent = String(idx + 1);
    });
  }

  function applyItemToRow(tr, item) {
    tr.querySelectorAll("[data-field]").forEach((input) => {
      const field = input.dataset.field;
      if (field.startsWith("after_tax")) return; // rebuilt separately below
      if (field === "gain_loss") {
        input.checked = !!item[field];
      } else if (document.activeElement !== input) {
        input.value = item[field] ?? "";
      }
    });
    ["a", "b"].forEach((side) => {
      const input = tr.querySelector(`input[data-field="after_tax_${side}"]`);
      if (input && document.activeElement !== input) {
        input.closest("td").innerHTML = afterTaxCellHtml(item, side);
      }
    });
    refreshComputedCells(tr, item);
  }

  function renderTotals(totals) {
    document.getElementById("eq-total-fmv").textContent = fmt(totals.total_fmv);
    document.getElementById("eq-total-debt").textContent = fmt(totals.total_debt);
    document.getElementById("eq-total-equity").textContent = fmt(totals.total_equity);
    document.getElementById("eq-total-before-a").textContent = fmt(totals.total_before_tax_a);
    document.getElementById("eq-total-before-b").textContent = fmt(totals.total_before_tax_b);
    document.getElementById("eq-total-after-a").textContent = fmt(totals.total_after_tax_a);
    document.getElementById("eq-total-after-b").textContent = fmt(totals.total_after_tax_b);

    const el = document.getElementById("eq-equalization");
    if (!totals.payer) {
      el.textContent = "Division is balanced at 50/50 — no equalization payment required.";
    } else {
      const payerLabel = totals.payer === "a" ? config.worksheet.party_a_label : config.worksheet.party_b_label;
      const payeeLabel = totals.payer === "a" ? config.worksheet.party_b_label : config.worksheet.party_a_label;
      el.textContent = `Equalization payment: ${payerLabel} pays ${payeeLabel} ${fmt(totals.equalization_amount)} to balance the division at 50/50.`;
    }
  }

  function selectRow(id) {
    selectedId = id;
    tbody.querySelectorAll("tr").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.itemId) === id));
    document.querySelectorAll(".eq-assign-btn").forEach((btn) => (btn.disabled = config.readonly || selectedId == null));
  }

  // Initial render
  config.items.forEach((item) => tbody.appendChild(buildRow(item)));
  renumberRows();
  renderTotals(computeTotalsClientSide());

  function computeTotalsClientSide() {
    // Rough initial totals before any edit — good enough for first paint;
    // the server's authoritative totals arrive on the very next mutation.
    // Using the same field names as calc.WorksheetTotals so renderTotals
    // doesn't need two code paths.
    let total_fmv = 0, total_debt = 0, total_before_tax_a = 0, total_before_tax_b = 0;
    let total_after_tax_a = 0, total_after_tax_b = 0;
    config.items.forEach((i) => {
      total_fmv += i.fmv || 0;
      total_debt += i.debt || 0;
      total_before_tax_a += i.before_tax_a || 0;
      total_before_tax_b += i.before_tax_b || 0;
      total_after_tax_a += i.after_tax_a ?? i.after_tax_a_computed ?? i.before_tax_a ?? 0;
      total_after_tax_b += i.after_tax_b ?? i.after_tax_b_computed ?? i.before_tax_b ?? 0;
    });
    const imbalance = Math.round((total_before_tax_a - total_before_tax_b) * 100) / 100;
    return {
      total_fmv, total_debt, total_equity: total_fmv - total_debt,
      total_before_tax_a, total_before_tax_b, total_after_tax_a, total_after_tax_b,
      equalization_amount: Math.round(Math.abs(imbalance) / 2 * 100) / 100,
      payer: imbalance === 0 ? null : imbalance > 0 ? "a" : "b",
    };
  }

  if (config.readonly) return;

  tbody.addEventListener("change", async (e) => {
    const input = e.target.closest("[data-field]");
    if (!input) return;
    const tr = input.closest("tr");
    const itemId = Number(tr.dataset.itemId);
    const field = input.dataset.field;

    let value;
    if (input.type === "checkbox") value = input.checked;
    else if (field === "description") value = input.value;
    else if (field === "tax_basis" || field.startsWith("after_tax")) value = numOrNull(input);
    else if (input.tagName === "SELECT") value = input.value;
    else value = numOrZero(input);

    const { item, totals } = await api("PATCH", `/equalizer/${config.worksheetId}/items/${itemId}`, { [field]: value });
    applyItemToRow(tr, item);
    renderTotals(totals);

    // Checking G/L with no tax rates configured yet does nothing visible
    // (the after-tax formula multiplies by a 0 rate) — nudge straight to
    // the Tax Rates panel instead of letting that silently no-op.
    if (field === "gain_loss" && value === true && ratesAreUnset()) {
      const label = (item.description || "This row").trim() || "This row";
      openTaxRatesModal(`${label} has Gain/Loss checked, but no tax rates are set yet — enter them below (or come back once you have real figures).`);
    }
  });

  tbody.addEventListener("click", async (e) => {
    const resetBtn = e.target.closest(".eq-reset-auto");
    if (resetBtn) {
      const tr = resetBtn.closest("tr");
      const itemId = Number(tr.dataset.itemId);
      const field = resetBtn.dataset.field;
      const { item, totals } = await api("PATCH", `/equalizer/${config.worksheetId}/items/${itemId}`, { [field]: null });
      applyItemToRow(tr, item);
      renderTotals(totals);
      return;
    }
    if (e.target.closest(".eq-delete-btn")) {
      const tr = e.target.closest("tr");
      const itemId = Number(tr.dataset.itemId);
      if (!confirm("Remove this row?")) return;
      const { totals } = await api("DELETE", `/equalizer/${config.worksheetId}/items/${itemId}`);
      tr.remove();
      renumberRows();
      if (selectedId === itemId) selectRow(null);
      renderTotals(totals);
      return;
    }
    const tr = e.target.closest("tr");
    if (tr) selectRow(Number(tr.dataset.itemId));
  });

  document.querySelectorAll(".eq-assign-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (selectedId == null) return;
      const { item, totals } = await api("POST", `/equalizer/${config.worksheetId}/items/${selectedId}/assign`, { side: btn.dataset.side });
      applyItemToRow(tbody.querySelector(`tr[data-item-id="${selectedId}"]`), item);
      renderTotals(totals);
    });
  });

  document.getElementById("eq-add-row").addEventListener("click", async () => {
    const { item, totals } = await api("POST", `/equalizer/${config.worksheetId}/items`, {});
    tbody.appendChild(buildRow(item));
    renumberRows();
    renderTotals(totals);
  });

  // ---- Settings modal ----
  const settingsModal = document.getElementById("eq-settings-modal");
  const namingSelect = document.getElementById("eq-naming-mode");
  const nameFields = document.getElementById("eq-name-fields");
  const nameWarning = document.getElementById("eq-name-warning");
  const partyALabelInput = document.getElementById("eq-party-a-label");
  const partyBLabelInput = document.getElementById("eq-party-b-label");
  const startingNamingMode = namingSelect.value;

  function syncNameFieldsVisibility() {
    nameFields.style.display = namingSelect.value === "first_names" ? "block" : "none";
  }

  async function autofillNames() {
    const names = await api("POST", `/equalizer/${config.worksheetId}/settings/autofill-names`, undefined);
    partyALabelInput.value = names.party_a_label || "";
    partyBLabelInput.value = names.party_b_label || "";
  }

  namingSelect.addEventListener("change", () => {
    syncNameFieldsVisibility();
    // Switching TO first-names mode for the first time (was husband_wife
    // when the modal opened) leaves the fields showing the literal words
    // "Husband"/"Wife" — carried straight from the DB defaults, not real
    // names — which reads as if a name were already set when it isn't.
    // Clear them and try to autofill immediately so that's obvious.
    if (namingSelect.value === "first_names" && startingNamingMode === "husband_wife") {
      partyALabelInput.value = "";
      partyBLabelInput.value = "";
      autofillNames();
    }
  });
  syncNameFieldsVisibility();

  document.getElementById("eq-settings-btn").addEventListener("click", () => (settingsModal.style.display = "flex"));

  document.getElementById("eq-autofill-names").addEventListener("click", autofillNames);

  document.getElementById("eq-settings-save").addEventListener("click", async () => {
    const namingMode = namingSelect.value;
    const partyALabel = namingMode === "husband_wife" ? "Husband" : partyALabelInput.value.trim();
    const partyBLabel = namingMode === "husband_wife" ? "Wife" : partyBLabelInput.value.trim();

    if (namingMode === "first_names" && (!partyALabel || !partyBLabel)) {
      nameWarning.style.display = "block";
      return;
    }
    nameWarning.style.display = "none";

    const payload = {
      naming_mode: namingMode,
      party_a_label: partyALabel,
      party_b_label: partyBLabel,
      party_a_role: document.getElementById("eq-party-a-role").value,
      party_b_role: document.getElementById("eq-party-b-role").value,
    };
    await api("PATCH", `/equalizer/${config.worksheetId}/settings`, payload);
    window.location.reload();
  });

  // ---- Tax Rates modal ----
  document.getElementById("eq-tax-rates-btn").addEventListener("click", () => openTaxRatesModal());

  document.getElementById("eq-tax-rates-save").addEventListener("click", async () => {
    const payload = {};
    RATE_FIELDS.forEach((field) => {
      payload[field] = numOrZero(document.getElementById(`eq-${field.replace(/_/g, "-")}`));
    });
    await api("PATCH", `/equalizer/${config.worksheetId}/settings`, payload);
    window.location.reload();
  });

  document.querySelectorAll(".eq-modal-cancel").forEach((btn) => {
    btn.addEventListener("click", () => {
      settingsModal.style.display = "none";
      taxModal.style.display = "none";
    });
  });
}
