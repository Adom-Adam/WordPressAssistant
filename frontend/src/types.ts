export interface ModelEntry {
  id: string;
  name: string;
  /** 相对 models/ 的 .model3.json 路径 */
  path: string;
  scale: number;
  offsetX: number;
  offsetY: number;
  /** 旧清单没有这个字段；读取时按 live2d 处理 */
  kind?: 'live2d';
}

export interface ModelManifest {
  default?: string | null;
  models: ModelEntry[];
}

/**
 * 一个 Spine 模型条目。
 *
 * Spine 和 Live2D 的资源结构不同：
 * - 骨架是 `.json`（不是 `.model3.json`），贴图信息在同名 `.atlas` 里；
 * - 动画由骨架自带，不需要 Live2D 的 motion 分组；
 * - 现有模型没有口型/眨眼参数，所以这里只记录“默认循环哪段动画”。
 */
export interface SpineModelEntry {
  kind: 'spine';
  id: string;
  name: string;
  /** 相对 models/ 的骨架 JSON 路径 */
  path: string;
  /** 相对 models/ 的 atlas 路径，必须与骨架同名 */
  atlas: string;
  /** 默认循环播放的动画名；骨架没有动画时为空字符串 */
  animation: string;
  /** 是否循环播放 */
  loop: boolean;
  /** CSS 显示缩放；1 表示完整画面，大于 1 会放大并裁掉边缘 */
  scale: number;
  offsetX: number;
  offsetY: number;
}

export interface SpineModelManifest {
  default?: string | null;
  models: SpineModelEntry[];
}

export interface Citation {
  title: string;
  url: string;
  post_id?: number;
  excerpt?: string;
  /** 命中的图片地址；有值时来源卡片会显示缩略图 */
  image_url?: string;
  kind?: 'article' | 'image';
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export type StreamEvent =
  | { type: 'delta'; content: string }
  | { type: 'citations'; citations: Citation[] }
  | { type: 'action'; action: { type: 'navigate'; url: string; label?: string } }
  | { type: 'done' }
  | { type: 'error'; message: string };
