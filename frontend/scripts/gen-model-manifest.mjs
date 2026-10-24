/**
 * 扫描 public/live2d/models 下的 Cubism 3/4 模型，生成 manifest.json。
 *
 * 约定：
 *   <模型目录>/
 *     ├── *.model3.json        ← 必需
 *     ├── model.json           ← 可选，{"name": "显示名", "scale": 1.0, "offsetX": 0, "offsetY": 0}
 *     └── 贴图/动作文件等
 *
 * 生成结果会被前端读取，用于「更换模型」上拉框。
 */
import { readdir, readFile, writeFile, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const modelsDir = path.join(root, 'public', 'live2d', 'models');

async function walk(dir, rel = '') {
  const entries = await readdir(dir, { withFileTypes: true });
  const found = [];
  for (const entry of entries) {
    const relPath = rel ? `${rel}/${entry.name}` : entry.name;
    if (entry.isDirectory()) {
      found.push(...(await walk(path.join(dir, entry.name), relPath)));
    } else if (entry.name.endsWith('.model3.json')) {
      found.push(relPath);
    }
  }
  return found;
}

function toId(relPath) {
  return relPath.split('/').slice(0, -1).join('-') || relPath.replace(/\.model3\.json$/, '');
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

async function main() {
  if (!existsSync(modelsDir)) {
    console.warn('[models] 目录不存在:', modelsDir);
    return;
  }

  const files = (await walk(modelsDir)).sort();
  const models = [];

  for (const relPath of files) {
    const modelDir = path.dirname(path.join(modelsDir, relPath));
    const meta = await readMeta(modelDir);
    const id = toId(relPath);
    models.push({
      id,
      name: meta.name || id,
      path: relPath,
      scale: typeof meta.scale === 'number' ? meta.scale : 1,
      offsetX: typeof meta.offsetX === 'number' ? meta.offsetX : 0,
      offsetY: typeof meta.offsetY === 'number' ? meta.offsetY : 0,
    });
  }

  const manifestPath = path.join(modelsDir, 'manifest.json');
  const manifest = {
    default: models.length ? models[0].id : null,
    models,
    generatedAt: new Date().toISOString(),
  };
  await writeFile(manifestPath, JSON.stringify(manifest, null, 2) + '\n', 'utf8');
  console.log(`[models] 已生成 manifest.json（${models.length} 个模型）`);
  for (const model of models) {
    console.log(`  - ${model.id} → ${model.path}`);
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
