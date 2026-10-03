(() => {
  "use strict";

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-put-back]");
    if (!button) return;
    if (!window.confirm("Put this file back where it was? Nothing is replaced; BookGuard stops if anything is in the way.")) return;
    button.disabled = true;
    try {
      const response = await fetch(`/api/quarantine/${encodeURIComponent(button.dataset.putBack)}/put-back`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: "PUT_BACK" }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
        throw new Error(detail || "Put back failed.");
      }
      window.alert(body.message || "Put back.");
      window.location.href = `/activity/cleanup/${body.cleanup_id}`;
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
