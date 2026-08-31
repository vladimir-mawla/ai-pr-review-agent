"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { ApiDataView } from "@/components/StatusViews";
import { getRecentReviews } from "@/lib/api";
import { useApiData } from "@/lib/useApiData";

export default function TracePickerPage() {
  const router = useRouter();
  const [reviewId, setReviewId] = useState("");
  const state = useApiData(() => getRecentReviews(50), []);

  function goToTrace(id: string) {
    if (id.trim()) router.push(`/trace/${encodeURIComponent(id.trim())}`);
  }

  return (
    <div>
      <h1>Trace</h1>
      <p className="muted">
        Reconstruct one review end-to-end from <code>agent_events</code>, by <code>review_id</code>{" "}
        alone.
      </p>

      <div className="card">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            goToTrace(reviewId);
          }}
          style={{ display: "flex", gap: 8 }}
        >
          <input
            type="text"
            placeholder="review_id"
            value={reviewId}
            onChange={(e) => setReviewId(e.target.value)}
            className="mono"
            style={{
              flex: 1,
              padding: "8px 10px",
              borderRadius: 6,
              border: "1px solid var(--border)",
              background: "var(--surface)",
              color: "var(--text)",
            }}
          />
          <button type="submit">View trace</button>
        </form>
      </div>

      <h2>Recent reviews</h2>
      <ApiDataView state={state} label="recent reviews">
        {(data) =>
          data.reviews.length === 0 ? (
            <div className="card">
              <p className="muted" style={{ margin: 0 }}>
                No reviews have completed the pipeline yet.
              </p>
            </div>
          ) : (
            <div className="card">
              <table>
                <thead>
                  <tr>
                    <th>Review</th>
                    <th>Repository</th>
                    <th>Status</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {data.reviews.map((review) => (
                    <tr
                      key={review.review_id}
                      style={{ cursor: "pointer" }}
                      onClick={() => goToTrace(review.review_id)}
                    >
                      <td className="mono">{review.review_id}</td>
                      <td>
                        {review.repository_owner}/{review.repository_name} #{review.pr_number}
                      </td>
                      <td>{review.status}</td>
                      <td>{new Date(review.created_at).toLocaleString()}</td>
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
