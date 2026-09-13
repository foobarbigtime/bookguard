(() => {
  "use strict";

  const ACTIVE_STATUSES = new Set([
    "preparing",
    "grab_requested",
    "queued",
    "downloading",
    "awaiting_staging",
    "staging_observed",
    "verified",
    "admitted",
    "finalizing",
    "cleanup_required",
  ]);

  const RECONCILE_STATUSES = new Set([
    "preparing",
    "grab_requested",
    "queued",
    "downloading",
    "awaiting_staging",
    "staging_observed",
  ]);

  const BLOCKER_LABELS = {
    automaticReacquisitionEnabled: "automatic reacquisition is disabled",
    binderyExternalImport: "Bindery is not in external import mode",
    actionsEnabled: "guarded actions are disabled",
    ebookActionsEnabled: "ebook quarantine/remediation is disabled",
    absolutePaths: "one or more ebook paths are not absolute",
    readRootExists: "the read-only ebook root is unavailable",
    sourceExists: "the source ebook is absent",
    pathMappingConfirmed: "the BookGuard and Bindery ebook paths do not agree",
    actionRootSeparate: "the writable action path is not a separate alias",
    actionRootExists: "the writable ebook action mount is absent",
    actionRootWritable: "the ebook action mount is not writable",
    actionAliasExists: "the ebook is absent through the writable alias",
    actionAliasSameFile: "the read-only and writable paths are not the same file",
    quarantineOutsideActionRoot: "quarantine overlaps the ebook library",
    admissionEnabled: "direct admission is disabled",
    admissionRootExists: "the writable admission mount is absent",
    admissionRootWritable: "the admission mount is not writable",
    binderyAutoGrabDisabled: "Bindery auto-grab must be disabled",
    binderyQueueComplete: "Bindery returned an incomplete queue",
    binderyQueueIdle: "another Bindery queue item is active",
    stagingInventoryComplete: "the staging inventory is incomplete",
    stagingEmpty: "the staging folder is not empty",
    noActiveAcquisition: "another BookGuard acquisition is active",
    coordinatorEnabled: "the supervised coordinator is disabled",
    explicitAdmissionRequired: "explicit admission approval is required",
  };

  const panel = document.getElementById("acquisitionPanel");
  const stateBadge = document.getElementById("acquisitionState");
  const message = document.getElementById("acquisitionMessage");
  const details = document.getElementById("acquisitionDetails");
  const controls = document.getElementById("acquisitionControls");
  const errorBox = document.getElementById("acquisitionError");
  const replacementPanel = document.getElementById("replacementPanel");
  const replacementTitle = document.getElementById("replacementTitle");
  const replacementSubtitle = document.getElementById("replacementSubtitle");
  const replacementBody = document.getElementById("replacementBody");

  if (!panel || !replacementPanel) return;

  let refreshTimer = null;
  let busy = false;

  function element(tag, options = {}) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = options.text;
    if (options.type) node.type = options.type;
    return node;
  }

  function clear(node) {
    node.replaceChildren();
  }

  function pretty(value) {
    return String(value || "unknown").replaceAll("_", " ");
  }

  function blockerText(blockers) {
    return (blockers || []).map((item) => BLOCKER_LABELS[item] || pretty(item));
  }

  function responseError(body, fallback) {
    return String(body?.detail || body?.error || body?.message || fallback);
  }

  async function requestJson(url, options = {}) {
    const response = await fetch(url, {cache: "no-store", ...options});
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseError(body, `Request failed (${response.status}).`));
    return body;
  }

  function actionButton(label, handler, className = "") {
    const button = element("button", {text: label, type: "button", className});
    button.addEventListener("click", handler);
    return button;
  }

  function setState(label, tone) {
    stateBadge.textContent = label;
    stateBadge.className = `workflow-state workflow-state-${tone}`;
  }

  function setBusy(value) {
    busy = value;
    panel.setAttribute("aria-busy", String(value));
    panel.querySelectorAll("button").forEach((button) => {
      button.disabled = value;
    });
  }

  function showError(target, text) {
    target.textContent = text;
    target.hidden = !text;
  }

  function renderDetails(items) {
    clear(details);
    for (const [label, value] of items) {
      const wrapper = element("div");
      wrapper.append(
        element("dt", {text: label}),
        element("dd", {text: value ?? "—"}),
      );
      details.append(wrapper);
    }
    details.hidden = items.length === 0;
  }

  function scheduleRefresh(active) {
    window.clearTimeout(refreshTimer);
    refreshTimer = window.setTimeout(refreshWorkflow, active ? 3000 : 15000);
  }

  async function runOperation(button, operation) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Working…";
    showError(errorBox, "");
    try {
      await operation();
      await refreshWorkflow();
    } catch (error) {
      showError(errorBox, error.message || "The operation failed.");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function reconcileAcquisition(acquisition, button) {
    await runOperation(button, async () => {
      await requestJson(`/api/automatic/acquisitions/${acquisition.id}/reconcile`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "RECONCILE_EBOOK_ACQUISITION"}),
      });
    });
  }

  async function admitAcquisition(acquisition, button) {
    const confirmed = window.confirm(
      "Admit this independently verified ebook?\n\n" +
      "BookGuard will verify the staged bytes again, publish without overwrite, " +
      "and retain the staged source until Bindery registration is confirmed."
    );
    if (!confirmed) return;
    await runOperation(button, async () => {
      await requestJson(`/api/automatic/acquisitions/${acquisition.id}/admit`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "ADMIT_EBOOK_ACQUISITION"}),
      });
    });
  }

  async function reconcileAdmission(acquisition, button) {
    await runOperation(button, async () => {
      await requestJson(`/api/automatic/admissions/${acquisition.admission_id}/reconcile`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "RECONCILE_ADMISSION"}),
      });
    });
  }

  async function finalizeAcquisition(acquisition, button) {
    const confirmed = window.confirm(
      "Finalize this registered acquisition?\n\n" +
      "BookGuard will remove only its terminal Bindery queue record and verified " +
      "staging copy. Download-client data and the admitted library ebook are retained."
    );
    if (!confirmed) return;
    await runOperation(button, async () => {
      await requestJson(`/api/automatic/acquisitions/${acquisition.id}/finalize`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "FINALIZE_EBOOK_ACQUISITION"}),
      });
    });
  }

  function renderWorkflow(coordinator, readiness, history, admissions) {
    clear(controls);
    showError(errorBox, coordinator.lastError || "");

    const acquisition = (history.items || []).find((item) => ACTIVE_STATUSES.has(item.status));
    const coordinatorLabel = coordinator.enabled
      ? `${coordinator.running ? "running" : "stopped"} · ${pretty(coordinator.state)}`
      : "disabled";

    if (!acquisition) {
      const latest = (history.items || [])[0];
      const needsAttention = ["failed", "review_required"].includes(latest?.status);
      if (needsAttention) {
        setState("Attention required", "review");
        message.textContent = latest.error || "The most recent replacement requires manual review.";
        renderDetails([
          ["Acquisition", `#${latest.id}`],
          ["Book", latest.candidate_title || `Bindery #${latest.book_id}`],
          ["Status", pretty(latest.status)],
          ["Coordinator", coordinatorLabel],
        ]);
        if (latest.status === "review_required") {
          const retry = actionButton(
            "Retry staged verification",
            () => reconcileAcquisition(latest, retry),
            "primary",
          );
          controls.append(retry);
        }
        controls.append(actionButton("Refresh status", refreshWorkflow));
        scheduleRefresh(false);
        return;
      }
      setState(readiness.ready ? "Ready" : "Safe idle", readiness.ready ? "ready" : "idle");
      message.textContent = readiness.ready
        ? "The safety gates permit one explicitly selected ebook acquisition. Start from a verified WRONG CONTENT result below."
        : `No replacement is active. ${blockerText(readiness.blockers).join("; ") || "The workflow is idle."}.`;
      renderDetails([
        ["Coordinator", coordinatorLabel],
        ["Staging", `${readiness.stagingCount || 0} ebook(s)`],
        ["Active queue", `${(readiness.activeQueueItems || []).length} item(s)`],
      ]);
      controls.append(actionButton("Refresh status", refreshWorkflow));
      scheduleRefresh(false);
      return;
    }

    const status = String(acquisition.status || "unknown");
    const admission = (admissions.items || []).find(
      (item) => Number(item.id) === Number(acquisition.admission_id),
    );
    const needsReview = status === "review_required" || status === "failed";
    const tone = status === "verified"
      ? "ready"
      : needsReview
      ? "review"
      : status === "finalized"
      ? "complete"
      : "active";

    setState(pretty(status), tone);
    message.textContent = status === "verified"
      ? "The staged ebook independently verified. BookGuard is paused for your explicit admission approval."
      : coordinator.enabled
      ? "The supervised coordinator is monitoring this operator-started replacement."
      : "A replacement is active. Use the manual progress controls or enable the supervised coordinator in the environment.";
    renderDetails([
      ["Acquisition", `#${acquisition.id}`],
      ["Book", acquisition.candidate_title || `Bindery #${acquisition.book_id}`],
      ["Queue", acquisition.queue_status || "not observed"],
      ["Coordinator", coordinatorLabel],
      ["Admission", admission ? `#${admission.id} · ${pretty(admission.status)}` : "not started"],
    ]);

    if (RECONCILE_STATUSES.has(status) && !coordinator.enabled) {
      const button = actionButton("Check progress", () => reconcileAcquisition(acquisition, button));
      controls.append(button);
    }
    if (status === "verified") {
      const button = actionButton("Admit verified ebook", () => admitAcquisition(acquisition, button), "primary");
      controls.append(button);
    }
    if (status === "admitted" && !coordinator.enabled && admission?.status !== "registered") {
      const button = actionButton("Check Bindery registration", () => reconcileAdmission(acquisition, button));
      controls.append(button);
    }
    if (
      !coordinator.enabled &&
      (status === "finalizing" || status === "cleanup_required" ||
        (status === "admitted" && admission?.status === "registered"))
    ) {
      const button = actionButton("Finalize safe cleanup", () => finalizeAcquisition(acquisition, button));
      controls.append(button);
    }
    controls.append(actionButton("Refresh status", refreshWorkflow));
    scheduleRefresh(true);
  }

  async function refreshWorkflow() {
    if (busy) return;
    window.clearTimeout(refreshTimer);
    setBusy(true);
    try {
      const [coordinator, readiness, history, admissions] = await Promise.all([
        requestJson("/api/automatic/acquisition-coordinator"),
        requestJson("/api/automatic/acquisition-readiness"),
        requestJson("/api/automatic/acquisitions?limit=20"),
        requestJson("/api/automatic/admissions?limit=20"),
      ]);
      renderWorkflow(coordinator, readiness, history, admissions);
    } catch (error) {
      setState("Unavailable", "error");
      message.textContent = "BookGuard could not load the supervised replacement status.";
      showError(errorBox, error.message || "Status request failed.");
      scheduleRefresh(false);
    } finally {
      setBusy(false);
    }
  }

  function candidateCard(candidate, resultId, allowStart) {
    const safe = candidate.bookguardSafe === true;
    const card = element("article", {className: `candidate-card ${safe ? "safe" : "rejected"}`});
    const copy = element("div");
    copy.append(element("strong", {text: candidate.title || "Untitled release"}));
    const meta = element("div", {className: "candidate-meta"});
    meta.append(
      element("span", {text: candidate.indexerName || "Unknown indexer"}),
      element("span", {text: candidate.protocol || "Unknown protocol"}),
      element("span", {text: candidate.size ? `${(Number(candidate.size) / 1048576).toFixed(2)} MiB` : "Unknown size"}),
    );
    copy.append(meta, element("p", {
      className: "candidate-reason",
      text: candidate.bookguardReason || (safe ? "Passed BookGuard's release gate." : "Rejected by the safety gate."),
    }));
    card.append(copy);

    if (safe && candidate.guid && allowStart) {
      const button = actionButton("Start this release", () => startCandidate(resultId, candidate, button), "primary");
      card.append(button);
    }
    return card;
  }

  function renderCandidates(resultId, candidates, messageText, allowStart = true) {
    const list = element("div", {className: "candidate-list"});
    if (!candidates.length) {
      list.append(element("p", {className: "muted", text: messageText || "No replacement releases were returned."}));
    } else {
      for (const candidate of candidates) {
        list.append(candidateCard(candidate, resultId, allowStart));
      }
    }
    replacementBody.append(list);
  }

  async function searchCandidates(resultId, bookId, allowStart, button) {
    button.disabled = true;
    button.textContent = "Searching…";
    try {
      const preview = await requestJson(`/api/automatic/books/${bookId}/replacement-preview`);
      renderCandidates(resultId, preview.results || [], preview.message, allowStart);
    } catch (error) {
      replacementBody.append(element("div", {className: "notice warning workflow-error", text: error.message}));
    } finally {
      button.remove();
    }
  }

  async function remediateWrongContent(resultId, bookId, button) {
    const confirmed = window.confirm(
      "Prepare this WRONG CONTENT ebook for replacement?\n\n" +
      "BookGuard will immediately verify it again, move the exact file to quarantine, " +
      "detach its Bindery association, and search for replacements. It will not start a download."
    );
    if (!confirmed) return;

    button.disabled = true;
    button.textContent = "Preparing…";
    try {
      const result = await requestJson(`/api/automatic/results/${resultId}/remediate-wrong-content`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({confirm: "REMEDIATE_WRONG_CONTENT"}),
      });
      clear(replacementBody);
      const summary = element("div", {className: "replacement-preflight"});
      summary.append(
        element("strong", {text: "Wrong ebook safely quarantined"}),
        element("p", {text: result.message || "Replacement preparation completed."}),
      );
      if (result.warning) summary.append(element("p", {className: "warning-text", text: result.warning}));
      replacementBody.append(summary);
      renderCandidates(
        resultId,
        result.replacementCandidates || [],
        "No safe replacement was found.",
      );
      await refreshWorkflow();
    } catch (error) {
      replacementBody.append(element("div", {className: "notice warning workflow-error", text: error.message}));
      button.disabled = false;
      button.textContent = "Quarantine wrong ebook and search";
    }
  }

  async function startCandidate(resultId, candidate, button) {
    const confirmed = window.confirm(
      `Start this explicitly selected replacement?\n\n${candidate.title || candidate.guid}\n\n` +
      "Bindery will download it into staging. BookGuard will not admit it unless the downloaded bytes independently verify and you approve admission."
    );
    if (!confirmed) return;

    button.disabled = true;
    button.textContent = "Starting…";
    try {
      await requestJson(`/api/automatic/results/${resultId}/acquisitions`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          candidateGuid: candidate.guid,
          confirm: "START_EBOOK_ACQUISITION",
        }),
      });
      replacementPanel.hidden = true;
      await refreshWorkflow();
      panel.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) {
      replacementBody.append(element("div", {className: "notice warning workflow-error", text: error.message}));
      button.disabled = false;
      button.textContent = "Start this release";
    }
  }

  async function openReplacement(resultId, bookId) {
    replacementPanel.hidden = false;
    replacementTitle.textContent = "Supervised replacement preflight";
    replacementSubtitle.textContent = `Scan result #${resultId} · Bindery book #${bookId}`;
    clear(replacementBody);
    replacementBody.append(element("p", {className: "muted", text: "Rechecking the exact wrong-content item…"}));
    replacementPanel.scrollIntoView({behavior: "smooth", block: "start"});

    try {
      const preview = await requestJson(`/api/automatic/results/${resultId}/wrong-content-preview`);
      clear(replacementBody);
      const preflight = element("div", {className: "replacement-preflight"});
      preflight.append(
        element("strong", {text: preview.safe ? "Ready for guarded preparation" : "Preparation is currently blocked"}),
        element("p", {
          text: preview.safe
            ? "Verification, Bindery identity, and the separately gated writable alias all passed preflight."
            : "BookGuard will not move or detach this item because one or more safety preflight checks failed.",
        }),
      );
      const facts = [
        ["Verdict", `${pretty(preview.verdict)} · ${preview.confidence || 0}%`],
        ["Source file", preview.sourceExists ? "present" : "absent"],
        ["Exact DB match", preview.exactDbMatch ? "yes" : "no"],
        ["Bindery association", preview.binderyTracksPath ? "exact" : "absent"],
        ["Writable action alias", preview.ebookActionReady ? "verified" : "blocked"],
      ];
      const factList = element("dl", {className: "workflow-details"});
      for (const [label, value] of facts) {
        const wrapper = element("div");
        wrapper.append(element("dt", {text: label}), element("dd", {text: value}));
        factList.append(wrapper);
      }
      preflight.append(factList);
      if (!preview.ebookActionReady && (preview.ebookActionBlockers || []).length) {
        preflight.append(element("p", {
          className: "candidate-reason",
          text: blockerText(preview.ebookActionBlockers).join("; "),
        }));
      }
      replacementBody.append(preflight);

      if (preview.safe) {
        const button = actionButton(
          "Quarantine wrong ebook and search",
          () => remediateWrongContent(resultId, bookId, button),
          "danger",
        );
        replacementBody.append(element("div", {className: "workflow-controls"}));
        replacementBody.lastElementChild.append(button);
      } else {
        const allowStart = !preview.sourceExists && !preview.binderyTracksPath;
        const button = actionButton(
          "Read-only replacement search",
          () => searchCandidates(resultId, bookId, allowStart, button),
        );
        replacementBody.append(element("div", {className: "workflow-controls"}));
        replacementBody.lastElementChild.append(button);
        if (!allowStart) {
          replacementBody.append(element("p", {
            className: "candidate-reason",
            text: "Search results will remain read-only until the existing file and Bindery association are safely prepared.",
          }));
        }
      }
    } catch (error) {
      clear(replacementBody);
      replacementBody.append(element("div", {className: "notice warning workflow-error", text: error.message}));
    }
  }

  document.addEventListener("click", (event) => {
    const previewButton = event.target.closest("[data-replacement-result]");
    if (previewButton) {
      openReplacement(
        Number(previewButton.dataset.replacementResult),
        Number(previewButton.dataset.replacementBook),
      );
      return;
    }
    if (event.target.closest("[data-replacement-close]")) replacementPanel.hidden = true;
  });

  refreshWorkflow();
})();
