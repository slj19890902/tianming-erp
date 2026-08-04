import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const apiTarget = environment.VITE_PROXY_TARGET || "http://127.0.0.1:8092";
  return {
    plugins: [react()],
    server: {
      proxy: {
        "/api": apiTarget,
        "/uploads": apiTarget
      }
    }
  };
});
