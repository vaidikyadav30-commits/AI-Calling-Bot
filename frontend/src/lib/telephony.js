/**
 * telephony.js — client for the Python control plane.
 *
 * Every call goes through the Vite dev server, which proxies /api/telephony to
 * the FastAPI service (see vite.config.js). No credential ever reaches the
 * browser: it only sees numbers, call records, and short-lived,
 * subscribe-only room tokens.
 */

const BASE = "/api/telephony";

/** Error carrying the server's stable `code`, so the UI can branch on it. */
export class TelephonyError extends Error {
  constructor(message, { code = "", status = 0 } = {}) {
    super(message);
    this.name = "TelephonyError";
    this.code = code;
    this.status = status;
  }
}

async function request(path, { method = "GET", body } = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new TelephonyError(
      "Could not reach the telephony service. Is it running? " +
        "(uv run python -m ai_caller.telephony.api)",
      { code: "unreachable" },
    );
  }

  const payload = await response.json().catch(() => null);

  if (!response.ok) {
    // FastAPI validation errors nest under `detail`; ours are flat.
    const detail = payload?.detail;
    const message =
      payload?.error ??
      (typeof detail === "string" ? detail : detail?.error) ??
      `Request failed (HTTP ${response.status}).`;
    throw new TelephonyError(message, {
      code: payload?.code ?? detail?.code ?? "",
      status: response.status,
    });
  }

  return payload;
}

/** Whether telephony is configured at all, and what it can do. */
export const fetchHealth = () => request("/health");

/** Phone numbers this LiveKit project holds, and how they route. */
export const fetchNumbers = ({ refresh = false } = {}) =>
  request(`/numbers${refresh ? "?refresh=true" : ""}`);

/** Whether inbound calls actually reach the agent, and through what. */
export const fetchInboundStatus = () => request("/inbound");

/** Calls currently up, inbound and outbound. */
export const fetchCalls = () => request("/calls").then((body) => body.calls ?? []);

/**
 * Start an outbound call.
 * @returns the call record plus a room token for listening in.
 */
export const placeCall = ({ to, from, context }) =>
  request("/calls", { method: "POST", body: { to, from, context } });

/** A fresh subscribe-only token for a call that is already running. */
export const monitorCall = (room) =>
  request(`/calls/${encodeURIComponent(room)}/monitor`, { method: "POST" });

/** End a call for everyone on it. */
export const hangupCall = (room) =>
  request(`/calls/${encodeURIComponent(room)}`, { method: "DELETE" });
