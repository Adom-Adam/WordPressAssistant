import { streamChat } from '../api/chat';
import type { AssistantConfig } from '../config';
import { Live2DManager } from '../live2d/manager';
import type { ChatMessage, Citation, ModelEntry, ModelManifest, StreamEvent } from '../types';

const ICON = {
  models: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3.5l1.7 4.8 4.8 1.7-4.8 1.7L12 16.5l-1.7-4.8-4.8-1.7 4.8-1.7L12 3.5z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M18.5 15.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8.8-2.2z" fill="currentColor" opacity=".65"/></svg>`,
  chat: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 5.8h14a1.7 1.7 0 011.7 1.7v8.2a1.7 1.7 0 01-1.7 1.7H10l-4.6 3.2a.6.6 0 01-.94-.5v-2.7H5a1.7 1.7 0 01-1.7-1.7V7.5A1.7 1.7 0 015 5.8z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><circle cx="8.6" cy="11.7" r="1.05" fill="currentColor"/><circle cx="12" cy="11.7" r="1.05" fill="currentColor"/><circle cx="15.4" cy="11.7" r="1.05" fill="currentColor"/></svg>`,
  mic: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="9" y="3.5" width="6" height="10.5" rx="3" stroke="currentColor" stroke-width="1.7"/><path d="M5.8 11.2a6.2 6.2 0 0012.4 0M12 17.4v3.1M9.2 20.5h5.6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`,
  close: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M7 7l10 10M17 7L7 17" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>`,
  send: `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4.5 11.7l14.8-6.4a.5.5 0 01.67.65l-5.2 13.5a.5.5 0 01-.92.03l-2.6-5.5-5.6-2.4a.5.5 0 01-.15-.88z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/><path d="M11.9 13.4l4.3-4.2" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>`,
};

const TEMPLATE = `
  <div class="l2d-stage">
    <canvas class="l2d-canvas" aria-hidden="true"></canvas>

    <div class="l2d-dock">
      <div class="l2d-model-menu" hidden>
        <div class="l2d-model-menu__title">换个形象</div>
        <div class="l2d-model-menu__list"></div>
      </div>
      <div class="l2d-dock__buttons">
        <button type="button" class="l2d-btn" data-action="models" aria-label="更换模型" title="更换模型">${ICON.models}</button>
        <button type="button" class="l2d-btn" data-action="voice" aria-label="语音输入" title="语音输入">${ICON.mic}</button>
        <button type="button" class="l2d-btn l2d-btn--primary" data-action="chat" aria-label="打开对话" title="和我聊聊">${ICON.chat}</button>
      </div>
    </div>

    <section class="l2d-chat" hidden aria-label="对话面板">
      <header class="l2d-chat__header">
        <div class="l2d-chat__title">
          <span class="l2d-status-dot"></span>
          <span class="l2d-chat__name">小助手</span>
        </div>
        <button type="button" class="l2d-chat__close" data-action="close" aria-label="收起对话" title="收起">${ICON.close}</button>
      </header>
      <div class="l2d-chat__messages" role="log" aria-live="polite"></div>
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
  private readonly chatPanel: HTMLElement;
  private readonly messagesEl: HTMLElement;
  private readonly input: HTMLTextAreaElement;
  private readonly sendBtn: HTMLButtonElement;
  private readonly modelsBtn: HTMLButtonElement;
  private readonly voiceBtn: HTMLButtonElement;
  private readonly chatBtn: HTMLButtonElement;
  private readonly modelMenu: HTMLElement;
  private readonly modelList: HTMLElement;
  private readonly toast: HTMLElement;

  private l2d: Live2DManager | null = null;
  private manifest: ModelManifest | null = null;
  private history: ChatMessage[] = [];
  private busy = false;
  private abort: AbortController | null = null;
  private recognition: Recognition | null = null;
  private voiceUnsupported = false;
  private listening = false;
  private voicePrefix = '';
  private toastTimer: number | null = null;

  constructor(private readonly config: AssistantConfig) {
    this.root = document.createElement('div');
    this.root.className = 'l2d-root';
    this.root.innerHTML = TEMPLATE;
    document.body.appendChild(this.root);

    this.canvas = must<HTMLCanvasElement>(this.root, '.l2d-canvas');
    this.chatPanel = must(this.root, '.l2d-chat');
    this.messagesEl = must(this.root, '.l2d-chat__messages');
    this.input = must<HTMLTextAreaElement>(this.root, '.l2d-chat__input');
    this.sendBtn = must<HTMLButtonElement>(this.root, '.l2d-chat__send');
    this.modelsBtn = must<HTMLButtonElement>(this.root, '[data-action="models"]');
    this.voiceBtn = must<HTMLButtonElement>(this.root, '[data-action="voice"]');
    this.chatBtn = must<HTMLButtonElement>(this.root, '[data-action="chat"]');
    this.modelMenu = must(this.root, '.l2d-model-menu');
    this.modelList = must(this.root, '.l2d-model-menu__list');
    this.toast = must(this.root, '.l2d-toast');

    this.bindEvents();
  }

  async start(): Promise<void> {
    try {
      this.l2d = new Live2DManager(this.canvas, this.config.assetBase);
      await this.loadModels();
    } catch (error) {
      console.error('[l2d-ai] Live2D 初始化失败', error);
      this.showToast('Live2D 没加载起来，检查模型文件和 Cubism Core 是否就位～', 6000);
    }
  }

  private bindEvents(): void {
    this.modelsBtn.addEventListener('click', () => this.toggleModelMenu());
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

    must<HTMLFormElement>(this.root, '.l2d-chat__form').addEventListener('submit', (event) => {
      event.preventDefault();
      void this.send();
    });

    document.addEventListener('pointerdown', (event) => {
      const target = event.target as Node;
      if (this.modelMenu.hidden) return;
      if (this.modelMenu.contains(target) || this.modelsBtn.contains(target)) return;
      this.modelMenu.hidden = true;
    });

    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return;
      this.modelMenu.hidden = true;
      if (!this.chatPanel.hidden) this.closeChat();
    });
  }

  private async loadModels(): Promise<void> {
    const manifestUrl = new URL(`${this.config.assetBase}models/manifest.json`, window.location.href).href;
    const response = await fetch(manifestUrl, { cache: 'no-cache' });
    if (!response.ok) throw new Error(`模型清单不可用（${response.status}）`);

    const manifest = (await response.json()) as ModelManifest;
    this.manifest = manifest;
    this.renderModelMenu();

    if (!manifest.models?.length) {
      this.showToast('还没找到模型：把模型文件夹放进 models/ 目录后重新构建～', 7000);
      return;
    }

    const first = manifest.models.find((item) => item.id === manifest.default) ?? manifest.models[0];
    await this.selectModel(first);
  }

  private renderModelMenu(): void {
    this.modelList.replaceChildren();
    const models = this.manifest?.models ?? [];
    if (!models.length) {
      const empty = document.createElement('div');
      empty.className = 'l2d-model-empty';
      empty.textContent = '暂无模型';
      this.modelList.appendChild(empty);
      return;
    }
    for (const model of models) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'l2d-model-item';
      button.dataset.modelId = model.id;
      button.textContent = model.name;
      button.addEventListener('click', () => {
        void this.selectModel(model);
      });
      this.modelList.appendChild(button);
    }
  }

  private async selectModel(model: ModelEntry): Promise<void> {
    if (!this.l2d) return;
    try {
      await this.l2d.load(model);
      for (const item of this.modelList.querySelectorAll<HTMLElement>('.l2d-model-item')) {
        item.classList.toggle('is-active', item.dataset.modelId === model.id);
      }
      this.modelMenu.hidden = true;
    } catch (error) {
      console.error('[l2d-ai] 模型加载失败', error);
      this.showToast(`「${model.name}」加载失败，检查模型文件是否完整～`, 6000);
    }
  }

  private toggleModelMenu(): void {
    this.modelMenu.hidden = !this.modelMenu.hidden;
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
    this.input.focus();
    this.scrollToBottom(true);
  }

  private closeChat(): void {
    this.chatPanel.hidden = true;
    this.root.classList.remove('is-chat-open');
    this.chatBtn.classList.remove('is-active');
  }

  private async send(): Promise<void> {
    if (this.busy) return;
    const text = this.input.value.trim();
    if (!text) return;

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
      await streamChat(
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
        this.abort.signal,
      );
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
      bubble.textContent = '呜…我这边连接不上后端。检查一下 AI 服务是否启动了好不好？';
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
    bubble.textContent = text;
  }

  private renderCitations(bubble: HTMLElement, citations: Citation[]): void {
    const existing = bubble.parentElement?.querySelector('.l2d-cites');
    existing?.remove();

    const wrap = document.createElement('div');
    wrap.className = 'l2d-cites';
    for (const citation of citations) {
      const link = document.createElement('a');
      link.className = 'l2d-cite';
      link.href = citation.url;
      link.title = citation.excerpt || citation.title;

      const label = document.createElement('span');
      label.className = 'l2d-cite__label';
      label.textContent = '📖';
      const title = document.createElement('span');
      title.className = 'l2d-cite__title';
      title.textContent = citation.title;
      const go = document.createElement('span');
      go.className = 'l2d-cite__go';
      go.textContent = '跳转 →';

      link.append(label, title, go);
      wrap.appendChild(link);
    }
    bubble.after(wrap);
  }

  private renderAction(bubble: HTMLElement, action: { type: 'navigate'; url: string; label?: string }): void {
    if (action.type !== 'navigate' || !action.url) return;
    const link = document.createElement('a');
    link.className = 'l2d-action';
    link.href = action.url;
    link.textContent = action.label || '带我去看看 →';
    bubble.after(link);
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
