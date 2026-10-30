/**
 * Spine 形象渲染器。
 *
 * 为什么不直接塞进 PixiJS？
 * - 项目的 Live2D 依赖 PixiJS 6.5.10，而官方 `spine-pixi` 运行时面向 Pixi 7/8；
 *   为了一棵骨骼树升级整个 Pixi，会连带影响 Live2D，风险不值得。
 * - `@esotericsoftware/spine-player` 自己创建 canvas 和 WebGL 上下文，
 *   只要给它一个独立的父容器，就能和 Live2D 的 canvas 完全隔离。
 *
 * 这个类只负责“加载一个 Spine 模型并循环播放”。聊天、语音、额度都不在这里。
 * 以后要加口型/表情时，可以从这里暴露 `setAnimation()`，不影响上层 UI。
 */
import { SpinePlayer, type SpinePlayerConfig } from '@esotericsoftware/spine-player';
import type { SpineModelEntry } from '../types';

function assetUrl(base: string, relativePath: string): string {
  // 模型路径里可能有中文、空格。URL 构造函数会负责编码，
  // 直接拼字符串在 Nginx 上容易 404。
  //
  // base 可能来自 WordPress 的完整 URL，也可能是本地开发的相对路径
  // （`./spine/`）。`new URL(relative, base)` 要求 base 是绝对地址，
  // 所以先相对当前页面解析一次。
  const absoluteBase = new URL(base, window.location.href);
  return new URL(relativePath, absoluteBase).href;
}

export class SpineManager {
  private player: SpinePlayer | null = null;

  constructor(
    private readonly container: HTMLElement,
    private readonly assetBase: string,
  ) {}

  /**
   * 加载并开始循环。
   *
   * 失败时抛出的错误由调用方决定是否回退到 Live2D；这里只保证
   * 已经创建的 canvas 被清掉，不会在页面上留下半张模型。
   */
  async load(entry: SpineModelEntry): Promise<void> {
    this.destroy();
    this.container.hidden = false;

    const config: SpinePlayerConfig = {
      jsonUrl: assetUrl(this.assetBase, `models/${entry.path}`),
      atlasUrl: assetUrl(this.assetBase, `models/${entry.atlas}`),
      // 透明背景：模型直接浮在博客页面上，不能带一块黑色/白色底板。
      alpha: true,
      backgroundColor: '00000000',
      fullScreenBackgroundColor: '00000000',
      premultipliedAlpha: true,
      // 访客不需要 Spine 官方的播放控件；交互也关掉，避免和聊天按钮抢点击。
      showControls: false,
      showLoading: false,
      interactive: false,
      preserveDrawingBuffer: false,
      mipmaps: false,
      // 胸像/半身模型在窄高的容器里容易被裁掉，留一点边距。
      viewport: {
        padLeft: '4%',
        padRight: '4%',
        padTop: '3%',
        padBottom: '1%',
      },
      // 动画名在构建清单时已经选好；空字符串表示只显示静默姿势。
      animation: entry.animation || undefined,
      success: (player) => {
        this.player = player;
        // 只有明确要求“不循环”时才重设；默认 setAnimation 就是循环。
        // 在 success 里设置后，player 自己的初始化不会再把循环状态改回去。
        if (!entry.loop && entry.animation) {
          player.setAnimation(entry.animation, false);
        }
        this.applyTransform(entry);
      },
      error: (_player, message) => {
        // SpinePlayer 在某些路径上会先调用 error 再抛异常。
        // 这里只记录，真正的 reject 由下面的同步异常或加载失败统一处理。
        console.error('[l2d-ai] Spine 加载失败', message);
      },
    };

    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const finish = (fn: () => void) => {
        if (settled) return;
        settled = true;
        fn();
      };

      try {
        this.player = new SpinePlayer(this.container, {
          ...config,
          success: (player) => {
            config.success?.(player);
            finish(resolve);
          },
          error: (player, message) => {
            config.error?.(player, message);
            finish(() => reject(new Error(message)));
          },
        });
      } catch (error) {
        this.destroy();
        finish(() => reject(error));
      }
    });
  }

  /**
   * `model.json` 里的偏移和缩放。
   *
   * SpinePlayer 自己会按“完整画面”等比缩放，宽幅模型放进窄容器时会显得小。
   * 这里用 CSS transform 做最后一点微调：`scale > 1` 放大并裁掉边缘，
   * `offsetX/offsetY` 把人物移回容器中间。只影响显示，不改骨骼坐标。
   */
  private applyTransform(entry: SpineModelEntry): void {
    const dom = this.player?.dom;
    if (!dom) return;

    const transforms: string[] = [];
    if (entry.offsetX || entry.offsetY) {
      transforms.push(`translate(${entry.offsetX}px, ${entry.offsetY}px)`);
    }
    if (entry.scale && entry.scale !== 1 && Number.isFinite(entry.scale)) {
      transforms.push(`scale(${entry.scale})`);
    }
    if (!transforms.length) return;

    dom.style.transform = transforms.join(' ');
    dom.style.transformOrigin = 'center bottom';
  }

  destroy(): void {
    const player = this.player;
    this.player = null;
    if (player) {
      try {
        player.dispose();
      } catch (error) {
        console.warn('[l2d-ai] Spine dispose 失败', error);
      }
    }
    // 构造失败时 player 还没赋值，但 DOM 已经塞进容器了；一并清干净。
    this.container.replaceChildren();
  }
}
