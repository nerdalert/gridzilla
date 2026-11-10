import { defineConfig } from "vite";
import path from "node:path";
import { fileURLToPath } from "node:url";

const frontendDir = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  root: frontendDir,
  build: {
    outDir: path.resolve(frontendDir, "../web/dist"),
    emptyOutDir: true,
    assetsDir: "assets",
  },
});
