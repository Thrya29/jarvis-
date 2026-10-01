// The animated core: shows at a glance whether JARVIS is idle, listening or working.

export type CoreState = "offline" | "idle" | "listening" | "thinking" | "replying";

const LABELS: Record<CoreState, string> = {
  offline: "Offline",
  idle: "Standing by",
  listening: "Listening",
  thinking: "Working",
  replying: "Replying",
};

export function coreState(connected: boolean, busy: boolean, streaming: boolean, voice: string): CoreState {
  if (!connected) return "offline";
  if (streaming) return "replying";
  if (busy) return "thinking";
  if (voice === "listening") return "listening";
  return "idle";
}

export function Reactor({ state, size = 120 }: { state: CoreState; size?: number }) {
  const color = state === "offline" ? "#ff3b5c" : state === "idle" ? "#0b8aa3" : "#00e5ff";
  const fast = state === "thinking" || state === "replying";
  const ticks = Array.from({ length: 36 }, (_, i) => i);
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 120 120"
      role="img"
      aria-label={`JARVIS: ${LABELS[state]}`}
      style={{ filter: `drop-shadow(0 0 ${fast ? 14 : 8}px ${color})` }}
    >
      <g className={`reactor-ring ${fast ? "spin-fast" : "spin-slow"}`}>
        {ticks.map((i) => (
          <line
            key={i}
            x1="60"
            y1="4"
            x2="60"
            y2={i % 3 === 0 ? 12 : 8}
            stroke={color}
            strokeWidth={i % 3 === 0 ? 2 : 1}
            opacity={0.7}
            transform={`rotate(${i * 10} 60 60)`}
          />
        ))}
      </g>
      <g className={`reactor-ring ${fast ? "spin-rev-fast" : "spin-rev"}`}>
        <circle
          cx="60"
          cy="60"
          r="40"
          fill="none"
          stroke={color}
          strokeWidth="3"
          strokeDasharray="48 14 8 14"
          opacity="0.85"
        />
      </g>
      <g className={`reactor-ring ${fast ? "spin-med" : "spin-slow"}`}>
        <circle cx="60" cy="60" r="30" fill="none" stroke={color} strokeWidth="1.5" strokeDasharray="2 6" />
      </g>
      <g className={`reactor-ring ${state === "listening" || fast ? "pulse" : ""}`}>
        <circle cx="60" cy="60" r="18" fill={color} opacity="0.18" />
        <circle cx="60" cy="60" r="11" fill={color} opacity={state === "offline" ? 0.5 : 0.95} />
      </g>
    </svg>
  );
}

export const coreLabel = (s: CoreState) => LABELS[s];
