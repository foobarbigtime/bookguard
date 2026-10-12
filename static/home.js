(() => {
  "use strict";

  // Keep "Right now" live. The page is drawn once, so a check started after it
  // loaded (or by someone else) would otherwise keep saying "Nothing is running".
  const card = document.getElementById("rightNow");
  if (!card) return;
  let wasActive = card.dataset.active === "1";

  function render(now) {
    const label = document.createElement("strong");
    label.textContent = now.label;
    const track = document.createElement("div");
    track.className = "progress-track";
    const bar = document.createElement("div");
    bar.className = "progress-bar";
    bar.style.width = `${now.percent || 0}%`;
    track.append(bar);
    const detail = document.createElement("small");
    const parts = [`${now.processed} of ${now.total}`];
    if (now.current) parts.push(`now: ${now.current}`);
    if (now.etaSeconds) parts.push(`about ${Math.max(1, Math.round(now.etaSeconds / 60))} min left`);
    detail.textContent = parts.join(" · ");
    card.replaceChildren(label, track, detail);
    card.dataset.active = "1";
  }

  async function poll() {
    try {
      const response = await fetch("/api/home/now", { cache: "no-store" });
      if (response.ok) {
        const now = await response.json();
        if (now.active) {
          wasActive = true;
          render(now);
        } else if (wasActive) {
          // It just finished: reload so the counts and attention list are current.
          window.location.reload();
          return;
        }
      }
    } catch (error) {
      // The next poll tries again.
    }
    setTimeout(poll, 4000);
  }
  setTimeout(poll, 1500);
})();
