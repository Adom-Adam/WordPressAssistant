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
  postId?: number;
  excerpt?: string;
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
