/**
 * Typed client for backend/api/dashboard.py's JSON routes.
 *
 * Every type below mirrors a pydantic response model in that module
 * exactly (field-for-field) so a schema drift between the two shows up as
 * a TypeScript compile error here, not a silent runtime mismatch.
 *
 * All calls are made from the browser (see the "use client" pages that
 * import this module) -- there is no server-side data fetching in this
 * app, so `next build` never depends on a reachable backend. See
 * next.config.ts's comment for why that separation was chosen.
 */

export const API_BASE_URL =
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

export interface FindingOut {
  agent_type: string;
  severity: string;
  category: string;
  file_path: string;
  line_start: number;
  line_end: number;
  confidence: string;
  rationale: string;
}

export interface ReviewOut {
  review_id: string;
  pr_number: number;
  repository_owner: string;
  repository_name: string;
  head_sha: string;
  status: string;
  overall_confidence: string;
  reason: string;
  findings: FindingOut[];
  created_at: string;
  posted_at: string | null;
}

export interface HitlQueueResponse {
  reviews: ReviewOut[];
  count: number;
}

export interface AgentMetricRow {
  agent: string;
  model: string;
  call_count: number;
  total_cost_usd: string;
  avg_latency_ms: number;
  total_tokens_in: number;
  total_tokens_out: number;
}

export interface AgentMetricsResponse {
  metrics: AgentMetricRow[];
  total_cost_usd: string;
  is_empty: boolean;
}

export interface TraceEventOut {
  id: number | null;
  event_type: string;
  ts: string;
  agent: string | null;
  model: string | null;
  tokens_in: number | null;
  tokens_out: number | null;
  cost_usd: string | null;
  latency_ms: number | null;
  outcome: string | null;
  confidence: string | null;
}

export interface TraceResponse {
  review_id: string;
  events: TraceEventOut[];
  review: ReviewOut | null;
}

export interface RecentReviewsResponse {
  reviews: ReviewOut[];
}

/** Raised for a non-2xx response -- carries the status so callers can render it honestly. */
export class DashboardApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "DashboardApiError";
  }
}

async function getJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, { cache: "no-store" });
  } catch (cause) {
    // A network-level failure (backend not running, DNS, CORS preflight
    // rejected) -- distinct from a real HTTP error status, but the caller
    // should render both the same way: "could not load data", not silence.
    throw new DashboardApiError(
      0,
      `could not reach the backend API at ${API_BASE_URL}${path}: ${
        cause instanceof Error ? cause.message : String(cause)
      }`,
    );
  }
  if (!response.ok) {
    const body = await response.text().catch(() => "");
    throw new DashboardApiError(
      response.status,
      `${path} returned HTTP ${response.status}${body ? `: ${body}` : ""}`,
    );
  }
  return (await response.json()) as T;
}

export function getHitlQueue(): Promise<HitlQueueResponse> {
  return getJson<HitlQueueResponse>("/api/hitl-queue");
}

export function getAgentMetrics(): Promise<AgentMetricsResponse> {
  return getJson<AgentMetricsResponse>("/api/agent-metrics");
}

export function getTrace(reviewId: string): Promise<TraceResponse> {
  return getJson<TraceResponse>(`/api/trace/${encodeURIComponent(reviewId)}`);
}

export function getRecentReviews(limit = 50): Promise<RecentReviewsResponse> {
  return getJson<RecentReviewsResponse>(`/api/reviews?limit=${limit}`);
}
