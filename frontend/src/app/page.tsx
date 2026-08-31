import Link from "next/link";

export default function HomePage() {
  return (
    <div>
      <h1>Operator Dashboard</h1>
      <p className="muted">
        Reads real data from the backend JSON API (<code>backend/api/dashboard.py</code>), which in
        turn reads from the append-only <code>agent_events</code> table and the <code>reviews</code>{" "}
        table this project&apos;s orchestrator writes to. Nothing on these pages is fabricated -- an
        empty database renders an honest empty state, not placeholder numbers.
      </p>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>HITL Queue</h2>
        <p className="muted">
          Reviews currently awaiting human approval: their findings, severities, confidence, and the
          exact reason they were routed for human review instead of auto-posted.
        </p>
        <Link href="/hitl">Open the HITL queue &rarr;</Link>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Cost &amp; Latency</h2>
        <p className="muted">
          Per-agent LLM call count, total cost, average latency, and token usage, aggregated with
          plain SQL over <code>agent_events</code>. Structured as a stand-in for a real TimescaleDB
          continuous aggregate (M12, not built) -- see the M13 build report&apos;s disclosed
          adaptation.
        </p>
        <Link href="/costs">Open cost &amp; latency &rarr;</Link>
      </div>

      <div className="card">
        <h2 style={{ marginTop: 0 }}>Trace</h2>
        <p className="muted">
          Reconstruct one review end-to-end, in time order, from a <code>review_id</code> alone --
          every span, LLM call, and routing decision recorded for it.
        </p>
        <Link href="/trace">Open the trace view &rarr;</Link>
      </div>
    </div>
  );
}
