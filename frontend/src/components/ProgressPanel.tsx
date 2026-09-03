"use client";

type Stage = {
  id: string;
  label: string;
};

const STAGES: Stage[] = [
  { id: "loading_component_data", label: "Loading component data" },
  { id: "compatibility_filtering", label: "Compatibility filtering" },
  { id: "guide_search", label: "Propulsion guide search" },
  { id: "cached_explore", label: "Cached Explore" },
  { id: "frame_sizing", label: "Frame sizing" },
  { id: "exact_validation", label: "Exact validation" },
  { id: "decision_ranking", label: "Decision ranking" },
  { id: "complete", label: "Complete" },
];

function stageIndex(stage?: string | null) {
  const i = STAGES.findIndex((s) => s.id === stage);
  return i < 0 ? 0 : i;
}

export function ProgressPanel({
  stage,
  progress,
  message,
  elapsed,
  eta,
}: {
  stage?: string | null;
  progress: number;
  message?: string | null;
  elapsed?: number | null;
  eta?: number | null;
}) {
  const idx = stageIndex(stage);
  const pct = Math.round(Math.max(0, Math.min(progress, 1)) * 100);

  return (
    <div className="panel rise p-6 md:p-8">
      <div className="mb-2 text-sm uppercase tracking-[0.18em] text-[var(--muted)]">
        Searching viable combinations
      </div>
      <h2 className="brand mb-6 text-3xl">{message || "Working…"}</h2>
      <div className="mb-6 h-2 overflow-hidden rounded bg-white/10">
        <div
          className="h-full bg-[var(--accent-2)] transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
      <ul className="space-y-3">
        {STAGES.map((s, i) => {
          const done = i < idx || stage === "complete";
          const active = i === idx && stage !== "complete";
          return (
            <li key={s.id} className="flex items-center gap-3 text-sm md:text-base">
              <span
                className={
                  done
                    ? "text-[var(--ok)]"
                    : active
                      ? "pulse-line text-[var(--accent)]"
                      : "text-[var(--muted)]"
                }
              >
                {done ? "✓" : active ? "●" : "○"}
              </span>
              <span className={active ? "text-[var(--ink)]" : "text-[var(--muted)]"}>
                {s.label}
                {active && message && s.id === "exact_validation" ? ` — ${message}` : ""}
              </span>
            </li>
          );
        })}
      </ul>
      <div className="mt-6 flex flex-wrap gap-6 text-sm text-[var(--muted)]">
        <span>Progress {pct}%</span>
        {elapsed != null && <span>Elapsed {Math.round(elapsed)}s</span>}
        {eta != null && <span>Estimated remaining {Math.round(eta)}s</span>}
      </div>
    </div>
  );
}
