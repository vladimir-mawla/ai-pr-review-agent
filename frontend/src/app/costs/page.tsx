"use client";

import { ApiDataView } from "@/components/StatusViews";
import { getAgentMetrics } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

export default function CostsPage() {
  const state = useApiData(getAgentMetrics, []);

  return (
    <div>
      <h1>Cost &amp; Latency</h1>
      <p className="muted">
        Aggregated from every <code>llm.call</code> row in <code>agent_events</code>, grouped by
        agent and model. This project has not built M12&apos;s TimescaleDB continuous aggregates
        (<code>pr_cost_hourly</code>/<code>agent_health_1m</code>) -- this query does the same
        aggregation with plain SQL over the real, unaggregated rows instead. See{" "}
        <code>backend/database/repository.py</code>&apos;s{" "}
        <code>aggregate_llm_calls_by_agent</code> for exactly how a future M12 swap narrows to this
        one query.
      </p>
      <ApiDataView state={state} label="cost and latency metrics">
        {(data) =>
          data.is_empty ? (
            <div className="card">
              <p className="muted" style={{ margin: 0 }}>
                No <code>llm.call</code> events have been recorded yet -- run a review through the
                pipeline (e.g. <code>python -m backend.cli.review_local</code>) to populate this
                view.
              </p>
            </div>
          ) : (
            <div className="card">
              <p style={{ marginTop: 0 }}>
                <strong>Total spend across every agent:</strong> ${data.total_cost_usd}
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Agent</th>
                    <th>Model</th>
                    <th>Calls</th>
                    <th>Total cost (USD)</th>
                    <th>Avg latency (ms)</th>
                    <th>Tokens in</th>
                    <th>Tokens out</th>
                  </tr>
                </thead>
                <tbody>
                  {data.metrics.map((row) => (
                    <tr key={`${row.agent}-${row.model}`}>
                      <td>{row.agent}</td>
                      <td className="mono">{row.model}</td>
                      <td>{row.call_count}</td>
                      <td>${row.total_cost_usd}</td>
                      <td>{row.avg_latency_ms}</td>
                      <td>{row.total_tokens_in}</td>
                      <td>{row.total_tokens_out}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )
        }
      </ApiDataView>
    </div>
  );
}
