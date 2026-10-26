import * as PIXI from 'pixi.js';
import { Live2DModel } from 'pixi-live2d-display/cubism4';
import type { ModelEntry } from '../types';

// pixi-live2d-display 的交互管理器会读取全局 PIXI
(window as Window).PIXI = PIXI;

export class Live2DManager {
  private app: PIXI.Application;
  private model: Live2DModel | null = null;
  private current: ModelEntry | null = null;
  private observer: ResizeObserver;
  private onMouseMove = (event: MouseEvent) => this.focus(event);

  constructor(
    private readonly canvas: HTMLCanvasElement,
    private readonly assetBase: string,
  ) {
    const parent = canvas.parentElement;
    if (!parent) throw new Error('Live2D canvas 缺少父容器');

    this.app = new PIXI.Application({
      view: canvas,
      transparent: true,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
      resizeTo: parent,
    });

    this.observer = new ResizeObserver(() => this.layout());
    this.observer.observe(parent);
    window.addEventListener('mousemove', this.onMouseMove, { passive: true });
  }

  async load(entry: ModelEntry): Promise<void> {
    const url = new URL(`${this.assetBase}models/${entry.path}`, window.location.href).href;
    const model = await Live2DModel.from(url, { autoInteract: false, autoUpdate: true });

    if (this.model) {
      this.app.stage.removeChild(this.model);
      this.model.destroy();
    }

    this.model = model;
    this.current = entry;
    this.app.stage.addChild(model as unknown as PIXI.DisplayObject);
    this.layout();
    this.playIdle();
  }

  get currentModelId(): string | null {
    return this.current?.id ?? null;
  }

  destroy(): void {
    this.observer.disconnect();
    window.removeEventListener('mousemove', this.onMouseMove);
    this.model?.destroy();
    this.model = null;
    this.app.destroy(false, { children: true, texture: true, baseTexture: true });
  }

  private layout(): void {
    const model = this.model;
    const entry = this.current;
    if (!model || !entry) return;

    const width = this.app.renderer.screen.width;
    const height = this.app.renderer.screen.height;
    const bounds = model.getLocalBounds();
    if (!bounds.height) return;

    model.anchor.set(0.5, 1);
    // 以 86% 容器高度为基准，按模型自带比例缩放。
    // 留出顶部空间，形象不会顶到浏览器边缘，也更少遮挡正文。
    const scale = ((height * 0.86) / bounds.height) * (entry.scale || 1);
    model.scale.set(scale);
    model.x = width / 2 + entry.offsetX;
    model.y = height + entry.offsetY;
  }

  private playIdle(): void {
    const model = this.model as unknown as {
      motion?: (group: string) => Promise<boolean> | undefined;
      internalModel?: { motionManager?: { groups?: { idle?: string } } };
    } | null;
    if (!model?.motion) return;
    const idleGroup = model.internalModel?.motionManager?.groups?.idle ?? 'Idle';
    Promise.resolve(model.motion(idleGroup)).catch(() => {
      // 模型没有 idle 动作时忽略
    });
  }

  private focus(event: MouseEvent): void {
    if (!this.model) return;
    try {
      // PIXI 的世界坐标以画布左上角为原点，需要把视口坐标换算到画布内
      const rect = this.canvas.getBoundingClientRect();
      this.model.focus(event.clientX - rect.left, event.clientY - rect.top);
    } catch {
      // 部分模型不支持视线跟随
    }
  }
}
