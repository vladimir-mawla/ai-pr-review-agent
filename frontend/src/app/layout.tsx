import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "PR Review Agent -- Operator Dashboard",
  description: "HITL queue, per-agent cost/latency, and review trace reconstruction.",
};

const NAV_LINKS = [
  { href: "/", label: "Overview" },
  { href: "/hitl", label: "HITL Queue" },
  { href: "/costs", label: "Cost & Latency" },
  { href: "/trace", label: "Trace" },
] as const;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header
          style={{
            borderBottom: "1px solid var(--border)",
            background: "var(--surface)",
          }}
        >
          <nav
            style={{
              maxWidth: 1080,
              margin: "0 auto",
              padding: "14px 20px",
              display: "flex",
              alignItems: "center",
              gap: 20,
            }}
          >
            <strong>PR Review Agent</strong>
            <span className="muted" style={{ fontSize: 13 }}>
              operator dashboard
            </span>
            <div style={{ marginLeft: "auto", display: "flex", gap: 16 }}>
              {NAV_LINKS.map((link) => (
                <Link key={link.href} href={link.href}>
                  {link.label}
                </Link>
              ))}
            </div>
          </nav>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
