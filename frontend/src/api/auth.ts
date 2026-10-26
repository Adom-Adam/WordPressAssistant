import type { AssistantAuthConfig, AssistantAuthUser } from '../config';

/**
 * 换取登录凭证时出的错。
 *
 * status 是 WordPress 返回的 HTTP 状态码：
 * - 401：未登录（通常是登录状态在另一个标签页被清掉了）；
 * - 403：nonce 过期（页面开太久，刷新即可）；
 * - 500：服务端没配置 JWT 密钥。
 */
export class AuthError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
    this.name = 'AuthError';
  }
}

interface TokenResponse {
  token: string;
  expires_at: number;
  user: AssistantAuthUser;
}

/**
 * token 只放在内存里，不写 localStorage。
 * 页面刷新就重新向 WordPress 换一次，避免 XSS 把长期凭证偷走。
 */
let cached: { key: string; token: string; expiresAt: number } | null = null;

/** 正在进行的请求。多个调用同时要 token 时，只发一次网络请求。 */
let inflight: Promise<string | null> | null = null;

export function resetAuthToken(): void {
  cached = null;
}

/**
 * 取一个可用的 JWT。
 *
 * 返回 null 表示“这个环境不需要 token”（本地开发没有注入 tokenUrl）。
 * 需要 token 但拿不到时抛 AuthError，由调用方决定怎么提示用户。
 */
export async function getAuthToken(auth: AssistantAuthConfig): Promise<string | null> {
  // 本地开发 / 未启用登录时，tokenUrl 为空，直接放行。
  if (!auth.tokenUrl) return null;

  if (!auth.loggedIn) {
    // required=false 时允许访客继续（是否放行由后端决定），不强行换 token。
    if (!auth.required) return null;
    throw new AuthError('请先登录后再和小助手聊天', 401, 'l2d_ai_not_logged_in');
  }

  const key = `${auth.tokenUrl}|${auth.user?.id ?? 0}`;
  const now = Date.now() / 1000;

  // 提前 30 秒换新，避免“刚拿到就过期”的竞态。
  if (cached && cached.key === key && cached.expiresAt - 30 > now) {
    return cached.token;
  }

  if (inflight) return inflight;

  inflight = requestToken(auth, key)
    .catch((error) => {
      // 失败不保留坏状态，下次点击可以重试。
      cached = null;
      throw error;
    })
    .finally(() => {
      inflight = null;
    });

  return inflight;
}

async function requestToken(auth: AssistantAuthConfig, key: string): Promise<string> {
  let response: Response;
  try {
    response = await fetch(auth.tokenUrl, {
      method: 'POST',
      // 带上 WordPress 的登录 Cookie；nonce 证明请求来自本站页面。
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'X-WP-Nonce': auth.nonce,
        Accept: 'application/json',
      },
    });
  } catch {
    throw new AuthError('连不上 WordPress，检查一下网络～', 0);
  }

  const raw = await response.text().catch(() => '');
  let data: unknown = null;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    // 有些安全插件会把错误渲染成 HTML，保留原文用于提示即可。
  }

  if (!response.ok) {
    const { message, code } = readError(data);
    const fallback =
      response.status === 401
        ? '请先登录后再和小助手聊天'
        : response.status === 403
          ? '登录状态已过期，请刷新页面'
          : `无法获取登录凭证（${response.status}）`;
    throw new AuthError(message || fallback, response.status, code);
  }

  const payload = data as Partial<TokenResponse> | null;
  if (!payload || typeof payload.token !== 'string' || !payload.token) {
    throw new AuthError('登录凭证格式不正确', 502, 'l2d_ai_bad_token');
  }

  const expiresAt =
    typeof payload.expires_at === 'number' && Number.isFinite(payload.expires_at)
      ? payload.expires_at
      : Date.now() / 1000 + 600;

  cached = { key, token: payload.token, expiresAt };
  return payload.token;
}

function readError(data: unknown): { message: string; code?: string } {
  if (!data || typeof data !== 'object') return { message: '' };
  const record = data as { message?: unknown; code?: unknown };
  return {
    message: typeof record.message === 'string' ? record.message : '',
    code: typeof record.code === 'string' ? record.code : undefined,
  };
}
