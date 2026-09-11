import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import {readFileSync,writeFileSync} from "node:fs";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const apiTarget = environment.VITE_PROXY_TARGET || "http://127.0.0.1:8092";
  return {
    base: "/factory-twin-assets/",
    plugins: [react(), {
      name:"mobile-goods-asset-entry",
      closeBundle(){
        const entry=readFileSync("../../static/factory-twin-assets/mobile-goods.html","utf8");
        const tags=(entry.match(/<(?:script|link)[^>]*(?:src|href)="[^"]+"[^>]*>(?:<\/script>)?/g)||[]).join("\n");
        const path="../../static/mobile_stocktake.html";
        const page=readFileSync(path,"utf8");
        writeFileSync(path,page.replace(/<!-- mobile-goods-assets:start -->[\s\S]*?<!-- mobile-goods-assets:end -->/,"<!-- mobile-goods-assets:start -->\n"+tags+"\n<!-- mobile-goods-assets:end -->"));
      }
    }],
    build: {
      outDir: "../../static/factory-twin-assets",
      // Keep immutable hashed assets for tabs still using a previous entry document.
      emptyOutDir: false,
      rollupOptions: {
        input: {
          editor: "index.html",
          warehouseTwin: "warehouse-twin.html",
          mobileGoods: "mobile-goods.html"
        }
      }
    },
    server: {
      proxy: {
        "/api": apiTarget,
        "/uploads": apiTarget
      }
    }
  };
});
