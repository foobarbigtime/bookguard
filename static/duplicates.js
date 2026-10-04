(() => {
  "use strict";

  async function post(url, confirm) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
      throw new Error(detail || "The request failed.");
    }
    return body;
  }

  document.addEventListener("click", async (event) => {
    const one = event.target.closest("[data-duplicate-hide]");
    const all = event.target.closest("[data-duplicates-hide-all]");
    if (!one && !all) return;
    const question = one
      ? `Hide the empty entry “${one.dataset.title}” in Bindery? You can include it again on the book's page in Bindery.`
      : "Hide every empty extra entry in Bindery? Each is checked again first, and each can be included again in Bindery.";
    if (!window.confirm(question)) return;
    const button = one || all;
    button.disabled = true;
    try {
      const result = one
        ? await post(`/api/duplicates/${encodeURIComponent(one.dataset.duplicateHide)}/hide`, "HIDE_DUPLICATE")
        : await post("/api/duplicates/hide-all", "HIDE_ALL_DUPLICATES");
      window.alert(result.message);
      window.location.reload();
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
