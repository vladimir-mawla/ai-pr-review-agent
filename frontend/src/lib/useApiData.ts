"use client";

import { useEffect, useState } from "react";

export type ApiDataState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; data: T };

/**
 * Fetches once on mount and exposes an honest tri-state result.
 *
 * There is no fourth "empty" state here on purpose -- an empty result
 * (e.g. an empty HITL queue) is a perfectly valid `T` and is the calling
 * page's job to render explicitly (see e.g. src/app/hitl/page.tsx), not
 * something this generic hook should try to detect or special-case.
 */
export function useApiData<T>(fetcher: () => Promise<T>, deps: unknown[] = []): ApiDataState<T> {
  const [state, setState] = useState<ApiDataState<T>>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setState({
            status: "error",
            message: error instanceof Error ? error.message : String(error),
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
