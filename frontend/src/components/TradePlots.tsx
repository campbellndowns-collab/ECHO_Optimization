"use client";

import {
  CartesianGrid,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

function num(v: unknown) {
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

export function TradePlots({ rows }: { rows: Record<string, unknown>[] }) {
  const points = rows
    .map((r) => ({
      endurance: num(r.endurance_min),
      cost: num(r.decision_total_estimated_cost_usd),
      auw: num(r.auw_kg),
      score: num(r.decision_matrix_score),
      prop: num(r.prop_diameter_in),
      frame: num(r.frame_mass_kg),
    }))
    .filter(
      (p) =>
        p.endurance != null &&
        p.cost != null &&
        p.auw != null &&
        p.score != null,
    );

  if (points.length === 0) {
    return (
      <div className="panel p-6 text-[var(--muted)]">
        Not enough designs to plot yet.
      </div>
    );
  }

  const charts = [
    {
      title: "Endurance vs estimated total cost",
      blurb:
        "Higher endurance usually costs more. Points toward the upper-left are attractive if budget is soft.",
      x: "cost" as const,
      y: "endurance" as const,
      xLabel: "Estimated total cost ($)",
      yLabel: "Endurance (min)",
    },
    {
      title: "Endurance vs AUW",
      blurb:
        "Heavier aircraft can carry larger batteries. Look for endurance gains that are not paid for with excess mass.",
      x: "auw" as const,
      y: "endurance" as const,
      xLabel: "AUW (kg)",
      yLabel: "Endurance (min)",
    },
    {
      title: "Decision score vs estimated cost",
      blurb:
        "The weighted matrix score already blends cost, endurance, mass, quality, and buildability.",
      x: "cost" as const,
      y: "score" as const,
      xLabel: "Estimated total cost ($)",
      yLabel: "Decision score",
    },
    {
      title: "Propeller diameter vs frame mass",
      blurb:
        "Larger props force longer arms and usually more frame mass after stress/deflection sizing.",
      x: "prop" as const,
      y: "frame" as const,
      xLabel: "Prop diameter (in)",
      yLabel: "Frame mass (kg)",
    },
  ];

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      {charts.map((c) => (
        <div key={c.title} className="panel p-5">
          <h3 className="mb-1 text-lg font-semibold">{c.title}</h3>
          <p className="mb-4 text-sm text-[var(--muted)]">{c.blurb}</p>
          <div className="h-64">
            <ResponsiveContainer width="100%" height="100%">
              <ScatterChart>
                <CartesianGrid stroke="rgba(180,210,235,0.12)" />
                <XAxis
                  type="number"
                  dataKey={c.x}
                  name={c.xLabel}
                  stroke="#9aafc3"
                  tick={{ fill: "#9aafc3", fontSize: 12 }}
                />
                <YAxis
                  type="number"
                  dataKey={c.y}
                  name={c.yLabel}
                  stroke="#9aafc3"
                  tick={{ fill: "#9aafc3", fontSize: 12 }}
                />
                <Tooltip
                  cursor={{ strokeDasharray: "3 3" }}
                  contentStyle={{
                    background: "#132033",
                    border: "1px solid rgba(180,210,235,0.2)",
                  }}
                />
                <Scatter data={points} fill="#d4a017" />
              </ScatterChart>
            </ResponsiveContainer>
          </div>
        </div>
      ))}
    </div>
  );
}
