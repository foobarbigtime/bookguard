(() => {
  "use strict";

  // Group the timeline under day headings in the viewer's own time zone.
  const list = document.querySelector(".activity-list");
  if (list) {
    const today = new Date();
    const yesterday = new Date(today);
    yesterday.setDate(today.getDate() - 1);
    let previous = "";
    for (const event of list.querySelectorAll(".activity-event")) {
      const date = new Date(event.dataset.day || "");
      if (Number.isNaN(date.getTime())) continue;
      const key = date.toDateString();
      if (key === previous) continue;
      previous = key;
      let label;
      if (key === today.toDateString()) label = "Today";
      else if (key === yesterday.toDateString()) label = "Yesterday";
      else {
        const options = { weekday: "long", month: "long", day: "numeric" };
        if (date.getFullYear() !== today.getFullYear()) options.year = "numeric";
        label = date.toLocaleDateString([], options);
      }
      const heading = document.createElement("h2");
      heading.className = "activity-day";
      heading.textContent = label;
      list.insertBefore(heading, event);
    }
  }

  // Changing a filter applies it straight away; the Apply button covers no-JS use.
  const form = document.querySelector(".activity-filters");
  if (form) {
    for (const select of form.querySelectorAll("select")) {
      select.addEventListener("change", () => form.submit());
    }
  }

  // Undo lives on the repair's own entry; the server re-checks everything.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-undo-repair]");
    if (!button) return;
    if (!window.confirm("Undo this metadata fix? BookGuard puts back the details the file had before, but only if nothing else has changed them since, then checks the result.")) return;
    button.disabled = true;
    try {
      const response = await fetch(`/api/repairs/${button.dataset.undoRepair}/undo`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: "UNDO" }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || "Undo failed.");
      window.location.reload();
    } catch (error) {
      window.alert(error.message);
      button.disabled = false;
    }
  });
})();
