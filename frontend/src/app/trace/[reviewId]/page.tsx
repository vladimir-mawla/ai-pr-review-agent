"use client";

import { useParams } from "next/navigation";

import { ApiDataView, SeverityBadge } from "@/components/StatusViews";
import { getTrace } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

export default function TraceDetailPage() {
  const params = useParams<{ reviewId: string }>();
  const reviewId = decodeURIComponent(params.reviewId);
  const state = useApiData(() => getTrace(reviewId), [reviewId]);

  return (
    <div>
      <h1>Trace</h1>
      <p className="muted mono">{reviewId}</p>

      <ApiDataView state={state} label="this review's trace">
        {(data) => (
          <>
            {data.review && (
              <div className="card">
                <h2 style={{ marginTop: 0 }}>Review outcome</h2>
                <p>
                  <strong>Status:</strong> {data.review.status} &mdash;{" "}
                  <strong>overall_confidence:</strong> {data.review.overall_confidence}
                </p>
                <p className="muted">{data.review.reason}</p>
                {data.review.findings.length > 0 && (
                  <table>
                    <thead>
                      <tr>
                        <th>Severity</th>
                        <th>Category</th>
                        <th>File</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.review.findings.map((f, i) => (
                        <tr key={i}>
                          <td>
                            <SeverityBadge severity={f.severity} />
                          </td>
                          <td>{f.category}</td>
                          <td className="mono">
                            {f.file_path}:{f.line_start}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            <div className="card">
              <h2 style={{ marginTop: 0 }}>Event timeline</h2>
              {data.events.length === 0 ? (
                <p className="muted" style={{ margin: 0 }}>
                  No events recorded for this review_id -- it may not exist, or may not have reached
                  the pipeline yet.
                </p>
              ) : (
                <table>
                  <thead>
                    <tr>
                      <th>Timestamp</th>
                      <th>Type</th>
                      <th>Agent</th>
                      <th>Model</th>
                      <th>Tokens (in/out)</th>
                      <th>Cost</th>
                      <th>Latency (ms)</th>
                      <th>Outcome / Confidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.events.map((event) => (
                      <tr key={event.id ?? `${event.event_type}-${event.ts}`}>
                        <td className="mono">{new Date(event.ts).toLocaleTimeString()}</td>
                        <td>{event.event_type}</td>
                        <td>{event.agent ?? "-"}</td>
                        <td className="mono">{event.model ?? "-"}</td>
                        <td>
                          {event.tokens_in ?? "-"} / {event.tokens_out ?? "-"}
                        </td>
                        <td>{event.cost_usd ? `$${event.cost_usd}` : "-"}</td>
                        <td>{event.latency_ms ?? "-"}</td>
                        <td>
                          {event.outcome ?? "-"}
                          {event.confidence ? ` (${event.confidence})` : ""}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </ApiDataView>
    </div>
  );
}
