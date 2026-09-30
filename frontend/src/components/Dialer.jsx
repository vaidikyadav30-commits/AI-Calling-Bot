import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchCalls,
  fetchHealth,
  fetchInboundStatus,
  fetchNumbers,
  hangupCall,
  monitorCall,
  placeCall,
} from "../lib/telephony";
import "./Dialer.css";

// Live calls are polled rather than pushed: the control plane is stateless and
// reads rooms from LiveKit, so there is nothing to subscribe to. Four seconds
// is frequent enough to feel live without hammering the API.
const CALLS_POLL_MS = 4000;

/** Group a raw E.164 string for display without pretending to know the format. */
function formatNumber(number) {
  if (!number) return "—";
  const digits = number.replace(/^\+/, "");
  if (digits.length < 7) return number;
  const country = digits.slice(0, digits.length - 10) || digits.slice(0, 1);
  const rest = digits.slice(country.length);
  return `+${country} ${rest.replace(/(\d{3})(?=\d)/g, "$1 ")}`.trim();
}

function elapsed(startedAt) {
  if (!startedAt) return "";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - startedAt));
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

// ──────────────────────────────────────────────────────────────
// Dialer — the agent's phone numbers, and the calls on them
// ──────────────────────────────────────────────────────────────
export default function Dialer({ onJoinCall, onTalkInBrowser }) {
  const [mode, setMode] = useState("inbound");

  const [health, setHealth] = useState(null);
  const [inventory, setInventory] = useState(null);
  const [inbound, setInbound] = useState(null);
  const [calls, setCalls] = useState([]);

  const [callerId, setCallerId] = useState("");
  const [destination, setDestination] = useState("");
  const [calleeName, setCalleeName] = useState("");

  const [loading, setLoading] = useState(true);
  const [dialing, setDialing] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");
  // Only the setter is used: bumping it re-renders, which is what advances the
  // call timers. The value itself is never read.
  const [, tickTimers] = useState(0);

  // ── Initial load ──
  // Nothing is set synchronously here: a previous error stays on screen until
  // the reload actually succeeds, which is both calmer to look at and keeps
  // this out of the cascading-render trap.
  const load = useCallback(async ({ refresh = false } = {}) => {
    try {
      const status = await fetchHealth();
      setError(null);
      setHealth(status);
      if (!status.enabled) {
        setLoading(false);
        return;
      }

      const [numbers, inboundStatus] = await Promise.all([
        fetchNumbers({ refresh }),
        fetchInboundStatus().catch(() => null),
      ]);
      setInventory(numbers);
      setInbound(inboundStatus);
      setCallerId((current) => current || numbers.defaultCallerId || "");
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Fetching on mount is exactly the "synchronize with an external system"
    // case the rule exists to allow; every setState inside `load` happens
    // after an await, so no render cascades from this call.
    // oxlint-disable-next-line react/set-state-in-effect
    load();
  }, [load]);

  // ── Live calls ──
  useEffect(() => {
    if (!health?.enabled) return undefined;

    let cancelled = false;
    const poll = async () => {
      try {
        const live = await fetchCalls();
        if (!cancelled) setCalls(live);
      } catch {
        // A transient failure here should not wipe the list on screen.
      }
    };

    poll();
    const timer = setInterval(poll, CALLS_POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [health?.enabled]);

  // Re-render once a second so the call timers advance.
  useEffect(() => {
    if (!calls.length) return undefined;
    const timer = setInterval(() => tickTimers((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [calls.length]);

  const numbers = useMemo(
    () => (inventory?.numbers ?? []).filter((number) => number.held),
    [inventory],
  );

  const dial = useCallback(
    async (event) => {
      event?.preventDefault();
      setDialing(true);
      setError(null);
      setNotice("");
      try {
        const call = await placeCall({
          to: destination,
          from: callerId || undefined,
          context: calleeName ? { name: calleeName } : undefined,
        });
        setNotice(`Calling ${formatNumber(call.toNumber)}…`);
        setDestination("");
        setCalleeName("");
        onJoinCall?.(call);
      } catch (err) {
        setError(err.message);
      } finally {
        setDialing(false);
      }
    },
    [destination, callerId, calleeName, onJoinCall],
  );

  const listen = useCallback(
    async (call) => {
      setError(null);
      try {
        const token = await monitorCall(call.room);
        onJoinCall?.({ ...call, ...token });
      } catch (err) {
        setError(err.message);
      }
    },
    [onJoinCall],
  );

  const hangup = useCallback(async (call) => {
    setError(null);
    try {
      await hangupCall(call.room);
      setCalls((current) => current.filter((item) => item.room !== call.room));
    } catch (err) {
      setError(err.message);
    }
  }, []);

  // ── States that are not the dialer ──
  if (loading) {
    return (
      <DialerShell>
        <div className="dialer-loading">
          <div className="connecting-spinner" />
          <span>Loading telephony…</span>
        </div>
      </DialerShell>
    );
  }

  if (health && !health.enabled) {
    return (
      <DialerShell onTalkInBrowser={onTalkInBrowser}>
        <SetupNotice
          title="Telephony is not configured"
          lines={[
            "Add your LiveKit credentials to .env.local:",
            "  LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET",
            "",
            "Then rent a number and route it to the agent:",
            "  uv run python scripts/setup_telephony.py --rent",
          ]}
          onRetry={() => load({ refresh: true })}
        />
      </DialerShell>
    );
  }

  return (
    <DialerShell
      onRefresh={() => load({ refresh: true })}
      onTalkInBrowser={onTalkInBrowser}
    >
      {/* ── Mode switch ── */}
      <div className="dialer-tabs" role="tablist">
        <button
          role="tab"
          aria-selected={mode === "inbound"}
          className={`dialer-tab ${mode === "inbound" ? "active" : ""}`}
          onClick={() => setMode("inbound")}
        >
          Inbound
          <span className="dialer-tab-sub">the agent answers</span>
        </button>
        <button
          role="tab"
          aria-selected={mode === "outbound"}
          className={`dialer-tab ${mode === "outbound" ? "active" : ""}`}
          onClick={() => setMode("outbound")}
        >
          Outbound
          <span className="dialer-tab-sub">
            {health?.canDialOut ? "the agent calls out" : "needs a trunk"}
          </span>
        </button>
      </div>

      {error && (
        <div className="dialer-alert error" role="alert">
          {error}
        </div>
      )}
      {notice && !error && <div className="dialer-alert notice">{notice}</div>}

      {(inventory?.warnings ?? []).map((warning) => (
        <div key={warning} className="dialer-alert warn">
          {warning}
        </div>
      ))}

      {mode === "inbound" ? (
        <InboundPanel
          numbers={numbers}
          inbound={inbound}
          agentName={health?.agentName}
        />
      ) : (
        <OutboundPanel
          numbers={numbers}
          callerId={callerId}
          setCallerId={setCallerId}
          destination={destination}
          setDestination={setDestination}
          calleeName={calleeName}
          setCalleeName={setCalleeName}
          dialing={dialing}
          canDial={health?.canDialOut}
          onDial={dial}
        />
      )}

      <LiveCalls calls={calls} onListen={listen} onHangup={hangup} />
    </DialerShell>
  );
}

// ──────────────────────────────────────────────────────────────
// Layout
// ──────────────────────────────────────────────────────────────
function DialerShell({ children, onRefresh, onTalkInBrowser }) {
  return (
    <div className="dialer-layout">
      <header className="room-header">
        <div className="room-logo">
          <div className="logo-mark">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <circle cx="6" cy="6" r="4" fill="#0c0c0b" />
            </svg>
          </div>
          <span className="logo-text">AI Caller</span>
          <div className="logo-divider" />
          <span className="logo-sub">Telephony</span>
        </div>

        {onTalkInBrowser && (
          <button className="btn" onClick={onTalkInBrowser}>
            Talk in browser
          </button>
        )}
        {onRefresh && (
          <button className="btn" onClick={onRefresh} title="Reload from LiveKit">
            Refresh
          </button>
        )}
      </header>

      <main className="dialer-main">{children}</main>
    </div>
  );
}

function SetupNotice({ title, lines, onRetry }) {
  return (
    <div className="dialer-setup card">
      <h2>{title}</h2>
      <pre>{lines.join("\n")}</pre>
      {onRetry && (
        <button className="btn" onClick={onRetry}>
          Check again
        </button>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────
// Inbound — the numbers callers dial
// ──────────────────────────────────────────────────────────────
function InboundPanel({ numbers, inbound, agentName }) {
  const ready = inbound?.ready;
  const routed = new Set((inbound?.routedNumbers ?? []).map((n) => n.number));

  return (
    <section className="dialer-panel card">
      <div className="dialer-panel-head">
        <h2>Receive calls</h2>
        <span className={`dialer-badge ${ready ? "ok" : "warn"}`}>
          {ready ? "Live" : "Not routed"}
        </span>
      </div>

      <p className="dialer-hint">
        {ready
          ? `Calls to the numbers below are answered by "${agentName}", with the ` +
            "same knowledge base and guardrails as the browser agent."
          : "These numbers do not reach the agent yet. Run " +
            "scripts/setup_telephony.py to create the dispatch rule."}
      </p>

      <ul className="dialer-numbers">
        {!numbers.length && (
          <li className="dialer-empty">
            No phone numbers yet. Every LiveKit plan includes one free US local
            number — rent it with{" "}
            <code>uv run python scripts/setup_telephony.py --rent</code>.
          </li>
        )}
        {numbers.map((number) => {
          const live = number.inboundReady || routed.has(number.number);
          return (
            <li key={number.id || number.number}>
              <span className={`status-dot ${live ? "connected" : ""}`} />
              <span className="dialer-number">{number.number}</span>
              <span className="dialer-number-name">
                {number.label}
                {number.numberType === "toll_free" ? " · toll-free" : ""}
              </span>
              <span className={`dialer-tag ${live ? "ok" : ""}`}>
                {live ? "answers with AI" : number.status}
              </span>
            </li>
          );
        })}
      </ul>

      {inbound?.dispatchRules?.length > 0 && (
        <details className="dialer-details">
          <summary>Dispatch rules</summary>
          <ul>
            {inbound.dispatchRules.map((rule) => (
              <li key={rule.id}>
                <code>{rule.id}</code> {rule.name} →{" "}
                {rule.agents.join(", ") || "no agent"}
                {rule.inboundNumbers?.length
                  ? ` · ${rule.inboundNumbers.join(", ")}`
                  : " · all numbers"}
              </li>
            ))}
          </ul>
        </details>
      )}

      {inbound?.error && <div className="dialer-alert warn">{inbound.error}</div>}
    </section>
  );
}

// ──────────────────────────────────────────────────────────────
// Outbound — needs a carrier trunk, which LiveKit numbers are not
// ──────────────────────────────────────────────────────────────
function OutboundPanel({
  numbers,
  callerId,
  setCallerId,
  destination,
  setDestination,
  calleeName,
  setCalleeName,
  dialing,
  canDial,
  onDial,
}) {
  if (!canDial) {
    return (
      <section className="dialer-panel card">
        <div className="dialer-panel-head">
          <h2>Place a call</h2>
          <span className="dialer-badge warn">Unavailable</span>
        </div>
        <p className="dialer-hint">
          LiveKit Phone Numbers supports inbound calls only, so there is no
          route for the agent to dial out. Outbound needs a LiveKit{" "}
          <em>outbound trunk</em> pointed at a carrier — once you have one, set{" "}
          <code>LIVEKIT_SIP_OUTBOUND_TRUNK_ID</code> in <code>.env.local</code>{" "}
          and this form starts working. Everything else — the agent, its
          knowledge base, the guardrails, the call monitor — is already in
          place.
        </p>
        <a
          className="btn dialer-browser-btn"
          href="https://docs.livekit.io/telephony/making-calls/outbound-trunk"
          target="_blank"
          rel="noreferrer"
        >
          How to create an outbound trunk
        </a>
      </section>
    );
  }

  return (
    <form className="dialer-panel card" onSubmit={onDial}>
      <div className="dialer-panel-head">
        <h2>Place a call</h2>
        <span className="dialer-hint">
          The agent joins first, then dials — the callee never hears silence.
        </span>
      </div>

      <label className="dialer-field">
        <span className="dialer-label">Call from</span>
        <input
          type="tel"
          inputMode="tel"
          placeholder="+12402124128"
          value={callerId}
          onChange={(event) => setCallerId(event.target.value)}
          list="caller-ids"
          autoComplete="off"
        />
        <datalist id="caller-ids">
          {numbers.map((number) => (
            <option key={number.id || number.number} value={number.number}>
              {number.label}
            </option>
          ))}
        </datalist>
        <span className="dialer-hint">
          Must be a number your outbound trunk is allowed to present.
        </span>
      </label>

      <label className="dialer-field">
        <span className="dialer-label">Call to</span>
        <input
          type="tel"
          inputMode="tel"
          placeholder="+971501234567"
          value={destination}
          onChange={(event) => setDestination(event.target.value)}
          autoComplete="off"
          required
        />
        <span className="dialer-hint">
          International format, starting with the country code.
        </span>
      </label>

      <label className="dialer-field">
        <span className="dialer-label">
          Who are you calling <em>(optional)</em>
        </span>
        <input
          type="text"
          placeholder="Aisha"
          value={calleeName}
          onChange={(event) => setCalleeName(event.target.value)}
          autoComplete="off"
        />
        <span className="dialer-hint">Passed to the agent for its opening line.</span>
      </label>

      <button
        type="submit"
        className="btn dialer-call-btn"
        disabled={dialing || !destination}
      >
        {dialing ? "Dialling…" : "Call"}
      </button>
    </form>
  );
}

// ──────────────────────────────────────────────────────────────
// Live calls
// ──────────────────────────────────────────────────────────────
function LiveCalls({ calls, onListen, onHangup }) {
  return (
    <section className="dialer-panel card">
      <div className="dialer-panel-head">
        <h2>Live calls</h2>
        <span className="dialer-hint">{calls.length || "none"}</span>
      </div>

      {!calls.length && (
        <p className="dialer-empty">
          Nothing in progress. Call your number and it appears here — listen in
          or end it from this list.
        </p>
      )}

      <ul className="dialer-calls">
        {calls.map((call) => (
          <li key={call.room}>
            <span
              className={`status-dot ${
                call.status === "active" ? "connected" : "connecting"
              }`}
            />
            <span className="dialer-call-dir">{call.direction}</span>
            <span className="dialer-number">
              {call.direction === "inbound"
                ? formatNumber(call.fromNumber)
                : formatNumber(call.toNumber)}
            </span>
            <span className="dialer-call-meta">
              {call.status}
              {call.startedAt ? ` · ${elapsed(call.startedAt)}` : ""}
            </span>
            <button className="btn btn-sm" onClick={() => onListen(call)}>
              Listen
            </button>
            <button className="btn btn-sm btn-end" onClick={() => onHangup(call)}>
              End
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
