import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxy = {
    "/api": {
      target: env.CORPCOM_API_URL || "http://127.0.0.1:8001",
      changeOrigin: true,
    },
  };
  return {
    plugins: [vue()],
    server: { host: "127.0.0.1", port: 5174, strictPort: true, proxy },
    preview: { host: "127.0.0.1", port: 4174, strictPort: true, proxy },
    test: { environment: "jsdom", clearMocks: true },
  };
});
