import { AuthError, getAuthToken, resetAuthToken } from '../api/auth';
import { ChatHttpError, streamChat } from '../api/chat';
import { fetchQuota, type QuotaInfo } from '../api/quota';
import type { AssistantConfig } from '../config';
// 只导入类型，不产生运行时 import。
// Live2D 管理器依赖 Cubism Core，如果 Core 没加载，静态导入会在模块求值阶段
// 直接抛错，连聊天面板都来不及创建。改成在 start() 里动态导入后，
// 即使 Live2D 挂了，聊天、语音等 UI 仍然可用，只是没有形象。
import type { Live2DManager } from '../live2d/manager';
import type { SpineManager } from '../spine/manager';
import type {
  ChatMessage,
  Citation,
  ModelManifest,
  SpineModelEntry,
  SpineModelManifest,
  StreamEvent,
} from '../types';

/** Live2D 和 Spine 渲染器共有的最小接口，上层只关心“能不能销毁”。 */
interface AvatarController {
  destroy(): void;
}

const ICON = {
  chat: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5.8h14a1.7 1.7 0 011.7 1.7v8.2a1.7 1.7 0 01-1.7 1.7H10l-4.6 3.2a.6.6 0 01-.94-.5v-2.7H5a1.7 1.7 0 01-1.7-1.7V7.5A1.7 1.7 0 015 5.8z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><circle cx="8.6" cy="11.7" r="1.05" fill="currentColor"/><circle cx="12" cy="11.7" r="1.05" fill="currentColor"/><circle cx="15.4" cy="11.7" r="1.05" fill="currentColor"/></svg>`,
  mic: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="3.5" width="6" height="10.5" rx="3" stroke="currentColor" stroke-width="1.7"/><path d="M5.8 11.2a6.2 6.2 0 0012.4 0M12 17.4v3.1M9.2 20.5h5.6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>`,
  send: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4.5 11.7l14.8-6.4a.5.5 0 01.67.65l-5.2 13.5a.5.5 0 01-.92.03l-2.6-5.5-5.6-2.4a.5.5 0 01-.15-.88z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M11.9 13.4l4.3-4.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`,
};

const TEMPLATE = `
  <div class="l2d-stage">
    <canvas class="l2d-canvas" aria-hidden="true"></canvas>
    <!-- Spine 使用自己的 canvas/WebGL 上下文，和 Live2D 的 Pixi canvas 隔离 -->
    <div class="l2d-spine" hidden aria-hidden="true"></div>

    <div class="l2d-dock">
      <div class="l2d-dock__buttons">
        <button type="button" class="l2d-btn" data-action="voice" aria-label="语音输入" title="语音输入">${ICON.mic}</button>
        <button type="button" class="l2d-btn l2d-btn--primary" data-action="chat" aria-label="打开对话" title="和我聊聊">${ICON.chat}</button>
      </div>
    </div>

    <section class="l2d-chat" hidden aria-label="对话面板">
      <header class="l2d-chat__header">
        <div class="l2d-chat__title">
          <span class="l2d-status-dot"></span>
          <span class="l2d-chat__name">梅林</span>
        </div>
        <button type="button" class="l2d-chat__close" data-action="close" aria-label="收起对话" title="收起">${ICON.close}</button>
      </header>
      <div class="l2d-chat__userbar" hidden>
        <span class="l2d-chat__user"></span>
        <span class="l2d-chat__quota" hidden></span>
        <a class="l2d-chat__logout" href="#" rel="nofollow" no-pjax>退出</a>
      </div>
      <div class="l2d-chat__messages" role="log" aria-live="polite"></div>
      <div class="l2d-gate" hidden>
        <div class="l2d-gate__title">登录后就能聊天啦</div>
        <p class="l2d-gate__text">用博客账号登录后，就能和我聊天啦～</p>
        <div class="l2d-gate__actions">
          <a class="l2d-gate__btn l2d-gate__btn--primary" data-auth="login" href="#" no-pjax>去登录</a>
          <a class="l2d-gate__btn" data-auth="register" href="#" no-pjax hidden>注册账号</a>
        </div>
      </div>
      <form class="l2d-chat__form">
        <textarea class="l2d-chat__input" rows="1" placeholder="问我站内的问题吧～" aria-label="输入消息"></textarea>
        <button type="submit" class="l2d-chat__send" aria-label="发送" title="发送">${ICON.send}</button>
      </form>
    </section>

    <div class="l2d-toast" hidden></div>
  </div>
`;

type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  start(): void;
  stop(): void;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: ((event: { error?: string }) => void) | null;
  onresult: ((event: SpeechResultEvent) => void) | null;
};

type SpeechResultEvent = {
  resultIndex: number;
  results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }>;
};

function must<T extends HTMLElement>(root: ParentNode, selector: string): T {
  const el = root.querySelector(selector);
  if (!el) throw new Error(`找不到元素：${selector}`);
  return el as T;
}

export class AssistantApp {
  private readonly root: HTMLElement;
  private readonly canvas: HTMLCanvasElement;
  private readonly spineContainer: HTMLElement;
  private readonly chatPanel: HTMLElement;
  private readonly chatName: HTMLElement;
  private readonly messagesEl: HTMLElement;
  private readonly form: HTMLFormElement;
  private readonly userbar: HTMLElement;
  private readonly userLabel: HTMLElement;
  private readonly quotaEl: HTMLElement;
  private readonly logoutLink: HTMLAnchorElement;
  private readonly gate: HTMLElement;
  private readonly gateLogin: HTMLAnchorElement;
  private readonly gateRegister: HTMLAnchorElement;
  private readonly input: HTMLTextAreaElement;
  private readonly sendBtn: HTMLButtonElement;
  private readonly voiceBtn: HTMLButtonElement;
  private readonly chatBtn: HTMLButtonElement;
  private readonly toast: HTMLElement;

  private avatar: AvatarController | null = null;
  private quota: QuotaInfo | null = null;
  private history: ChatMessage[] = [];
  private busy = false;
  private abort: AbortController | null = null;
  private recognition: Recognition | null = null;
  private voiceUnsupported = false;
  private listening = false;
  private voicePrefix = '';
  private toastTimer: number | null = null;

  // 引用悬浮卡片：整页只创建一个，显示时移动到对应序号旁边。
  // 放在 .l2d-root 下、但用 position: fixed 定位，这样不会被聊天记录的
  // overflow 滚动容器裁掉。
  private citePopover: HTMLElement | null = null;
  private activeCite: HTMLButtonElement | null = null;
  private citeHideTimer: number | null = null;

  constructor(private readonly config: AssistantConfig) {
    this.root = document.createElement('div');
    this.root.className = 'l2d-root';
    this.root.innerHTML = TEMPLATE;
    // 尺寸由 WP 后台注入。CSS 变量让“小/中/大/自定义”共用同一套样式，
    // 不需要为每一档写一份 CSS。
    this.root.style.setProperty('--l2d-width', `${config.appearance.width}px`);
    this.root.style.setProperty('--l2d-height', `${config.appearance.height}px`);
    document.body.appendChild(this.root);

    this.canvas = must<HTMLCanvasElement>(this.root, '.l2d-canvas');
    this.spineContainer = must(this.root, '.l2d-spine');
    this.chatPanel = must(this.root, '.l2d-chat');
    // 面板标题跟着人设走：后台改了名字，这里只改文本，不需要改模板。
    this.chatName = must(this.root, '.l2d-chat__name');
    this.chatName.textContent = config.assistantName;
    this.chatPanel.setAttribute('aria-label', `${config.assistantName}的对话面板`);
    this.messagesEl = must(this.root, '.l2d-chat__messages');
    this.form = must<HTMLFormElement>(this.root, '.l2d-chat__form');
    this.userbar = must(this.root, '.l2d-chat__userbar');
    this.userLabel = must(this.root, '.l2d-chat__user');
    this.quotaEl = must(this.root, '.l2d-chat__quota');
    this.logoutLink = must<HTMLAnchorElement>(this.root, '.l2d-chat__logout');
    this.gate = must(this.root, '.l2d-gate');
    this.gateLogin = must<HTMLAnchorElement>(this.root, '[data-auth="login"]');
    this.gateRegister = must<HTMLAnchorElement>(this.root, '[data-auth="register"]');
    this.input = must<HTMLTextAreaElement>(this.root, '.l2d-chat__input');
    this.sendBtn = must<HTMLButtonElement>(this.root, '.l2d-chat__send');
    this.voiceBtn = must<HTMLButtonElement>(this.root, '[data-action="voice"]');
    this.chatBtn = must<HTMLButtonElement>(this.root, '[data-action="chat"]');
    this.toast = must(this.root, '.l2d-toast');

    this.applyAuthState();
    this.bindEvents();
  }

  async start(): Promise<void> {
    // 管理员在后台选择形象引擎。Spine 失败时不把整个助手拖下水：
    // 清掉半成品，回退到 Live2D，聊天功能始终可用。
    if (this.config.avatarType === 'spine') {
      const loaded = await this.startSpine();
      if (loaded) return;
      this.showToast('Spine 模型没加载起来，先用 Live2D 顶上～', 6000);
    }
    await this.startLive2D();
  }

  private async startLive2D(): Promise<void> {
    try {
      // 动态 import：Cubism Core 缺失时，错误只落在 try/catch 里，
      // 不会影响构造函数已经创建好的聊天 UI。
      const { Live2DManager: Manager } = await import('../live2d/manager');
      const manager = new Manager(this.canvas, this.config.assetBase);
      this.avatar = manager;
      this.spineContainer.hidden = true;
      this.canvas.hidden = false;
      await this.loadLive2DModels(manager);
    } catch (error) {
      console.error('[l2d-ai] Live2D 初始化失败', error);
      this.showToast('Live2D 没加载起来，检查模型文件和 Cubism Core 是否就位～', 6000);
    }
  }

  /**
   * 尝试加载 Spine。返回 false 表示“这次没成，可以回退到 Live2D”。
   *
   * 注意：Spine 的官方 player 会在自身内部抛异常（错误先经过 error 回调），
   * 所以这里必须同时处理 Promise reject 和同步 throw。
   */
  private async startSpine(): Promise<boolean> {
    let manager: SpineManager | null = null;
    try {
      const manifest = await this.fetchManifest<SpineModelManifest>(this.config.spineAssetBase);
      if (!manifest.models?.length) {
        console.warn('[l2d-ai] Spine 清单为空');
        return false;
      }

      const configured = manifest.models.find((item) => item.id === this.config.spineModel);
      const fallback = manifest.models.find((item) => item.id === manifest.default);
      const model: SpineModelEntry | undefined = configured ?? fallback ?? manifest.models[0];
      if (!model) return false;

      const { SpineManager } = await import('../spine/manager');
      manager = new SpineManager(this.spineContainer, this.config.spineAssetBase);
      // 先藏 Live2D 的 canvas，避免两个渲染器同时存在时出现重影。
      this.canvas.hidden = true;
      this.spineContainer.hidden = false;
      await manager.load(model);
      this.avatar = manager;
      return true;
    } catch (error) {
      console.error('[l2d-ai] Spine 初始化失败', error);
      manager?.destroy();
      this.spineContainer.hidden = true;
      this.canvas.hidden = false;
      return false;
    }
  }

  /**
   * 根据登录态切换界面：
   * - 访客且要求登录：隐藏消息区和输入框，显示登录引导；
   * - 已登录：显示用户名和退出链接，正常聊天；
   * - 本地开发（没有 tokenUrl）：保持原来的纯聊天界面。
   */
  private applyAuthState(): void {
    const { auth } = this.config;
    const blocked = this.authBlocked();
    const showUser = Boolean(auth.tokenUrl) && auth.loggedIn;

    this.userbar.hidden = !showUser;
    if (showUser) {
      const name = auth.user?.name || '已登录';
      this.userLabel.textContent = auth.user?.isAdmin ? `${name} · 管理员` : name;
      this.logoutLink.href = auth.logoutUrl || '/wp-login.php?action=logout';
      // 额度显示失败不影响聊天，refreshQuota 内部已经吞掉异常。
      if (!blocked) void this.refreshQuota();
    } else {
      this.quotaEl.hidden = true;
    }

    this.gate.hidden = !blocked;
    this.messagesEl.hidden = blocked;
    this.form.hidden = blocked;
    if (blocked) {
      this.gateLogin.href = auth.loginUrl || '/wp-login.php';
      this.gateRegister.hidden = !auth.registerUrl;
      if (auth.registerUrl) this.gateRegister.href = auth.registerUrl;
    }
  }

  private authBlocked(): boolean {
    return this.config.auth.required && !this.config.auth.loggedIn;
  }

  /** 从后端刷新额度并更新显示；失败时保持上一次的值。 */
  private async refreshQuota(): Promise<void> {
    const quota = await fetchQuota(this.config.apiBase, this.config.auth);
    if (!quota) return;
    this.quota = quota;
    this.renderQuota();
  }

  private renderQuota(): void {
    const quota = this.quota;
    if (!quota || !this.config.auth.loggedIn) {
      this.quotaEl.hidden = true;
      return;
    }

    this.quotaEl.hidden = false;
    this.quotaEl.classList.toggle('is-empty', !quota.unlimited && quota.remaining === 0);

    if (quota.unlimited) {
      this.quotaEl.textContent = '管理员 · 不限量';
      this.quotaEl.title = '管理员不受每日额度限制';
      return;
    }

    const remaining = quota.remaining ?? 0;
    this.quotaEl.textContent = remaining > 0 ? `今日剩余 ${remaining} 条` : '今日额度已用完';
    const globalText =
      quota.globalRemaining !== null && quota.globalLimit > 0
        ? `；全站剩余 ${quota.globalRemaining} 条`
        : '';
    this.quotaEl.title = `每人每日 ${quota.limit} 条${globalText}`;
  }

  private bindEvents(): void {
    this.chatBtn.addEventListener('click', () => this.toggleChat());
    must(this.root, '[data-action="close"]').addEventListener('click', () => this.closeChat());
    this.voiceBtn.addEventListener('click', () => this.toggleVoice());

    this.input.addEventListener('input', () => this.autoGrowInput());
    this.input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        void this.send();
      }
    });

    this.form.addEventListener('submit', (event) => {
      event.preventDefault();
      void this.send();
    });

    document.addEventListener('pointerdown', (event) => {
      const target = event.target as Node;
      // 点来源序号或卡片本身都不算“点外面”。
      const onCite = target instanceof Element && target.closest('.l2d-cite');
      const inPopover = this.citePopover?.contains(target) ?? false;
      if (!onCite && !inPopover) this.hideCitationPopover();
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      this.hideCitationPopover();
      if (!this.chatPanel.hidden) this.closeChat();
    });

    // 弹出卡片用 fixed 定位，锚点滚动或窗口尺寸变化后位置就失效了，
    // 所以直接收起，等用户再次悬浮时重新计算。
    window.addEventListener('scroll', () => this.hideCitationPopover(), true);
    window.addEventListener('resize', () => this.hideCitationPopover());
  }

  /** 读取 `models/manifest.json`；两种形象的清单结构不同，由调用方指定类型。 */
  private async fetchManifest<T>(base: string): Promise<T> {
    const manifestUrl = new URL(`${base}models/manifest.json`, window.location.href).href;
    const response = await fetch(manifestUrl, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`模型清单不可用（${response.status}）`);
    return (await response.json()) as T;
  }

  private async loadLive2DModels(manager: Live2DManager): Promise<void> {
    const manifest = await this.fetchManifest<ModelManifest>(this.config.assetBase);

    if (!manifest.models?.length) {
      this.showToast('还没找到模型：把模型文件夹放进 models/ 目录后重新构建～', 7000);
      return;
    }

    // 模型由管理员在 WordPress 后台选定，普通访客没有切换入口。
    // 依次尝试：后台选定的模型 → manifest 的默认值 → 清单里的第一个。
    const configured = manifest.models.find((item) => item.id === this.config.live2dModel);
    const fallback = manifest.models.find((item) => item.id === manifest.default);
    const first = configured ?? fallback ?? manifest.models[0];
    try {
      await manager.load(first);
    } catch (error) {
      console.error('[l2d-ai] 模型加载失败', error);
      this.showToast(`「${first.name}」加载失败，检查模型文件是否完整～`, 6000);
    }
  }

  private toggleChat(): void {
    if (this.chatPanel.hidden) this.openChat();
    else this.closeChat();
  }

  private openChat(): void {
    this.chatPanel.hidden = false;
    this.root.classList.add('is-chat-open');
    this.chatBtn.classList.add('is-active');
    this.chatPanel.classList.remove('l2d-chat--closing');
    if (!this.authBlocked()) {
      this.input.focus();
      // 每次打开都刷新一次，管理员刚在后台改过额度也能马上看到。
      void this.refreshQuota();
    }
    this.scrollToBottom(true);
  }

  private closeChat(): void {
    this.hideCitationPopover();
    this.chatPanel.hidden = true;
    this.root.classList.remove('is-chat-open');
    this.chatBtn.classList.remove('is-active');
  }

  private async send(): Promise<void> {
    if (this.busy) return;
    if (this.authBlocked()) {
      this.showToast('登录后就能和我聊天啦～', 4000);
      return;
    }

    const text = this.input.value.trim();
    if (!text) return;

    // 先拿到 token，再清空输入框、插入消息。
    // 反过来做的话，凭证失败时用户会看到自己的话已经发出去了，却没有任何回复。
    let token: string | null = null;
    try {
      token = await getAuthToken(this.config.auth);
    } catch (error) {
      console.error('[l2d-ai] 获取登录凭证失败', error);
      this.showToast(
        error instanceof AuthError ? error.message : '暂时无法确认登录状态，稍后再试～',
        6000,
      );
      return;
    }
    // 等 token 的时间里，可能已经有一条消息在发送了。
    if (this.busy) return;

    this.input.value = '';
    this.autoGrowInput();
    this.appendMessage('user', text);
    this.history.push({ role: 'user', content: text });

    this.busy = true;
    this.setBusy(true);
    const { bubble } = this.appendMessage('assistant', '');
    const typing = document.createElement('span');
    typing.className = 'l2d-typing';
    typing.innerHTML = '<i></i><i></i><i></i>';
    bubble.appendChild(typing);

    let answer = '';
    let failed = false;

    try {
      this.abort = new AbortController();
      const result = await streamChat(
        this.config.apiBase,
        {
          messages: this.history.slice(-20),
          pageUrl: window.location.href,
          pageTitle: document.title,
        },
        (event) => this.handleStreamEvent(event, {
          onDelta: (chunk) => {
            if (!answer) typing.remove();
            answer += chunk;
            this.renderBubbleText(bubble, answer);
            this.scrollToBottom();
          },
          onCitations: (items) => {
            this.renderCitations(bubble, items);
            this.scrollToBottom(true);
          },
          onAction: (action) => this.renderAction(bubble, action),
          onError: (detail) => {
            failed = true;
            typing.remove();
            bubble.classList.add('is-error');
            bubble.textContent = detail || '呜…后端好像睡着了，稍后再试试？';
            this.scrollToBottom(true);
          },
        }),
        { token, signal: this.abort.signal },
      );
      if (result.quota) {
        this.quota = result.quota;
        this.renderQuota();
      }
      typing.remove();
      if (!answer && !failed) {
        bubble.textContent = '（这次没有收到内容，再问我一次吧～）';
      }
    } catch (error) {
      if ((error as Error).name === 'AbortError') return;
      failed = true;
      typing.remove();
      console.error('[l2d-ai] 对话失败', error);
      bubble.classList.add('is-error');
      if (error instanceof ChatHttpError && (error.status === 401 || error.status === 403)) {
        resetAuthToken();
        bubble.textContent = '登录状态过期了，刷新一下页面再和我聊天吧～';
      } else if (error instanceof ChatHttpError && error.status === 429) {
        // 后端在流开始前就返回 429，说明今天这条额度已经用完。
        // detail 会区分“个人额度”和“全站额度”，直接展示更准确。
        bubble.textContent =
          error.detail || '今天的聊天额度已经用完啦，明天再来找我玩吧～';
        void this.refreshQuota();
      } else if (error instanceof ChatHttpError && error.status === 503) {
        // 服务端没配 JWT 密钥，用户自己解决不了，提示联系站长。
        bubble.textContent = `${this.config.assistantName}还没准备好登录凭证，先联系站长看看吧～`;
      } else {
        bubble.textContent = '呜…我这边连接不上后端。检查一下 AI 服务是否启动了好不好？';
      }
    } finally {
      this.abort = null;
      this.busy = false;
      this.setBusy(false);
      this.scrollToBottom();
    }

    if (answer && !failed) {
      this.history.push({ role: 'assistant', content: answer });
    }
  }

  private handleStreamEvent(
    event: StreamEvent,
    hooks: {
      onDelta: (chunk: string) => void;
      onCitations: (citations: Citation[]) => void;
      onAction: (action: { type: 'navigate'; url: string; label?: string }) => void;
      onError: (detail: string) => void;
    },
  ): void {
    switch (event.type) {
      case 'delta':
        hooks.onDelta(event.content);
        break;
      case 'citations':
        if (Array.isArray(event.citations) && event.citations.length) hooks.onCitations(event.citations);
        break;
      case 'action':
        hooks.onAction(event.action);
        break;
      case 'error':
        hooks.onError(event.message);
        break;
      case 'done':
      default:
        break;
    }
  }

  private appendMessage(role: 'user' | 'assistant', text: string): { message: HTMLElement; bubble: HTMLElement } {
    const message = document.createElement('div');
    message.className = `l2d-msg l2d-msg--${role}`;
    const bubble = document.createElement('div');
    bubble.className = 'l2d-bubble';
    if (text) bubble.textContent = text;
    message.appendChild(bubble);
    this.messagesEl.appendChild(message);
    this.scrollToBottom(true);
    return { message, bubble };
  }

  private renderBubbleText(bubble: HTMLElement, text: string): void {
    // 模型现在允许输出 Markdown 代码块。这里不引入完整 Markdown 解析器
    // （体积大、还要处理 XSS），只把 ``` 围起来的部分渲染成 <pre><code>。
    // 所有内容仍然用 textContent 写入，永远不会执行 HTML。
    bubble.textContent = '';

    const segments = text.split('```');
    segments.forEach((segment, index) => {
      if (!segment) return;

      if (index % 2 === 0) {
        bubble.appendChild(document.createTextNode(segment));
        return;
      }

      // 代码块第一行可能是语言标记（js、bash……），单独去掉。
      const lines = segment.replace(/^\n/, '').split('\n');
      if (lines.length > 1 && /^[A-Za-z0-9_+#.-]{1,20}$/.test(lines[0].trim())) {
        lines.shift();
      }

      const pre = document.createElement('pre');
      const code = document.createElement('code');
      code.textContent = lines.join('\n').replace(/\n$/, '');
      pre.appendChild(code);
      bubble.appendChild(pre);
    });
  }

  private renderCitations(bubble: HTMLElement, citations: Citation[]): void {
    const existing = bubble.parentElement?.querySelector('.l2d-cites');
    existing?.remove();
    this.hideCitationPopover();

    // 回答下方只留一排小小的序号，像论文脚注。
    // 标题、图片和跳转都放进悬浮卡片里，避免把聊天气泡撑得又宽又乱。
    const wrap = document.createElement('div');
    wrap.className = 'l2d-cites';
    wrap.setAttribute('aria-label', '参考来源');

    citations.forEach((citation, index) => {
      const marker = document.createElement('button');
      marker.type = 'button';
      marker.className = 'l2d-cite';
      marker.textContent = `[${index + 1}]`;
      marker.setAttribute('aria-label', `来源 ${index + 1}：${citation.title}`);
      marker.setAttribute('aria-expanded', 'false');
      marker.setAttribute('aria-controls', 'l2d-cite-pop');

      // 桌面端悬浮即可预览；键盘用户聚焦时也会打开。
      // 触屏没有 hover，所以 click 同样能打开，点页面其他地方收起。
      marker.addEventListener('mouseenter', () => this.showCitationPopover(marker, citation));
      marker.addEventListener('mouseleave', () => this.scheduleCiteHide());
      marker.addEventListener('focus', () => this.showCitationPopover(marker, citation));
      marker.addEventListener('blur', () => this.scheduleCiteHide());
      marker.addEventListener('click', () => this.showCitationPopover(marker, citation));

      wrap.appendChild(marker);
    });

    // 事件到达顺序不固定：如果跳转按钮已经先渲染，就把序号插到它前面，
    // 始终保持「气泡 → 来源序号 → 跳转按钮」。
    const action = bubble.parentElement?.querySelector('.l2d-action');
    if (action) action.before(wrap);
    else bubble.after(wrap);
  }

  /** 创建（或复用）唯一一个浮动来源卡片。 */
  private ensureCitePopover(): HTMLElement {
    if (this.citePopover) return this.citePopover;

    const pop = document.createElement('div');
    pop.id = 'l2d-cite-pop';
    pop.className = 'l2d-cite-pop';
    pop.setAttribute('role', 'dialog');
    pop.setAttribute('aria-label', '来源详情');

    // 鼠标从序号移进卡片时需要一点时间，离开时延迟收起；
    // 进入卡片或聚焦卡片里的链接时取消这个延迟。
    pop.addEventListener('mouseenter', () => this.cancelCiteHide());
    pop.addEventListener('mouseleave', () => this.scheduleCiteHide());
    pop.addEventListener('focusin', () => this.cancelCiteHide());
    pop.addEventListener('focusout', () => this.scheduleCiteHide());

    this.root.appendChild(pop);
    this.citePopover = pop;
    return pop;
  }

  private showCitationPopover(anchor: HTMLButtonElement, citation: Citation): void {
    const pop = this.ensureCitePopover();
    this.cancelCiteHide();

    if (this.activeCite && this.activeCite !== anchor) {
      this.activeCite.setAttribute('aria-expanded', 'false');
    }
    this.activeCite = anchor;
    anchor.setAttribute('aria-expanded', 'true');

    const head = document.createElement('div');
    head.className = 'l2d-cite-pop__head';

    // 只有真的有图才创建 <img>。没有图就什么都不放，
    // 不用书本/图片小图标占位——那种图标一眼就像模板生成的。
    if (citation.image_url) {
      const thumb = document.createElement('img');
      thumb.className = 'l2d-cite-pop__thumb';
      thumb.src = citation.image_url;
      thumb.alt = '';
      thumb.loading = 'lazy';
      thumb.addEventListener('error', () => thumb.remove(), { once: true });
      head.appendChild(thumb);
    }

    const body = document.createElement('div');
    body.className = 'l2d-cite-pop__body';

    const title = document.createElement('div');
    title.className = 'l2d-cite-pop__title';
    title.textContent = citation.title;
    body.appendChild(title);

    if (citation.excerpt) {
      const excerpt = document.createElement('div');
      excerpt.className = 'l2d-cite-pop__excerpt';
      excerpt.textContent = citation.excerpt;
      body.appendChild(excerpt);
    }

    const open = document.createElement('a');
    open.className = 'l2d-cite-pop__open';
    open.href = citation.url;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    open.textContent = '打开文章 →';
    body.appendChild(open);

    head.appendChild(body);
    pop.replaceChildren(head);

    // 先显示出来才能量到真实高度，再决定放在序号上方还是下方。
    pop.classList.add('is-visible');
    pop.style.visibility = 'hidden';
    this.positionCitePopover(anchor, pop);
    pop.style.visibility = '';
  }

  private positionCitePopover(anchor: HTMLButtonElement, pop: HTMLElement): void {
    const margin = 8;
    const gap = 8;
    const rect = anchor.getBoundingClientRect();
    const popRect = pop.getBoundingClientRect();

    // 优先放在序号下方：序号通常位于最新回答的下方，下面的聊天记录
    // 区域是空的，不会挡住刚读完的回答。下方空间不够时再翻到上方。
    const spaceBelow = window.innerHeight - rect.bottom - gap - margin;
    let top = spaceBelow >= popRect.height ? rect.bottom + gap : rect.top - popRect.height - gap;
    if (top < margin) top = rect.bottom + gap;
    top = Math.min(top, window.innerHeight - popRect.height - margin);
    top = Math.max(margin, top);

    let left = rect.left;
    left = Math.min(left, window.innerWidth - popRect.width - margin);
    left = Math.max(margin, left);

    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(top)}px`;
  }

  private scheduleCiteHide(): void {
    this.cancelCiteHide();
    this.citeHideTimer = window.setTimeout(() => this.hideCitationPopover(), 160);
  }

  private cancelCiteHide(): void {
    if (this.citeHideTimer === null) return;
    window.clearTimeout(this.citeHideTimer);
    this.citeHideTimer = null;
  }

  private hideCitationPopover(): void {
    this.cancelCiteHide();
    this.activeCite?.setAttribute('aria-expanded', 'false');
    this.activeCite = null;
    this.citePopover?.classList.remove('is-visible');
  }

  private renderAction(bubble: HTMLElement, action: { type: 'navigate'; url: string; label?: string }): void {
    if (action.type !== 'navigate' || !action.url) return;
    const link = document.createElement('a');
    link.className = 'l2d-action';
    link.href = action.url;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    link.textContent = action.label || '带我去看看 →';
    // 有来源序号时排在序号后面，让序号紧贴气泡。
    const cites = bubble.parentElement?.querySelector('.l2d-cites');
    if (cites) cites.after(link);
    else bubble.after(link);
    this.scrollToBottom(true);
  }

  private setBusy(busy: boolean): void {
    this.sendBtn.disabled = busy;
    this.sendBtn.classList.toggle('is-busy', busy);
  }

  private autoGrowInput(): void {
    this.input.style.height = 'auto';
    this.input.style.height = `${Math.min(this.input.scrollHeight, 96)}px`;
  }

  private scrollToBottom(force = false): void {
    const el = this.messagesEl;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 90;
    if (force || nearBottom) el.scrollTop = el.scrollHeight;
  }

  private toggleVoice(): void {
    if (this.listening) {
      this.recognition?.stop();
      return;
    }
    if (this.authBlocked()) {
      this.openChat();
      this.showToast('登录后就能用语音啦～', 4000);
      return;
    }
    if (this.voiceUnsupported) {
      this.showToast('当前浏览器不支持语音识别，用 Chrome 或 Edge 试试～', 5000);
      return;
    }
    if (!this.recognition) this.setupVoice();
    if (!this.recognition) return;

    this.voicePrefix = this.input.value;
    try {
      this.recognition.start();
    } catch {
      this.showToast('语音识别启动失败，再点一次试试～');
    }
  }

  private setupVoice(): void {
    const Ctor =
      (window as unknown as { SpeechRecognition?: new () => Recognition }).SpeechRecognition ??
      (window as unknown as { webkitSpeechRecognition?: new () => Recognition }).webkitSpeechRecognition;

    if (!Ctor) {
      this.voiceUnsupported = true;
      this.showToast('当前浏览器不支持语音识别，用 Chrome 或 Edge 试试～', 5000);
      return;
    }

    const recognition = new Ctor();
    recognition.lang = this.config.voiceLang;
    recognition.continuous = false;
    recognition.interimResults = true;

    recognition.onstart = () => {
      this.listening = true;
      this.voiceBtn.classList.add('is-listening');
      this.voiceBtn.title = '正在听…再点一次结束';
    };

    recognition.onresult = (event) => {
      let finalText = '';
      let interim = '';
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const result = event.results[i];
        const transcript = result[0]?.transcript ?? '';
        if (result.isFinal) finalText += transcript;
        else interim += transcript;
      }
      this.input.value = this.voicePrefix + finalText + interim;
      this.autoGrowInput();
    };

    recognition.onerror = (event) => {
      if (event.error !== 'aborted') {
        this.showToast('没听清，再说一次试试～', 3500);
      }
    };

    recognition.onend = () => {
      this.listening = false;
      this.voiceBtn.classList.remove('is-listening');
      this.voiceBtn.title = '语音输入';
      this.input.focus();
    };

    this.recognition = recognition;
  }

  private showToast(message: string, duration = 4200): void {
    this.toast.textContent = message;
    this.toast.hidden = false;
    this.toast.classList.add('is-visible');
    if (this.toastTimer !== null) window.clearTimeout(this.toastTimer);
    this.toastTimer = window.setTimeout(() => {
      this.toast.classList.remove('is-visible');
      this.toastTimer = window.setTimeout(() => {
        this.toast.hidden = true;
      }, 240);
    }, duration);
  }
}
