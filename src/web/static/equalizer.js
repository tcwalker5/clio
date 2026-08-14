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

// Comma-groups the integer part of a money-field's raw text, live, while
// preserving whatever decimal portion is there as typed — deliberately NOT
// a Number()/toLocaleString() round-trip, which would silently eat a
// trailing "." or in-progress decimal digits (typing "1000.5" would
// collapse back to "1,000" the instant the "." was typed). Sign and
// decimals pass through untouched; only digit-grouping happens.
function formatMoneyLive(str) {
  const negative = str.includes("-");
  const body = str.replace(/-/g, "");
  const dotIndex = body.indexOf(".");
  const rawInt = dotIndex === -1 ? body : body.slice(0, dotIndex);
  const rawDec = dotIndex === -1 ? null : body.slice(dotIndex + 1);
  const intPart = rawInt.replace(/\D/g, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  let result = intPart;
  if (rawDec !== null) result += "." + rawDec.replace(/\D/g, "");
  return (negative ? "-" : "") + result;
}

// Format-on-load / after a save round-trip, where there's no in-progress
// typing or cursor to preserve — just comma-group a plain number.
function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "";
  return formatMoneyLive(String(value));
}

function parseMoneyInput(str) {
  return str.replace(/,/g, "").trim();
}

// Where to put the cursor in a freshly-reformatted string so it lands in
// the same spot relative to the digits the user was looking at — count
// digits before the old cursor position, then find the position after
// that many digits in the new (possibly re-comma'd) string. Commas
// shifting around a cursor is exactly the bug naive live-formatting hits.
function cursorPosForDigitCount(formatted, digitCount) {
  if (digitCount <= 0) return 0;
  let count = 0;
  for (let i = 0; i < formatted.length; i++) {
    if (/[0-9.]/.test(formatted[i])) {
      count++;
      if (count === digitCount) return i + 1;
    }
  }
  return formatted.length;
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

  // Comma-formatted money fields are type="text" now, not type="number",
  // so there's no browser-level guarantee the string parses cleanly —
  // strip commas first, and fall back to empty/0 on garbage input (e.g. a
  // stray paste) rather than sending NaN, which JSON.stringify silently
  // turns into null and would crash the NOT NULL fmv/debt columns.
  function numOrNull(input) {
    const raw = parseMoneyInput(input.value);
    if (raw === "") return null;
    const n = parseFloat(raw);
    return Number.isNaN(n) ? null : n;
  }

  function numOrZero(input) {
    const raw = parseMoneyInput(input.value);
    if (raw === "") return 0;
    const n = parseFloat(raw);
    return Number.isNaN(n) ? 0 : n;
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
      <input type="text" inputmode="decimal" class="eq-money ${isAuto ? "eq-auto" : ""}" data-field="after_tax_${side}" value="${formatMoney(value)}" title="${isAuto ? "Auto-computed — edit to override" : "Manual override"}">
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
    tr.appendChild(cell(`<input type="text" inputmode="decimal" class="eq-money" data-field="fmv" value="${formatMoney(item.fmv)}">`));
    tr.appendChild(cell(`<input type="text" inputmode="decimal" class="eq-money" data-field="debt" value="${formatMoney(item.debt)}">`));
    tr.appendChild(cell(`<span class="eq-equity"></span>`));
    tr.appendChild(cell(`<input type="text" inputmode="decimal" class="eq-money" data-field="before_tax_a" value="${formatMoney(item.before_tax_a)}">`));
    tr.appendChild(cell(`<input type="text" inputmode="decimal" class="eq-money" data-field="before_tax_b" value="${formatMoney(item.before_tax_b)}">`));
    tr.appendChild(cell(`<input type="text" inputmode="decimal" class="eq-money" data-field="tax_basis" placeholder="FMV" value="${formatMoney(item.tax_basis)}">`));

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
    tr.appendChild(cell(`<button type="button" class="secondary eq-delete-btn">&times;</button>`));

    refreshComputedCells(tr, item);
    return tr;
  }

  // Equity display, plus a visual flag for a row whose Assigned columns
  // don't add up to its equity — catches both a row that's completely
  // unassigned (Before-Tax A and B both still $0, the easy-to-miss trap
  // where someone fills in FMV/Debt and assumes that's enough) AND a
  // row that's only *partially* assigned (e.g. Assigned sums to $15,000
  // on a $35,000-equity row — a real case that produced a silently wrong
  // equalization number, 2026-08-14, since the equalization math only
  // ever reads the Assigned/Before-Tax columns, not FMV/Debt directly).
  // Renumbering happens separately (renumberRows()) since it needs to run
  // across the whole table, not just one row.
  function refreshComputedCells(tr, item) {
    const equityVal = (Number(item.fmv) || 0) - (Number(item.debt) || 0);
    tr.querySelector(".eq-equity").textContent = fmt(equityVal);

    const beforeA = Number(item.before_tax_a) || 0;
    const beforeB = Number(item.before_tax_b) || 0;
    const mismatch = Math.round((equityVal - (beforeA + beforeB)) * 100) / 100;
    tr.classList.toggle("eq-unassigned", mismatch !== 0);
    const numEl = tr.querySelector(".eq-row-num");
    numEl.title = mismatch !== 0
      ? `Assigned columns (${fmt(beforeA + beforeB)}) don't add up to this row's equity (${fmt(equityVal)}) — off by ${fmt(mismatch)}. Select this row and click H, W, or =, or fix the Assigned columns directly.`
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
        input.value = input.classList.contains("eq-money") ? formatMoney(item[field]) : item[field] ?? "";
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

    // Two lines, matching the legacy Propertizer tool's own Summary page —
    // one equalization figure from Before-Tax totals, one from After-Tax.
    // They're identical whenever nothing on the worksheet uses G/L (the
    // common case), but diverge for real once a row has a taxable gain.
    function equalizationLine(label, amount, payer) {
      if (!payer) return `${label}: Division is balanced at 50/50 — no equalization payment required.`;
      const payerLabel = payer === "a" ? config.worksheet.party_a_label : config.worksheet.party_b_label;
      const payeeLabel = payer === "a" ? config.worksheet.party_b_label : config.worksheet.party_a_label;
      return `${label}: ${payerLabel} pays ${payeeLabel} ${fmt(amount)} to balance the division at 50/50.`;
    }
    document.getElementById("eq-equalization").innerHTML =
      `<p>${equalizationLine("Before-Tax", totals.equalization_amount, totals.payer)}</p>` +
      `<p>${equalizationLine("After-Tax", totals.equalization_amount_after_tax, totals.payer_after_tax)}</p>`;
  }

  function selectRow(id) {
    selectedId = id;
    tbody.querySelectorAll("tr").forEach((tr) => tr.classList.toggle("selected", Number(tr.dataset.itemId) === id));
    document.querySelectorAll(".eq-assign-btn").forEach((btn) => (btn.disabled = selectedId == null));
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
    function equalizationOf(a, b) {
      const imbalance = Math.round((a - b) * 100) / 100;
      return {
        amount: Math.round(Math.abs(imbalance) / 2 * 100) / 100,
        payer: imbalance === 0 ? null : imbalance > 0 ? "a" : "b",
      };
    }
    const before = equalizationOf(total_before_tax_a, total_before_tax_b);
    const after = equalizationOf(total_after_tax_a, total_after_tax_b);
    return {
      total_fmv, total_debt, total_equity: total_fmv - total_debt,
      total_before_tax_a, total_before_tax_b, total_after_tax_a, total_after_tax_b,
      equalization_amount: before.amount, payer: before.payer,
      equalization_amount_after_tax: after.amount, payer_after_tax: after.payer,
    };
  }

  // Live comma-grouping as you type — reformats on every keystroke and
  // re-places the cursor so typing isn't interrupted by commas shifting
  // around underneath it. No network call here; that's still the
  // "change" handler below, on blur/commit.
  tbody.addEventListener("input", (e) => {
    const input = e.target.closest("input.eq-money");
    if (!input) return;
    const cursorPos = input.selectionStart ?? input.value.length;
    const digitsBeforeCursor = (input.value.slice(0, cursorPos).match(/[0-9.]/g) || []).length;
    input.value = formatMoneyLive(input.value);
    const newPos = cursorPosForDigitCount(input.value, digitsBeforeCursor);
    input.setSelectionRange(newPos, newPos);
  });

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
