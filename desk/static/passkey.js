"use strict";
// WebAuthn helpers shared by the sign-in page and Settings. JSON <-> browser objects, with a fallback
// for browsers that lack PublicKeyCredential.parse*FromJSON / toJSON.
window.Passkey = (() => {
  const toBytes = (s) => { const b = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)); return Uint8Array.from(b, (c) => c.charCodeAt(0)); };
  const toB64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  const supported = () => Boolean(window.PublicKeyCredential && navigator.credentials && window.isSecureContext);

  function creationOptions(o) {
    if (PublicKeyCredential.parseCreationOptionsFromJSON) return PublicKeyCredential.parseCreationOptionsFromJSON(o);
    return { ...o, challenge: toBytes(o.challenge), user: { ...o.user, id: toBytes(o.user.id) },
      excludeCredentials: (o.excludeCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })) };
  }
  function requestOptions(o) {
    if (PublicKeyCredential.parseRequestOptionsFromJSON) return PublicKeyCredential.parseRequestOptionsFromJSON(o);
    return { ...o, challenge: toBytes(o.challenge), allowCredentials: (o.allowCredentials || []).map((c) => ({ ...c, id: toBytes(c.id) })) };
  }
  function toJSON(cred) {
    if (cred.toJSON) { try { return cred.toJSON(); } catch (err) { /* fall through */ } }
    const r = cred.response, out = { id: cred.id, rawId: toB64(cred.rawId), type: cred.type, clientExtensionResults: cred.getClientExtensionResults ? cred.getClientExtensionResults() : {}, response: { clientDataJSON: toB64(r.clientDataJSON) } };
    if (r.attestationObject) { out.response.attestationObject = toB64(r.attestationObject); if (r.getTransports) out.response.transports = r.getTransports(); }
    if (r.authenticatorData) { out.response.authenticatorData = toB64(r.authenticatorData); out.response.signature = toB64(r.signature); if (r.userHandle) out.response.userHandle = toB64(r.userHandle); }
    if (cred.authenticatorAttachment) out.authenticatorAttachment = cred.authenticatorAttachment;
    return out;
  }
  async function create(options) { return toJSON(await navigator.credentials.create({ publicKey: creationOptions(options) })); }
  async function get(options) { return toJSON(await navigator.credentials.get({ publicKey: requestOptions(options) })); }
  const cancelled = (err) => err && (err.name === "NotAllowedError" || err.name === "AbortError");
  return { supported, create, get, cancelled };
})();
