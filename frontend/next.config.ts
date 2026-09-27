import type { NextConfig } from "next";

// The browser only talks to the Next.js server; /api/* is proxied to FastAPI, so the session
// cookie stays first-party and SameSite=Strict works. Evaluated at build time.
const apiUrl = process.env.API_INTERNAL_URL ?? "http://localhost:8000";

const config: NextConfig = {
  output: "standalone",
  poweredByHeader: false,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${apiUrl}/api/:path*` }];
  },
};

export default config;
