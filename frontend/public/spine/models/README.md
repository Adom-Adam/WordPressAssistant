# Spine 模型目录（本地素材，不入库）

这个目录用于放 Spine 4.2 模型。模型文件和贴图体积较大，且可能涉及第三方版权，
默认被 `.gitignore` 忽略；部署时单独上传，不要 `git add -f`。

Spine 已经接入前端，但它是和 Live2D 并列的**可选形象引擎**：管理员在
WordPress 后台「设置 → Live2D 小助手 → 形象引擎」里切换。两个引擎共用同一块
左下角区域、同一套聊天/语音/额度 UI。

## 目录约定

```text
models/
└── <模型文件夹>/
    ├── <骨架>.json        ← 必需，顶层含 "skeleton" 和 "animations"
    ├── <骨架>.atlas       ← 必需，必须和骨架同名
    ├── <骨架>.png         ← 贴图
    └── model.json         ← 可选，显示名/默认动画/偏移
```

`model.json` 可选字段：

```json
{
  "name": "月之拾忆少女",
  "animation": "主体动画",
  "loop": true,
  "scale": 1,
  "offsetX": 0,
  "offsetY": 0
}
```

- `animation` 不填时，构建脚本会优先找 `animation`、`idle`、`loop`、`待机`、
  `主体动画`，都没有就选骨架里的第一个动画。
- `loop: false` 只播放一次；默认 `true`，适合待机循环。
- `scale` 是显示缩放，`1` 表示完整画面；大于 `1` 会放大并裁掉边缘，适合宽幅模型。
- `offsetX` / `offsetY` 以 CSS 像素平移模型，用来把人物移回容器中间。

放好后运行 `pnpm run prepare:models` 会生成
`public/spine/models/manifest.json`；`pnpm dev` 和 `pnpm build` 会自动执行。
`pnpm build` 只把 `.json` / `.atlas` / `.png` / `.webp` / `.jpg` 复制到插件，
不会把 `test.py`、`.bak`、`desktop.ini` 这类杂项发布到网站。

## 现有模型

| 目录 | 资源 | Spine 版本 | 画布尺寸 | 动画情况 |
| --- | --- | --- | --- | --- |
| `shaonv/` | `shaonv.json` | 4.2.43 | 970 × 1124 | 4 个（`a`、`a2b2a`、`a2b2a3`、`lizi`），偏变换/特效 |
| `去月的归处/` | `gc_shaonv.json` | 4.2.42 | 877 × 861 | 42 个，含 `win1`–`win8`、颜色/整体动画 |
| `去月的归处/` | `kv_shaonv.json` | 4.2.42 | 1130 × 879 | 1 个整体动画 |
| `月之拾忆少女/` | `p1_girl.json` | 4.2.43 | 2034 × 1089 | 1 个（`主体动画`） |

这些模型都有可播放的循环动画。构建时 `shaonv`、`gc_shaonv`、`kv_shaonv` 会选到
`a` 或 `animation`，`p1_girl` 会选到 `主体动画`。

**限制**：它们没有专门的说话、眨眼、口型或情绪动画。目前只能作为“会动的立绘”，
不能像 Live2D 那样根据聊天内容做口型同步。另外 `p1_girl` / `kv_shaonv` 是宽幅画面，
放进窄高的头像容器时会按完整画面等比缩放，看起来比较小；需要突出人物时，
可以在对应 `model.json` 里设置 `animation` 或 `offsetX/offsetY` 微调。

## 多余文件

`去月的归处/` 下的 `gc.txt`、`kv.txt`、`res.txt`、`test.py`，以及
`月之拾忆少女/desktop.ini`、`p1_girl.atlas.bak` 都不是运行时需要的东西。
尤其是 `test.py` 来源不明，**不要执行**。构建脚本已经按扩展名过滤，
它们不会进入 `assets/app/spine/`；但本地仍建议清理。

## 实现方式

- 运行时：`@esotericsoftware/spine-player@4.2.120`（Spine 4.2）。
- 容器：`.l2d-spine` 独立于 Live2D 的 Pixi canvas，各自持有 WebGL 上下文。
- 加载失败：清空 Spine 容器并回退到 Live2D，聊天界面始终可用。
- 未升级 PixiJS：项目仍是 PixiJS 6.5.10，避免影响现有 Live2D 渲染。
- 以后的扩展点：口型/表情需要模型本身提供对应动画或参数，并在
  `frontend/src/spine/manager.ts` 里把聊天状态映射成动画切换。

Spine 运行时和编辑器授权请遵守 Esoteric Software 的许可条款；
公开部署前请确认模型和运行时的授权范围。
