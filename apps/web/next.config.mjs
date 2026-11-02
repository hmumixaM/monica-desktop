const nextConfig = {
  output: "standalone",
  typedRoutes: false,
  async rewrites() {
    const apiOrigin = process.env.API_INTERNAL_URL || "http://localhost:8000";
    return [{ source: "/api/:path*", destination: `${apiOrigin}/api/:path*` }];
  }
};

export default nextConfig;
