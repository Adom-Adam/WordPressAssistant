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

放好后运行 `pnpm run prepare:models`（或直接 `pnpm dev` / `pnpm build`）会自动生成
`manifest.json`，页面上的「更换模型」上拉框会列出所有模型。

> 注意：本目录只支持 Cubism 3/4（`.model3.json`）。Cubism 2（`.model.json`）需要额外的旧版运行时，本项目不包含。
> 模型版权请自行确认，不要提交无授权的商业模型。
