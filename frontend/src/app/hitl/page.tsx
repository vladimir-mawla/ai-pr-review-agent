"use client";

import { ApiDataView, SeverityBadge } from "@/components/StatusViews";
import { getHitlQueue, type HitlQueueResponse } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

function ReviewCard({ review }: { review: HitlQueueResponse["reviews"][number] }) {
  return (
    <div className="card">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <h3 style={{ margin: 0 }}>
          {review.repository_owner}/{review.repository_name} #{review.pr_number}
        </h3>
        <span className="muted mono" style={{ fontSize: 12 }}>
          {review.review_id}
        </span>
      </div>
      <p style={{ marginBottom: 4 }}>
        <strong>overall_confidence:</strong> {review.overall_confidence}
      </p>
      <p className="muted" style={{ marginTop: 0 }}>
        <strong>Routing reason:</strong> {review.reason}
      </p>
      {review.findings.length === 0 ? (
        <p className="muted">No findings recorded on this review.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Severity</th>
              <th>Agent</th>
              <th>Category</th>
              <th>File</th>
              <th>Confidence</th>
              <th>Rationale</th>
            </tr>
          </thead>
          <tbody>
            {review.findings.map((finding, i) => (
              <tr key={i}>
                <td>
                  <SeverityBadge severity={finding.severity} />
                </td>
                <td>{finding.agent_type}</td>
                <td>{finding.category}</td>
                <td className="mono">
                  {finding.file_path}:{finding.line_start}
                  {finding.line_end !== finding.line_start ? `-${finding.line_end}` : ""}
                </td>
                <td>{finding.confidence}</td>
                <td>{finding.rationale}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function HitlQueuePage() {
  const state = useApiData(getHitlQueue, []);

  return (
    <div>
      <h1>HITL Queue</h1>
      <p className="muted">
        Reviews awaiting human approval -- queued because at least one CRITICAL finding was present,
        or overall confidence fell below the configured threshold. Excludes reviews that were
        auto-posted.
      </p>
      <ApiDataView state={state} label="the HITL queue">
        {(data) =>
          data.count === 0 ? (
            <div className="card">
              <p className="muted" style={{ margin: 0 }}>
                No reviews are currently queued for human approval.
              </p>
            </div>
          ) : (
            <>
              <p className="muted">{data.count} review(s) awaiting approval.</p>
              {data.reviews.map((review) => (
                <ReviewCard key={review.review_id} review={review} />
              ))}
            </>
          )
        }
      </ApiDataView>
    </div>
  );
}
