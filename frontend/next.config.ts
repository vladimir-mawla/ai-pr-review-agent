import type { NextConfig } from "next";

// M13: no server-side rendering depends on the backend being reachable at
// build time -- every dashboard view is a Client Component that fetches
// backend.api.dashboard's JSON routes directly from the browser (see
// src/lib/api.ts). This keeps `next build` (this milestone's CI job)
// fully decoupled from whether a real backend/Postgres is running.
const nextConfig: NextConfig = {
  reactStrictMode: true,
};

export default nextConfig;
