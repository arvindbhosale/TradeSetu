"use strict";
// Applied before the page paints, so a saved theme does not flash the other one first.
(() => {
  let saved = null;
  try { saved = localStorage.getItem("desk-theme"); } catch (err) { /* storage blocked */ }
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
})();
