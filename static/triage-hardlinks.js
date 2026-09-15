(() => {
  "use strict";
  const check = document.getElementById("checkHardlinks");
  const results = document.getElementById("hardlinkResults");
  if (!check || !results) return;

  function line(parent, value) {
    const p = document.createElement("p");
    p.textContent = value;
    parent.append(p);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "The request failed.");
    return body;
  }

  async function preview(fileId, parent, button) {
    button.disabled = true;
    try {
      const proof = await request(`/api/hardlink-conflicts/${fileId}/preview`);
      line(parent, proof.reason);
      if (!proof.safe) return;
      line(parent, `Keep: ${proof.retained.author} — ${proof.retained.title}`);
      line(parent, proof.retained.stored_path);
      line(parent, `Remove association: ${proof.wrong.author} — ${proof.wrong.title}`);
      line(parent, proof.wrong.stored_path);
      line(parent, "Both filenames and audiobook registrations will be preserved.");
      const correct = document.createElement("button");
      correct.type = "button";
      correct.textContent = "Remove wrong ebook association";
      correct.disabled = !proof.ready;
      correct.addEventListener("click", async () => {
        if (!window.confirm(
          `Remove only the ebook association from ${proof.wrong.title}?\n\n` +
          `${proof.wrong.stored_path}\n\nKeep ${proof.retained.title}, both files, and all audiobook registrations.`
        )) return;
        correct.disabled = true;
        try {
          const response = await request(`/api/hardlink-conflicts/${fileId}/correct`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ confirm: "REMOVE_WRONG_EBOOK_ASSOCIATION", token: proof.token }),
          });
          line(parent, response.message);
        } catch (error) {
          line(parent, error.message);
          line(parent, "Run Check shared files again to inspect the current state before any further action.");
        }
      });
      parent.append(correct);
    } catch (error) {
      line(parent, error.message);
    } finally {
      button.disabled = false;
    }
  }

  check.addEventListener("click", async () => {
    check.disabled = true;
    results.replaceChildren();
    line(results, "Checking current registrations…");
    try {
      const report = await request("/api/hardlink-conflicts");
      results.replaceChildren();
      if (!report.items.length) line(results, "No conflicts found between tracked hard-linked ebook files.");
      if (!report.actionsEnabled) line(results, "Audit only: Bindery actions are disabled in Settings.");
      for (const conflict of report.items) {
        const card = document.createElement("div");
        card.className = "notice warning";
        for (const row of conflict.associations) {
          line(card, `${row.author} — ${row.title}: ${row.stored_path}`);
        }
        if (conflict.candidateFileId !== null) {
          const button = document.createElement("button");
          button.type = "button";
          button.textContent = "Verify correction preview";
          button.addEventListener("click", () => preview(conflict.candidateFileId, card, button));
          card.append(button);
        } else {
          line(card, "This shared-file conflict needs review; no unique staging association can be selected.");
        }
        results.append(card);
      }
      for (const entry of report.history) {
        line(results, `Correction ${entry.id}: ${entry.status}${entry.error ? ` — ${entry.error}` : ""}`);
        if (entry.status === "running" || entry.status === "needs_review") {
          const reconcile = document.createElement("button");
          reconcile.type = "button";
          reconcile.textContent = "Recheck interrupted correction";
          reconcile.addEventListener("click", async () => {
            if (!window.confirm("Recheck this interrupted correction? This verifies current state without repeating the Bindery mutation.")) return;
            reconcile.disabled = true;
            try {
              const response = await request(`/api/hardlink-conflicts/history/${entry.id}/reconcile`, {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ confirm: "RECONCILE_HARDLINK_CORRECTION" }),
              });
              line(results, response.message);
            } catch (error) {
              line(results, error.message);
            }
          });
          results.append(reconcile);
        }
      }
    } catch (error) {
      results.replaceChildren();
      line(results, error.message);
    } finally {
      check.disabled = false;
    }
  });
})();
