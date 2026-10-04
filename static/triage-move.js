(() => {
  "use strict";
  const panel = document.getElementById("movePanel");
  const body = document.getElementById("moveBody");
  const subtitle = document.getElementById("moveSubtitle");
  if (!panel || !body || !subtitle) return;

  const BLOCKERS = {
    notAnEbook: "Only ebooks can be moved this way.",
    notProvenMissingEbookOfAnotherBook:
      "Verification no longer proves this file is another book's missing ebook.",
    noVerifiedFingerprint: "Verification did not record a fingerprint for this file.",
    binderyActionsDisabled:
      "Bindery actions are off. Enable them in Settings (BOOKGUARD_ALLOW_ACTIONS) to move files.",
    binderyImportModeNotNormal:
      "Bindery's import mode is external or could not be read, so Bindery would not move the file.",
    targetAlreadyHasEbook: "The right book already has an ebook.",
    sourceNotTrackedByThisBookOnly:
      "Bindery does not track this file under exactly the book it was filed as.",
    destinationOutsideLibrary: "Bindery would put the file outside the ebook library.",
  };

  const STATUS = {
    requested: "Requested",
    pending: "Waiting for Bindery to finish",
    confirmed: "Moved and confirmed",
    fingerprint_mismatch: "Moved, but the file there does not match the verified file",
    request_failed: "Bindery refused the move",
  };

  function blockerText(code) {
    if (code.startsWith("binderyPreview:")) {
      return `Bindery's preview did not report a move (it answered "${code.slice(15)}").`;
    }
    return BLOCKERS[code] || code;
  }

  function line(value, className = "") {
    const p = document.createElement("p");
    p.textContent = value;
    if (className) p.className = className;
    body.append(p);
    return p;
  }

  function details(entries) {
    const dl = document.createElement("dl");
    dl.className = "workflow-details";
    for (const [label, value] of entries) {
      const item = document.createElement("div");
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = label;
      dd.textContent = value || "—";
      item.append(dt, dd);
      dl.append(item);
    }
    body.append(dl);
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "The request failed.");
    return result;
  }

  function open(title) {
    panel.hidden = false;
    subtitle.textContent = title;
    body.replaceChildren();
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function showMove(move) {
    details([
      ["Move", `#${move.id}`],
      ["Status", STATUS[move.status] || move.status],
      ["Moved to", `${move.target_title} (Bindery book ${move.target_book_id})`],
      ["New path", move.destination],
    ]);
    if (!["confirmed", "request_failed"].includes(move.status)) {
      const check = document.createElement("button");
      check.type = "button";
      check.dataset.moveReconcile = String(move.id);
      check.textContent = "Check again";
      body.append(check);
    }
  }

  async function preview(resultId) {
    open("Checking whether this file can be moved…");
    let proof;
    try {
      proof = await request(`/api/triage/${resultId}/move-preview`);
    } catch (error) {
      subtitle.textContent = "The move could not be checked.";
      line(error.message);
      return;
    }
    subtitle.textContent = proof.targetTitle
      ? `This file is really "${proof.targetTitle}", which has no ebook.`
      : "Verification does not name another book for this file.";
    details([
      ["Filed under", `Bindery book ${proof.sourceBookId}`],
      ["Really belongs to", `${proof.targetTitle} (Bindery book ${proof.targetBookId})`],
      ["Current path", proof.sourcePath],
      ["New path", proof.destination],
      ["Language", proof.language && proof.language.declared ? proof.language.label : "Not declared in the file"],
    ]);
    if (proof.language && proof.language.otherLanguage) {
      line(
        `This file is in ${proof.language.label}. Moving it files the ${proof.language.label} edition under ` +
        `“${proof.targetTitle}”, but it is not one of your library languages. Quarantine it instead.`,
        "notice warning",
      );
    }
    if (!proof.eligible) {
      line("This move is not safe right now:");
      const list = document.createElement("ul");
      for (const code of proof.blockers || []) {
        const item = document.createElement("li");
        item.textContent = blockerText(code);
        list.append(item);
      }
      body.append(list);
      return;
    }
    line(
      "Bindery will move the file into the right book's folder. BookGuard verifies the file " +
      "again first and never writes to the library itself. The book it was filed under will " +
      "then have no ebook; use Replacement workflow afterwards if you want one for it.",
      "muted",
    );
    const move = document.createElement("button");
    move.type = "button";
    move.className = "danger";
    move.textContent = `Move to ${proof.targetTitle}`;
    move.addEventListener("click", () => confirmMove(resultId, proof, move));
    body.append(move);
  }

  async function confirmMove(resultId, proof, button) {
    if (!window.confirm(
      `Ask Bindery to move this file to "${proof.targetTitle}"?\n\n` +
      `From: ${proof.sourcePath}\nTo: ${proof.destination}\n\n` +
      "BookGuard checks the result afterwards. It does not download anything."
    )) return;
    button.disabled = true;
    button.textContent = "Moving… Bindery can take up to two minutes";
    try {
      const result = await request(`/api/triage/${resultId}/move-to-correct-book`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: "MOVE_TO_CORRECT_BOOK" }),
      });
      button.remove();
      line(result.message);
      showMove(result.move);
    } catch (error) {
      button.textContent = "Move not done";
      line(error.message);
      line("Close this panel and verify the file again before trying anything else.");
    }
  }

  async function reconcile(moveId, button) {
    button.disabled = true;
    open("Checking the move again (read-only)…");
    try {
      const move = await request(`/api/catalogue-moves/${moveId}/reconcile`, { method: "POST" });
      subtitle.textContent = "Bindery was not asked to do anything again.";
      showMove(move);
    } catch (error) {
      line(error.message);
    } finally {
      button.disabled = false;
    }
  }

  document.addEventListener("click", (event) => {
    const start = event.target.closest("[data-move-result]");
    if (start) { preview(Number(start.dataset.moveResult)); return; }
    const check = event.target.closest("[data-move-reconcile]");
    if (check) { reconcile(Number(check.dataset.moveReconcile), check); return; }
    if (event.target.closest("[data-move-close]")) panel.hidden = true;
  });
})();
