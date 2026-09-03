const API_BASE =
  process.env.NEXT_PUBLIC_API_BASE?.replace(/\/$/, "") ||
  "http://127.0.0.1:43127";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers || {}),
    },
    cache: "no-store",
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export type CatalogItem = {
  id: string;
  label: string;
  manufacturer?: string | null;
  model?: string | null;
  mass_g?: number | null;
  extra?: Record<string, unknown>;
};

export type EvaluateResponse = {
  valid: boolean;
  rejection_reason?: string | null;
  rejection_details?: Record<string, unknown> | null;
  motor_id?: string;
  propeller_id?: string;
  battery_id?: string;
  esc_id?: string;
  auw_kg?: number;
  endurance_min?: number;
  hover_throttle_pct?: number;
  hover_rpm?: number;
  max_rpm?: number;
  hover_power_w?: number;
  hover_pack_current_a?: number;
  max_pack_current_a?: number;
  max_motor_current_a?: number;
  max_twr?: number;
  frame?: {
    material: string;
    arm_tube_od_mm: number;
    arm_tube_wall_mm: number;
    arm_tube_length_m: number;
    arm_stress_mpa: number;
    arm_tip_deflection_mm: number;
    stress_utilization_pct: number;
    deflection_utilization_pct: number;
    tip_to_tip_span_m: number;
    frame_mass_kg: number;
  };
  cost?: {
    motors_usd?: number | null;
    props_usd?: number | null;
    battery_usd?: number | null;
    escs_usd?: number | null;
    frame_allowance_usd: number;
    camera_avionics_allowance_usd: number;
    propulsion_estimated_usd: number;
    total_estimated_usd: number;
    price_estimated_categories: string;
    price_actual_category_count: number;
  };
  quality_score?: number;
  buildability_score?: number;
  decision_matrix_score?: number;
  evaluation_mode?: string;
};

export type JobStatus = {
  job_id: string;
  status: string;
  stage?: string | null;
  progress: number;
  message?: string | null;
  elapsed_seconds?: number | null;
  estimated_remaining_seconds?: number | null;
  error?: string | null;
};

export type JobResults = {
  job_id: string;
  status: string;
  top_designs: Record<string, unknown>[];
  pool: Record<string, unknown>[];
  run_settings?: Record<string, unknown> | null;
  timing?: Record<string, unknown> | null;
  cache_statistics?: Record<string, unknown> | null;
};

export const client = {
  catalog: (kind: "motors" | "propellers" | "battery-cells" | "escs", q = "") =>
    api<{ items: CatalogItem[]; total: number }>(
      `/catalog/${kind}?q=${encodeURIComponent(q)}&limit=40`,
    ),
  evaluate: (body: Record<string, unknown>) =>
    api<EvaluateResponse>("/evaluate", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  createJob: (body: Record<string, unknown>) =>
    api<{ job_id: string }>("/optimization-jobs", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  jobStatus: (id: string) => api<JobStatus>(`/optimization-jobs/${id}`),
  jobResults: (id: string) => api<JobResults>(`/optimization-jobs/${id}/results`),
  rank: (designs: Record<string, unknown>[], weights: Record<string, number>) =>
    api<{ designs: Record<string, unknown>[] }>("/rank", {
      method: "POST",
      body: JSON.stringify({ designs, ...weights }),
    }),
};

export { API_BASE };
