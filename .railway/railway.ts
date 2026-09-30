/**
 * Railway Infrastructure as Code — the two services this repo deploys.
 *
 * Both run the SAME image, built from the repository root Dockerfile. They
 * differ only in their start command:
 *
 *   agent  the LiveKit worker. Holds no HTTP port; LiveKit Cloud dispatches
 *          jobs to it over an outbound connection, so it needs no domain.
 *   web    the dialer UI plus the telephony control plane, on one origin.
 *          This is the service that gets the public domain.
 *
 * One image for both is deliberate: the UI and the agent can never drift to
 * different commits, and a deploy is one build instead of two.
 *
 * Secrets are NOT declared here. They live as Railway service variables so no
 * credential is ever committed — see DEPLOY.md for the list and `railway
 * variables --set`. Adding them below would put them in git.
 *
 * Usage (requires `npm install railway` and the Railway CLI):
 *
 *     railway link
 *     railway config plan     # preview
 *     railway config apply
 *
 * Everything here can equally be set by hand in the dashboard; see DEPLOY.md.
 */
import { defineRailway, group, project, service } from "railway/iac";

// Both services read the same credentials, so Railway variables are set on
// both. These are the non-secret ones — safe to keep in git, and the reason
// the deployment needs no .env.local.
const SHARED_ENV = {
  // The dispatch name the frontend asks for and the worker registers under.
  // They must match or the browser joins a room no worker serves.
  AGENT_NAME: "my-agent",
};

export default defineRailway(() => {
  // The LiveKit worker. No healthcheck and no domain: it never listens for
  // inbound HTTP, so Railway has nothing to probe. ALWAYS restart, because a
  // worker that has exited answers no calls and there is nothing to preserve
  // by leaving it down.
  const agent = service("agent", {
    start: "uv run python src/agent.py start",
    env: SHARED_ENV,
  });

  // The public service: dialer UI, /api/connection-details, and the telephony
  // control plane. Binds 0.0.0.0:$PORT, both of which Railway supplies and
  // config.py reads.
  const web = service("web", {
    start: "uv run python -m ai_caller.webapp",
    // Unauthenticated by design, so the UI can explain its own state before
    // anyone is logged in — which is exactly what makes it a good probe.
    healthcheck: "/api/telephony/health",
    // The image imports ~13s of LiveKit libraries before it serves anything.
    healthcheckTimeout: 300,
    env: {
      ...SHARED_ENV,
      // This service is meant to be reachable from the internet, so the
      // refusal to bind publicly would be wrong here.
      //
      // It is not the only guard. Once an outbound trunk is configured, this
      // service also refuses to start without TELEPHONY_ALLOWED_NUMBERS — set
      // that as a Railway variable, not here, so the numbers stay out of git.
      // See DEPLOY.md.
      TELEPHONY_ALLOW_PUBLIC: "true",
    },
  });

  return project("ai-caller", {
    resources: [group("AI Caller", [web, agent])],
  });
});
