import { randomUUID } from "node:crypto";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { AccessToken, RoomAgentDispatch, RoomConfiguration } from "livekit-server-sdk";
import { defineConfig, loadEnv } from "vite";

const HERE = dirname(fileURLToPath(import.meta.url));

// Credentials live in the project-root .env.local, the same file the Python
// agent reads. They are NOT prefixed with VITE_, so Vite never inlines them
// into the browser bundle — the API secret stays on the server side.
const PROJECT_ROOT = resolve(HERE, "..");

const ENDPOINT = "/api/connection-details";

/**
 * Serves the connection-details endpoint from the Vite dev server, mirroring
 * the LiveKit Cloud frontend architecture: the browser asks for a room and
 * gets back a scoped, short-lived token. It never sees the API secret.
 *
 * The agent dispatch is embedded in the token via RoomConfiguration, so the
 * agent is dispatched atomically when the room is created. That removes the
 * race where the browser asks for an agent before the worker has registered.
 */
function connectionDetailsPlugin(env) {
  const url = env.LIVEKIT_URL;
  const apiKey = env.LIVEKIT_API_KEY;
  const apiSecret = env.LIVEKIT_API_SECRET;
  const agentName = env.AGENT_NAME || "my-agent";

  const missing = Object.entries({
    LIVEKIT_URL: url,
    LIVEKIT_API_KEY: apiKey,
    LIVEKIT_API_SECRET: apiSecret,
  })
    .filter(([, value]) => !value)
    .map(([name]) => name);

  const sendJson = (res, status, body) => {
    res.statusCode = status;
    res.setHeader("Content-Type", "application/json");
    res.setHeader("Cache-Control", "no-store");
    res.end(JSON.stringify(body));
  };

  return {
    name: "ai-caller-connection-details",
    configureServer(server) {
      if (missing.length > 0) {
        server.config.logger.warn(
          `[ai-caller] Missing ${missing.join(", ")} in ${PROJECT_ROOT}\\.env.local — ` +
            `${ENDPOINT} will return an error until they are set.`,
        );
      }

      server.middlewares.use(ENDPOINT, async (_req, res) => {
        if (missing.length > 0) {
          return sendJson(res, 500, {
            error: `Missing ${missing.join(", ")} in .env.local. Add them and restart the dev server.`,
          });
        }

        try {
          // A fresh room per call: no chance of colliding with an agent left
          // over from a previous session.
          const roomName = `ai-caller-${randomUUID().slice(0, 8)}`;
          const identity = `user-${randomUUID().slice(0, 8)}`;

          const at = new AccessToken(apiKey, apiSecret, { identity, ttl: "15m" });
          at.addGrant({
            roomJoin: true,
            room: roomName,
            canPublish: true,
            canSubscribe: true,
            canPublishData: true,
          });
          at.roomConfig = new RoomConfiguration({
            agents: [new RoomAgentDispatch({ agentName })],
          });

          return sendJson(res, 200, {
            serverUrl: url,
            roomName,
            participantName: identity,
            participantToken: await at.toJwt(),
          });
        } catch (err) {
          server.config.logger.error(`[ai-caller] ${ENDPOINT} failed: ${err?.message ?? err}`);
          return sendJson(res, 500, { error: err?.message ?? "Failed to mint a token." });
        }
      });
    },
  };
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Empty prefix loads every var, not just VITE_ ones. This value is only used
  // inside the plugin above, which runs in Node.
  const env = loadEnv(mode, PROJECT_ROOT, "");

  // The telephony control plane is a Python service (src/ai_caller/telephony/api.py).
  // Proxying to it keeps Twilio and LiveKit credentials on the server side and
  // leaves the browser talking to a same-origin path, so there is no CORS to
  // configure in development.
  const telephonyPort = env.TELEPHONY_API_PORT || "8080";
  const telephonyTarget = `http://127.0.0.1:${telephonyPort}`;

  return {
    plugins: [react(), connectionDetailsPlugin(env)],
    server: {
      port: 3000,
      open: true,
      proxy: {
        "/api/telephony": {
          target: telephonyTarget,
          changeOrigin: true,
          // A clear message beats a blank screen when the Python service is
          // simply not running yet.
          configure: (proxy) => {
            proxy.on("error", (err) => {
              console.warn(
                `[ai-caller] telephony service unreachable at ${telephonyTarget}: ` +
                  `${err.message}\n` +
                  "           Start it with: uv run python -m ai_caller.telephony.api",
              );
            });
          },
        },
      },
    },
  };
});
