(() => {
  "use strict";
  const detail = document.getElementById("reviewDetail");
  const dataElement = document.getElementById("reviewData");
  if (!detail || !dataElement) return;

  const data = JSON.parse(dataElement.textContent || "{}");
  const items = new Map((data.items || []).map((item) => [item.id, item]));
  const latestMove = {};
  for (const move of [...(data.moves || [])].reverse()) latestMove[move.result_id] = move;

  const WHAT_IS_WRONG = {
    unsafe: () => "This file failed BookGuard's safety checks. It may be damaged or unsafe to open.",
    move: (item) => `This file is “${item.otherBook.title}”, another book in your library that has no ebook, not “${item.title}”.`,
    duplicate: (item) => `This file is a copy of “${item.otherBook.title}”, another book in your library, not “${item.title}”.`,
    wrong: (item) => item.contains
      ? `This file contains ${item.contains}, not “${item.title}”.`
      : `This file is not “${item.title}”.`,
    metadata: () => "This is the right book, but the title or author stored inside the file is wrong.",
    verified: () => "This file is the right book.",
    undecided: (item) => item.verified
      ? "BookGuard could not prove what this file is."
      : "The library scan flagged this file, and it has not been verified yet.",
  };

  // ---- small DOM helpers (server text is only ever set as textContent)
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("data-") || key === "role") node.setAttribute(key, value);
      else node[key] = value;
    }
    for (const child of children) if (child) node.append(child);
    return node;
  }

  function section(title, ...children) {
    return el("section", { class: "detail-section" }, el("h3", { text: title }), ...children);
  }

  function button(label, handler, className = "") {
    const node = el("button", { type: "button", text: label, class: className });
    node.addEventListener("click", () => handler(node));
    return node;
  }

  async function request(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detailText = typeof body.detail === "string" ? body.detail : body.detail?.message;
      throw new Error(detailText || "The request failed.");
    }
    return body;
  }

  function post(url, confirmWord) {
    return request(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: confirmWord }),
    });
  }

  function status(message) {
    const node = detail.querySelector(".detail-status");
    if (node) {
      node.textContent = message;
      node.hidden = !message;
    }
  }

  async function run(buttonNode, work) {
    buttonNode.disabled = true;
    status("");
    try {
      await work();
    } catch (error) {
      status(error.message);
    } finally {
      buttonNode.disabled = false;
    }
  }

  // ---- actions (the same guarded endpoints and confirmations as Triage)
  function verifyAgain(item) {
    return (node) => run(node, async () => {
      node.textContent = "Verifying…";
      await request(`/api/verification/${item.id}/run`, { method: "POST" });
      window.location.reload();
    });
  }

  function keep(item) {
    const label = item.classification === "REJECT" ? "Keep (false positive)" : "Mark reviewed";
    return button(label, (node) => run(node, async () => {
      if (!window.confirm(`${label}? This changes only BookGuard's review state, not Bindery or your files.`)) return;
      await post(`/api/triage/${item.id}/keep`, "KEEP");
      window.location.reload();
    }));
  }

  function metadataFix(item) {
    return button("Review metadata fix", (node) => run(node, async () => {
      const preview = await request(`/api/verification/${item.id}/repair-preview`);
      if (!preview.eligible || !preview.safe) throw new Error(preview.reason || "This book's details cannot be fixed automatically.");
      const change = `Before: ${preview.before.title || "—"} / ${preview.before.author || "—"}\n` +
        `After: ${preview.after.title || "—"} / ${preview.after.author || "—"}`;
      if (preview.mode !== "safe") {
        window.alert(`${change}\n\nPreview only. Switch metadata repair to Safe mode in Settings, and make the ebook folder writable, to apply it.`);
        return;
      }
      if (!window.confirm(`${change}\n\nBookGuard checks the file again before writing. Apply this fix?`)) return;
      const result = await post(`/api/verification/${item.id}/repair`, "REPAIR");
      window.alert(`Fixed and checked. You can undo it from Repairs (#${result.repair_id}).`);
      window.location.reload();
    }), "primary");
  }

  function guardedAction(item, action) {
    const label = action === "detach" ? "Detach from Bindery…" : "Quarantine…";
    return button(label, (node) => run(node, async () => {
      const preview = await request(`/api/triage/${item.id}/action-preview?action=${action}`);
      if (!preview.safe) throw new Error(preview.reason || "The safety check failed.");
      const text = action === "detach"
        ? `Remove only Bindery's record of this file?\n\n${preview.stored_path}\n\nThe file stays where it is.`
        : `Quarantine this file?\n\n${preview.local_path}\n\nBookGuard removes Bindery's record and moves the file to quarantine. Nothing is deleted.`;
      if (!window.confirm(text)) return;
      const result = await post(`/api/triage/${item.id}/${action}`, action === "detach" ? "DETACH" : "QUARANTINE");
      window.alert(result.message || "Done.");
      window.location.reload();
    }), "danger");
  }

  function replaceAction(item) {
    return button("Replace…", (node) => run(node, async () => {
      const preview = await request(`/api/triage/${item.id}/action-preview?action=quarantine`);
      if (!preview.safe) throw new Error(preview.reason || "The safety check failed.");
      const text = `Replace this file?\n\n${preview.local_path}\n\nBookGuard moves it to quarantine (nothing is deleted). ` +
        "Bindery then blocklists the download it came from, searches for a new copy and imports it.";
      if (!window.confirm(text)) return;
      const result = await post(`/api/triage/${item.id}/replace`, "REPLACE");
      window.alert(result.message || "Done.");
      window.location.reload();
    }), "danger");
  }

  function primaryActions(item) {
    const actions = [];
    const move = latestMove[item.id];
    if (item.group === "move") {
      if (move && move.status === "confirmed") actions.push(el("span", { class: "muted", text: `Moved to ${move.target_title}.` }));
      else if (move && move.status !== "request_failed") actions.push(el("button", { type: "button", text: "Check move", "data-move-reconcile": String(move.id) }));
      else actions.push(el("button", { type: "button", class: "primary", text: "Move to correct book", "data-move-result": String(item.id) }));
    }
    if (item.group === "metadata") actions.push(metadataFix(item));
    if (["wrong", "duplicate", "move"].includes(item.group)) {
      actions.push(el("a", {
        class: "button-link" + (item.group === "wrong" ? " primary-link" : ""),
        href: `/review/triage?classification=${encodeURIComponent(item.classification)}#verification-cell-${item.id}`,
        text: "Start a replacement in Triage",
      }));
    }
    actions.push(keep(item));
    actions.push(button(item.verified ? "Verify again" : "Verify now", verifyAgain(item)));
    return actions;
  }

  function technicalDetails(item) {
    const rows = [
      ["Scan result", `#${item.id} · ${item.classification} · ${item.reasonCode.replaceAll("_", " ")} · risk ${item.riskScore}`],
      ["Bindery book", String(item.bookId)],
      ["Format", item.format],
      ["Path", item.storedPath],
      ["Verification", item.verified ? `${item.verdict.replaceAll("_", " ")} · ${item.confidence}% confidence` : "Not verified"],
    ];
    const dl = el("dl", { class: "detail-facts" });
    for (const [label, value] of rows) dl.append(el("dt", { text: label }), el("dd", { text: value || "—" }));
    const links = el("div", { class: "detail-actions" });
    if (item.verificationId) links.append(el("a", { class: "button-link", href: `/activity/verification/${item.verificationId}`, text: "Verification record" }));
    links.append(el("a", { class: "button-link", href: `/review/triage?classification=${encodeURIComponent(item.classification)}`, text: "Open in Triage" }));
    return el("details", { class: "detail-section" }, el("summary", { text: "Technical details" }), dl, links);
  }

  function show(item) {
    for (const row of document.querySelectorAll(".review-row")) {
      row.classList.toggle("active", Number(row.dataset.reviewId) === item.id);
    }
    const evidence = el("ul", { class: "detail-evidence" });
    const language = item.language || {};
    evidence.append(el("li", {
      text: language.declared ? `Language: ${language.label} (declared in the file).` : "Language: not declared in the file.",
    }));
    if (item.explanation) evidence.append(el("li", { text: item.explanation }));
    if (item.verified) evidence.append(el("li", { text: `Verified with ${item.confidence}% confidence.` }));
    for (const reason of item.scanReasons || []) evidence.append(el("li", { text: `Library scan: ${reason}` }));
    if (!evidence.children.length) evidence.append(el("li", { text: "No evidence has been recorded yet." }));

    const parts = [
      el("div", { class: "detail-title" },
        el("span", { class: "detail-group", text: item.groupLabel }),
        el("h2", { text: item.title }),
        el("p", { class: "muted", text: [item.author, item.format].filter(Boolean).join(" · ") })),
      section("What's wrong", el("p", { text: (WHAT_IS_WRONG[item.group] || WHAT_IS_WRONG.undecided)(item) })),
      section("How BookGuard knows", evidence),
      section("Suggestion",
        language.nonEnglish ? el("p", { class: "lang-warning", text: `This book is in ${language.label}. If you only keep English books, quarantine it instead of keeping or moving it.` }) : null,
        el("p", { text: item.suggestion }),
        el("div", { class: "detail-actions" }, ...primaryActions(item))),
      el("p", { class: "detail-status notice warning", role: "alert", hidden: true }),
    ];
    if (data.allowActions) {
      parts.push(el("section", { class: "detail-section danger-zone" },
        el("h3", { text: "Danger zone" }),
        el("p", { class: "muted", text: "Detach removes only Bindery's record; the file stays. Quarantine also moves the file out of the library. Replace quarantines it and has Bindery fetch a new copy. Nothing is ever deleted." }),
        el("div", { class: "detail-actions" }, guardedAction(item, "detach"), guardedAction(item, "quarantine"), replaceAction(item))));
    } else {
      parts.push(el("p", { class: "muted small-print", text: "Detach and quarantine appear here when Bindery actions are enabled in Settings." }));
    }
    parts.push(technicalDetails(item));
    detail.replaceChildren(...parts);
    if (window.matchMedia("(max-width: 950px)").matches) detail.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  document.addEventListener("click", (event) => {
    const row = event.target.closest("[data-review-id]");
    if (!row) return;
    const item = items.get(Number(row.dataset.reviewId));
    if (item) {
      show(item);
      history.replaceState(null, "", `${location.pathname}${location.search}#book-${item.id}`);
    }
  });

  const fromHash = Number((location.hash.match(/^#book-(\d+)$/) || [])[1]);
  const first = items.get(fromHash) || (data.items || [])[0];
  if (first) show(first);
})();
