function initDropzone(zoneId, inputId, formId) {
  const zone = document.getElementById(zoneId);
  const input = document.getElementById(inputId);
  const form = document.getElementById(formId);
  if (!zone || !input || !form) return;

  zone.addEventListener("click", () => input.click());

  zone.addEventListener("dragover", (e) => {
    e.preventDefault();
    zone.classList.add("dragover");
  });

  zone.addEventListener("dragleave", () => zone.classList.remove("dragover"));

  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      form.requestSubmit();
    }
  });

  input.addEventListener("change", () => {
    if (input.files.length) form.requestSubmit();
  });
}
