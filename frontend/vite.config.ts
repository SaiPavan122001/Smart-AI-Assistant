/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";

// In development the UI talks to the backend through this proxy, so the API token never needs
// CORS and the browser only ever sees same-origin requests.
const backend = process.env.SAA_BACKEND_URL ?? "http://127.0.0.1:8000";

// Strict CSP for production builds only (the dev server needs inline scripts for React refresh).
const csp = (): Plugin => ({
  name: "saa-csp",
  apply: "build",
  transformIndexHtml: () => [
    {
      tag: "meta",
      attrs: {
        "http-equiv": "Content-Security-Policy",
        content: "default-src 'self'; script-src 'self'; connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
      },
      injectTo: "head-prepend",
    },
  ],
});

export default defineConfig({
  plugins: [react(), csp()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: backend, changeOrigin: false, ws: true },
    },
  },
  build: { sourcemap: false, target: "es2022" },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
  },
});
