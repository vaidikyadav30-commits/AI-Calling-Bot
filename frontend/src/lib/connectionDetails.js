/**
 * connectionDetails.js — asks the dev server for a room and a scoped token.
 *
 * Token minting and agent dispatch happen in the Vite middleware
 * (see vite.config.js), so the LiveKit API secret never reaches the browser.
 */

const ENDPOINT = "/api/connection-details";

/**
 * @returns {Promise<{serverUrl: string, roomName: string, participantName: string, participantToken: string}>}
 */
export async function fetchConnectionDetails() {
  let response;
  try {
    response = await fetch(ENDPOINT);
  } catch {
    throw new Error("Could not reach the dev server. Is `npm run dev` still running?");
  }

  const body = await response.json().catch(() => null);

  if (!response.ok) {
    throw new Error(body?.error ?? `Connection request failed (HTTP ${response.status}).`);
  }
  if (!body?.participantToken || !body?.serverUrl) {
    throw new Error("Malformed response from the connection endpoint.");
  }

  return body;
}
