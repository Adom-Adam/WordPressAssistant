# Live2D 模型目录

把 **Cubism 3 / 4** 模型放在这个目录下，一个模型一个文件夹：

```
models/
├── 你的模型名/
│   ├── 你的模型名.model3.json   ← 必需（入口文件）
│   ├── 你的模型名.moc3
│   ├── 贴图.png
│   ├── *.motion3.json / *.exp3.json / *.physics3.json
│   └── model.json               ← 可选，用来设置显示名与缩放
```

`model.json` 示例（可选）：

```json
{
  "name": "梅林",
  "scale": 1.0,
  "offsetX": 0,
  "offsetY": 0
}
```

放好后运行 `pnpm run prepare:models` 会重新生成 `manifest.json`；
`pnpm dev` / `pnpm build` 也会顺带生成。前台没有更换模型的按钮，
管理员在 WordPress 后台「设置 → Live2D 小助手 → 形象引擎」选择 Live2D，
再从「Live2D 模型」里选择具体模型。
`pnpm build` 会把 `core/` 和 `models/` 复制到插件的 `assets/app/live2d/`。

### 和 Spine 的关系

同一个后台还可以选择 **Spine** 引擎，模型放在隔壁的
`frontend/public/spine/models/`。两者共用同一块左下角区域和同一套聊天 UI：

- Live2D：支持视线跟随、动作/表情文件，适合 Cubism 模型；
- Spine：播放骨架自带的循环动画，目前没有口型/眨眼参数；
- Spine 加载失败（缺 atlas、WebGL 不可用等）会自动回退到 Live2D；
- 两个引擎各自使用独立 canvas/渲染器，不会为了 Spine 升级 PixiJS。

Spine 的目录约定、`model.json` 字段和现有模型动画清单见
[../../spine/models/README.md](../../spine/models/README.md)。

> 注意：本目录只支持 Cubism 3/4（`.model3.json`）。Cubism 2（`.model.json`）需要额外的旧版运行时，本项目不包含。
> 模型版权请自行确认，不要提交无授权的商业模型；Spine 运行时和编辑器授权同样需要遵守 Esoteric Software 条款。
