(() => {
  "use strict";

  async function post(url, confirmWord) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: confirmWord }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = typeof body.detail === "string" ? body.detail : body.detail?.message;
      throw new Error(detail || "The request failed.");
    }
    return body;
  }

  const scan = document.getElementById("homeScanButton");
  if (scan) {
    scan.addEventListener("click", async () => {
      if (!window.confirm("Scan the library now? A scan only reads files; it changes nothing.")) return;
      scan.disabled = true;
      try {
        await post("/api/scan", "SCAN");
        window.location.reload();
      } catch (error) {
        window.alert(error.message);
        scan.disabled = false;
      }
    });
  }

  const observe = document.getElementById("observeButton");
  if (observe) {
    observe.addEventListener("click", async () => {
      observe.disabled = true;
      try {
        const result = await post("/api/automatic/observe/run", "RUN_OBSERVE_MODE");
        window.alert(result.message || "Observe check finished. Nothing was changed.");
        window.location.reload();
      } catch (error) {
        window.alert(error.message);
        observe.disabled = false;
      }
    });
  }

  // While a scan or check runs, refresh the page every few seconds to show progress.
  const now = document.getElementById("rightNow");
  if (now && now.dataset.active === "1") setTimeout(() => window.location.reload(), 8000);
})();
