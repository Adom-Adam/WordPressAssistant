import type { ChatMessage, StreamEvent } from '../types';

export interface ChatPayload {
  messages: ChatMessage[];
  pageUrl?: string;
  pageTitle?: string;
}

export interface ChatOptions {
  /** WordPress 换来的 JWT；为空时不带 Authorization 头（本地开发）。 */
  token?: string | null;
  signal?: AbortSignal;
}

/** 后端返回非 2xx 时抛出，带着状态码方便上层区分“登录过期”和“服务没开”。 */
export class ChatHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ChatHttpError';
  }
}

/**
 * 以 SSE 方式读取后端流式回复。
 * 后端每个事件是一行 `data: {...}\n\n`。
 */
export async function streamChat(
  apiBase: string,
  payload: ChatPayload,
  onEvent: (event: StreamEvent) => void,
  options: ChatOptions = {},
): Promise<void> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (options.token) headers.Authorization = `Bearer ${options.token}`;

  const response = await fetch(`${apiBase}/chat`, {
    method: 'POST',
    headers,
    body: JSON.stringify(payload),
    signal: options.signal,
  });

  if (!response.ok) {
    const raw = await response.text().catch(() => '');
    const detail = extractDetail(raw);
    throw new ChatHttpError(
      `请求失败（${response.status}）${detail ? `：${detail.slice(0, 200)}` : ''}`,
      response.status,
    );
  }
  if (!response.body) {
    throw new Error('当前浏览器不支持流式响应');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let boundary = buffer.indexOf('\n\n');
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf('\n\n');
      dispatchBlock(block, onEvent);
    }
  }

  const tail = buffer.trim();
  if (tail) dispatchBlock(tail, onEvent);
}

/**
 * FastAPI 的错误是 `{"detail": "..."}`，插件的错误是 `{"message": "..."}`。
 * 能解析出文字就用文字，否则退回原始响应。
 */
function extractDetail(raw: string): string {
  if (!raw) return '';
  try {
    const data = JSON.parse(raw) as { detail?: unknown; message?: unknown };
    if (typeof data.detail === 'string') return data.detail;
    if (typeof data.message === 'string') return data.message;
  } catch {
    // 不是 JSON，直接用原文
  }
  return raw;
}

function dispatchBlock(block: string, onEvent: (event: StreamEvent) => void): void {
  for (const line of block.split('\n')) {
    const trimmed = line.trim();
    if (!trimmed.startsWith('data:')) continue;
    const data = trimmed.slice(5).trim();
    if (!data || data === '[DONE]') continue;
    try {
      onEvent(JSON.parse(data) as StreamEvent);
    } catch {
      // 忽略心跳或非 JSON 行
    }
  }
}
