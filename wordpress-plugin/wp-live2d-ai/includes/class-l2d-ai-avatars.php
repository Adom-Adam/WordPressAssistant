<?php
/**
 * 用户头像：国内可用的头像源 + 用户自己上传。
 *
 * 两件事都通过 WordPress 的过滤器完成，不改核心、不改主题：
 * - `get_avatar_data` 优先级 999：先看用户有没有上传过头像；没有就把
 *   Gravatar 地址换到国内镜像（Cravatar / WeAvatar / 自定义）。
 * - 资料页 `show_user_profile` 增加上传/删除区块，文件存 uploads，
 *   用户 meta 只记 URL 和路径，不建媒体库附件。
 *
 * 站点后台另有「Live2D 小助手 · 头像」页，用来切换头像源和是否允许上传。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Avatars {

	const PAGE_SLUG = 'l2d-ai-avatars';

	const OPTION_SOURCE  = 'l2d_ai_avatar_source';
	const OPTION_CUSTOM  = 'l2d_ai_avatar_custom_base';
	const OPTION_UPLOAD  = 'l2d_ai_avatar_upload_enabled';

	const META_URL  = 'l2d_ai_avatar_url';
	const META_PATH = 'l2d_ai_avatar_path';

	const NONCE_ACTION = 'l2d_ai_avatars';
	const MAX_BYTES    = 2097152; // 2 MB

	const TRANSIENT_NOTICE = 'l2d_ai_avatars_notice_';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_avatar_upload', array( __CLASS__, 'handle_upload' ) );
		add_action( 'admin_post_l2d_ai_avatar_remove', array( __CLASS__, 'handle_remove' ) );
		add_action( 'admin_post_l2d_ai_avatar_settings', array( __CLASS__, 'handle_settings' ) );
		add_action( 'show_user_profile', array( __CLASS__, 'render_profile_section' ), 11 );
		add_action( 'deleted_user', array( __CLASS__, 'handle_deleted_user' ) );
		add_filter( 'get_avatar_data', array( __CLASS__, 'filter_avatar_data' ), 999, 2 );
	}

	public static function source() {
		$value = (string) get_option( self::OPTION_SOURCE, 'cravatar' );
		return in_array( $value, array( 'default', 'cravatar', 'weavatar', 'custom' ), true )
			? $value
			: 'cravatar';
	}

	public static function upload_enabled() {
		return '1' === (string) get_option( self::OPTION_UPLOAD, '1' );
	}

	public static function mirror_base() {
		$source = self::source();
		if ( 'cravatar' === $source ) {
			return 'https://cravatar.cn/avatar/';
		}
		if ( 'weavatar' === $source ) {
			return 'https://weavatar.com/avatar/';
		}
		if ( 'custom' === $source ) {
			$custom = trim( (string) get_option( self::OPTION_CUSTOM, '' ) );
			if ( '' === $custom ) {
				return '';
			}
			return rtrim( $custom, '/' ) . '/';
		}
		return '';
	}

	private static function rewrite_url( $url ) {
		$base = self::mirror_base();
		if ( '' === $base || '' === (string) $url ) {
			return $url;
		}
		// 只换 /avatar/ 之前的来源，保留后面的哈希和参数（?s=、&d=）。
		$rewritten = preg_replace( '#^https?://[^/]+/avatar/#i', $base, (string) $url );
		return is_string( $rewritten ) && '' !== $rewritten ? $rewritten : $url;
	}

	/**
	 * 解析 get_avatar_data 的第一个参数：用户 ID、WP_User、评论、文章或邮箱。
	 */
	private static function resolve_user_id( $id_or_email ) {
		if ( is_numeric( $id_or_email ) ) {
			return (int) $id_or_email;
		}
		if ( $id_or_email instanceof WP_User ) {
			return (int) $id_or_email->ID;
		}
		if ( $id_or_email instanceof WP_Comment ) {
			$email = (string) $id_or_email->comment_author_email;
			$user  = $email ? get_user_by( 'email', $email ) : false;
			return $user instanceof WP_User ? (int) $user->ID : 0;
		}
		if ( $id_or_email instanceof WP_Post ) {
			return (int) $id_or_email->post_author;
		}
		if ( is_string( $id_or_email ) && is_email( $id_or_email ) ) {
			$user = get_user_by( 'email', $id_or_email );
			return $user instanceof WP_User ? (int) $user->ID : 0;
		}
		return 0;
	}

	public static function filter_avatar_data( $args, $id_or_email ) {
		$user_id = self::resolve_user_id( $id_or_email );
		if ( $user_id > 0 && self::upload_enabled() ) {
			$url = (string) get_user_meta( $user_id, self::META_URL, true );
			if ( '' !== $url ) {
				$args['url']          = $url;
				$args['found_avatar'] = true;
				return $args;
			}
		}
		if ( ! empty( $args['url'] ) ) {
			$args['url'] = self::rewrite_url( $args['url'] );
		}
		return $args;
	}

	// ------------------------------------------------------------------
	// 资料页：上传 / 删除
	// ------------------------------------------------------------------
	public static function render_profile_section( $user ) {
		$user_id = isset( $user->ID ) ? (int) $user->ID : 0;
		if ( $user_id <= 0 || $user_id !== get_current_user_id() ) {
			return;
		}

		$custom_url = (string) get_user_meta( $user_id, self::META_URL, true );
		$notice     = isset( $_GET['l2d-ai-avatar'] ) ? sanitize_key( wp_unslash( $_GET['l2d-ai-avatar'] ) ) : '';
		?>
		<h2 id="l2d-ai-avatar">头像</h2>
		<div class="l2d-ai-account-card" style="max-width:640px;padding:16px 20px;border:1px solid #dcdcde;border-radius:8px;background:#fff;">
			<?php if ( 'uploaded' === $notice ) : ?>
				<p style="color:#1d2327;">头像已更新。</p>
			<?php elseif ( 'removed' === $notice ) : ?>
				<p style="color:#1d2327;">已恢复默认头像。</p>
			<?php elseif ( 'too_large' === $notice ) : ?>
				<p style="color:#b32d2e;">图片太大，请控制在 2MB 以内。</p>
			<?php elseif ( 'bad_type' === $notice ) : ?>
				<p style="color:#b32d2e;">只支持 JPG / PNG / GIF / WebP 图片。</p>
			<?php elseif ( 'failed' === $notice ) : ?>
				<p style="color:#b32d2e;">上传失败，请重试。</p>
			<?php endif; ?>

			<p style="margin-top:0;">当前头像：<br>
				<?php echo get_avatar( $user_id, 64 ); ?>
			</p>

			<?php if ( self::upload_enabled() ) : ?>
				<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" enctype="multipart/form-data">
					<input type="hidden" name="action" value="l2d_ai_avatar_upload">
					<?php wp_nonce_field( self::NONCE_ACTION ); ?>
					<input type="file" name="l2d_ai_avatar" accept="image/jpeg,image/png,image/gif,image/webp" required>
					<button type="submit" class="button button-secondary">上传头像</button>
					<p class="description">支持 JPG / PNG / GIF / WebP，2MB 以内。上传后会覆盖站点默认头像。</p>
				</form>
				<?php if ( '' !== $custom_url ) : ?>
					<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-top:8px;">
						<input type="hidden" name="action" value="l2d_ai_avatar_remove">
						<?php wp_nonce_field( self::NONCE_ACTION ); ?>
						<button type="submit" class="button button-link-delete">恢复默认头像</button>
					</form>
				<?php endif; ?>
			<?php else : ?>
				<p class="description">站长关闭了自定义头像，当前使用站点默认头像。</p>
			<?php endif; ?>
		</div>
		<?php
	}

	public static function handle_upload() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );
		if ( ! self::upload_enabled() ) {
			self::redirect( 'failed' );
		}

		$file = isset( $_FILES['l2d_ai_avatar'] ) ? $_FILES['l2d_ai_avatar'] : null; // phpcs:ignore WordPress.Security.ValidatedSanitizedInput
		if ( ! is_array( $file ) || empty( $file['tmp_name'] ) || ! empty( $file['error'] ) ) {
			self::redirect( 'failed' );
		}
		if ( (int) $file['size'] > self::MAX_BYTES ) {
			self::redirect( 'too_large' );
		}
		if ( ! is_uploaded_file( $file['tmp_name'] ) ) {
			self::redirect( 'failed' );
		}

		require_once ABSPATH . 'wp-admin/includes/file.php';
		$uploaded = wp_handle_upload(
			$file,
			array(
				'test_form' => false,
				'mimes'     => array(
					'jpg|jpeg|jpe' => 'image/jpeg',
					'png'          => 'image/png',
					'gif'          => 'image/gif',
					'webp'         => 'image/webp',
				),
			)
		);
		if ( ! is_array( $uploaded ) || ! empty( $uploaded['error'] ) || empty( $uploaded['file'] ) ) {
			self::redirect( 'failed' );
		}
		$info = @getimagesize( $uploaded['file'] );
		$size = @filesize( $uploaded['file'] );
		if ( false === $info ) {
			@unlink( $uploaded['file'] );
			self::redirect( 'bad_type' );
		}
		if ( false !== $size && $size > self::MAX_BYTES ) {
			@unlink( $uploaded['file'] );
			self::redirect( 'too_large' );
		}

		$user_id = get_current_user_id();
		self::delete_stored_file( $user_id );
		update_user_meta( $user_id, self::META_URL, esc_url_raw( $uploaded['url'] ) );
		update_user_meta( $user_id, self::META_PATH, (string) $uploaded['file'] );
		self::redirect( 'uploaded' );
	}

	public static function handle_remove() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		$user_id = get_current_user_id();
		self::delete_stored_file( $user_id );
		delete_user_meta( $user_id, self::META_URL );
		delete_user_meta( $user_id, self::META_PATH );
		self::redirect( 'removed' );
	}

	public static function handle_deleted_user( $user_id ) {
		// 用户被删除时顺手清掉头像文件；user meta 由 WordPress 自己清。
		self::delete_stored_file( $user_id );
	}

	private static function delete_stored_file( $user_id ) {
		$path = (string) get_user_meta( $user_id, self::META_PATH, true );
		if ( '' === $path ) {
			return;
		}
		$uploads = wp_get_upload_dir();
		$base    = isset( $uploads['basedir'] ) ? wp_normalize_path( (string) $uploads['basedir'] ) : '';
		$target  = wp_normalize_path( $path );
		// 只删 uploads 目录里的文件，避免被 meta 里的路径带到别处。
		if ( '' !== $base && 0 === strpos( $target, trailingslashit( $base ) ) && file_exists( $target ) ) {
			@unlink( $target );
		}
	}

	private static function redirect( $notice ) {
		wp_safe_redirect(
			add_query_arg(
				'l2d-ai-avatar',
				$notice,
				admin_url( 'profile.php' )
			) . '#l2d-ai-avatar'
		);
		exit;
	}

	// ------------------------------------------------------------------
	// 后台设置页
	// ------------------------------------------------------------------
	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · 头像',
			'Live2D 小助手 · 头像',
			'manage_options',
			self::PAGE_SLUG,
			array( __CLASS__, 'render_page' )
		);
	}

	private static function page_url( array $args = array() ) {
		return add_query_arg(
			array_merge( array( 'page' => self::PAGE_SLUG ), $args ),
			admin_url( 'options-general.php' )
		);
	}

	private static function notice_key() {
		return self::TRANSIENT_NOTICE . get_current_user_id();
	}

	public static function handle_settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		$source = isset( $_POST['source'] ) ? sanitize_key( wp_unslash( $_POST['source'] ) ) : 'cravatar';
		if ( ! in_array( $source, array( 'default', 'cravatar', 'weavatar', 'custom' ), true ) ) {
			$source = 'cravatar';
		}
		$custom = isset( $_POST['custom_base'] ) ? esc_url_raw( trim( (string) wp_unslash( $_POST['custom_base'] ) ) ) : '';
		$upload = ! empty( $_POST['upload_enabled'] );

		update_option( self::OPTION_SOURCE, $source );
		update_option( self::OPTION_CUSTOM, $custom );
		update_option( self::OPTION_UPLOAD, $upload ? '1' : '0' );

		set_transient(
			self::notice_key(),
			array( 'message' => '头像设置已保存。', 'error' => false ),
			60
		);
		wp_safe_redirect( self::page_url() );
		exit;
	}

	public static function render_page() {
		$source   = self::source();
		$custom   = (string) get_option( self::OPTION_CUSTOM, '' );
		$upload   = self::upload_enabled();
		$notice   = get_transient( self::notice_key() );
		delete_transient( self::notice_key() );
		$mirror   = self::mirror_base();
		?>
		<div class="wrap">
			<h1>用户头像</h1>
			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<p class="description">
				WordPress 默认头像来自 Gravatar，国内网络经常加载失败。这里可以换成国内可访问的镜像，
				或者让用户在自己的资料页上传头像。上传的头像优先于镜像。
			</p>

			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_avatar_settings">
				<?php wp_nonce_field( self::NONCE_ACTION ); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row">默认头像来源</th>
						<td>
							<select name="source">
								<option value="default" <?php selected( $source, 'default' ); ?>>WordPress 默认（Gravatar）</option>
								<option value="cravatar" <?php selected( $source, 'cravatar' ); ?>>Cravatar（国内镜像）</option>
								<option value="weavatar" <?php selected( $source, 'weavatar' ); ?>>WeAvatar（国内镜像）</option>
								<option value="custom" <?php selected( $source, 'custom' ); ?>>自定义镜像</option>
							</select>
							<p class="description">
								当前生效：<?php echo '' !== $mirror ? esc_html( $mirror ) : 'WordPress 默认（Gravatar）'; ?>
							</p>
						</td>
					</tr>
					<tr>
						<th scope="row">自定义镜像地址</th>
						<td>
							<input type="url" name="custom_base" value="<?php echo esc_attr( $custom ); ?>" class="regular-text" placeholder="https://example.com/avatar/">
							<p class="description">只有来源选「自定义镜像」时使用。末尾会自动补 `/`。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">允许用户上传头像</th>
						<td>
							<label>
								<input type="checkbox" name="upload_enabled" value="1" <?php checked( $upload ); ?>>
								允许用户在资料页上传自己的头像
							</label>
							<p class="description">文件保存在 uploads 目录，只记用户 meta，不进媒体库；支持 JPG/PNG/GIF/WebP，2MB 以内。</p>
						</td>
					</tr>
				</table>
				<?php submit_button( '保存头像设置' ); ?>
			</form>
		</div>
		<?php
	}
}
