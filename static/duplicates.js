(() => {
  "use strict";

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-duplicates-fix]");
    if (!button) return;
    if (!window.confirm("Fix every proven duplicate in Bindery now? Each change is checked again first and can be undone in Bindery.")) return;
    button.disabled = true;
    try {
      const response = await fetch("/api/duplicates/fix", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: "FIX_DUPLICATES" }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "The request failed.");
      setTimeout(() => window.location.reload(), 3000);
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
