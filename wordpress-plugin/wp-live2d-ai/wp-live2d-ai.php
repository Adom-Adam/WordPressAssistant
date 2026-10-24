<?php
/**
 * Plugin Name:       Live2D AI 小助手
 * Plugin URI:        https://github.com/Adom-Adam/WordPressAssistant
 * Description:       在博客左下角显示 Live2D AI 助手。前端只负责展示与交互，AI 逻辑由独立的 Python 服务提供。
 * Version:           0.1.0
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            Adom
 * Text Domain:       wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Plugin {

	const VERSION          = '0.1.0';
	const OPTION_API_BASE  = 'l2d_ai_api_base';
	const SETTINGS_GROUP   = 'l2d_ai_settings';

	public static function init() {
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'enqueue_assets' ), 20 );
		add_action( 'admin_menu', array( __CLASS__, 'add_settings_page' ) );
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
		add_filter( 'plugin_action_links_' . plugin_basename( __FILE__ ), array( __CLASS__, 'action_links' ) );
	}

	/**
	 * 前端资源。只有构建产物存在时才注入，避免开发环境报 404。
	 */
	public static function enqueue_assets() {
		if ( ! apply_filters( 'l2d_ai_should_load', true ) ) {
			return;
		}

		$app_dir = plugin_dir_path( __FILE__ ) . 'assets/app/';
		if ( ! is_readable( $app_dir . 'app.js' ) ) {
			return;
		}

		$app_url = plugin_dir_url( __FILE__ ) . 'assets/app/';
		$css     = $app_dir . 'app.css';

		if ( file_exists( $css ) ) {
			wp_enqueue_style( 'l2d-ai-app', $app_url . 'app.css', array(), self::VERSION );
		}

		// Cubism Core 必须在业务脚本之前加载。
		wp_enqueue_script(
			'l2d-ai-cubism-core',
			$app_url . 'live2d/core/live2dcubismcore.min.js',
			array(),
			null,
			true
		);

		wp_enqueue_script(
			'l2d-ai-app',
			$app_url . 'app.js',
			array( 'l2d-ai-cubism-core' ),
			self::VERSION,
			true
		);

		$config = apply_filters(
			'l2d_ai_frontend_config',
			array(
				'apiBase'   => self::api_base(),
				'assetBase' => $app_url . 'live2d/',
				'title'     => get_bloginfo( 'name' ),
				'voiceLang' => 0 === strpos( get_locale(), 'zh' ) ? 'zh-CN' : 'en-US',
			)
		);

		wp_add_inline_script(
			'l2d-ai-app',
			'window.L2D_AI_CONFIG = ' . wp_json_encode( $config ) . ';',
			'before'
		);
	}

	public static function api_base() {
		$value = get_option( self::OPTION_API_BASE, '/ai-api' );
		return untrailingslashit( trim( (string) $value ) );
	}

	public static function sanitize_api_base( $value ) {
		$value = trim( (string) $value );
		if ( '' === $value ) {
			return '/ai-api';
		}
		if ( 0 === strpos( $value, 'http://' ) || 0 === strpos( $value, 'https://' ) ) {
			return esc_url_raw( untrailingslashit( $value ) );
		}
		// 允许同域相对路径，例如 /ai-api
		return '/' . ltrim( sanitize_text_field( $value ), '/' );
	}

	public static function add_settings_page() {
		add_options_page(
			'Live2D 小助手',
			'Live2D 小助手',
			'manage_options',
			'l2d-ai',
			array( __CLASS__, 'render_settings_page' )
		);
	}

	public static function register_settings() {
		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_API_BASE,
			array(
				'type'              => 'string',
				'sanitize_callback' => array( __CLASS__, 'sanitize_api_base' ),
				'default'           => '/ai-api',
			)
		);
	}

	public static function render_settings_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}
		?>
		<div class="wrap">
			<h1>Live2D 小助手</h1>
			<form method="post" action="options.php">
				<?php settings_fields( self::SETTINGS_GROUP ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row">
							<label for="l2d-ai-api-base">AI 服务地址</label>
						</th>
						<td>
							<input
								type="text"
								id="l2d-ai-api-base"
								name="<?php echo esc_attr( self::OPTION_API_BASE ); ?>"
								value="<?php echo esc_attr( self::api_base() ); ?>"
								class="regular-text"
								placeholder="/ai-api"
							/>
							<p class="description">
								默认 <code>/ai-api</code>：由 Nginx 把该路径反向代理到 Python 服务
								（例如 <code>http://127.0.0.1:8000/api/</code>）。<br />
								也可以直接填完整地址，例如 <code>https://ai.example.com/api</code>（需要后端配置 CORS）。
							</p>
						</td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>

			<h2>模型放在哪里？</h2>
			<p>
				模型文件由构建流程复制到插件目录：<br />
				<code><?php echo esc_html( plugin_dir_path( __FILE__ ) . 'assets/app/live2d/models/' ); ?></code>
			</p>
			<p class="description">
				更推荐的做法：把模型放进仓库的 <code>frontend/public/live2d/models/</code>，
				然后执行 <code>pnpm build</code>，构建会自动把它们同步过来。
			</p>
		</div>
		<?php
	}

	public static function action_links( $links ) {
		$settings = sprintf(
			'<a href="%s">设置</a>',
			esc_url( admin_url( 'options-general.php?page=l2d-ai' ) )
		);
		array_unshift( $links, $settings );
		return $links;
	}
}

L2D_AI_Plugin::init();
