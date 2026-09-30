import React, { useEffect, useRef } from "react";
import "./TranscriptFeed.css";

/**
 * TranscriptFeed — scrolling live transcript display.
 * Props:
 *   segments – array of { id, role: "user"|"agent", text, isFinal }
 */
export default function TranscriptFeed({ segments = [] }) {
  const bottomRef = useRef(null);

  // Auto-scroll to bottom on new segments
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [segments]);

  if (segments.length === 0) {
    return (
      <div className="transcript-feed transcript-feed--empty">
        <p>No conversation yet</p>
        <span className="transcript-empty-hint">Speak to start a session</span>
      </div>
    );
  }


  return (
    <div className="transcript-feed" role="log" aria-live="polite" aria-label="Conversation transcript">
      {segments.map((seg) => (
        <div
          key={seg.id}
          className={`transcript-bubble transcript-bubble--${seg.role} ${seg.isFinal ? "" : "transcript-bubble--streaming"}`}
        >
          <span className="transcript-role">
            {seg.role === "user" ? "You" : "Agent"}
          </span>
          <p className="transcript-text">{seg.text}</p>
          {!seg.isFinal && <span className="typing-cursor" aria-hidden="true" />}
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
