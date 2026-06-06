import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom"],
          markdown: ["react-markdown", "remark-gfm"],
          state: ["zustand", "@tanstack/react-query"],
        },
      },
    },
  },
  server: {
    proxy: {
      "/api": "http://localhost:3002",
      "/thumbnails": "http://localhost:3002",
    },
  },
  publicDir: "public",
});
