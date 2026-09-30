import { useMemo } from "react";
import { useLocalParticipant, useTranscriptions } from "@livekit/components-react";

/**
 * Both sides of the conversation, oldest first.
 *
 * `useVoiceAssistant()` only exposes the agent's transcriptions — it has no
 * user-side equivalent. `useTranscriptions()` is the official replacement for
 * the deprecated per-track hooks: it reads the `lk.transcription` text-stream
 * topic, which carries transcripts for every participant, and already collapses
 * each segment's interim and final streams into a single entry keyed by
 * `lk.segment_id`.
 *
 * @returns {{id: string, role: "user"|"agent", text: string, isFinal: boolean, ts: number}[]}
 */
export function useTranscript() {
  const transcriptions = useTranscriptions();
  const { localParticipant } = useLocalParticipant();
  const localIdentity = localParticipant?.identity;

  return useMemo(
    () =>
      transcriptions
        .map(({ text, participantInfo, streamInfo }) => {
          const attributes = streamInfo?.attributes ?? {};
          return {
            id: attributes["lk.segment_id"] ?? streamInfo?.id,
            role: participantInfo?.identity === localIdentity ? "user" : "agent",
            text,
            isFinal: attributes["lk.transcription_final"] === "true",
            ts: Number(streamInfo?.timestamp ?? 0),
          };
        })
        .filter((segment) => segment.text?.trim())
        .sort((a, b) => a.ts - b.ts),
    [transcriptions, localIdentity],
  );
}
