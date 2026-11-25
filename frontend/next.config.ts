import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The Docker runtime image ships only .next/standalone (see Dockerfile).
  output: "standalone",
  poweredByHeader: false,
};

export default nextConfig;
