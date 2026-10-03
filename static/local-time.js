(() => {
  "use strict";
  // Show stored UTC timestamps in the viewer's own time zone: "Today 23:52", "Sep 28 10:32".
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const time = (d) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  for (const element of document.querySelectorAll("time[data-local]")) {
    const value = element.getAttribute("datetime") || "";
    const date = new Date(/[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`);
    if (Number.isNaN(date.getTime())) continue;
    let label;
    // data-local="time": the page already shows the day (Activity's day headings).
    if (element.dataset.local === "time") label = time(date);
    else if (sameDay(date, now)) label = `Today ${time(date)}`;
    else if (sameDay(date, yesterday)) label = `Yesterday ${time(date)}`;
    else {
      const options = { month: "short", day: "numeric" };
      if (date.getFullYear() !== now.getFullYear()) options.year = "numeric";
      label = `${date.toLocaleDateString([], options)} ${time(date)}`;
    }
    element.textContent = label;
    element.title = date.toLocaleString();
  }
})();
