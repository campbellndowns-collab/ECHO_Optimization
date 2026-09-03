"use client";

import { FormEvent, useEffect, useMemo, useState, useTransition } from "react";
import { SearchableSelect } from "@/components/SearchableSelect";
import { ProgressPanel } from "@/components/ProgressPanel";
import { TradePlots } from "@/components/TradePlots";
import {
  EvaluateResponse,
  JobResults,
  JobStatus,
  client,
} from "@/lib/api";

type Mode = "optimize" | "lock";
type BatteryMode = "optimize" | "cell";

type Weights = {
  weight_cost: number;
  weight_endurance: number;
  weight_aircraft_mass: number;
  weight_quality: number;
  weight_complexity: number;
};

const defaultWeights: Weights = {
  weight_cost: 35,
  weight_endurance: 25,
  weight_aircraft_mass: 17,
  weight_quality: 13,
  weight_complexity: 10,
};

function normalize(w: Weights) {
  const sum =
    w.weight_cost +
    w.weight_endurance +
    w.weight_aircraft_mass +
    w.weight_quality +
    w.weight_complexity;
  const s = sum > 0 ? sum : 1;
  return {
    cost: (100 * w.weight_cost) / s,
    endurance: (100 * w.weight_endurance) / s,
    mass: (100 * w.weight_aircraft_mass) / s,
    quality: (100 * w.weight_quality) / s,
    buildability: (100 * w.weight_complexity) / s,
  };
}

function fmt(n?: number | null, digits = 2) {
  if (n == null || Number.isNaN(n)) return "—";
  return Number(n).toFixed(digits);
}

export default function HomePage() {
  const [motorMode, setMotorMode] = useState<Mode>("lock");
  const [propMode, setPropMode] = useState<Mode>("lock");
  const [batteryMode, setBatteryMode] = useState<BatteryMode>("cell");
  const [escMode, setEscMode] = useState<Mode>("lock");

  const [motorId, setMotorId] = useState("NeuMotors_4610MC-207");
  const [propId, setPropId] = useState("APC_19x10E");
  const [cellId, setCellId] = useState("fcde6dc9431046231f6f");
  const [topology, setTopology] = useState("6S4P");
  const [escId, setEscId] = useState("4420b0e1b6c93b81ca45");

  const [fixedMass, setFixedMass] = useState(0.5);
  const [maxAuw, setMaxAuw] = useState(10);
  const [minTwr, setMinTwr] = useState(1.8);
  const [packOverhead, setPackOverhead] = useState(0.1);
  const [escMargin, setEscMargin] = useState(1.15);
  const [clearance, setClearance] = useState(50);
  const [margin, setMargin] = useState(1.25);
  const [weights, setWeights] = useState<Weights>(defaultWeights);

  const [view, setView] = useState<"configure" | "progress" | "results">("configure");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [fixedResult, setFixedResult] = useState<EvaluateResponse | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [jobResults, setJobResults] = useState<JobResults | null>(null);
  const [selectedIdx, setSelectedIdx] = useState(0);
  const [pending, startTransition] = useTransition();

  const norm = useMemo(() => normalize(weights), [weights]);
  const allLocked =
    motorMode === "lock" &&
    propMode === "lock" &&
    batteryMode === "cell" &&
    escMode === "lock";

  useEffect(() => {
    if (!jobId || view !== "progress") return;
    let stop = false;
    const tick = async () => {
      try {
        const s = await client.jobStatus(jobId);
        if (stop) return;
        setJobStatus(s);
        if (s.status === "complete") {
          const r = await client.jobResults(jobId);
          if (stop) return;
          setJobResults(r);
          setView("results");
          setBusy(false);
        } else if (s.status === "failed") {
          setError(s.error || "Optimization failed");
          setBusy(false);
          setView("configure");
        }
      } catch (e) {
        if (!stop) setError(e instanceof Error ? e.message : String(e));
      }
    };
    tick();
    const id = setInterval(tick, 1500);
    return () => {
      stop = true;
      clearInterval(id);
    };
  }, [jobId, view]);

  async function onEvaluateFixed(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    setFixedResult(null);
    setJobResults(null);
    try {
      const res = await client.evaluate({
        motor_id: motorId,
        propeller_id: propId,
        battery_cell_id: cellId,
        battery_topology: topology,
        esc_id: escId,
        fixed_mass_kg: fixedMass,
        max_auw_kg: maxAuw,
        min_twr: minTwr,
        pack_overhead_fraction: packOverhead,
        esc_current_margin: escMargin,
        frame_prop_clearance_mm: clearance,
        frame_structural_margin: margin,
      });
      setFixedResult(res);
      setView("results");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function onOptimize(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    setFixedResult(null);
    setJobResults(null);
    try {
      const body: Record<string, unknown> = {
        motor_mode: motorMode,
        prop_mode: propMode,
        battery_mode: batteryMode === "cell" ? "cell" : "optimize",
        esc_mode: escMode,
        fixed_mass_kg: fixedMass,
        max_auw_kg: maxAuw,
        min_twr: minTwr,
        pack_overhead_fraction: packOverhead,
        esc_current_margin: escMargin,
        frame_prop_clearance_mm: clearance,
        frame_structural_margin: margin,
        search_depth: "standard",
        ...weights,
      };
      if (motorMode === "lock") body.motor_id = motorId;
      if (propMode === "lock") body.prop_id = propId;
      if (batteryMode === "cell") body.battery_id = cellId;
      if (escMode === "lock") body.esc_id = escId;
      const { job_id } = await client.createJob(body);
      setJobId(job_id);
      setView("progress");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const tableRows = jobResults?.top_designs || [];
  const pool = jobResults?.pool || [];
  const selected = tableRows[selectedIdx] || tableRows[0];

  async function onRerank(next: Weights) {
    if (!pool.length) {
      setWeights(next);
      return;
    }
    setWeights(next);
    startTransition(async () => {
      const ranked = await client.rank(pool, next);
      setJobResults((prev) =>
        prev
          ? {
              ...prev,
              pool: ranked.designs,
              top_designs: ranked.designs.slice(0, 25),
            }
          : prev,
      );
      setSelectedIdx(0);
    });
  }

  return (
    <main className="relative mx-auto min-h-screen w-full max-w-6xl px-4 pb-20 pt-8 md:px-8">
      <header className="rise mb-10 flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="mb-2 text-xs uppercase tracking-[0.22em] text-[var(--accent-2)]">
            Multidisciplinary quadrotor design
          </div>
          <h1 className="brand text-4xl md:text-6xl">Drone Optimizer</h1>
          <p className="mt-3 max-w-2xl text-lg text-[var(--muted)]">
            Lock what you know. Optimize what you don&apos;t.
          </p>
        </div>
        <nav className="flex gap-2 text-sm">
          {(["configure", "progress", "results"] as const).map((v) => (
            <button
              key={v}
              type="button"
              className={`btn ${view === v ? "btn-primary" : "btn-ghost"}`}
              onClick={() => setView(v)}
            >
              {v}
            </button>
          ))}
        </nav>
      </header>

      {view === "configure" && (
        <section className="rise grid gap-8 lg:grid-cols-[1.15fr_0.85fr]">
          <div className="relative min-h-[28rem] overflow-hidden panel">
            <div
              className="absolute inset-0"
              style={{
                background:
                  "radial-gradient(circle at 30% 40%, rgba(58,166,160,0.25), transparent 45%), radial-gradient(circle at 70% 60%, rgba(212,160,23,0.2), transparent 40%), linear-gradient(135deg, #102033, #0b1520)",
              }}
            />
            <svg
              className="absolute inset-0 h-full w-full opacity-70"
              viewBox="0 0 800 520"
              aria-hidden
            >
              <g stroke="rgba(232,238,246,0.35)" fill="none" strokeWidth="1.5">
                <circle cx="400" cy="260" r="34" />
                <line x1="400" y1="260" x2="210" y2="120" />
                <line x1="400" y1="260" x2="590" y2="120" />
                <line x1="400" y1="260" x2="210" y2="400" />
                <line x1="400" y1="260" x2="590" y2="400" />
                <circle cx="210" cy="120" r="48" />
                <circle cx="590" cy="120" r="48" />
                <circle cx="210" cy="400" r="48" />
                <circle cx="590" cy="400" r="48" />
              </g>
            </svg>
            <div className="relative z-10 flex h-full flex-col justify-end p-8">
              <p className="max-w-md text-[var(--muted)]">
                Component data → propulsion modeling → battery sizing → structural
                frame sizing → constraints → multidisciplinary search → decision
                analysis.
              </p>
            </div>
          </div>

          <form className="panel space-y-5 p-6" onSubmit={allLocked ? onEvaluateFixed : onOptimize}>
            <ComponentBlock
              title="Motor"
              mode={motorMode}
              setMode={setMotorMode}
              select={
                <SearchableSelect
                  kind="motors"
                  value={motorId}
                  onChange={setMotorId}
                  disabled={motorMode !== "lock"}
                />
              }
            />
            <ComponentBlock
              title="Propeller"
              mode={propMode}
              setMode={setPropMode}
              select={
                <SearchableSelect
                  kind="propellers"
                  value={propId}
                  onChange={setPropId}
                  disabled={propMode !== "lock"}
                />
              }
            />
            <div>
              <div className="mb-2 flex items-center justify-between gap-3">
                <span className="label mb-0">Battery</span>
                <select
                  className="field w-auto"
                  value={batteryMode}
                  onChange={(e) => setBatteryMode(e.target.value as BatteryMode)}
                >
                  <option value="optimize">Optimize</option>
                  <option value="cell">Lock cell, optimize S/P</option>
                </select>
              </div>
              {batteryMode === "cell" ? (
                <div className="grid gap-3">
                  <SearchableSelect kind="battery-cells" value={cellId} onChange={setCellId} />
                  <input
                    className="field"
                    value={topology}
                    onChange={(e) => setTopology(e.target.value)}
                    placeholder="Topology e.g. 6S4P (required for fixed evaluate)"
                  />
                </div>
              ) : (
                <div className="field opacity-50">Optimize pack candidates</div>
              )}
            </div>
            <ComponentBlock
              title="ESC"
              mode={escMode}
              setMode={setEscMode}
              select={
                <SearchableSelect
                  kind="escs"
                  value={escId}
                  onChange={setEscId}
                  disabled={escMode !== "lock"}
                />
              }
            />

            <div className="grid grid-cols-2 gap-3">
              <NumberField label="Fixed mass (kg)" value={fixedMass} setValue={setFixedMass} />
              <NumberField label="Max AUW (kg)" value={maxAuw} setValue={setMaxAuw} />
              <NumberField label="Min T/W" value={minTwr} setValue={setMinTwr} />
              <NumberField label="Pack overhead" value={packOverhead} setValue={setPackOverhead} step={0.01} />
              <NumberField label="ESC margin" value={escMargin} setValue={setEscMargin} step={0.01} />
              <NumberField label="Tip clearance (mm)" value={clearance} setValue={setClearance} />
              <NumberField label="Structural margin" value={margin} setValue={setMargin} step={0.01} />
            </div>

            <WeightEditor weights={weights} onChange={setWeights} norm={norm} />

            {error && (
              <div className="rounded border border-[var(--danger)]/40 bg-[var(--danger)]/10 p-3 text-sm">
                {error}
              </div>
            )}

            <div className="flex flex-wrap gap-3">
              <button className="btn btn-primary" type="submit" disabled={busy}>
                {allLocked ? "Evaluate fixed configuration" : "Start optimization job"}
              </button>
              <button
                className="btn btn-ghost"
                type="button"
                disabled={busy || !allLocked}
                onClick={onEvaluateFixed}
              >
                Force fixed evaluate
              </button>
            </div>
          </form>
        </section>
      )}

      {view === "progress" && (
        <ProgressPanel
          stage={jobStatus?.stage}
          progress={jobStatus?.progress || 0}
          message={jobStatus?.message}
          elapsed={jobStatus?.elapsed_seconds}
          eta={jobStatus?.estimated_remaining_seconds}
        />
      )}

      {view === "results" && fixedResult && (
        <FixedResults result={fixedResult} />
      )}

      {view === "results" && jobResults && (
        <div className="space-y-8">
          <WeightEditor
            weights={weights}
            onChange={onRerank}
            norm={norm}
            note={pending ? "Re-ranking exact pool…" : "Changing weights does not rerun PyThrust."}
          />
          {selected && <TopCard row={selected} />}
          <div className="panel overflow-x-auto">
            <table className="min-w-full text-left text-sm">
              <thead className="text-[var(--muted)]">
                <tr>
                  {[
                    "#",
                    "Score",
                    "Cost",
                    "Endurance",
                    "AUW",
                    "Quality",
                    "Build",
                    "Motor",
                    "Prop",
                    "Battery",
                    "ESC",
                  ].map((h) => (
                    <th key={h} className="px-3 py-3 font-medium">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {tableRows.map((row, i) => (
                  <tr
                    key={String(row.explore_candidate_id || i)}
                    className={`cursor-pointer border-t border-[var(--line)] hover:bg-white/5 ${
                      i === selectedIdx ? "bg-white/5" : ""
                    }`}
                    onClick={() => setSelectedIdx(i)}
                  >
                    <td className="px-3 py-2">{i + 1}</td>
                    <td className="px-3 py-2">{fmt(num(row.decision_matrix_score), 1)}</td>
                    <td className="px-3 py-2">${fmt(num(row.decision_total_estimated_cost_usd), 0)}</td>
                    <td className="px-3 py-2">{fmt(num(row.endurance_min), 1)} min</td>
                    <td className="px-3 py-2">{fmt(num(row.auw_kg), 3)} kg</td>
                    <td className="px-3 py-2">{fmt(num(row.decision_quality_score), 1)}</td>
                    <td className="px-3 py-2">{fmt(num(row.decision_complexity_score), 1)}</td>
                    <td className="px-3 py-2">{String(row.motor_model || row.motor_id)}</td>
                    <td className="px-3 py-2">{String(row.prop_model || row.prop_id)}</td>
                    <td className="px-3 py-2">
                      {String(row.battery_model || "")} {String(row.battery_topology || "")}
                    </td>
                    <td className="px-3 py-2">{String(row.esc_model || row.esc_id)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {selected && <DesignDetail row={selected} />}
          <TradePlots rows={pool.length ? pool : tableRows} />
        </div>
      )}
    </main>
  );
}

function num(v: unknown) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function ComponentBlock({
  title,
  mode,
  setMode,
  select,
}: {
  title: string;
  mode: Mode;
  setMode: (m: Mode) => void;
  select: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-3">
        <span className="label mb-0">{title}</span>
        <select
          className="field w-auto"
          value={mode}
          onChange={(e) => setMode(e.target.value as Mode)}
        >
          <option value="optimize">Optimize</option>
          <option value="lock">Lock specific</option>
        </select>
      </div>
      {select}
    </div>
  );
}

function NumberField({
  label,
  value,
  setValue,
  step = 0.1,
}: {
  label: string;
  value: number;
  setValue: (n: number) => void;
  step?: number;
}) {
  return (
    <label>
      <span className="label">{label}</span>
      <input
        className="field"
        type="number"
        step={step}
        value={value}
        onChange={(e) => setValue(Number(e.target.value))}
      />
    </label>
  );
}

function WeightEditor({
  weights,
  onChange,
  norm,
  note,
}: {
  weights: Weights;
  onChange: (w: Weights) => void;
  norm: ReturnType<typeof normalize>;
  note?: string;
}) {
  const fields: { key: keyof Weights; label: string; pct: number }[] = [
    { key: "weight_cost", label: "Cost", pct: norm.cost },
    { key: "weight_endurance", label: "Endurance", pct: norm.endurance },
    { key: "weight_aircraft_mass", label: "Aircraft weight", pct: norm.mass },
    { key: "weight_quality", label: "Design quality", pct: norm.quality },
    { key: "weight_complexity", label: "Buildability", pct: norm.buildability },
  ];
  return (
    <div className="rounded border border-[var(--line)] p-3">
      <div className="mb-2 text-sm text-[var(--muted)]">
        Decision priorities {note ? `— ${note}` : "(auto-normalized)"}
      </div>
      <div className="grid gap-2">
        {fields.map((f) => (
          <label key={f.key} className="grid grid-cols-[7rem_1fr_3.5rem] items-center gap-2 text-sm">
            <span>{f.label}</span>
            <input
              type="range"
              min={0}
              max={100}
              value={weights[f.key]}
              onChange={(e) =>
                onChange({ ...weights, [f.key]: Number(e.target.value) })
              }
            />
            <span className="text-right text-[var(--muted)]">{f.pct.toFixed(0)}%</span>
          </label>
        ))}
      </div>
    </div>
  );
}

function TopCard({ row }: { row: Record<string, unknown> }) {
  return (
    <div className="panel rise grid gap-4 p-6 md:grid-cols-4">
      <Metric label="Decision score" value={fmt(num(row.decision_matrix_score), 1)} />
      <Metric label="Estimated cost" value={`$${fmt(num(row.decision_total_estimated_cost_usd), 0)}`} />
      <Metric label="Endurance" value={`${fmt(num(row.endurance_min), 1)} min`} />
      <Metric label="AUW" value={`${fmt(num(row.auw_kg), 3)} kg`} />
      <Metric label="Frame mass" value={`${fmt(num(row.frame_mass_kg), 3)} kg`} />
      <Metric label="T/W" value={fmt(num(row.max_twr), 2)} />
      <Metric label="Buildability" value={fmt(num(row.decision_complexity_score), 1)} />
      <Metric label="Quality" value={fmt(num(row.decision_quality_score), 1)} />
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="text-2xl font-semibold">{value}</div>
    </div>
  );
}

function FixedResults({ result }: { result: EvaluateResponse }) {
  if (!result.valid) {
    return (
      <div className="panel p-6">
        <h2 className="mb-2 text-2xl">Configuration rejected</h2>
        <p className="text-[var(--danger)]">{result.rejection_reason}</p>
        <pre className="mt-4 overflow-auto text-xs text-[var(--muted)]">
          {JSON.stringify(result.rejection_details, null, 2)}
        </pre>
      </div>
    );
  }
  return (
    <div className="space-y-6">
      <div className="panel rise grid gap-4 p-6 md:grid-cols-4">
        <Metric label="Decision score" value={fmt(result.decision_matrix_score, 1)} />
        <Metric label="Estimated cost" value={`$${fmt(result.cost?.total_estimated_usd, 0)}`} />
        <Metric label="Endurance" value={`${fmt(result.endurance_min, 1)} min`} />
        <Metric label="AUW" value={`${fmt(result.auw_kg, 3)} kg`} />
        <Metric label="Frame mass" value={`${fmt(result.frame?.frame_mass_kg, 3)} kg`} />
        <Metric label="T/W" value={fmt(result.max_twr, 2)} />
        <Metric label="Buildability" value={fmt(result.buildability_score, 1)} />
        <Metric label="Quality" value={fmt(result.quality_score, 1)} />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <div className="panel p-5">
          <h3 className="mb-3 text-lg font-semibold">Performance</h3>
          <KV k="Hover throttle" v={`${fmt(result.hover_throttle_pct, 1)}%`} />
          <KV k="Hover power" v={`${fmt(result.hover_power_w, 1)} W`} />
          <KV k="Hover / max RPM" v={`${fmt(result.hover_rpm, 0)} / ${fmt(result.max_rpm, 0)}`} />
          <KV k="Hover pack current" v={`${fmt(result.hover_pack_current_a, 2)} A`} />
          <KV k="Max pack current" v={`${fmt(result.max_pack_current_a, 2)} A`} />
          <KV k="Max motor current" v={`${fmt(result.max_motor_current_a, 2)} A`} />
        </div>
        <div className="panel p-5">
          <h3 className="mb-3 text-lg font-semibold">Frame</h3>
          <KV k="Material" v={result.frame?.material || "—"} />
          <KV
            k="Arm tube"
            v={`${fmt(result.frame?.arm_tube_od_mm, 0)} × ${fmt(result.frame?.arm_tube_wall_mm, 1)} mm`}
          />
          <KV k="Span" v={`${fmt(result.frame?.tip_to_tip_span_m, 3)} m`} />
          <KV k="Arm stress" v={`${fmt(result.frame?.arm_stress_mpa, 1)} MPa`} />
          <KV k="Stress utilization" v={`${fmt(result.frame?.stress_utilization_pct, 1)}%`} />
          <KV k="Deflection" v={`${fmt(result.frame?.arm_tip_deflection_mm, 2)} mm`} />
          <KV k="Deflection utilization" v={`${fmt(result.frame?.deflection_utilization_pct, 1)}%`} />
        </div>
        <div className="panel p-5">
          <h3 className="mb-3 text-lg font-semibold">Cost</h3>
          <KV k="Frame allowance" v={`$${fmt(result.cost?.frame_allowance_usd, 0)}`} />
          <KV k="DIY camera/avionics" v={`$${fmt(result.cost?.camera_avionics_allowance_usd, 0)}`} />
          <KV k="Propulsion estimate" v={`$${fmt(result.cost?.propulsion_estimated_usd, 0)}`} />
          <KV k="Total estimate" v={`$${fmt(result.cost?.total_estimated_usd, 0)}`} />
          <KV k="Fallback categories" v={result.cost?.price_estimated_categories || "none"} />
        </div>
        <div className="panel p-5">
          <h3 className="mb-3 text-lg font-semibold">Components</h3>
          <KV k="Motor" v={result.motor_id || "—"} />
          <KV k="Propeller" v={result.propeller_id || "—"} />
          <KV k="Battery" v={result.battery_id || "—"} />
          <KV k="ESC" v={result.esc_id || "—"} />
          <KV k="Evaluation mode" v={result.evaluation_mode || "—"} />
        </div>
      </div>
    </div>
  );
}

function DesignDetail({ row }: { row: Record<string, unknown> }) {
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="panel p-5">
        <h3 className="mb-3 text-lg font-semibold">Component breakdown</h3>
        <KV k="Motor" v={`${row.motor_manufacturer || ""} ${row.motor_model || row.motor_id}`} />
        <KV k="Prop" v={String(row.prop_model || row.prop_id)} />
        <KV
          k="Battery"
          v={`${row.battery_manufacturer || ""} ${row.battery_model || ""} ${row.battery_topology || ""}`}
        />
        <KV k="ESC" v={`${row.esc_manufacturer || ""} ${row.esc_model || row.esc_id}`} />
        <KV k="Frame tube" v={`${row.frame_arm_tube_od_mm}×${row.frame_arm_tube_wall_mm} mm`} />
      </div>
      <div className="panel p-5">
        <h3 className="mb-3 text-lg font-semibold">Decision score breakdown</h3>
        <KV k="Cost score" v={fmt(num(row.decision_cost_score), 1)} />
        <KV k="Endurance score" v={fmt(num(row.decision_endurance_score), 1)} />
        <KV k="Mass score" v={fmt(num(row.decision_weight_score), 1)} />
        <KV k="Quality score" v={fmt(num(row.decision_quality_score), 1)} />
        <KV k="Buildability score" v={fmt(num(row.decision_complexity_score), 1)} />
        <KV k="Price fallbacks" v={String(row.decision_price_estimated_categories || "none")} />
      </div>
    </div>
  );
}

function KV({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-[var(--line)] py-2 text-sm">
      <span className="text-[var(--muted)]">{k}</span>
      <span className="text-right">{v}</span>
    </div>
  );
}
