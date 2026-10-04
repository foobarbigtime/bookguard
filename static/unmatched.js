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
    const check = event.target.closest("[data-unmatched-check]");
    const attach = event.target.closest("[data-unmatched-attach]");
    if (!check && !attach) return;
    const button = check || attach;
    if (attach && !window.confirm(`Attach these files to “${attach.dataset.book}”? Bindery can undo this on its Import page.`)) return;
    button.disabled = true;
    try {
      if (check) {
        await post("/api/unmatched/check", "CHECK_UNMATCHED");
        setTimeout(() => window.location.reload(), 3000);
      } else {
        const result = await post(`/api/unmatched/${encodeURIComponent(attach.dataset.unmatchedAttach)}/attach`, "ATTACH");
        window.alert(result.message);
        window.location.reload();
      }
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
