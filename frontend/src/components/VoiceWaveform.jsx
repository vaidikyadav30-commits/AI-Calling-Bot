import React, { useRef, useEffect } from "react";

/**
 * VoiceWaveform — minimal monochrome canvas bar visualizer.
 * Props:
 *   audioLevel  (0–1)   actual volume from AudioContext
 *   isActive    (bool)  whether to animate
 *   barColor    (string) active bar color  e.g. "#f0ede8"
 *   dimColor    (string) inactive bar color e.g. "#2a2826"
 *   barCount    (number) default 28
 */
export default function VoiceWaveform({
  audioLevel = 0,
  isActive   = false,
  barColor   = "#f0ede8",
  dimColor   = "#2a2826",
  barCount   = 28,
}) {
  const canvasRef = useRef(null);
  const barsRef   = useRef(new Float32Array(barCount).fill(0.04));
  const rafRef    = useRef(null);
  const propsRef  = useRef({ audioLevel, isActive, barColor, dimColor });

  // keep latest props accessible in the RAF loop without re-subscribing
  useEffect(() => {
    propsRef.current = { audioLevel, isActive, barColor, dimColor };
  });

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    const draw = (ts) => {
      const { audioLevel, isActive, barColor, dimColor } = propsRef.current;
      const bars  = barsRef.current;
      const W     = canvas.width;
      const H     = canvas.height;
      const gap   = 2;
      const barW  = Math.max(1.5, (W - gap * (barCount - 1)) / barCount);
      const step  = barW + gap;

      ctx.clearRect(0, 0, W, H);

      for (let i = 0; i < barCount; i++) {
        // Natural, centre-biased noise
        const phase  = (ts * 0.0025 + i * 0.55);
        const noise  = (Math.sin(phase) * 0.5 + 0.5)
                     * (Math.sin(phase * 1.7 + 1) * 0.5 + 0.5);

        const target = isActive
          ? Math.max(0.04, audioLevel * noise + noise * 0.18)
          : 0.04 + Math.sin(ts * 0.001 + i) * 0.012 + 0.008;

        bars[i] += (target - bars[i]) * 0.18;

        const barH  = bars[i] * H * 0.85;
        const x     = i * step;
        const y     = (H - barH) / 2;
        const alpha = isActive ? 0.5 + bars[i] * 1.5 : 0.3;

        ctx.globalAlpha = Math.min(1, alpha);
        ctx.fillStyle   = isActive ? barColor : dimColor;

        const r = Math.min(barW / 2, 2);
        ctx.beginPath();
        ctx.roundRect(x, y, barW, barH, r);
        ctx.fill();
      }
      ctx.globalAlpha = 1;

      rafRef.current = requestAnimationFrame(draw);
    };

    rafRef.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(rafRef.current);
  }, [barCount]); // only re-mount if barCount changes

  return (
    <canvas
      ref={canvasRef}
      width={236}
      height={40}
      style={{ display: "block", width: "100%", height: "40px" }}
      aria-hidden="true"
    />
  );
}
