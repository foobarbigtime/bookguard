(() => {
  "use strict";

  // While a scan or check runs, refresh the page every few seconds to show progress.
  const now = document.getElementById("rightNow");
  if (now && now.dataset.active === "1") setTimeout(() => window.location.reload(), 8000);
})();
