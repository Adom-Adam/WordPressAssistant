import { getAuthToken } from './auth';
import type { AssistantAuthConfig } from '../config';

/**
 * 额度信息有两个来源：
 * 1. 打开面板时 GET /api/quota（不消耗次数）；
 * 2. 每次聊天响应头（服务端已经算好，省一次请求）。
 *
 * 两个来源的字段名不一样，所以统一转换成一个接口，UI 只认这一种。
 */
export interface QuotaInfo {
  unlimited: boolean;
  limit: number;
  used: number | null;
  remaining: number | null;
  globalLimit: number;
  globalRemaining: number | null;
  /** 额外额度余额：不随每天刷新，用完之后才动它。 */
  credit: number;
  /** 后端判定的身份；管理员单独一档，方便 UI 决定要不要显示兑换入口。 */
  tier: 'user' | 'member' | 'admin';
}

interface QuotaResponse {
  unlimited?: boolean;
  limit?: number;
  used?: number | null;
  remaining?: number | null;
  global_limit?: number;
  global_remaining?: number | null;
  credit_balance?: number;
  tier?: string;
}

function normalizeTier(value: string | null | undefined): QuotaInfo['tier'] {
  return value === 'member' || value === 'admin' ? value : 'user';
}

/** 从聊天响应的 HTTP 头里读额度。没有额度头时返回 null。 */
export function quotaFromHeaders(headers: Headers): QuotaInfo | null {
  if (headers.get('x-quota-unlimited') === '1') {
    return {
      unlimited: true,
      limit: 0,
      used: null,
      remaining: null,
      globalLimit: Number(headers.get('x-global-quota-limit') ?? 0),
      globalRemaining: numberOrNull(headers.get('x-global-quota-remaining')),
      credit: numberOrNull(headers.get('x-quota-credit')) ?? 0,
      tier: 'admin',
    };
  }

  const remaining = numberOrNull(headers.get('x-quota-remaining'));
  if (remaining === null) return null;

  return {
    unlimited: false,
    limit: Number(headers.get('x-quota-limit') ?? 0),
    used: null,
    remaining,
    globalLimit: Number(headers.get('x-global-quota-limit') ?? 0),
    globalRemaining: numberOrNull(headers.get('x-global-quota-remaining')),
    credit: numberOrNull(headers.get('x-quota-credit')) ?? 0,
    tier: normalizeTier(headers.get('x-quota-tier')),
  };
}

/**
 * 查询额度。失败时返回 null：
 * 额度显示属于“锦上添花”，不能因为它报错就打断聊天。
 */
export async function fetchQuota(
  apiBase: string,
  auth: AssistantAuthConfig,
): Promise<QuotaInfo | null> {
  // 本地开发没有 tokenUrl，也就不连真实后端，直接跳过。
  if (!auth.tokenUrl || !auth.loggedIn) return null;

  let token: string | null;
  try {
    token = await getAuthToken(auth);
  } catch {
    return null;
  }

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    const response = await fetch(`${apiBase}/quota`, {
      headers,
      cache: 'no-store',
      // 同源生产环境不需要 credentials；这里加上也不影响 Nginx 反代。
      credentials: 'same-origin',
    });
    if (!response.ok) return null;
    const data = (await response.json()) as QuotaResponse;
    return {
      unlimited: data.unlimited === true,
      limit: Number(data.limit ?? 0),
      used: typeof data.used === 'number' ? data.used : null,
      remaining: typeof data.remaining === 'number' ? data.remaining : null,
      globalLimit: Number(data.global_limit ?? 0),
      globalRemaining:
        typeof data.global_remaining === 'number' ? data.global_remaining : null,
      credit: typeof data.credit_balance === 'number' ? data.credit_balance : 0,
      tier: normalizeTier(data.tier),
    };
  } catch {
    return null;
  }
}

function numberOrNull(value: string | null): number | null {
  if (value === null || value === '') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * 兑换一个码。
 *
 * 失败也返回 200 语义的布尔值而不是抛异常：兑换是用户主动触发的
 * 一次性动作，调用方只需要拿到一句能直接展示的话。
 */
export async function redeemCode(
  apiBase: string,
  auth: AssistantAuthConfig,
  code: string,
): Promise<{ ok: boolean; message: string }> {
  let token: string | null = null;
  try {
    token = await getAuthToken(auth);
  } catch {
    return { ok: false, message: '登录状态已过期，请刷新页面后再试' };
  }
  if (!token) return { ok: false, message: '请先登录后再兑换' };

  try {
    const response = await fetch(`${apiBase}/redeem`, {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ code }),
      credentials: 'same-origin',
    });

    let data: { message?: string; detail?: string } = {};
    try {
      data = (await response.json()) as { message?: string; detail?: string };
    } catch {
      // 非 JSON 响应（网关错误页等）走下面的通用文案
    }

    if (!response.ok) {
      return { ok: false, message: data.detail || '兑换失败，请稍后再试' };
    }
    return { ok: true, message: data.message || '兑换成功' };
  } catch {
    return { ok: false, message: '兑换失败，请稍后再试' };
  }
}
