import { cpSync, existsSync } from 'node:fs';
import path from 'node:path';
import { defineConfig, type Plugin } from 'vite';

/** 构建产物目录（相对 frontend 的路径在这里统一解析成绝对路径）。 */
const appOutDir = path.resolve(process.cwd(), '../wordpress-plugin/wp-live2d-ai/assets/app');
const publicDir = path.resolve(process.cwd(), 'public');

/**
 * Spine 模型目录里经常混着作者留下的 `.txt`、`.bak`、`desktop.ini`，
 * 甚至 `test.py`。它们不是运行资源，不能跟着模型一起发布到网站根目录。
 * 这里只放行浏览器真正需要的骨架、atlas 和图片。
 */
const SPINE_EXTENSIONS = new Set(['.json', '.atlas', '.png', '.webp', '.jpg', '.jpeg']);

function copySpineFilter(source: string): boolean {
  const name = path.basename(source);
  if (name.startsWith('.')) return false;
  // 目录没有扩展名，继续递归；文件则按白名单过滤。
  if (!path.extname(name)) return true;
  return SPINE_EXTENSIONS.has(path.extname(name).toLowerCase());
}

/**
 * 构建时手动复制静态资源（Live2D / Spine）。
 *
 * 默认 Vite 会把整个 `public/` 原样复制，连垃圾文件一起带走。
 * 这里在 `publicDir: false` 的前提下只复制两个形象目录，
 * 并给 Spine 加上扩展名白名单。
 */
function copyAvatarAssets(): Plugin {
  return {
    name: 'copy-avatar-assets',
    apply: 'build',
    closeBundle() {
      const targets: Array<{
        from: string;
        to: string;
        filter?: (source: string) => boolean;
      }> = [
        { from: path.join(publicDir, 'live2d'), to: path.join(appOutDir, 'live2d') },
        {
          from: path.join(publicDir, 'spine'),
          to: path.join(appOutDir, 'spine'),
          filter: copySpineFilter,
        },
      ];

      for (const target of targets) {
        if (!existsSync(target.from)) {
          console.warn(`[assets] 跳过不存在的目录：${target.from}`);
          continue;
        }
        cpSync(target.from, target.to, {
          recursive: true,
          force: true,
          filter: target.filter,
        });
      }
      console.log('[assets] 已复制 Live2D / Spine 静态资源');
    },
  };
}

/**
 * 开发时：前端 5173，后端 18080，/ai-api 代理到 /api。
 * 构建时：输出到 WordPress 插件的 assets/app，供插件直接 enqueue。
 */
export default defineConfig(({ command }) => ({
  base: './',
  // 构建时关掉自动复制，交给 copyAvatarAssets 精确控制。
  publicDir: command === 'build' ? false : 'public',
  plugins: [copyAvatarAssets()],
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
}));
