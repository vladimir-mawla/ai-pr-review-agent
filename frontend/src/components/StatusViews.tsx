import type { ReactNode } from "react";

import type { ApiDataState } from "@/lib/useApiData";

export function SeverityBadge({ severity }: { severity: string }) {
  return <span className={`badge badge-${severity}`}>{severity}</span>;
}

export function Loading({ label }: { label: string }) {
  return <p className="muted">Loading {label}...</p>;
}

export function ErrorBox({ message }: { message: string }) {
  return (
    <div className="error-box">
      <strong>Could not load this data.</strong>
      <div className="mono" style={{ marginTop: 6 }}>
        {message}
      </div>
    </div>
  );
}

/**
 * Renders the shared loading/error states for any `useApiData` result and
 * hands the ready `data` to `children` -- every dashboard page uses this so
 * "backend unreachable" and "still loading" always look the same, and a
 * page's own body only ever has to handle the real, honest empty-vs-full
 * data case (e.g. "no reviews queued" vs "N reviews queued").
 */
export function ApiDataView<T>({
  state,
  label,
  children,
}: {
  state: ApiDataState<T>;
  label: string;
  children: (data: T) => ReactNode;
}) {
  if (state.status === "loading") return <Loading label={label} />;
  if (state.status === "error") return <ErrorBox message={state.message} />;
  return <>{children(state.data)}</>;
}
