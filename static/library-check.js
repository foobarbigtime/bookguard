(() => {
  "use strict";
  const buttons = Array.from(document.querySelectorAll("[data-library-check]"));
  if (!buttons.length) return;
  const status = document.querySelector("[data-library-check-status]") || document.createElement("p");
  if (!status.parentNode) {
    status.setAttribute("role", "status");
    status.setAttribute("aria-live", "polite");
    buttons[0].parentNode.append(status);
  }

  function checkMessage(state) {
    const job = state.verification || {};
    const scan = state.scan || {};
    const unmatched = state.unmatched || {};
    if (state.scan_running) {
      return `Scanning assigned files: ${scan.processed || 0} of ${scan.total || 0}.`;
    }
    if (job.status === "running") return `Verifying unresolved results: ${job.processed || 0} of ${job.total || 0}. ${job.cacheHits || 0} saved proofs reused. ${job.postponed || 0} postponed. ${job.current || ""}`;
    if (unmatched.running) return `Checking unassigned files: ${unmatched.checked || 0} of ${unmatched.total || 0}.`;
    if (state.status === "running") return "Starting the next check…";
    if (state.busy) return "Another library task is running…";
    if (state.status === "failed") return `Check stopped during ${state.phase}: ${state.error}. Completed results are retained.`;
    if (state.status === "complete") {
      const result = state.verification_result || {};
      const checked = state.unmatched_result || {};
      const verified = Math.max(0, (result.processed || 0) - (result.postponed || 0));
      const postponed = result.postponed ? ` ${result.postponed} postponed: ${result.postponedReason || "catalogue unavailable"}.` : "";
      return `Check complete: ${verified} results verified, ${result.cacheHits || 0} saved proofs reused, ${checked.checked || 0} unassigned files checked.${postponed}`;
    }
    if (job.status === "failed") return `Last verification failed: ${job.error}.`;
    if (scan.status === "running") return "The last scan was interrupted. Start Check library to refresh the results.";
    if (!scan.id) return "No scan yet. Start Check library to find what needs attention.";
    return "Ready to check. The list below contains stored results from the last scan.";
  }

  let timer;
  let wasBusy = false;
  let starting = false;
  async function refresh() {
    clearTimeout(timer);
    try {
      const response = await fetch("/api/library-check", { cache: "no-store" });
      const state = await response.json();
      if (!response.ok) throw new Error(state.detail || "Unable to read check status.");
      status.textContent = checkMessage(state);
      buttons.forEach(button => { button.disabled = starting || state.busy; });
      if (wasBusy && !state.busy && state.status !== "failed") {
        window.location.reload();
        return;
      }
      wasBusy = state.busy;
    } catch (error) {
      status.textContent = `Status unavailable: ${error.message}`;
      buttons.forEach(button => { button.disabled = starting; });
    }
    timer = setTimeout(refresh, 2500);
  }

  buttons.forEach(button => {
    button.disabled = true;
    button.addEventListener("click", async () => {
      if (starting) return;
      starting = true;
      buttons.forEach(item => { item.disabled = true; });
      try {
        const response = await fetch("/api/library-check", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: "CHECK_LIBRARY" }),
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || "Unable to start library check.");
        wasBusy = true;
      } catch (error) {
        window.alert(error.message);
      } finally {
        starting = false;
        refresh();
      }
    });
  });
  refresh();
})();
