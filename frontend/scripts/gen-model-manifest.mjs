/**
 * 扫描 `public/` 下的形象资源，生成两份清单：
 *
 * 1. `public/live2d/models/manifest.json` —— Cubism 3/4
 *    <模型目录>/
 *      ├── *.model3.json        ← 必需
 *      ├── model.json           ← 可选，{"name": "显示名", "scale": 1.0, "offsetX": 0, "offsetY": 0}
 *      └── 贴图/动作文件等
 *
 * 2. `public/spine/models/manifest.json` —— Spine 4.2
 *    <模型目录>/
 *      ├── *.json               ← 骨架（含 "skeleton" 与 "animations"）
 *      ├── 同名 *.atlas         ← 必需
 *      ├── 同名 *.png           ← 贴图
 *      └── model.json           ← 可选，{"name", "animation", "loop", "scale", "offsetX", "offsetY"}
 *
 * 两个目录都是“放进去就能用”，不需要手写清单。构建和 `pnpm dev` 前都会运行本脚本。
 */
import { readdir, readFile, writeFile } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const live2dModelsDir = path.join(root, 'public', 'live2d', 'models');
const spineModelsDir = path.join(root, 'public', 'spine', 'models');

/** 递归收集匹配后缀的文件，返回相对路径（统一用 `/`，便于写进 JSON）。 */
async function walk(dir, suffix, rel = '') {
  const entries = await readdir(dir, { withFileTypes: true });
  const found = [];
  for (const entry of entries) {
    const relPath = rel ? `${rel}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      found.push(...(await walk(path.join(dir, entry.name), suffix, relPath)));
    } else if (entry.name.toLowerCase().endsWith(suffix)) {
      found.push(relPath);
    }
  }
  return found;
}

async function readMeta(dir) {
  const metaPath = path.join(dir, 'model.json');
  if (!existsSync(metaPath)) return {};
  try {
    return JSON.parse(await readFile(metaPath, 'utf8'));
  } catch (err) {
    console.warn(`[models] 无法解析 ${metaPath}: ${err.message}`);
    return {};
  }
}

function toLive2DId(relPath) {
  return relPath.split('/').slice(0, -1).join('-') || relPath.replace(/\.model3\.json$/, '');
}

async function generateLive2DManifest() {
  if (!existsSync(live2dModelsDir)) {
    console.warn('[models] Live2D 目录不存在:', live2dModelsDir);
    return [];
  }

  const files = (await walk(live2dModelsDir, '.model3.json')).sort();
  const models = [];

  for (const relPath of files) {
    const modelDir = path.dirname(path.join(live2dModelsDir, relPath));
    const meta = await readMeta(modelDir);
    const id = toLive2DId(relPath);
    models.push({
      kind: 'live2d',
      id,
      name: meta.name || id,
      path: relPath,
      scale: typeof meta.scale === 'number' ? meta.scale : 1,
      offsetX: typeof meta.offsetX === 'number' ? meta.offsetX : 0,
      offsetY: typeof meta.offsetY === 'number' ? meta.offsetY : 0,
    });
  }

  const manifest = {
    default: models.length ? models[0].id : null,
    models,
    generatedAt: new Date().toISOString(),
  };
  await writeFile(
    path.join(live2dModelsDir, 'manifest.json'),
    JSON.stringify(manifest, null, 2) + '\n',
    'utf8',
  );
  console.log(`[models] Live2D：已生成 manifest.json（${models.length} 个模型）`);
  for (const model of models) console.log(`  - ${model.id} → ${model.path}`);
}

/**
 * 判断一个 JSON 是不是 Spine 骨架。
 *
 * Spine 导出的 JSON 顶层有 `skeleton`（版本、尺寸）和 `animations`。
 * 同目录的 `model.json` 是我们的显示配置，不是骨架，必须跳过；
 * 也不要靠文件名猜，否则把 `package.json` 之类误收进来。
 */
function isSpineSkeleton(data) {
  return Boolean(
    data &&
      typeof data === 'object' &&
      data.skeleton &&
      typeof data.skeleton === 'object' &&
      data.animations &&
      typeof data.animations === 'object',
  );
}

/** 优先选名字明确的待机动画；都没有就退回骨架里的第一个。 */
const PREFERRED_ANIMATIONS = ['animation', 'idle', 'loop', '待机', '主体动画'];

function pickAnimation(animations, meta) {
  const names = Object.keys(animations || {});
  if (!names.length) return '';

  if (typeof meta.animation === 'string' && meta.animation) {
    if (names.includes(meta.animation)) return meta.animation;
    console.warn(
      `[models] 配置的动画「${meta.animation}」不存在，可选：${names.slice(0, 8).join(', ')}`,
    );
  }

  return PREFERRED_ANIMATIONS.find((name) => names.includes(name)) ?? names[0];
}

async function generateSpineManifest() {
  if (!existsSync(spineModelsDir)) {
    console.warn('[models] Spine 目录不存在:', spineModelsDir);
    return [];
  }

  const files = (await walk(spineModelsDir, '.json'))
    .filter((relPath) => path.posix.basename(relPath) !== 'manifest.json')
    .sort();

  const skeletons = [];
  for (const relPath of files) {
    const absPath = path.join(spineModelsDir, relPath);
    let data;
    try {
      data = JSON.parse(await readFile(absPath, 'utf8'));
    } catch (err) {
      // model.json 可能故意写得不完整；只对真正的骨架报错，避免刷屏。
      if (path.posix.basename(relPath) !== 'model.json') {
        console.warn(`[models] 跳过无法解析的 JSON ${relPath}: ${err.message}`);
      }
      continue;
    }
    if (!isSpineSkeleton(data)) continue;

    const atlasPath = relPath.replace(/\.json$/i, '.atlas');
    if (!existsSync(path.join(spineModelsDir, atlasPath))) {
      console.warn(`[models] 跳过 ${relPath}：找不到同名 atlas（${atlasPath}）`);
      continue;
    }

    const relDir = path.posix.dirname(relPath);
    skeletons.push({
      data,
      relPath,
      atlasPath,
      relDir: relDir === '.' ? '' : relDir,
      stem: path.posix.basename(relPath, path.posix.extname(relPath)),
    });
  }

  // 同一个文件夹里有多个骨架时，显示名要带骨架名，否则下拉框会出现
  // 两个一模一样的“去月的归处”。
  const perDir = new Map();
  for (const item of skeletons) {
    perDir.set(item.relDir, (perDir.get(item.relDir) ?? 0) + 1);
  }

  const models = [];
  for (const item of skeletons) {
    const modelDir = path.dirname(path.join(spineModelsDir, item.relPath));
    const meta = await readMeta(modelDir);
    const folderLabel = item.relDir
      ? item.relDir.split('/').filter(Boolean).pop()
      : item.stem;
    const duplicate = (perDir.get(item.relDir) ?? 0) > 1;
    const id = item.relPath.replace(/\.json$/i, '');

    models.push({
      kind: 'spine',
      id,
      name: meta.name || (duplicate ? `${folderLabel} · ${item.stem}` : folderLabel),
      path: item.relPath,
      atlas: item.atlasPath,
      animation: pickAnimation(item.data.animations, meta),
      loop: meta.loop !== false,
      scale: typeof meta.scale === 'number' ? meta.scale : 1,
      offsetX: typeof meta.offsetX === 'number' ? meta.offsetX : 0,
      offsetY: typeof meta.offsetY === 'number' ? meta.offsetY : 0,
    });
  }

  const manifest = {
    default: models.length ? models[0].id : null,
    models,
    generatedAt: new Date().toISOString(),
  };
  await writeFile(
    path.join(spineModelsDir, 'manifest.json'),
    JSON.stringify(manifest, null, 2) + '\n',
    'utf8',
  );
  console.log(`[models] Spine：已生成 manifest.json（${models.length} 个模型）`);
  for (const model of models) {
    console.log(`  - ${model.id} → ${model.path}（动画：${model.animation || '无'}）`);
  }
}

async function main() {
  await generateLive2DManifest();
  await generateSpineManifest();
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
