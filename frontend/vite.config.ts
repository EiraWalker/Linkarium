import { defineConfig } from "vite";

export default defineConfig({
  base: "/",
  oxc: { jsx: { importSource: "preact" } },
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: {
    manifest: true,
    target: "es2022",
    sourcemap: false,
    assetsInlineLimit: 0,
  },
});
