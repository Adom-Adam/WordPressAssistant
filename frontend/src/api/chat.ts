import type { ChatMessage, StreamEvent } from '../types';

export interface ChatPayload {
  messages: ChatMessage[];
  pageUrl?: string;
  pageTitle?: string;
}

/**
 * 以 SSE 方式读取后端流式回复。
 * 后端每个事件是一行 `data: {...}\n\n`。
 */
export async function streamChat(
  apiBase: string,
  payload: ChatPayload,
  onEvent: (event: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${apiBase}/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => '');
    throw new Error(`请求失败（${response.status}）${detail ? `：${detail.slice(0, 200)}` : ''}`);
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
