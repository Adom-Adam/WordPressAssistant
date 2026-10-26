import { defineConfig } from 'vite';

/**
 * 开发时：前端 5173，后端 18080，/ai-api 代理到 /api。
 * 构建时：输出到 WordPress 插件的 assets/app，供插件直接 enqueue。
 */
export default defineConfig({
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/ai-api': {
        target: 'http://127.0.0.1:18080',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/ai-api/, '/api'),
      },
    },
  },
  build: {
    outDir: '../wordpress-plugin/wp-live2d-ai/assets/app',
    emptyOutDir: true,
    sourcemap: false,
    cssCodeSplit: false,
    rollupOptions: {
      input: 'src/main.ts',
      output: {
        // WordPress 用经典 <script> 加载，因此打成 IIFE 单文件
        format: 'iife',
        name: 'L2DAI',
        inlineDynamicImports: true,
        entryFileNames: 'app.js',
        assetFileNames: 'app.[ext]',
      },
    },
  },
});
