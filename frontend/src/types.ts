export interface ModelEntry {
  id: string;
  name: string;
  /** 相对 models/ 的 .model3.json 路径 */
  path: string;
  scale: number;
  offsetX: number;
  offsetY: number;
}

export interface ModelManifest {
  default?: string | null;
  models: ModelEntry[];
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
