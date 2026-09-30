import { useState, useCallback, useEffect } from "react";
import {
  RoomContext,
  useVoiceAssistant,
  BarVisualizer,
  useConnectionState,
  useRoomInfo,
  useLocalParticipant,
  useTrackVolume,
  useAudioPlayback,
  RoomAudioRenderer,
} from "@livekit/components-react";
import "@livekit/components-styles";
import { ConnectionState, Room, RoomEvent } from "livekit-client";
import { fetchConnectionDetails } from "./lib/connectionDetails";
import { hangupCall } from "./lib/telephony";
import { useTranscript } from "./hooks/useTranscript";
import VoiceWaveform from "./components/VoiceWaveform";
import TranscriptFeed from "./components/TranscriptFeed";
import Dialer from "./components/Dialer";
import "./App.css";

// ──────────────────────────────────────────────────────────────
// AgentRoom — rendered inside RoomContext
//
// Serves two situations: a browser conversation, where you hold the
// microphone, and monitoring a phone call, where you hold nothing. The
// monitor token is subscribe-only, so the mic controls are not merely
// hidden — there is nothing they could publish.
// ──────────────────────────────────────────────────────────────
function AgentRoom({ room, call, onLeave, onEndCall }) {
  const monitor = Boolean(call);
  const { state: agentState, audioTrack } = useVoiceAssistant();

  const connectionState = useConnectionState();
  const { name: roomName } = useRoomInfo();
  const { localParticipant, microphoneTrack, isMicrophoneEnabled } = useLocalParticipant();

  // Mic level straight off the published LiveKit track — no second
  // getUserMedia stream competing for the microphone.
  const micVolume = useTrackVolume(microphoneTrack?.track);

  // Browsers block audio playback until the user interacts with the page.
  const { canPlayAudio, startAudio } = useAudioPlayback(room);

  const muted = !isMicrophoneEnabled;
  const toggleMic = useCallback(async () => {
    if (!localParticipant) return;
    await localParticipant.setMicrophoneEnabled(muted);
  }, [localParticipant, muted]);

  // Both sides of the conversation
  const segments = useTranscript();

  const isSpeaking = agentState === "speaking";

  // Derive readable state
  const stateLabel =
    connectionState === ConnectionState.Connecting ? "connecting" :
    connectionState === ConnectionState.Reconnecting ? "reconnecting" :
    agentState === "thinking"  ? "thinking" :
    agentState === "speaking"  ? "speaking" :
    agentState === "listening" ? "listening" : "idle";

  const csKey =
    connectionState === ConnectionState.Connected    ? "connected" :
    connectionState === ConnectionState.Connecting   ? "connecting" :
    connectionState === ConnectionState.Reconnecting ? "reconnecting" : "disconnected";

  const farEnd = call?.direction === "inbound" ? call?.fromNumber : call?.toNumber;

  return (
    <div className="room-layout">
      {/* ── Header ── */}
      <header className="room-header">
        <div className="room-logo">
          <div className="logo-mark">
            <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
              <circle cx="6" cy="6" r="4" fill="#0c0c0b"/>
            </svg>
          </div>
          <span className="logo-text">AI Caller</span>
          <div className="logo-divider" />
          <span className="logo-sub">{monitor ? "Phone call" : "Voice Agent"}</span>
        </div>

        {/* Connection status */}
        <div className="status-dot-wrap" aria-label={`Status: ${csKey}`}>
          <span className={`status-dot ${csKey}`} />
          <span>{monitor ? farEnd || roomName : roomName || "connecting…"}</span>
        </div>

        {monitor && (
          <button className="btn" onClick={onLeave} title="Leave the call running">
            Stop listening
          </button>
        )}

        {/* End button */}
        <button
          id="btn-disconnect"
          className="btn btn-end"
          onClick={onEndCall}
          title={monitor ? "Hang up on both sides" : "End session and reconnect"}
        >
          End call
        </button>
      </header>

      {/* ── Main ── */}
      <main className="room-main">
        {/* Left — agent orb + waveforms */}
        <div className="left-panel">
          {/* Agent orb */}
          <div className="agent-orb-section">
            <div className={`agent-orb ${stateLabel}`} aria-hidden="true">
              <div className="orb-inner" />
            </div>
            <div className="agent-label">
              <div className="agent-name">Voice Agent</div>
              <div className="agent-state">{stateLabel}</div>
            </div>
          </div>

          {monitor && (
            <div className="call-summary">
              <div className="call-summary-row">
                <span>Direction</span>
                <span>{call.direction}</span>
              </div>
              <div className="call-summary-row">
                <span>{call.direction === "inbound" ? "From" : "To"}</span>
                <span>{farEnd || "—"}</span>
              </div>
              <div className="call-summary-row">
                <span>{call.direction === "inbound" ? "On" : "Via"}</span>
                <span>
                  {(call.direction === "inbound" ? call.toNumber : call.fromNumber) || "—"}
                </span>
              </div>
              <div className="call-summary-note">
                Listening only — the caller cannot hear you.
              </div>
            </div>
          )}

          {/* Waveforms */}
          <div className="waveform-section">
            {/* Agent waveform */}
            <div className="waveform-row">
              <div className="waveform-row-label">
                <span>Agent</span>
                {isSpeaking && <span className="active-badge" />}
              </div>
              {audioTrack ? (
                <BarVisualizer
                  track={audioTrack}
                  state={agentState}
                  barCount={28}
                  style={{
                    height: "40px",
                    width: "100%",
                    "--lk-fg": "#c8b89a",
                    "--lk-bg": "var(--bg-overlay)",
                  }}
                />
              ) : (
                <VoiceWaveform
                  audioLevel={0}
                  isActive={false}
                  barColor="#c8b89a"
                  dimColor="#2a2826"
                />
              )}
            </div>

            {/* User waveform — real mic volume, or the caller on a phone call */}
            {!monitor && (
              <div className="waveform-row">
                <div className="waveform-row-label">
                  <span>You</span>
                  {micVolume > 0.05 && !muted && <span className="active-badge" />}
                </div>
                <VoiceWaveform
                  audioLevel={muted ? 0 : micVolume}
                  isActive={!muted && micVolume > 0.02}
                  barColor="#f0ede8"
                  dimColor="#2a2826"
                />
              </div>
            )}
          </div>

          {/* Controls */}
          <div className="controls-section">
            {!monitor && (
              <button
                id="btn-mic-toggle"
                className={`btn mic-btn ${muted ? "muted" : ""}`}
                onClick={toggleMic}
                aria-pressed={muted}
                title={muted ? "Unmute microphone" : "Mute microphone"}
              >
                {/* Mic SVG icon */}
                <svg className="mic-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                  {muted ? (
                    <>
                      <path d="M10 8V5a2 2 0 0 0-4 0v3" strokeLinecap="round"/>
                      <path d="M1 1l14 14" strokeLinecap="round"/>
                      <path d="M5.5 12.5A5 5 0 0 0 13 8" strokeLinecap="round"/>
                      <line x1="8" y1="15" x2="8" y2="12.5" strokeLinecap="round"/>
                      <line x1="5.5" y1="15" x2="10.5" y2="15" strokeLinecap="round"/>
                    </>
                  ) : (
                    <>
                      <rect x="6" y="1" width="4" height="9" rx="2"/>
                      <path d="M3 8a5 5 0 0 0 10 0" strokeLinecap="round"/>
                      <line x1="8" y1="15" x2="8" y2="13" strokeLinecap="round"/>
                      <line x1="5.5" y1="15" x2="10.5" y2="15" strokeLinecap="round"/>
                    </>
                  )}
                </svg>
                {muted ? "Unmute" : "Mute"}
              </button>
            )}

            {!canPlayAudio && (
              <button className="btn" onClick={() => startAudio()}>
                Enable audio
              </button>
            )}
          </div>
        </div>

        {/* Right — transcript */}
        <div className="right-panel">
          <div className="transcript-header">
            <span className="transcript-title">Transcript</span>
            <span className="transcript-count">{segments.length} messages</span>
          </div>
          <TranscriptFeed segments={segments} />
        </div>
      </main>

      {/* Play agent audio */}
      <RoomAudioRenderer />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────
// SessionView — owns one LiveKit Room for the life of a session
// ──────────────────────────────────────────────────────────────
function SessionView({ session, onLeave }) {
  // The Room is built inside the effect and published here once it is
  // connected. It cannot be created once and reused: `disconnect()` closes the
  // peer-connection engine for good, so reconnecting the same instance fails
  // with "PC manager is closed". React StrictMode makes that certain rather
  // than occasional — it mounts, unmounts (which disconnects), then remounts.
  const [room, setRoom] = useState(null);
  const [error, setError] = useState(null);

  // Connecting and disconnecting belong in the *same* effect, so every attempt
  // owns the Room it tears down. The `cancelled` flag distinguishes "our own
  // teardown aborted this connect" — which is normal and silent — from a real
  // connection failure worth showing.
  useEffect(() => {
    const attempt = new Room();
    let cancelled = false;

    const onDisconnected = () => {
      if (cancelled) return;
      setError(
        session.call
          ? "The call ended."
          : "Call ended. Reconnect to start a new session.",
      );
    };
    attempt.on(RoomEvent.Disconnected, onDisconnected);

    // On a browser session, enable the mic and connect concurrently.
    // preConnectBuffer starts capturing immediately, so anything said while
    // the room is still connecting is buffered and delivered to the agent
    // rather than lost. A monitor publishes nothing, so it only connects.
    const tasks = [attempt.connect(session.serverUrl, session.token)];
    if (!session.call) {
      tasks.push(
        attempt.localParticipant.setMicrophoneEnabled(true, undefined, {
          preConnectBuffer: true,
        }),
      );
    }

    Promise.all(tasks)
      .then(() => {
        if (cancelled) return;
        setError(null);
        setRoom(attempt);
      })
      .catch((err) => {
        if (cancelled) return;
        console.error("Connection error:", err);
        setError(err?.message ?? "Could not connect.");
      });

    return () => {
      cancelled = true;
      attempt.off(RoomEvent.Disconnected, onDisconnected);
      attempt.disconnect();
      setRoom((current) => (current === attempt ? null : current));
    };
  }, [session]);

  const leave = useCallback(async () => {
    await room?.disconnect();
    onLeave();
  }, [room, onLeave]);

  const endCall = useCallback(async () => {
    // On a phone call, leaving the room is not enough: the caller would still
    // be connected to the agent. Deleting the room is what hangs up.
    if (session.call?.room) {
      try {
        await hangupCall(session.call.room);
      } catch (err) {
        console.error("Hangup failed:", err);
      }
    }
    await room?.disconnect();
    onLeave();
  }, [room, session.call, onLeave]);

  if (error) {
    return (
      <div className="connecting-overlay">
        <span style={{ color: "#e57373", fontSize: 13 }}>⚠ {error}</span>
        <button className="btn" onClick={onLeave}>Back to dialer</button>
      </div>
    );
  }

  // `room` is only set once the connection succeeded, so its absence is the
  // connecting state — no separate flag to keep in step with it.
  if (!room) {
    return (
      <div className="connecting-overlay">
        <div className="connecting-spinner" />
        <span className="connecting-text">
          {session.call ? "Joining the call…" : "Connecting…"}
        </span>
      </div>
    );
  }

  return (
    <RoomContext.Provider value={room}>
      <AgentRoom
        room={room}
        call={session.call}
        onLeave={leave}
        onEndCall={endCall}
      />
    </RoomContext.Provider>
  );
}

// ──────────────────────────────────────────────────────────────
// Root App — the dialer is home; a session takes over when there is one
// ──────────────────────────────────────────────────────────────
export default function App() {
  const [session, setSession] = useState(null);
  const [error, setError] = useState(null);

  // A phone call: join its room read-only with the token the control plane
  // issued when the call was placed (or re-issued for monitoring).
  const joinCall = useCallback((call) => {
    if (!call?.viewerToken || !call?.serverUrl) {
      setError("The call started but no viewer token was returned.");
      return;
    }
    setSession({
      serverUrl: call.serverUrl,
      token: call.viewerToken,
      call,
    });
  }, []);

  // A browser conversation with the same agent, for testing without a phone.
  const talkInBrowser = useCallback(async () => {
    setError(null);
    try {
      const details = await fetchConnectionDetails();
      setSession({
        serverUrl: details.serverUrl,
        token: details.participantToken,
        call: null,
      });
    } catch (err) {
      setError(err?.message ?? "Could not start a browser session.");
    }
  }, []);

  const leave = useCallback(() => setSession(null), []);

  if (session) {
    // Keyed so joining a different call tears the old Room down and builds a
    // new one, rather than reusing a connection bound to the previous room.
    return (
      <SessionView
        key={session.call?.room ?? session.token}
        session={session}
        onLeave={leave}
      />
    );
  }

  return (
    <>
      {error && (
        <div className="app-error" role="alert">
          ⚠ {error}
        </div>
      )}
      <Dialer onJoinCall={joinCall} onTalkInBrowser={talkInBrowser} />
    </>
  );
}
