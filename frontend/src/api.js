const configured = import.meta.env.VITE_BACKEND_URL?.trim();
export const API_BASE = (configured || `${window.location.protocol}//${window.location.hostname}:8000`).replace(/\/$/, "");
export const PORTAL_URL = (import.meta.env.VITE_PORTAL_URL?.trim() || `${window.location.protocol}//${window.location.hostname}:8002`).replace(/\/$/, "");

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      Accept: "application/json",
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
    ...options,
  });

  const text = await response.text();
  let body = {};
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { detail: text };
  }

  if (!response.ok) {
    const message = typeof body.detail === "string"
      ? body.detail
      : JSON.stringify(body.detail || body);
    throw new Error(message || `HTTP ${response.status}`);
  }
  return body;
}

export const api = {
  health: () => request("/health-summary"),
  incidents: () => request("/incidents"),
  incident: (id) => request(`/incidents/${id}`),
  catalog: () => request("/catalog"),
  injectFault: (name) => request(`/faults/${encodeURIComponent(name)}`, { method: "POST" }),
  demo: (scenario) => request(`/demo/run/${encodeURIComponent(scenario)}`, { method: "POST" }),
  reset: () => request("/reset", { method: "POST" }),
  approve: (id) => request(`/approvals/${id}/approve`, {
    method: "POST",
    body: JSON.stringify({ decided_by: "dashboard_operator" }),
  }),
  reject: (id) => request(`/approvals/${id}/reject`, {
    method: "POST",
    body: JSON.stringify({ decided_by: "dashboard_operator" }),
  }),
};

export function eventStreamUrl() {
  return `${API_BASE}/stream`;
}

export function screenshotUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;

  // Screenshot paths are recorded by the RPA worker. The backend must expose
  // its screenshots directory at /screenshots for the preview to load.
  const filename = String(path).split(/[\\/]/).pop();
  return `${API_BASE}/screenshots/${encodeURIComponent(filename)}`;
}