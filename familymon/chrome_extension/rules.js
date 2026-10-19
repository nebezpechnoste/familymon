// Pure matching rules, shared by the service worker and the node tests.
// Mirrors Chrome's URLBlocklist host semantics: a bare domain covers itself
// and every subdomain; an allowed entry wins over a blocked one.

function normalise(host) {
  return String(host || "").toLowerCase().replace(/\.$/, "");
}

function hostMatches(host, domain) {
  const d = normalise(domain);
  return d !== "" && (host === d || host.endsWith("." + d));
}

// The blocked domain the host falls under, or null (also null when allowed).
export function blockedHost(host, blocked, allowed = []) {
  const h = normalise(host);
  if (h === "") return null;
  if (allowed.some((d) => hostMatches(h, d))) return null;
  for (const d of blocked) {
    if (hostMatches(h, d)) return normalise(d);
  }
  return null;
}

// Hostname of a tab URL, or null for non-web URLs (chrome://, about:, file:).
export function tabHost(url) {
  let u;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== "http:" && u.protocol !== "https:") return null;
  return u.hostname;
}

export function normaliseConfig(raw) {
  const list = (v) => (Array.isArray(v) ? v.filter((x) => typeof x === "string") : []);
  return {
    active: raw?.active === true,
    blocked: list(raw?.blocked),
    allowed: list(raw?.allowed),
    subject: typeof raw?.subject === "string" ? raw.subject : "",
    until: typeof raw?.until === "string" ? raw.until : "",
  };
}
