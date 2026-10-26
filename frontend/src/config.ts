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

export interface AssistantConfig {
  /** AI 后端地址，例如 /ai-api */
  apiBase: string;
  /** Live2D 资源根地址，必须以 / 结尾 */
  assetBase: string;
  /** 站点名，用于对话面板标题 */
  title: string;
  /** 语音识别语言 */
  voiceLang: string;
  /** 登录相关配置 */
  auth: AssistantAuthConfig;
}

interface InjectedConfig {
  apiBase?: string;
  assetBase?: string;
  title?: string;
  voiceLang?: string;
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

function normalizeAuth(injected: InjectedConfig['auth']): AssistantAuthConfig {
  if (!injected) return DEV_AUTH;
  return {
    required: injected.required ?? true,
    loggedIn: injected.loggedIn ?? false,
    user: injected.user ?? null,
    loginUrl: injected.loginUrl ?? '',
    registerUrl: injected.registerUrl ?? '',
    logoutUrl: injected.logoutUrl ?? '',
    tokenUrl: injected.tokenUrl ?? '',
    nonce: injected.nonce ?? '',
  };
}

export function getConfig(): AssistantConfig {
  const injected = window.L2D_AI_CONFIG ?? {};
  return {
    apiBase: (injected.apiBase ?? '/ai-api').replace(/\/$/, ''),
    assetBase: withTrailingSlash(injected.assetBase ?? '/live2d/'),
    title: injected.title ?? '博客小助手',
    voiceLang: injected.voiceLang ?? 'zh-CN',
    auth: normalizeAuth(injected.auth),
  };
}
