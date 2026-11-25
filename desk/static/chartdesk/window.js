// @ts-check
// The chart window (/charts): just charts, no desk around them. "Pop out" on the Charts tab opens it with
// the chart's stock (?venue=&symbol=&tf=), shown alone; the window can also show several charts at once.

import { mountGrid } from "./grid.js";

const meta = /** @type {HTMLMetaElement|null} */ (document.querySelector('meta[name="desk-token"]'));
const TOKEN = meta ? meta.content : "";

/** @param {string} path @param {{ method?: string, body?: any }} [opts] */
async function api(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch("/api" + path, { method, body: body ? JSON.stringify(body) : undefined,
      headers: { "X-Desk-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) } });
  } catch (err) {
    throw new Error("The desk is not answering. Check that it is running, then reload this window.");
  }
  let payload = null;
  try { payload = await response.json(); } catch (err) { /* empty body */ }
  if (!response.ok) {
    const detail = payload && (payload.error || payload.detail);
    const error = /** @type {Error & { payload?: any }} */ (new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`));
    error.payload = payload || {};
    if (response.status === 401 && error.payload.signed_out) window.location.replace("/login");
    throw error;
  }
  return payload;
}

const q = new URLSearchParams(location.search);
const venue = q.get("venue") || "", symbol = (q.get("symbol") || "").trim().toUpperCase(), tf = q.get("tf") || "";
const seed = ["crypto", "india", "imported"].includes(venue) && symbol && symbol.length <= 40 ? { venue, symbol, tf: /^[0-9a-z]{1,4}$/.test(tf) ? tf : "" } : null;
if (seed) document.title = `${symbol} · Charts`;

const root = /** @type {HTMLElement} */ (document.getElementById("charts"));
const grid = mountGrid(root, { api, window: true, seed });
grid.show();
