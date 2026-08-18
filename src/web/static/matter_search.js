// Wires up every ".matter-search-input" on the page into a type-to-filter
// dropdown over a client-side matter list (no per-keystroke network call —
// the full open-matters list is small enough to embed once per page load).
// Selecting a result fills the sibling form's ".matter-search-id" hidden
// field, which is what actually gets submitted.
function initMatterSearch(matters) {
  const MAX_RESULTS = 8;

  document.querySelectorAll(".matter-search-input").forEach((input) => {
    const results = input.parentElement.querySelector(".matter-search-results");
    const form = input.closest("form");
    const hiddenId = form.querySelector(".matter-search-id");
    if (!results || !hiddenId) return;

    function clearError() {
      input.classList.remove("matter-search-invalid");
      const error = input.parentElement.querySelector(".matter-search-error");
      if (error) error.remove();
    }

    // A hidden input's `required` attribute isn't enforced by any browser
    // (hidden fields are exempt from the Constraint Validation API), so
    // pressing Enter/clicking submit without ever picking a suggestion —
    // matter_id is still blank — would otherwise reach the server and
    // surface a raw FastAPI validation error on a blank page. Block it here
    // instead, with an inline message pointing at the field to fix.
    form.addEventListener("submit", (e) => {
      if (hiddenId.value) return;
      e.preventDefault();
      input.classList.add("matter-search-invalid");
      input.focus();
      if (!input.parentElement.querySelector(".matter-search-error")) {
        const error = document.createElement("div");
        error.className = "matter-search-error";
        error.textContent = "Pick a matter from the list before submitting.";
        input.parentElement.appendChild(error);
      }
    });

    function render(query) {
      const q = query.trim().toLowerCase();
      if (!q) {
        results.style.display = "none";
        results.innerHTML = "";
        return;
      }
      const matches = matters.filter((m) => m.name.toLowerCase().includes(q)).slice(0, MAX_RESULTS);
      if (!matches.length) {
        results.innerHTML = '<div class="matter-search-empty">No matches</div>';
        results.style.display = "block";
        return;
      }
      results.innerHTML = matches
        .map((m) => `<div class="matter-search-item" data-id="${m.id}">${m.name}</div>`)
        .join("");
      results.style.display = "block";
    }

    input.addEventListener("input", () => {
      hiddenId.value = "";
      clearError();
      render(input.value);
    });

    input.addEventListener("focus", () => {
      if (input.value.trim()) render(input.value);
    });

    // mousedown (not click) fires before the input's blur handler, so a
    // selection registers before the dropdown gets hidden.
    results.addEventListener("mousedown", (e) => {
      const item = e.target.closest(".matter-search-item");
      if (!item) return;
      hiddenId.value = item.dataset.id;
      input.value = item.textContent;
      results.style.display = "none";
      results.innerHTML = "";
      clearError();
    });

    input.addEventListener("blur", () => {
      setTimeout(() => { results.style.display = "none"; }, 150);
    });
  });
}
