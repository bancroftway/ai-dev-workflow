import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Required for the Docker build (Dockerfile) -- traces only the files each route actually
  // needs into .next/standalone, so the deployed image doesn't need full node_modules (plan
  // Section D).
  output: "standalone",
};

export default nextConfig;
