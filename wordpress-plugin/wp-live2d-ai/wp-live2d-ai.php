<?php
/**
 * Plugin Name:       Live2D AI 小助手
 * Plugin URI:        https://github.com/Adom-Adam/WordPressAssistant
 * Description:       在博客左下角显示 Live2D AI 助手。前端只负责展示与交互，AI 逻辑由独立的 Python 服务提供。
 * Version:           0.2.0
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            Adom
 * Text Domain:       wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Plugin {

	const VERSION          = '0.2.0';
	const OPTION_API_BASE  = 'l2d_ai_api_base';
	const SETTINGS_GROUP   = 'l2d_ai_settings';
	const REST_NAMESPACE   = 'l2d-ai/v1';
	const JWT_TTL          = 900; // 15 分钟；过期后前端重新向 WordPress 换新 token。
	const JWT_ISSUER       = 'l2d-ai-wordpress';
	const JWT_AUDIENCE     = 'l2d-ai-backend';

	public static function init() {
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'enqueue_assets' ), 20 );
		add_action( 'rest_api_init', array( __CLASS__, 'register_routes' ) );
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
				'auth'      => self::auth_config(),
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

	/**
	 * 当前页面地址，用于登录后回到原文章。
	 *
	 * 用 $wp->request 而不是原始 REQUEST_URI：后者已经包含
	 * WordPress 的安装子目录，再交给 home_url() 会重复拼接。
	 */
	private static function current_url() {
		global $wp;
		if ( $wp instanceof WP && '' !== (string) $wp->request ) {
			return esc_url_raw( home_url( '/' . ltrim( (string) $wp->request, '/' ) ) );
		}
		return esc_url_raw( home_url( '/' ) );
	}

	/**
	 * 注入前端的登录态。这里只放公开信息：
	 * user id、显示名、是否管理员，以及登录/注册/退出链接和 REST nonce。
	 * JWT 密钥绝不进入前端。
	 */
	public static function auth_config() {
		$logged_in = is_user_logged_in();
		$user      = $logged_in ? wp_get_current_user() : null;
		$user_data = null;

		if ( $user instanceof WP_User ) {
			$user_data = array(
				'id'      => (int) $user->ID,
				'name'    => (string) $user->display_name,
				'isAdmin' => current_user_can( 'manage_options' ),
			);
		}

		return array(
			'required'    => (bool) apply_filters( 'l2d_ai_auth_required', true ),
			'loggedIn'    => $logged_in,
			'user'        => $user_data,
			'loginUrl'    => wp_login_url( self::current_url() ),
			'registerUrl' => get_option( 'users_can_register' ) ? wp_registration_url() : '',
			'logoutUrl'   => wp_logout_url( self::current_url() ),
			'tokenUrl'    => esc_url_raw( rest_url( self::REST_NAMESPACE . '/token' ) ),
			'nonce'       => wp_create_nonce( 'wp_rest' ),
		);
	}

	/**
	 * 注册换取短时 JWT 的 REST 接口。
	 */
	public static function register_routes() {
		register_rest_route(
			self::REST_NAMESPACE,
			'/token',
			array(
				'methods'             => WP_REST_Server::CREATABLE,
				'callback'            => array( __CLASS__, 'issue_token' ),
				'permission_callback' => array( __CLASS__, 'token_permission' ),
			)
		);
	}

	/**
	 * 只有已登录且带有效 REST nonce 的请求才能换 token。
	 */
	public static function token_permission( WP_REST_Request $request ) {
		if ( ! is_user_logged_in() ) {
			return new WP_Error(
				'l2d_ai_not_logged_in',
				'请先登录',
				array( 'status' => 401 )
			);
		}

		$nonce = $request->get_header( 'X-WP-Nonce' );
		if ( ! $nonce || ! wp_verify_nonce( $nonce, 'wp_rest' ) ) {
			return new WP_Error(
				'l2d_ai_bad_nonce',
				'登录状态已过期，请刷新页面',
				array( 'status' => 403 )
			);
		}

		return true;
	}

	/**
	 * 签发 JWT。token 只证明“这个请求来自已登录的某个 WordPress 用户”，
	 * 真正的额度判断在后端做。
	 */
	public static function issue_token() {
		$secret = self::jwt_secret();
		if ( is_wp_error( $secret ) ) {
			return $secret;
		}

		$user = wp_get_current_user();
		if ( ! $user instanceof WP_User || ! $user->ID ) {
			return new WP_Error( 'l2d_ai_no_user', '找不到当前用户', array( 'status' => 401 ) );
		}

		$now  = time();
		$data = array(
			'iss'   => self::JWT_ISSUER,
			'aud'   => self::JWT_AUDIENCE,
			'sub'   => (string) $user->ID,
			'name'  => (string) $user->display_name,
			'admin' => current_user_can( 'manage_options' ),
			'iat'   => $now,
			'exp'   => $now + self::JWT_TTL,
			'jti'   => wp_generate_uuid4(),
		);

		$token = self::encode_jwt( $data, $secret );
		if ( is_wp_error( $token ) ) {
			return $token;
		}

		$response = new WP_REST_Response(
			array(
				'token'      => $token,
				'expires_at' => $now + self::JWT_TTL,
				'user'       => array(
					'id'      => (int) $user->ID,
					'name'    => (string) $user->display_name,
					'isAdmin' => current_user_can( 'manage_options' ),
				),
			),
			200
		);
		$response->header( 'Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0' );
		$response->header( 'Pragma', 'no-cache' );
		return $response;
	}

	/**
	 * 共享密钥必须由 wp-config.php 提供，长度至少 32 字符。
	 * 这里故意不回退到 AUTH_KEY，避免前后端各自猜一个值。
	 */
	private static function jwt_secret() {
		if ( ! defined( 'L2D_AI_JWT_SECRET' ) ) {
			return new WP_Error(
				'l2d_ai_jwt_secret_missing',
				'缺少 L2D_AI_JWT_SECRET，请在 wp-config.php 中配置',
				array( 'status' => 500 )
			);
		}

		$secret = (string) L2D_AI_JWT_SECRET;
		if ( strlen( $secret ) < 32 ) {
			return new WP_Error(
				'l2d_ai_jwt_secret_short',
				'L2D_AI_JWT_SECRET 至少需要 32 个字符',
				array( 'status' => 500 )
			);
		}

		return $secret;
	}

	/**
	 * 最小 HS256 JWT 实现，避免为了一个 token 引入 Composer 依赖。
	 */
	private static function encode_jwt( array $payload, $secret ) {
		$header_json  = wp_json_encode( array( 'alg' => 'HS256', 'typ' => 'JWT' ) );
		$payload_json = wp_json_encode( $payload );

		if ( false === $header_json || false === $payload_json ) {
			return new WP_Error( 'l2d_ai_jwt_encode', '无法生成 JWT', array( 'status' => 500 ) );
		}

		$segments   = array(
			self::base64url_encode( $header_json ),
			self::base64url_encode( $payload_json ),
		);
		$signature  = hash_hmac( 'sha256', implode( '.', $segments ), $secret, true );
		$segments[] = self::base64url_encode( $signature );

		return implode( '.', $segments );
	}

	private static function base64url_encode( $value ) {
		return rtrim( strtr( base64_encode( $value ), '+/', '-_' ), '=' );
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
								（例如 <code>http://127.0.0.1:18080/api/</code>）。<br />
								也可以直接填完整地址，例如 <code>https://ai.example.com/api</code>（需要后端配置 CORS）。
							</p>
						</td>
					</tr>
				</table>
				<?php submit_button(); ?>
			</form>

			<h2>登录凭证</h2>
			<table class="form-table" role="presentation">
				<tr>
					<th scope="row">换取 Token 的接口</th>
					<td>
						<code><?php echo esc_html( rest_url( self::REST_NAMESPACE . '/token' ) ); ?></code>
						<p class="description">
							前端只在已登录时调用它，用 WordPress 的登录 Cookie + REST nonce 换一个 15 分钟有效的 JWT。
							请求头需要带 <code>X-WP-Nonce</code>。
						</p>
					</td>
				</tr>
				<tr>
					<th scope="row">JWT 共享密钥</th>
					<td>
						<?php if ( is_wp_error( self::jwt_secret() ) ) : ?>
							<p style="color:#b32d2e;"><strong>未配置</strong>，现在调用上面的接口会返回 500。</p>
							<p>在 <code>wp-config.php</code> 里、<code>/* That's all, stop editing! */</code> 之前添加：</p>
							<pre>define( 'L2D_AI_JWT_SECRET', '把下面生成的值贴在这里' );</pre>
							<p>生成一个随机值（在服务器终端执行）：</p>
							<pre>openssl rand -hex 32</pre>
							<p class="description">
								同一个值还要写进后端 <code>.env</code> 的 <code>L2D_AI_JWT_SECRET</code>，两处必须完全一致。
								长度至少 32 个字符。
							</p>
						<?php else : ?>
							<p style="color:#1e7b3c;"><strong>已配置</strong>（<?php echo esc_html( (string) strlen( (string) L2D_AI_JWT_SECRET ) ); ?> 个字符）。</p>
							<p class="description">出于安全考虑，这里不显示密钥内容。</p>
						<?php endif; ?>
					</td>
				</tr>
			</table>

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
