/** 登录用户信息，由 WordPress 插件注入（只含公开字段）。 */
export interface AssistantAuthUser {
  id: number;
  name: string;
  isAdmin: boolean;
}

/**
 * 登录态配置。
 *
 * 这些值全部由 WordPress 插件通过 `window.L2D_AI_CONFIG.auth` 注入：
 * - `required`：是否要求登录后才能聊天；
 * - `loggedIn` / `user`：当前访客的登录状态；
 * - `loginUrl` / `registerUrl` / `logoutUrl`：站内登录相关地址；
 * - `tokenUrl`：向 WordPress 换取短时 JWT 的 REST 接口；
 * - `nonce`：WordPress REST nonce，证明请求确实来自这个登录会话。
 *
 * 注意：这里没有任何密钥。JWT 签名密钥只存在于 wp-config.php 和后端 .env。
 */
export interface AssistantAuthConfig {
  required: boolean;
  loggedIn: boolean;
  user: AssistantAuthUser | null;
  loginUrl: string;
  registerUrl: string;
  logoutUrl: string;
  tokenUrl: string;
  nonce: string;
}

/** Live2D 形象的显示尺寸，由 WordPress 后台注入。 */
export interface AssistantAppearance {
  /** 预设档位；custom 表示直接使用 width/height */
  preset: 'small' | 'medium' | 'large' | 'custom';
  /** 容器宽度（CSS 像素） */
  width: number;
  /** 容器高度（CSS 像素） */
  height: number;
}

/** 形象引擎：Live2D 或 Spine。两者共用同一个容器和聊天 UI。 */
export type AvatarEngine = 'live2d' | 'spine';

export interface AssistantConfig {
  /** AI 后端地址，例如 /ai-api */
  apiBase: string;
  /** Live2D 资源根地址，必须以 / 结尾 */
  assetBase: string;
  /** Spine 资源根地址，必须以 / 结尾 */
  spineAssetBase: string;
  /** 当前使用的形象引擎 */
  avatarType: AvatarEngine;
  /** 人设名字（例如“梅林”），用于对话面板标题和提示文案 */
  assistantName: string;
  /** 语音识别语言 */
  voiceLang: string;
  /** 形象尺寸 */
  appearance: AssistantAppearance;
  /** 后台选定的 Live2D 模型 id；空字符串表示使用 manifest 默认值 */
  live2dModel: string;
  /** 后台选定的 Spine 模型 id；空字符串表示使用 manifest 默认值 */
  spineModel: string;
  /** 登录相关配置 */
  auth: AssistantAuthConfig;
}

interface InjectedConfig {
  apiBase?: string;
  assetBase?: string;
  spineAssetBase?: string;
  avatarType?: string;
  assistantName?: string;
  voiceLang?: string;
  appearance?: Partial<AssistantAppearance> | null;
  live2dModel?: string;
  spineModel?: string;
  auth?: Partial<AssistantAuthConfig> | null;
}

declare global {
  interface Window {
    L2D_AI_CONFIG?: InjectedConfig;
    PIXI?: unknown;
  }
}

function withTrailingSlash(value: string): string {
  return value.endsWith('/') ? value : `${value}/`;
}

/**
 * 从 Live2D 资源地址推出同级的 Spine 地址。
 *
 * WordPress 插件会把两者都显式注入；这里只是给本地开发和旧版插件兜底。
 * 例如 `/assets/app/live2d/` → `/assets/app/spine/`。
 */
function siblingAssetBase(assetBase: string, name: string): string {
  try {
    const url = new URL(assetBase, window.location.href);
    return withTrailingSlash(new URL(`../${name}/`, url).href);
  } catch {
    return withTrailingSlash(`/${name}/`);
  }
}

/**
 * 本地 Vite 开发时的默认登录态。
 *
 * 没有 WordPress 注入时，如果照搬“必须登录”，本地就会永远停在登录提示上，
 * 连界面都调不了。所以开发环境默认“已登录、不强制”，
 * 且 tokenUrl 为空 —— getAuthToken() 会直接返回 null，不请求任何接口。
 */
const DEV_AUTH: AssistantAuthConfig = {
  required: false,
  loggedIn: true,
  user: { id: 0, name: '本地开发', isAdmin: true },
  loginUrl: '',
  registerUrl: '',
  logoutUrl: '',
  tokenUrl: '',
  nonce: '',
};

const DEV_APPEARANCE: AssistantAppearance = {
  preset: 'medium',
  width: 260,
  height: 460,
};

/** 把后台传来的尺寸夹到安全范围，避免 0 或超大值把页面撑坏。 */
function normalizeAppearance(
  injected: InjectedConfig['appearance'],
): AssistantAppearance {
  if (!injected) return DEV_APPEARANCE;

  const preset =
    injected.preset === 'small' ||
    injected.preset === 'medium' ||
    injected.preset === 'large' ||
    injected.preset === 'custom'
      ? injected.preset
      : DEV_APPEARANCE.preset;

  const clamp = (value: unknown, min: number, max: number, fallback: number) => {
    const number = Number(value);
    if (!Number.isFinite(number)) return fallback;
    return Math.min(max, Math.max(min, Math.round(number)));
  };

  return {
    preset,
    width: clamp(injected.width, 160, 420, DEV_APPEARANCE.width),
    height: clamp(injected.height, 260, 720, DEV_APPEARANCE.height),
  };
}

/**
 * 还原 WordPress 内联脚本里的 URL。
 *
 * 典型情况：wp_logout_url() 里的 & 被缓存/优化插件输出成 &amp;。
 * 如果直接赋给 href，浏览器会把 `_wpnonce` 当成 `amp;_wpnonce`，
 * WordPress 认为 nonce 缺失，于是先跳到“确认退出”页。
 */
function normalizeUrl(value: unknown): string {
  if (typeof value !== 'string') return '';
  return value.replace(/&amp;/gi, '&').replace(/&#0*38;/g, '&');
}

function normalizeAuth(injected: InjectedConfig['auth']): AssistantAuthConfig {
  if (!injected) return DEV_AUTH;
  return {
    required: injected.required ?? true,
    loggedIn: injected.loggedIn ?? false,
    user: injected.user ?? null,
    loginUrl: normalizeUrl(injected.loginUrl),
    registerUrl: normalizeUrl(injected.registerUrl),
    logoutUrl: normalizeUrl(injected.logoutUrl),
    tokenUrl: normalizeUrl(injected.tokenUrl),
    nonce: injected.nonce ?? '',
  };
}

/** 人设名字：后台没填或只填了空格时，退回默认的“梅林”。 */
function normalizeAssistantName(value: unknown): string {
  if (typeof value !== 'string') return '梅林';
  return value.trim() || '梅林';
}

export function getConfig(): AssistantConfig {
  const injected = window.L2D_AI_CONFIG ?? {};
  const assetBase = withTrailingSlash(injected.assetBase ?? '/live2d/');
  return {
    apiBase: (injected.apiBase ?? '/ai-api').replace(/\/$/, ''),
    assetBase,
    spineAssetBase: injected.spineAssetBase
      ? withTrailingSlash(injected.spineAssetBase)
      : siblingAssetBase(assetBase, 'spine'),
    // 只认明确的 spine；其余情况一律回到 Live2D，避免后台填错导致没有形象。
    avatarType: injected.avatarType === 'spine' ? 'spine' : 'live2d',
    assistantName: normalizeAssistantName(injected.assistantName),
    voiceLang: injected.voiceLang ?? 'zh-CN',
    appearance: normalizeAppearance(injected.appearance),
    live2dModel: typeof injected.live2dModel === 'string' ? injected.live2dModel : '',
    spineModel: typeof injected.spineModel === 'string' ? injected.spineModel : '',
    auth: normalizeAuth(injected.auth),
  };
}
