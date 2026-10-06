import type { NextConfig } from "next";

// The browser only talks to the Next.js server; /api/* is proxied to FastAPI, so the session
// cookie stays first-party and SameSite=Strict works. Evaluated at build time.
const apiUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  // The proxy gives up after 30 seconds by default. A question to the AI makes up to four model
  // calls, so give it five minutes: if the proxy cut the line, the answer and its cost would
  // still be stored while the user saw an error.
  experimental: { proxyTimeout: 300_000 },
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default config;
