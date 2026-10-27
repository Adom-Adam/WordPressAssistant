<?php
/**
 * Plugin Name:       Live2D AI 小助手
 * Plugin URI:        https://github.com/Adom-Adam/WordPressAssistant
 * Description:       在博客左下角显示 Live2D AI 助手。前端只负责展示与交互，AI 逻辑由独立的 Python 服务提供。
 * Version:           0.3.3
 * Requires at least: 6.0
 * Requires PHP:      7.4
 * Author:            Adom
 * Text Domain:       wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Plugin {

	const VERSION          = '0.3.3';
	const OPTION_API_BASE  = 'l2d_ai_api_base';
	const OPTION_NAME      = 'l2d_ai_assistant_name';
	const OPTION_SIZE      = 'l2d_ai_size_preset';
	const OPTION_MODEL     = 'l2d_ai_live2d_model';
	const OPTION_WIDTH     = 'l2d_ai_size_width';
	const OPTION_HEIGHT    = 'l2d_ai_size_height';
	/** 没有单独设置时使用的人设名字。改人设时只需改这里或后台设置。 */
	const DEFAULT_NAME     = '梅林';
	const SETTINGS_GROUP   = 'l2d_ai_settings';
	const REST_NAMESPACE   = 'l2d-ai/v1';
	const JWT_TTL          = 900; // 15 分钟；过期后前端重新向 WordPress 换新 token。
	const JWT_ADMIN_TTL    = 120; // 后台设置页只需要一次请求，有效期越短越安全。
	const JWT_ISSUER       = 'l2d-ai-wordpress';
	const JWT_AUDIENCE     = 'l2d-ai-backend';

	/** 三个预设档位的实际像素尺寸。custom 表示使用管理员填写的宽高。 */
	const SIZE_PRESETS = array(
		'small'  => array( 'width' => 200, 'height' => 360 ),
		'medium' => array( 'width' => 260, 'height' => 460 ),
		'large'  => array( 'width' => 330, 'height' => 600 ),
	);

	public static function init() {
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'enqueue_assets' ), 20 );
		add_action( 'rest_api_init', array( __CLASS__, 'register_routes' ) );
		add_action( 'admin_menu', array( __CLASS__, 'add_settings_page' ) );
		add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
		add_action( 'admin_post_l2d_ai_save_backend_settings', array( __CLASS__, 'save_backend_settings' ) );
		add_action( 'wp_head', array( __CLASS__, 'hide_auth_nav_for_logged_in' ) );
		add_action( 'wp_footer', array( __CLASS__, 'render_pjax_optout_script' ) );
		add_action( 'wp_footer', array( __CLASS__, 'render_auth_nav_script' ) );
		add_filter( 'wp_nav_menu_items', array( __CLASS__, 'append_logout_link' ), 10, 2 );
		// 主题可能在登录后默认把人送进后台个人资料页；这里用较高优先级覆盖。
		add_filter( 'login_redirect', array( __CLASS__, 'login_redirect_to_home' ), 999, 3 );
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
				'apiBase'       => self::api_base(),
				'assetBase'     => $app_url . 'live2d/',
				'assistantName' => self::assistant_name(),
				'voiceLang'     => 0 === strpos( get_locale(), 'zh' ) ? 'zh-CN' : 'en-US',
				'appearance'    => self::appearance_config(),
				'live2dModel'   => self::live2d_model(),
				'auth'          => self::auth_config(),
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
	 * 人设名字，用于对话面板左上角等前台文案。
	 *
	 * 单独抽成选项而不是写死在前端，换人设时只改后台设置即可；
	 * 以后如果还要显示“馆员名”“角色名”之类，也可以复用同一个值。
	 */
	public static function assistant_name() {
		$value = trim( (string) get_option( self::OPTION_NAME, self::DEFAULT_NAME ) );
		return '' !== $value ? $value : self::DEFAULT_NAME;
	}

	public static function sanitize_assistant_name( $value ) {
		$value = trim( sanitize_text_field( (string) $value ) );
		return '' !== $value ? $value : self::DEFAULT_NAME;
	}

	/** 当前档位。只接受四个已知值，写错时回退到 medium。 */
	public static function size_preset() {
		$value = (string) get_option( self::OPTION_SIZE, 'medium' );
		if ( 'custom' === $value || isset( self::SIZE_PRESETS[ $value ] ) ) {
			return $value;
		}
		return 'medium';
	}

	public static function sanitize_size_preset( $value ) {
		$value = sanitize_key( (string) $value );
		if ( 'custom' === $value || isset( self::SIZE_PRESETS[ $value ] ) ) {
			return $value;
		}
		return 'medium';
	}

	public static function size_width() {
		return max( 160, min( 420, (int) get_option( self::OPTION_WIDTH, 260 ) ) );
	}

	public static function sanitize_size_width( $value ) {
		return max( 160, min( 420, absint( $value ) ) );
	}

	public static function size_height() {
		return max( 260, min( 720, (int) get_option( self::OPTION_HEIGHT, 460 ) ) );
	}

	public static function sanitize_size_height( $value ) {
		return max( 260, min( 720, absint( $value ) ) );
	}

	/** 注入前端的尺寸：预设换算成实际像素，custom 才用管理员填写的值。 */
	public static function appearance_config() {
		$preset = self::size_preset();
		if ( 'custom' === $preset ) {
			$width  = self::size_width();
			$height = self::size_height();
		} else {
			$width  = self::SIZE_PRESETS[ $preset ]['width'];
			$height = self::SIZE_PRESETS[ $preset ]['height'];
		}

		return array(
			'preset' => $preset,
			'width'  => $width,
			'height' => $height,
		);
	}

	/**
	 * 读取构建后的 Live2D 模型清单。
	 *
	 * 前台不再让用户选模型，管理员在设置页从这里列出的模型里选一个。
	 * 清单由 `frontend/scripts/gen-model-manifest.mjs` 在构建时生成。
	 */
	private static function live2d_manifest() {
		static $manifest = null;
		if ( is_array( $manifest ) ) {
			return $manifest;
		}

		$path = plugin_dir_path( __FILE__ ) . 'assets/app/live2d/models/manifest.json';
		if ( ! is_readable( $path ) ) {
			$manifest = array();
			return $manifest;
		}

		$data     = json_decode( (string) file_get_contents( $path ), true );
		$manifest = is_array( $data ) ? $data : array();
		return $manifest;
	}

	/** 返回 id => 显示名，供后台下拉框使用。 */
	private static function live2d_models() {
		$manifest = self::live2d_manifest();
		$models   = array();

		if ( empty( $manifest['models'] ) || ! is_array( $manifest['models'] ) ) {
			return $models;
		}

		foreach ( $manifest['models'] as $model ) {
			if ( ! is_array( $model ) || empty( $model['id'] ) ) {
				continue;
			}
			$id   = (string) $model['id'];
			$name = isset( $model['name'] ) && '' !== (string) $model['name']
				? (string) $model['name']
				: $id;

			$models[ $id ] = $name;
		}

		return $models;
	}

	/** 清单里的默认模型；清单丢失时返回空字符串。 */
	private static function live2d_default_model() {
		$models = self::live2d_models();
		if ( ! $models ) {
			return '';
		}

		$manifest = self::live2d_manifest();
		$default  = isset( $manifest['default'] ) ? (string) $manifest['default'] : '';
		if ( '' !== $default && isset( $models[ $default ] ) ) {
			return $default;
		}

		return (string) array_key_first( $models );
	}

	/** 当前生效的模型。保存过的 id 失效时自动回退到清单默认值。 */
	public static function live2d_model() {
		$value  = (string) get_option( self::OPTION_MODEL, '' );
		$models = self::live2d_models();

		if ( '' !== $value && isset( $models[ $value ] ) ) {
			return $value;
		}

		return self::live2d_default_model();
	}

	public static function sanitize_live2d_model( $value ) {
		$value  = sanitize_text_field( (string) $value );
		$models = self::live2d_models();

		return isset( $models[ $value ] ) ? $value : self::live2d_default_model();
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
	 * 登录后默认回到站点首页。
	 *
	 * 为什么需要这个过滤器：
	 * 导航里的登录链接是手工加的 `wp-login.php`，没有带 redirect_to。
	 * WordPress/Argon 在没有目标时可能把用户送进后台个人资料页，
	 * 对普通读者来说很突兀。
	 *
	 * 处理规则：
	 * - 从导航/登录页直接登录：WordPress 登录表单默认把 redirect_to
	 *   填成 /wp-admin/（不是空字符串），这里把它和空目标一起改成首页；
	 * - 从文章页的助手登录门进来时，wp_login_url() 会带 redirect_to，
	 *   仍然回到那篇文章；
	 * - 主题即使拿到了首页/文章目标，仍可能把 $redirect_to 改到后台
	 *   （常见是 profile.php）；这种情况下改回用户原本的目标；
	 * - 用户明确打开后台页面时保持原目标。
	 */
	public static function login_redirect_to_home( $redirect_to, $requested_redirect_to, $user ) {
		if ( is_wp_error( $user ) ) {
			return $redirect_to;
		}

		$requested_path = '';
		if ( is_string( $requested_redirect_to ) && '' !== $requested_redirect_to ) {
			$requested_path = (string) wp_parse_url( $requested_redirect_to, PHP_URL_PATH );
		}

		$redirect_path = is_string( $redirect_to )
			? (string) wp_parse_url( $redirect_to, PHP_URL_PATH )
			: '';

		// 用 admin_url() 的路径，兼容 WordPress 装在子目录的情况。
		$admin_path = untrailingslashit( (string) wp_parse_url( admin_url(), PHP_URL_PATH ) );
		if ( '' === $admin_path ) {
			$admin_path = '/wp-admin';
		}
		$admin_prefix = $admin_path . '/';

		// 登录表单默认目标就是后台首页，对读者来说等于“没有目标”。
		if ( '' === $requested_path || $requested_path === $admin_path || $requested_path === $admin_prefix ) {
			return esc_url_raw( home_url( '/' ) );
		}

		$requested_is_admin = ( 0 === strpos( $requested_path, $admin_prefix ) );
		$redirect_is_admin  = ( 0 === strpos( $redirect_path, $admin_prefix ) );

		// 用户本来要去前台文章/首页，主题却把结果改到后台：
		// 用用户原本的目标覆盖。wp_validate_redirect() 会拦掉外部域名，
		// 非法时退回首页。
		if ( $redirect_is_admin && ! $requested_is_admin ) {
			return wp_validate_redirect( $requested_redirect_to, home_url( '/' ) );
		}

		return $redirect_to;
	}

	/**
	 * 登录后隐藏导航里的“登录 / 注册”。
	 *
	 * 这两个链接是 Argon 菜单里的普通链接，不是主题模板逻辑。
	 * 这里只在登录状态输出 CSS，访客仍然能看到它们；
	 * 选择器排除 action=logout，避免以后主题自带退出链接时被误伤。
	 */
	public static function hide_auth_nav_for_logged_in() {
		if ( ! is_user_logged_in() ) {
			return;
		}

		echo '<style id="l2d-ai-hide-auth-nav">'
			. '.navbar-nav li:has(> a[href*="wp-login.php"]:not([href*="action=logout"])){display:none!important;}'
			. '</style>';
	}

	/**
	 * 在 Argon 主导航末尾追加“个人”和“退出”链接。
	 *
	 * 为什么用 wp_logout_url()：
	 * WordPress 的退出动作需要 nonce，直接写 /wp-login.php?action=logout
	 * 会多一步确认页，某些安全插件下还可能失败。
	 *
	 * 为什么不看 menu_class：
	 * Argon 的菜单可能是自定义遍历渲染的，最终 <ul> 的 class
	 * 不一定原样出现在 $args->menu_class 里。这里改为看菜单内容：
	 * 只给包含登录/注册链接的那个菜单追加，避免落到页脚菜单。
	 */
	public static function append_logout_link( $items, $args ) {
		if ( ! is_user_logged_in() ) {
			return $items;
		}

		if ( false === strpos( $items, 'wp-login.php' ) ) {
			return $items;
		}

		// `no-pjax` 是 Argon 主题的“别接管这个链接”标记。
		// 不加的话，主题会用 AJAX 局部刷新 wp-login.php，
		// 第一次点击看起来毫无反应，第二次才落到确认页。
		$account_link = '<li class="nav-item l2d-ai-account">'
			. '<a class="nav-link" no-pjax href="' . esc_url( admin_url( 'profile.php' ) ) . '">'
			. '<span class="nav-link-inner--text">个人</span>'
			. '</a></li>';
		$logout_link  = '<li class="nav-item l2d-ai-logout">'
			. '<a class="nav-link" no-pjax href="' . esc_url( wp_logout_url( home_url( '/' ) ) ) . '">'
			. '<span class="nav-link-inner--text">退出</span>'
			. '</a></li>';

		if ( false === strpos( $items, 'l2d-ai-account' ) ) {
			$items .= $account_link;
		}
		if ( false === strpos( $items, 'action=logout' ) ) {
			$items .= $logout_link;
		}

		return $items;
	}

	/**
	 * 让登录/退出链接绕开 Argon 的 PJAX。
	 *
	 * Argon 的主题脚本会拦截页面上所有普通 `<a>`：
	 * `$(document).pjax("a[href]:not([no-pjax])...")`。
	 * `wp-login.php` 不是普通文章页，PJAX 找不到它要的容器，
	 * 第一次点击看起来“没反应”，第二次才落到确认页。
	 *
	 * 给链接加 `no-pjax` 是主题自己提供的退出开关。这里对访客和登录用户
	 * 都输出一次，覆盖主题菜单里原本就有的登录/注册链接。
	 */
	public static function render_pjax_optout_script() {
		?>
		<script id="l2d-ai-pjax-optout">
		(function () {
			document.querySelectorAll('a[href*="wp-login.php"]').forEach(function (link) {
				link.setAttribute('no-pjax', '');
			});
		})();
		</script>
		<?php
	}

	/**
	 * 前端兜底：有些主题会自己遍历菜单项，不经过 wp_nav_menu_items。
	 *
	 * 这段脚本只在登录状态输出，做三件事：
	 * 1. 隐藏导航里的登录/注册；
	 * 2. 补“个人”入口；
	 * 3. 补“退出”链接。
	 *
	 * 退出地址由 PHP 生成，仍然带 nonce；脚本本身不拼 URL。
	 */
	public static function render_auth_nav_script() {
		if ( ! is_user_logged_in() ) {
			return;
		}

		$logout_url   = wp_logout_url( home_url( '/' ) );
		$personal_url = admin_url( 'profile.php' );
		?>
		<script id="l2d-ai-auth-nav">
		(function () {
			var logoutUrl = <?php echo wp_json_encode( $logout_url ); ?>;
			var personalUrl = <?php echo wp_json_encode( $personal_url ); ?>;

			function decodeUrl(url) {
				// 有些缓存/优化插件会把内联脚本里的 & 转成 &amp;。
				// 直接拿它当 href，WordPress 会认为 nonce 参数名是
				// amp;_wpnonce，于是先落到“确认退出”页。
				return String(url).replace(/&amp;/gi, '&').replace(/&#0*38;/g, '&');
			}

			function ensureItem(nav, className, url, label) {
				if (nav.querySelector('.' + className)) {
					return;
				}

				var item = document.createElement('li');
				item.className = 'nav-item ' + className;

				var link = document.createElement('a');
				link.className = 'nav-link';
				// 关键：告诉 Argon 的 PJAX 不要接管这个链接。
				link.setAttribute('no-pjax', '');
				link.href = decodeUrl(url);

				var text = document.createElement('span');
				text.className = 'nav-link-inner--text';
				text.textContent = label;

				link.appendChild(text);
				item.appendChild(link);

				// “个人”放在“退出”前面；如果退出已经存在，就插在它前面。
				var logout = nav.querySelector('.l2d-ai-logout');
				if (logout && className !== 'l2d-ai-logout') {
					nav.insertBefore(item, logout);
				} else {
					nav.appendChild(item);
				}
			}

			function updateNav(nav) {
				// 已经存在的退出链接（主题自带或 PHP 过滤器加的）统一换成
				// 本次页面加载生成的新地址。这样即使主题漏了 nonce，
				// 或缓存/优化插件把 & 二次转义，第一次点击也能直接退出。
				// 先修好 href，下面的“隐藏登录”才知道它是退出、不该隐藏。
				nav.querySelectorAll('a[href*="action=logout"]').forEach(function (link) {
					link.href = decodeUrl(logoutUrl);
					// 主题可能自己加过退出链接；一并标记为不交给 PJAX。
					link.setAttribute('no-pjax', '');
					var item = link.closest('li');
					if (item) {
						item.classList.add('l2d-ai-logout');
					}
				});

				// 隐藏登录/注册；保留退出。
				nav.querySelectorAll('a[href*="wp-login.php"]').forEach(function (link) {
					if (link.href.indexOf('action=logout') !== -1) {
						return;
					}
					var item = link.closest('li');
					if (item) {
						item.style.display = 'none';
					}
				});

				ensureItem(nav, 'l2d-ai-account', personalUrl, '个人');
				ensureItem(nav, 'l2d-ai-logout', logoutUrl, '退出');
			}

			function init() {
				var navs = document.querySelectorAll('.navbar-nav-hover');
				if (!navs.length) {
					// 没找到 Argon 的主导航类名时，只选包含登录链接的菜单，
					// 避免把“退出”挂到搜索框旁边的空导航上。
					navs = [];
					document.querySelectorAll('.navbar-nav').forEach(function (nav) {
						if (nav.querySelector('a[href*="wp-login.php"]')) {
							navs.push(nav);
						}
					});
				}
				navs.forEach(updateNav);
			}

			if (document.readyState === 'loading') {
				document.addEventListener('DOMContentLoaded', init);
			} else {
				init();
			}
		})();
		</script>
		<?php
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

	/**
	 * 把后台的相对路径拼成完整 URL。
	 *
	 * `/ai-api` 这种相对地址走 Nginx 反代；也允许管理员直接填完整地址。
	 * 这里由 WordPress 服务器自己发请求，所以完整地址必须是服务器能访问到的。
	 */
	private static function backend_url( $path ) {
		$base = self::api_base();
		if ( preg_match( '#^https?://#i', $base ) ) {
			return untrailingslashit( $base ) . $path;
		}
		return home_url( '/' . ltrim( $base, '/' ) . $path );
	}

	/**
	 * 后台专用短时 JWT：2 分钟有效，只用于 WordPress 服务器 → Python 后端。
	 * 浏览器完全接触不到这个 token。
	 */
	private static function admin_token() {
		$secret = self::jwt_secret();
		if ( is_wp_error( $secret ) ) {
			return $secret;
		}

		$user = wp_get_current_user();
		if ( ! $user instanceof WP_User || ! $user->ID ) {
			return new WP_Error( 'l2d_ai_no_user', '找不到当前用户', array( 'status' => 401 ) );
		}
		if ( ! current_user_can( 'manage_options' ) ) {
			return new WP_Error( 'l2d_ai_not_admin', '需要管理员权限', array( 'status' => 403 ) );
		}

		$now   = time();
		$token = self::encode_jwt(
			array(
				'iss'   => self::JWT_ISSUER,
				'aud'   => self::JWT_AUDIENCE,
				'sub'   => (string) $user->ID,
				'name'  => (string) $user->display_name,
				'admin' => true,
				'iat'   => $now,
				'exp'   => $now + self::JWT_ADMIN_TTL,
				'jti'   => wp_generate_uuid4(),
			),
			$secret
		);

		return $token;
	}

	/**
	 * 调用后端接口。成功返回数组，失败返回 WP_Error。
	 * 错误信息会带上 HTTP 状态码和后端的 detail，方便在设置页直接看到原因。
	 */
	private static function request_backend( $method, $path, $body = null ) {
		$token = self::admin_token();
		if ( is_wp_error( $token ) ) {
			return $token;
		}

		$args = array(
			'method'  => $method,
			'timeout' => 8,
			'headers' => array(
				'Authorization' => 'Bearer ' . $token,
				'Accept'        => 'application/json',
			),
		);
		if ( null !== $body ) {
			$args['headers']['Content-Type'] = 'application/json';
			$args['body']                    = wp_json_encode( $body );
		}

		$response = wp_remote_request( self::backend_url( $path ), $args );
		if ( is_wp_error( $response ) ) {
			return new WP_Error(
				'l2d_ai_backend_unreachable',
				'无法连接 AI 服务：' . $response->get_error_message()
			);
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		$raw  = (string) wp_remote_retrieve_body( $response );
		$data = json_decode( $raw, true );

		if ( $code < 200 || $code >= 300 ) {
			$detail = '';
			if ( is_array( $data ) && isset( $data['detail'] ) ) {
				if ( is_string( $data['detail'] ) ) {
					$detail = $data['detail'];
				} elseif ( is_array( $data['detail'] ) ) {
					// Pydantic 的 422 是 [{loc, msg, ...}]，只取可读的 msg。
					$messages = array();
					foreach ( $data['detail'] as $item ) {
						if ( is_array( $item ) && isset( $item['msg'] ) ) {
							$messages[] = (string) $item['msg'];
						}
					}
					$detail = implode( '；', $messages );
				}
			}
			return new WP_Error(
				'l2d_ai_backend_error',
				sprintf( 'AI 服务返回 %d%s', $code, '' !== $detail ? '：' . $detail : '' ),
				array( 'status' => $code )
			);
		}

		if ( ! is_array( $data ) ) {
			return new WP_Error( 'l2d_ai_backend_json', 'AI 服务返回的内容不是 JSON' );
		}

		return $data;
	}

	/** 读取后端当前设置。短缓存 30 秒，避免每次刷新设置页都多打一次后端。 */
	private static function backend_snapshot( $force = false ) {
		$cache_key = 'l2d_ai_backend_snapshot_' . md5( self::api_base() );
		if ( ! $force ) {
			$cached = get_transient( $cache_key );
			if ( is_array( $cached ) ) {
				return $cached;
			}
		}

		$data = self::request_backend( 'GET', '/admin/settings' );
		if ( is_wp_error( $data ) ) {
			return $data;
		}
		set_transient( $cache_key, $data, 30 );
		return $data;
	}

	private static function clear_backend_cache() {
		delete_transient( 'l2d_ai_backend_snapshot_' . md5( self::api_base() ) );
	}

	private static function notice_key() {
		return 'l2d_ai_admin_notice_' . get_current_user_id();
	}

	private static function settings_page_url( array $args = array() ) {
		return add_query_arg(
			array_merge( array( 'page' => 'l2d-ai' ), $args ),
			admin_url( 'options-general.php' )
		);
	}

	/**
	 * 保存后端运营参数。
	 *
	 * 表单提交到 admin-post.php，而不是 options.php：
	 * 这些值不在 WordPress 的 options 表里，而是保存在 Python 后端的 JSON 文件，
	 * 所以需要 WordPress 服务器自己带 admin JWT 去 PUT。
	 */
	public static function save_backend_settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_save_backend_settings' );

		// 第一层校验：在 WordPress 这边就给出清楚的中文错误，避免把明显不合法的值送出去。
		$rules = array(
			'daily_quota'           => array( 0, 1000 ),
			'global_daily_quota'    => array( 0, 1000000 ),
			'agent_max_model_calls' => array( 1, 20 ),
			'agent_max_tool_calls'  => array( 1, 20 ),
			'agent_max_seconds'     => array( 5, 300 ),
		);

		$patch = array();
		foreach ( $rules as $key => $range ) {
			$raw = isset( $_POST[ $key ] ) ? trim( (string) wp_unslash( $_POST[ $key ] ) ) : '';
			if ( '' === $raw || ! preg_match( '/^\d+$/', $raw ) ) {
				self::redirect_with_error( $key . ' 必须是非负整数' );
			}
			$value = (int) $raw;
			if ( $value < $range[0] || $value > $range[1] ) {
				self::redirect_with_error(
					sprintf( '%s 必须在 %d 到 %d 之间', $key, $range[0], $range[1] )
				);
			}
			$patch[ $key ] = $value;
		}

		$model = isset( $_POST['model'] ) ? trim( (string) wp_unslash( $_POST['model'] ) ) : '';
		if ( ! preg_match( '/^[A-Za-z0-9._:-]{1,100}$/', $model ) ) {
			self::redirect_with_error( '模型名只能包含字母、数字、点、下划线、冒号和连字符，最长 100 字符' );
		}
		$patch['model'] = $model;

		$result = self::request_backend( 'PUT', '/admin/settings', $patch );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_error( $result->get_error_message() );
		}

		self::clear_backend_cache();
		wp_safe_redirect( self::settings_page_url( array( 'l2d-ai-saved' => '1' ) ) );
		exit;
	}

	private static function redirect_with_error( $message ) {
		set_transient( self::notice_key(), (string) $message, 60 );
		wp_safe_redirect( self::settings_page_url() );
		exit;
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

		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_NAME,
			array(
				'type'              => 'string',
				'sanitize_callback' => array( __CLASS__, 'sanitize_assistant_name' ),
				'default'           => self::DEFAULT_NAME,
			)
		);

		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_SIZE,
			array(
				'type'              => 'string',
				'sanitize_callback' => array( __CLASS__, 'sanitize_size_preset' ),
				'default'           => 'medium',
			)
		);

		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_MODEL,
			array(
				'type'              => 'string',
				'sanitize_callback' => array( __CLASS__, 'sanitize_live2d_model' ),
				'default'           => '',
			)
		);

		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_WIDTH,
			array(
				'type'              => 'integer',
				'sanitize_callback' => array( __CLASS__, 'sanitize_size_width' ),
				'default'           => 260,
			)
		);

		register_setting(
			self::SETTINGS_GROUP,
			self::OPTION_HEIGHT,
			array(
				'type'              => 'integer',
				'sanitize_callback' => array( __CLASS__, 'sanitize_size_height' ),
				'default'           => 460,
			)
		);
	}

	public static function render_settings_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$notice = get_transient( self::notice_key() );
		if ( $notice ) {
			delete_transient( self::notice_key() );
		}

		// “重新读取”链接会带上 l2d-ai-retry，跳过 30 秒缓存。
		$snapshot = self::backend_snapshot( isset( $_GET['l2d-ai-retry'] ) );
		$saved    = isset( $_GET['l2d-ai-saved'] ) && '1' === $_GET['l2d-ai-saved'];
		?>
		<div class="wrap">
			<h1>Live2D 小助手</h1>

			<?php if ( $saved ) : ?>
				<div class="notice notice-success is-dismissible">
					<p>后端运营参数已保存，立即生效，不需要重启服务。</p>
				</div>
			<?php endif; ?>

			<?php if ( $notice ) : ?>
				<div class="notice notice-error">
					<p><strong>保存失败：</strong><?php echo esc_html( $notice ); ?></p>
				</div>
			<?php endif; ?>

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
					<tr>
						<th scope="row">
							<label for="l2d-ai-assistant-name">人设名字</label>
						</th>
						<td>
							<input
								type="text"
								id="l2d-ai-assistant-name"
								name="<?php echo esc_attr( self::OPTION_NAME ); ?>"
								value="<?php echo esc_attr( self::assistant_name() ); ?>"
								class="regular-text"
								placeholder="<?php echo esc_attr( self::DEFAULT_NAME ); ?>"
							/>
							<p class="description">
								显示在对话面板左上角。改成人设的名字即可；留空会恢复默认的“<?php echo esc_html( self::DEFAULT_NAME ); ?>”。
							</p>
						</td>
					</tr>
					<tr>
						<th scope="row">Live2D 形象大小</th>
						<td>
							<select name="<?php echo esc_attr( self::OPTION_SIZE ); ?>" id="l2d-ai-size-preset">
								<?php
								$presets = array(
									'small'  => '小（200 × 360）',
									'medium' => '中（260 × 460）',
									'large'  => '大（330 × 600）',
									'custom' => '自定义',
								);
								foreach ( $presets as $value => $label ) :
									?>
									<option value="<?php echo esc_attr( $value ); ?>" <?php selected( self::size_preset(), $value ); ?>>
										<?php echo esc_html( $label ); ?>
									</option>
								<?php endforeach; ?>
							</select>
							<p class="description">
								预设档位会覆盖下面的宽高；只有选择“自定义”时才使用手填数值。
								修改后访客刷新页面即可看到。
							</p>
							<p>
								<label for="l2d-ai-size-width">宽度</label>
								<input
									type="number"
									id="l2d-ai-size-width"
									name="<?php echo esc_attr( self::OPTION_WIDTH ); ?>"
									value="<?php echo esc_attr( self::size_width() ); ?>"
									min="160"
									max="420"
									step="1"
									class="small-text"
								/>
								px
								&nbsp;&nbsp;
								<label for="l2d-ai-size-height">高度</label>
								<input
									type="number"
									id="l2d-ai-size-height"
									name="<?php echo esc_attr( self::OPTION_HEIGHT ); ?>"
									value="<?php echo esc_attr( self::size_height() ); ?>"
									min="260"
									max="720"
									step="1"
									class="small-text"
								/>
								px
							</p>
							<p class="description">自定义范围：宽 160–420 px，高 260–720 px。手机端会自动缩小。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">
							<label for="l2d-ai-live2d-model">Live2D 形象</label>
						</th>
						<td>
							<?php $live2d_models = self::live2d_models(); ?>
							<?php if ( $live2d_models ) : ?>
								<select name="<?php echo esc_attr( self::OPTION_MODEL ); ?>" id="l2d-ai-live2d-model">
									<?php foreach ( $live2d_models as $model_id => $model_name ) : ?>
										<option value="<?php echo esc_attr( $model_id ); ?>" <?php selected( self::live2d_model(), $model_id ); ?>>
											<?php echo esc_html( $model_name ); ?>
										</option>
									<?php endforeach; ?>
								</select>
								<p class="description">
									所有访客都会看到这个形象；前台不再提供“更换模型”按钮。
									模型清单来自构建后的 <code>manifest.json</code>。
								</p>
							<?php else : ?>
								<p class="description">
									没有找到 <code>assets/app/live2d/models/manifest.json</code>。
									先执行 <code>pnpm --dir frontend build</code>，再刷新此页。
								</p>
							<?php endif; ?>
						</td>
					</tr>
				</table>
				<?php submit_button( '保存显示设置' ); ?>
			</form>

			<hr />

			<h2>聊天额度与运行参数</h2>
			<?php if ( is_wp_error( $snapshot ) ) : ?>
				<div class="notice notice-error inline">
					<p><strong>读取后端设置失败：</strong><?php echo esc_html( $snapshot->get_error_message() ); ?></p>
				</div>
				<p class="description">
					请依次检查：AI 服务地址是否正确、Python 服务是否在运行、
					<code>L2D_AI_JWT_SECRET</code> 在 <code>wp-config.php</code> 与后端 <code>.env</code> 中是否完全一致。
				</p>
				<p>
					<a class="button" href="<?php echo esc_url( self::settings_page_url( array( 'l2d-ai-retry' => '1' ) ) ); ?>">重新读取</a>
				</p>
			<?php else : ?>
				<?php
				$settings = isset( $snapshot['settings'] ) && is_array( $snapshot['settings'] ) ? $snapshot['settings'] : array();
				$defaults = isset( $snapshot['defaults'] ) && is_array( $snapshot['defaults'] ) ? $snapshot['defaults'] : array();
				$backend  = isset( $snapshot['backend'] ) && is_array( $snapshot['backend'] ) ? $snapshot['backend'] : array();

				$get = static function ( $array, $key, $fallback = '' ) {
					return array_key_exists( $key, $array ) ? $array[ $key ] : $fallback;
				};
				?>
				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
					<input type="hidden" name="action" value="l2d_ai_save_backend_settings" />
					<?php wp_nonce_field( 'l2d_ai_save_backend_settings' ); ?>
					<table class="form-table" role="presentation">
						<tr>
							<th scope="row">
								<label for="l2d-ai-daily-quota">普通用户每日额度</label>
							</th>
							<td>
								<input
									type="number"
									id="l2d-ai-daily-quota"
									name="daily_quota"
									value="<?php echo esc_attr( $get( $settings, 'daily_quota', 0 ) ); ?>"
									min="0"
									max="1000"
									step="1"
									class="small-text"
								/>
								条 / 天
								<p class="description">
									每位普通用户每天最多发起多少次聊天；管理员不受限制。
									设为 <code>0</code> 表示所有人（含普通用户）都不能聊天。当前默认：<?php echo esc_html( $get( $defaults, 'daily_quota', '—' ) ); ?>。
								</p>
							</td>
						</tr>
						<tr>
							<th scope="row">
								<label for="l2d-ai-global-quota">全站每日总额度</label>
							</th>
							<td>
								<input
									type="number"
									id="l2d-ai-global-quota"
									name="global_daily_quota"
									value="<?php echo esc_attr( $get( $settings, 'global_daily_quota', 0 ) ); ?>"
									min="0"
									max="1000000"
									step="1"
									class="small-text"
								/>
								条 / 天
								<p class="description">
									所有普通用户合计的每日上限，用来防止额度被刷爆；管理员聊天不计入。
									<strong>设为 <code>0</code> 表示不限制。</strong>
									当前默认：<?php echo esc_html( $get( $defaults, 'global_daily_quota', '—' ) ); ?>。
								</p>
							</td>
						</tr>
						<tr>
							<th scope="row">单次回答内部上限</th>
							<td>
								<label for="l2d-ai-model-calls">模型调用</label>
								<input
									type="number"
									id="l2d-ai-model-calls"
									name="agent_max_model_calls"
									value="<?php echo esc_attr( $get( $settings, 'agent_max_model_calls', 6 ) ); ?>"
									min="1"
									max="20"
									step="1"
									class="small-text"
								/>
								次
								&nbsp;&nbsp;
								<label for="l2d-ai-tool-calls">工具调用</label>
								<input
									type="number"
									id="l2d-ai-tool-calls"
									name="agent_max_tool_calls"
									value="<?php echo esc_attr( $get( $settings, 'agent_max_tool_calls', 6 ) ); ?>"
									min="1"
									max="20"
									step="1"
									class="small-text"
								/>
								次
								<p class="description">
									防止一次提问触发大量重复工具调用而浪费额度。普通问答通常用不到上限。
								</p>
							</td>
						</tr>
						<tr>
							<th scope="row">
								<label for="l2d-ai-max-seconds">单次回答超时</label>
							</th>
							<td>
								<input
									type="number"
									id="l2d-ai-max-seconds"
									name="agent_max_seconds"
									value="<?php echo esc_attr( $get( $settings, 'agent_max_seconds', 60 ) ); ?>"
									min="5"
									max="300"
									step="1"
									class="small-text"
								/>
								秒
								<p class="description">超过这个时间会中止回答，并退还本次占用的额度。</p>
							</td>
						</tr>
						<tr>
							<th scope="row">
								<label for="l2d-ai-model">DeepSeek 模型名</label>
							</th>
							<td>
								<input
									type="text"
									id="l2d-ai-model"
									name="model"
									value="<?php echo esc_attr( $get( $settings, 'model', 'deepseek-chat' ) ); ?>"
									class="regular-text"
									maxlength="100"
								/>
								<p class="description">
									例如 <code>deepseek-chat</code>。只允许字母、数字、点、下划线、冒号和连字符。
									保存后只影响新请求，已有的对话不受影响。
								</p>
							</td>
						</tr>
					</table>
					<?php submit_button( '保存后端参数' ); ?>
				</form>

				<h2>只读运行信息</h2>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row">后端监听端口</th>
						<td>
							<code><?php echo esc_html( null === $get( $backend, 'listen_port', null ) ? '未知（仅 Linux 可读取）' : $get( $backend, 'listen_port', '' ) ); ?></code>
							<p class="description">端口由 systemd 启动参数决定。要修改端口，需要改 systemd 和 Nginx 配置并重启服务。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">DeepSeek API Key</th>
						<td>
							<?php if ( $get( $backend, 'api_key_configured', false ) ) : ?>
								<span style="color:#1e7b3c;">已配置</span>
							<?php else : ?>
								<span style="color:#b32d2e;">未配置</span>（需要在服务器后端的 <code>.env</code> 中填写）
							<?php endif; ?>
							<p class="description">出于安全考虑，这里不显示 Key 的内容，也不能在这里修改。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">演示模式</th>
						<td>
							<?php echo $get( $backend, 'mock', false ) ? '开启（返回测试回复，不调用 DeepSeek）' : '关闭'; ?>
						</td>
					</tr>
					<tr>
						<th scope="row">后端 JWT 密钥</th>
						<td>
							<?php if ( $get( $backend, 'jwt_configured', false ) ) : ?>
								<span style="color:#1e7b3c;">已配置</span>
							<?php else : ?>
								<span style="color:#b32d2e;">未配置</span>，聊天接口会拒绝所有请求。
							<?php endif; ?>
						</td>
					</tr>
					<tr>
						<th scope="row">额度重置时区</th>
						<td><code><?php echo esc_html( $get( $backend, 'quota_timezone', 'Asia/Shanghai' ) ); ?></code></td>
					</tr>
				</table>
			<?php endif; ?>

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
