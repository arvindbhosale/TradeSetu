"use strict";
// After signing in: the page the visit was for (only the Kite login link from the morning reminder), else the desk.
const AFTER = new URLSearchParams(window.location.search).get("next") === "/broker/login" ? "/broker/login" : "/";
const remember = () => document.getElementById("remember").checked;
document.getElementById("remember").addEventListener("change", (event) => { document.getElementById("remember-hint").hidden = !event.target.checked; });
document.getElementById("form-login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target, button = form.querySelector("button"), box = document.getElementById("login-error");
  const codeRow = document.getElementById("code-row");
  box.hidden = true; button.disabled = true; button.textContent = "Signing in…";
  try {
    const response = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: form.username.value, password: form.password.value, code: form.code.value, remember: remember() }) });
    if (response.ok) { window.location.replace(AFTER); return; }
    let message = "Sign-in failed.", needsCode = false;
    try { const data = await response.json(); message = data.error || message; needsCode = Boolean(data.needs_code); } catch (err) { /* not JSON */ }
    if (needsCode && codeRow.hidden) {
      // Password accepted so far: ask for the second step and keep what was typed.
      codeRow.hidden = false; button.disabled = false; button.textContent = "Sign in"; form.code.focus();
      box.textContent = message; box.hidden = false; return;
    }
    box.textContent = message;
  } catch (err) { box.textContent = "The desk is not answering. Try again."; }
  box.hidden = false; button.disabled = false; button.textContent = "Sign in"; form.password.value = ""; form.code.value = "";
});

(() => {
  const button = document.getElementById("passkey-login"), box = document.getElementById("login-error");
  if (!window.Passkey || !Passkey.supported()) return;
  button.hidden = false; document.getElementById("passkey-row").hidden = false;
  button.addEventListener("click", async () => {
    box.hidden = true; button.disabled = true; button.textContent = "Waiting for your passkey…";
    try {
      const post = (url, body) => fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      const start = await post("/api/passkey/options");
      const begun = await start.json();
      if (!start.ok) throw new Error(begun.error || "Passkey sign-in is not available.");
      const credential = await Passkey.get(begun.options);
      const done = await post("/api/passkey/login", { token: begun.token, credential, remember: remember() });
      if (done.ok) { window.location.replace(AFTER); return; }
      throw new Error((await done.json()).error || "The passkey was not accepted.");
    } catch (err) {
      box.textContent = Passkey.cancelled(err) ? "Passkey sign-in was cancelled." : err.message; box.hidden = false;
    }
    button.disabled = false; button.textContent = "Sign in with a passkey";
  });
})();
