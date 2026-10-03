(() => {
  "use strict";

  const RUNS = {
    scan: { url: "/api/scan", confirm: "SCAN", ask: "Scan the whole library now? A scan only reads files; it changes nothing." },
    imports: { url: "/api/imports/check", confirm: "CHECK_IMPORTS", ask: "" },
    observe: { url: "/api/automatic/observe/run", confirm: "RUN_OBSERVE_MODE", ask: "" },
  };

  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-run-task]");
    if (!button) return;
    const run = RUNS[button.dataset.runTask];
    if (!run || (run.ask && !window.confirm(run.ask))) return;
    button.disabled = true;
    try {
      const response = await fetch(run.url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: run.confirm }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
        throw new Error(detail || "The task could not start.");
      }
      // The import check runs in the background; give it a moment before refreshing.
      setTimeout(() => window.location.reload(), button.dataset.runTask === "imports" ? 2500 : 300);
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
