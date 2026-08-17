// Moore/Marsden worksheet grid — same live-spreadsheet shape as Equalizer's
// (small JSON endpoints, autosaving as staff edit), but with one real
// difference: the calculation is *recursive* — every segment's cumulative
// community interest carries into every later segment's math — so editing
// or deleting any one row can change every downstream row's figures, not
// just its own. Every mutating call below therefore rebuilds the *entire*
// grid from the server's returned segment list, rather than patching one
// row in place the way equalizer.js does.

const moneyFmt = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });

function fmt(n) {
  return moneyFmt.format(n || 0);
}

function fmtPercent(n) {
  if (n === null || n === undefined) return "—";
  return (n * 100).toFixed(2) + "%";
}

// Same live comma-grouping helpers as equalizer.js (duplicated rather than
// imported — each subproject owns its own script here, same convention as
// the Python side).
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

function formatMoney(value) {
  if (value === null || value === undefined || value === "") return "";
  return formatMoneyLive(String(value));
}

function parseMoneyInput(str) {
  return str.replace(/,/g, "").trim();
}

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

const ORDINALS = { 1: "1st", 2: "2nd", 3: "3rd" };
function ordinal(n) {
  return ORDINALS[n] || `${n}th`;
}

// Mirrors moore_marsden/calc.py's default_segment_label() — used only to
// show a placeholder for the label field until staff type something real.
function defaultSegmentLabel(segmentType, refinanceNumber) {
  if (segmentType === "purchase") return "Purchase";
  if (segmentType === "valuation") return "Current Valuation";
  if (segmentType === "refinance") return `${ordinal(refinanceNumber || 1)} Refinance`;
  return segmentType;
}

function initMooreMarsdenGrid(config) {
  const grid = document.getElementById("mm-grid");

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

  function numOrZero(input) {
    const raw = parseMoneyInput(input.value);
    if (raw === "") return 0;
    const n = parseFloat(raw);
    return Number.isNaN(n) ? 0 : n;
  }

  function numOrNull(input) {
    const raw = parseMoneyInput(input.value);
    if (raw === "") return null;
    const n = parseFloat(raw);
    return Number.isNaN(n) ? null : n;
  }

  function field(label, html) {
    return `<label>${label}${html}</label>`;
  }

  function moneyField(label, field_, value) {
    return field(label, `<input type="text" inputmode="decimal" class="mm-money" data-field="${field_}" value="${formatMoney(value)}">`);
  }

  function computedItem(label, value, extraClass) {
    return `<div class="mm-computed-item ${extraClass || ""}"><span class="mm-computed-label">${label}</span><span class="mm-computed-value">${value}</span></div>`;
  }

  // Each segment is a stacked "step card" — a header line plus two wrapping
  // field grids (editable inputs, then read-only computed figures) — rather
  // than one very wide table row. This mirrors the legacy report's own
  // per-Step sectioned layout and, unlike a wide table, needs no horizontal
  // scrolling: the grids wrap to as many lines as the panel width allows.
  function buildCard(segment, stepIndex, refinanceOrdinal) {
    const card = document.createElement("div");
    card.className = "mm-segment-card";
    card.dataset.segmentId = segment.id;

    const isPurchase = segment.segment_type === "purchase";
    const isRefinance = segment.segment_type === "refinance";
    const autoLabel = defaultSegmentLabel(segment.segment_type, refinanceOrdinal);
    const stepPrefix = isPurchase ? "" : `Step ${stepIndex}: `;

    const inputFields = [
      field("Date", `<input type="date" data-field="event_date" value="${segment.event_date || ""}">`),
      moneyField("Property Value", "property_value", segment.property_value),
      moneyField("Loan Balance", "loan_balance", segment.loan_balance),
      isPurchase ? "" : moneyField("Principal Reduction (this period)", "community_principal_reduction", segment.community_principal_reduction),
      moneyField("SP Contribution", "sp_contribution", segment.sp_contribution),
      moneyField("CP Contribution", "cp_contribution", segment.cp_contribution),
      field("Notes", `<input type="text" data-field="notes" value="${(segment.notes || "").replace(/"/g, "&quot;")}">`),
    ].join("");

    const computedItems = isPurchase
      ? [
          computedItem("Cumulative Community Interest", fmt(segment.cumulative_cp), "mm-cumulative"),
          computedItem("SP Total", fmt(segment.sp_total)),
        ].join("")
      : [
          computedItem("Basis", fmt(segment.basis)),
          computedItem("Appreciation", fmt(segment.appreciation)),
          computedItem("Community %", fmtPercent(segment.community_pct)),
          computedItem("Community Appreciation", fmt(segment.community_appreciation)),
          computedItem("Cumulative Community Interest", fmt(segment.cumulative_cp), "mm-cumulative"),
          computedItem("SP Total", fmt(segment.sp_total)),
        ].join("");

    card.innerHTML = `
      <div class="mm-segment-head">
        <span class="mm-segment-title">${stepPrefix}<input type="text" data-field="event_label" placeholder="${autoLabel}" value="${(segment.event_label || "").replace(/"/g, "&quot;")}"></span>
        ${isRefinance ? `<button type="button" class="secondary mm-delete-btn">&times;</button>` : ""}
      </div>
      <div class="mm-field-grid">${inputFields}</div>
      <div class="mm-computed-grid">${computedItems}</div>
    `;

    return card;
  }

  function renderGrid(segments) {
    grid.innerHTML = "";
    let refinanceOrdinal = 0;
    segments.forEach((segment, idx) => {
      if (segment.segment_type === "refinance") refinanceOrdinal += 1;
      grid.appendChild(buildCard(segment, idx, refinanceOrdinal));
    });
  }

  function renderSummary(totals) {
    const ownerLabel = config.worksheet.owner_spouse_label || "Owner Spouse";
    const nonOwnerLabel = config.worksheet.non_owner_spouse_label || "Non-Owner Spouse";
    document.getElementById("mm-summary").innerHTML = `
      <p>The community interest in the property is: <strong>${fmt(totals.total_community_interest)}</strong></p>
      <p>${ownerLabel}'s share: ${fmt(totals.owner_spouse_share)} &nbsp;&nbsp; ${nonOwnerLabel}'s share: ${fmt(totals.non_owner_spouse_share)}</p>
    `;
  }

  // Initial render
  renderGrid(config.segments);
  renderSummary(computeTotalsClientSide(config.segments));

  function computeTotalsClientSide(segments) {
    // Rough initial totals before any edit — good enough for first paint;
    // the server's authoritative totals arrive on the very next mutation.
    const last = segments[segments.length - 1];
    const total = (last && last.cumulative_cp) || 0;
    const half = Math.round((total / 2) * 100) / 100;
    return { total_community_interest: total, owner_spouse_share: half, non_owner_spouse_share: Math.round((total - half) * 100) / 100 };
  }

  // Live comma-grouping as you type, no network call — same as equalizer.js.
  grid.addEventListener("input", (e) => {
    const input = e.target.closest("input.mm-money");
    if (!input) return;
    const cursorPos = input.selectionStart ?? input.value.length;
    const digitsBeforeCursor = (input.value.slice(0, cursorPos).match(/[0-9.]/g) || []).length;
    input.value = formatMoneyLive(input.value);
    const newPos = cursorPosForDigitCount(input.value, digitsBeforeCursor);
    input.setSelectionRange(newPos, newPos);
  });

  grid.addEventListener("change", async (e) => {
    const input = e.target.closest("[data-field]");
    if (!input) return;
    const card = input.closest(".mm-segment-card");
    const segmentId = Number(card.dataset.segmentId);
    const field = input.dataset.field;

    let value;
    if (field === "event_label" || field === "event_date" || field === "notes") value = input.value;
    else if (field === "loan_balance") value = numOrNull(input);
    else value = numOrZero(input);

    const { segments, totals } = await api("PATCH", `/moore-marsden/${config.worksheetId}/segments/${segmentId}`, { [field]: value });
    renderGrid(segments);
    renderSummary(totals);
  });

  grid.addEventListener("click", async (e) => {
    if (!e.target.closest(".mm-delete-btn")) return;
    const card = e.target.closest(".mm-segment-card");
    const segmentId = Number(card.dataset.segmentId);
    if (!confirm("Remove this refinance row?")) return;
    const { segments, totals } = await api("DELETE", `/moore-marsden/${config.worksheetId}/segments/${segmentId}`);
    renderGrid(segments);
    renderSummary(totals);
  });

  document.getElementById("mm-add-segment").addEventListener("click", async () => {
    const { segments, totals } = await api("POST", `/moore-marsden/${config.worksheetId}/segments`, {});
    renderGrid(segments);
    renderSummary(totals);
  });

  // Prompted every click, even on a re-save — same reasoning as
  // equalizer.js's own save-filename prompt.
  document.getElementById("mm-save-btn").addEventListener("click", (e) => {
    const today = new Date().toISOString().slice(0, 10);
    const defaultName = config.worksheet.clio_document_name
      ? config.worksheet.clio_document_name.replace(/\.pdf$/i, "")
      : `moore-marsden-${today}`;
    const chosen = prompt("Save to Clio as (filename):", defaultName);
    if (chosen === null) {
      e.preventDefault();
      return;
    }
    document.getElementById("mm-save-filename").value = chosen;
  });

  // ---- Settings modal ----
  const settingsModal = document.getElementById("mm-settings-modal");
  const ownerLabelInput = document.getElementById("mm-owner-label");
  const nonOwnerLabelInput = document.getElementById("mm-non-owner-label");
  const beforeMarriageCheckbox = document.getElementById("mm-acquired-before-marriage");
  const marriageFields = document.getElementById("mm-marriage-fields");

  function syncMarriageFieldsVisibility() {
    marriageFields.style.display = beforeMarriageCheckbox.checked ? "block" : "none";
  }
  beforeMarriageCheckbox.addEventListener("change", syncMarriageFieldsVisibility);
  syncMarriageFieldsVisibility();

  document.getElementById("mm-settings-btn").addEventListener("click", () => (settingsModal.style.display = "flex"));

  document.getElementById("mm-autofill-names").addEventListener("click", async () => {
    const names = await api("POST", `/moore-marsden/${config.worksheetId}/settings/autofill-names`, undefined);
    ownerLabelInput.value = names.owner_spouse_label || "";
    nonOwnerLabelInput.value = names.non_owner_spouse_label || "";
  });

  document.getElementById("mm-settings-save").addEventListener("click", async () => {
    const payload = {
      owner_spouse_label: ownerLabelInput.value.trim(),
      non_owner_spouse_label: nonOwnerLabelInput.value.trim(),
      acquired_before_marriage: beforeMarriageCheckbox.checked,
      date_of_marriage: document.getElementById("mm-date-of-marriage").value || null,
      value_at_date_of_marriage: numOrNull(document.getElementById("mm-value-at-marriage")),
    };
    await api("PATCH", `/moore-marsden/${config.worksheetId}/settings`, payload);
    window.location.reload();
  });

  document.querySelectorAll(".mm-modal-cancel").forEach((btn) => {
    btn.addEventListener("click", () => {
      settingsModal.style.display = "none";
    });
  });
}
