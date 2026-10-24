export interface AssistantConfig {
  /** AI 后端地址，例如 /ai-api */
  apiBase: string;
  /** Live2D 资源根地址，必须以 / 结尾 */
  assetBase: string;
  /** 站点名，用于对话面板标题 */
  title: string;
  /** 语音识别语言 */
  voiceLang: string;
}

declare global {
  interface Window {
    L2D_AI_CONFIG?: Partial<AssistantConfig>;
    PIXI?: unknown;
  }
}

function withTrailingSlash(value: string): string {
  return value.endsWith('/') ? value : `${value}/`;
}

export function getConfig(): AssistantConfig {
  const injected = window.L2D_AI_CONFIG ?? {};
  return {
    apiBase: (injected.apiBase ?? '/ai-api').replace(/\/$/, ''),
    assetBase: withTrailingSlash(injected.assetBase ?? '/live2d/'),
    title: injected.title ?? '博客小助手',
    voiceLang: injected.voiceLang ?? 'zh-CN',
  };
}
