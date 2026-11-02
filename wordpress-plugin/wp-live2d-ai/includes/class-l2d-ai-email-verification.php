<?php
/**
 * 注册邮箱验证。
 *
 * 为什么要有这一步：注销后重新注册会继承额度，而“同一个邮箱”在没有
 * 验证的情况下只是用户自己填的一行字。知道别人注销邮箱的人可以抢先
 * 注册，把额度领走。验证邮件发到哪个邮箱、链接被谁点开，才能证明
 * 注册者至少控制着这个邮箱。
 *
 * 设计上的几个选择：
 * - token 只存 HMAC，不存明文；邮件链接里的明文只出现一次；
 * - 链接先打开确认页（GET），用户点按钮（POST）才消费 token。
 *   邮箱安全网关可能会预取邮件里的链接，GET 直接验证会让用户自己
 *   再点时看到“链接已失效”；
 * - 未验证不拦 WordPress 登录，只拦小助手、QQ 绑定和额度继承。
 *   这样用户还能进个人资料页点“重新发送验证邮件”；
 * - 老用户升级时一次性标记为已验证，避免整个站点突然用不了小助手。
 *
 * @package wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Email_Verification {

	/** 已验证时间戳；有值就表示这个账号的邮箱已经通过验证。 */
	const META_VERIFIED = '_l2d_ai_email_verified';

	/** token 的 HMAC、过期时间、上次发送时间，都只存在用户自己的 meta 里。 */
	const META_TOKEN    = '_l2d_ai_email_token_hash';
	const META_EXPIRES  = '_l2d_ai_email_token_expires';
	const META_SENT_AT  = '_l2d_ai_email_sent_at';

	/** 迁移标记：插件升级后只跑一次，把升级前的用户视为已验证。 */
	const MIGRATION_OPTION  = 'l2d_ai_email_verification_migrated';
	const MIGRATION_VERSION = '1';

	const NONCE_ACTION     = 'l2d_ai_email_verification';
	const TOKEN_TTL        = 86400; // 24 小时。
	const RESEND_COOLDOWN  = 300;   // 同一账号 5 分钟内不重复发信。
	const NOTICE_PREFIX    = 'l2d_ai_email_notice_';
	const MAIL_ERROR_KEY   = 'l2d_ai_email_mail_error';

	/** 只记录本类发信失败的提示，不把其它插件的密码重置失败也算进来。 */
	private static $sending = false;

	/** 资料页底部表单的目标用户；0 表示本次没有需要输出的表单。 */
	private static $footer_user_id = 0;

	/** 本次是否渲染了「重新发送验证邮件」按钮。 */
	private static $need_resend_form = false;

	/** 本次是否渲染了「手动标记为已验证」按钮。 */
	private static $need_admin_verify_form = false;

	public static function init() {
		add_action( 'user_register', array( __CLASS__, 'on_register' ) );
		add_action( 'profile_update', array( __CLASS__, 'on_profile_update' ), 10, 2 );

		add_action( 'show_user_profile', array( __CLASS__, 'render_card' ) );
		add_action( 'edit_user_profile', array( __CLASS__, 'render_card' ) );
		add_action( 'admin_footer', array( __CLASS__, 'render_forms' ) );

		// GET：显示确认页，不消费 token；POST：用户点按钮后才真正验证。
		add_action( 'admin_post_nopriv_l2d_ai_verify_email', array( __CLASS__, 'render_confirm_page' ) );
		add_action( 'admin_post_l2d_ai_verify_email', array( __CLASS__, 'render_confirm_page' ) );
		add_action( 'admin_post_nopriv_l2d_ai_confirm_email', array( __CLASS__, 'handle_confirm' ) );
		add_action( 'admin_post_l2d_ai_confirm_email', array( __CLASS__, 'handle_confirm' ) );

		add_action( 'admin_post_l2d_ai_resend_verification', array( __CLASS__, 'handle_resend' ) );
		add_action( 'admin_post_l2d_ai_admin_verify_email', array( __CLASS__, 'handle_admin_verify' ) );

		add_filter( 'login_message', array( __CLASS__, 'login_message' ) );

		// SMTP 出故障时，至少让管理员在后台看到提示，而不是只让用户干等。
		add_action( 'wp_mail_failed', array( __CLASS__, 'record_mail_failure' ) );
		add_action( 'admin_notices', array( __CLASS__, 'render_mail_failure_notice' ) );

		// 升级后第一次请求执行一次迁移；之后只检查一个 option。
		add_action( 'init', array( __CLASS__, 'maybe_migrate_existing_users' ), 1 );
	}

	// ------------------------------------------------------------------
	// 注册与状态判断
	// ------------------------------------------------------------------

	/**
	 * 新用户注册后发验证邮件。
	 *
	 * 管理员在后台建号、WP-CLI 导入这类“由可信后台创建”的账号直接
	 * 标记为已验证，避免批量导入时给每个用户发一封邮件。
	 */
	public static function on_register( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return;
		}

		if (
			user_can( $user_id, 'manage_options' )
			|| ( is_admin() && current_user_can( 'create_users' ) )
			|| ( defined( 'WP_CLI' ) && WP_CLI )
		) {
			self::mark_verified( $user_id );
			return;
		}

		if ( ! apply_filters( 'l2d_ai_email_verification_required', true, $user_id ) ) {
			self::mark_verified( $user_id );
			return;
		}

		$result = self::send_verification( $user_id, true );
		if ( is_wp_error( $result ) ) {
			error_log(
				'L2D AI 注册验证邮件发送失败 user=' . $user_id
				. ': ' . $result->get_error_message()
			);
		}
	}

	/**
	 * 邮箱改了就重新验证。
	 *
	 * WordPress 对普通用户改邮箱本来就会先给新地址发确认信；确认完成后
	 * 才会真正改 user_email，并触发这个钩子。这里再发一封验证信，
	 * 是为了让“验证状态”始终跟着当前邮箱走，而不是跟着旧邮箱。
	 */
	public static function on_profile_update( $user_id, $old_user_data ) {
		$user_id = (int) $user_id;
		$user    = get_userdata( $user_id );
		if ( ! $user || ! isset( $user->user_email ) ) {
			return;
		}

		$old_email = ( is_object( $old_user_data ) && isset( $old_user_data->user_email ) )
			? strtolower( trim( (string) $old_user_data->user_email ) )
			: '';
		$new_email = strtolower( trim( (string) $user->user_email ) );

		if ( $old_email === $new_email ) {
			return;
		}

		self::clear_verification( $user_id );
		if ( '' === $new_email ) {
			return;
		}

		$result = self::send_verification( $user_id, true );
		if ( is_wp_error( $result ) ) {
			error_log(
				'L2D AI 改邮箱后的验证邮件发送失败 user=' . $user_id
				. ': ' . $result->get_error_message()
			);
		}
	}

	/**
	 * 这个账号的邮箱是否已验证。
	 *
	 * 管理员不拦：避免站点只有一个人时把自己锁在门外。普通用户必须
	 * 有 META_VERIFIED。返回值只用于小助手/绑定/继承的门槛，不参与
	 * WordPress 自身的登录权限判断。
	 */
	public static function is_verified( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return false;
		}

		if ( user_can( $user_id, 'manage_options' ) ) {
			return true;
		}

		return (int) get_user_meta( $user_id, self::META_VERIFIED, true ) > 0;
	}

	public static function mark_verified( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return;
		}

		update_user_meta( $user_id, self::META_VERIFIED, time() );
		// 验证完成后 token 就没有用了，立即清掉，避免旧链接再次生效。
		delete_user_meta( $user_id, self::META_TOKEN );
		delete_user_meta( $user_id, self::META_EXPIRES );
		delete_user_meta( $user_id, self::META_SENT_AT );
	}

	private static function clear_verification( $user_id ) {
		delete_user_meta( $user_id, self::META_VERIFIED );
		delete_user_meta( $user_id, self::META_TOKEN );
		delete_user_meta( $user_id, self::META_EXPIRES );
		delete_user_meta( $user_id, self::META_SENT_AT );
	}

	/**
	 * 生成 token 并发验证邮件。
	 *
	 * 先发信、成功后才写入 token：如果 SMTP 失败，用户手里还是上一封
	 * 邮件里的旧 token，不会出现“新链接没收到、旧链接也失效了”。
	 *
	 * @param int  $user_id 用户 ID。
	 * @param bool $force   跳过 5 分钟冷却（注册、改邮箱时使用）。
	 * @return true|WP_Error
	 */
	public static function send_verification( $user_id, $force = false ) {
		$user_id = (int) $user_id;
		$user    = get_userdata( $user_id );
		if ( ! $user || ! isset( $user->ID ) ) {
			return new WP_Error( 'l2d_ai_no_user', '找不到这个账号。' );
		}

		$email = trim( (string) $user->user_email );
		if ( '' === $email ) {
			return new WP_Error( 'l2d_ai_no_email', '这个账号还没有填写邮箱。' );
		}

		if ( ! $force ) {
			$last = (int) get_user_meta( $user_id, self::META_SENT_AT, true );
			if ( $last > 0 && ( time() - $last ) < self::RESEND_COOLDOWN ) {
				return new WP_Error(
					'l2d_ai_email_cooldown',
					'验证邮件刚刚发过，请过几分钟再试。'
				);
			}
		}

		try {
			$token = bin2hex( random_bytes( 32 ) );
		} catch ( Exception $e ) {
			return new WP_Error( 'l2d_ai_no_random', '生成验证链接失败，请稍后再试。' );
		}

		$link = add_query_arg(
			array(
				'action' => 'l2d_ai_verify_email',
				'uid'    => $user_id,
				'token'  => $token,
			),
			admin_url( 'admin-post.php' )
		);

		$site   = wp_specialchars_decode( get_bloginfo( 'name' ), ENT_QUOTES );
		$subject = sprintf( '[%s] 请验证你的邮箱', $site );
		$message = "你好，\n\n"
			. "请点击下面的链接完成邮箱验证：\n\n"
			. $link . "\n\n"
			. "链接 24 小时内有效，且只能使用一次。\n"
			. "如果这不是你本人操作，忽略这封邮件即可。\n\n"
			. '-- ' . $site;

		self::$sending = true;
		try {
			$sent = wp_mail( $email, $subject, $message );
		} finally {
			self::$sending = false;
		}

		if ( ! $sent ) {
			return new WP_Error(
				'l2d_ai_mail_failed',
				'验证邮件发送失败，请稍后再试或联系站长。'
			);
		}

		update_user_meta(
			$user_id,
			self::META_TOKEN,
			hash_hmac( 'sha256', $token, wp_salt( 'auth' ) )
		);
		update_user_meta( $user_id, self::META_EXPIRES, time() + self::TOKEN_TTL );
		update_user_meta( $user_id, self::META_SENT_AT, time() );

		return true;
	}

	/** token 是否属于这个用户、未过期、且仍然有效。不消费 token。 */
	public static function token_is_valid( $user_id, $token ) {
		$user_id = (int) $user_id;
		$token   = (string) $token;

		if ( $user_id <= 0 || 1 !== preg_match( '/^[a-f0-9]{64}$/', $token ) ) {
			return false;
		}

		$stored  = (string) get_user_meta( $user_id, self::META_TOKEN, true );
		$expires = (int) get_user_meta( $user_id, self::META_EXPIRES, true );
		if ( '' === $stored || $expires <= 0 || $expires < time() ) {
			return false;
		}

		return hash_equals( $stored, hash_hmac( 'sha256', $token, wp_salt( 'auth' ) ) );
	}

	// ------------------------------------------------------------------
	// 验证链接
	// ------------------------------------------------------------------

	/** GET：只显示确认按钮，不消费 token。 */
	public static function render_confirm_page() {
		$user_id = isset( $_GET['uid'] ) ? absint( $_GET['uid'] ) : 0;
		$token   = ( isset( $_GET['token'] ) && is_string( $_GET['token'] ) )
			? (string) wp_unslash( $_GET['token'] )
			: '';

		if ( ! self::token_is_valid( $user_id, $token ) ) {
			wp_die(
				'这个验证链接无效或已经过期。请回到个人资料页重新发送验证邮件。',
				'邮箱验证',
				array( 'response' => 400 )
			);
		}

		$user  = get_userdata( $user_id );
		$email = ( $user && isset( $user->user_email ) ) ? (string) $user->user_email : '';

		$body = '<p>正在验证账号 <strong>' . esc_html( $email ) . '</strong> 的邮箱。</p>'
			. '<form method="post" action="' . esc_url( admin_url( 'admin-post.php' ) ) . '" style="margin-top:20px;">'
			. '<input type="hidden" name="action" value="l2d_ai_confirm_email">'
			. '<input type="hidden" name="uid" value="' . esc_attr( (string) $user_id ) . '">'
			. '<input type="hidden" name="token" value="' . esc_attr( $token ) . '">'
			. '<button type="submit" style="padding:10px 18px;font-size:15px;cursor:pointer;">确认验证</button>'
			. '</form>'
			. '<p style="margin-top:20px;color:#646970;font-size:13px;">如果不是你本人操作，关闭这个页面即可。</p>';

		self::render_page( '确认邮箱验证', $body );
	}

	/** POST：用户点了确认按钮，消费 token。 */
	public static function handle_confirm() {
		$user_id = isset( $_POST['uid'] ) ? absint( $_POST['uid'] ) : 0;
		$token   = ( isset( $_POST['token'] ) && is_string( $_POST['token'] ) )
			? (string) wp_unslash( $_POST['token'] )
			: '';

		if ( ! self::token_is_valid( $user_id, $token ) ) {
			wp_die(
				'这个验证链接无效或已经过期。请回到个人资料页重新发送验证邮件。',
				'邮箱验证',
				array( 'response' => 400 )
			);
		}

		self::mark_verified( $user_id );

		if ( is_user_logged_in() && get_current_user_id() === $user_id ) {
			self::notice( $user_id, '邮箱验证成功。', false );
			wp_safe_redirect( admin_url( 'profile.php' ) );
			exit;
		}

		wp_die(
			'邮箱验证成功。现在可以返回网站登录了。<br><br>'
			. '<a href="' . esc_url( home_url( '/' ) ) . '">返回首页</a>',
			'邮箱验证',
			array( 'response' => 200 )
		);
	}

	/** 一个极简的独立页面：验证链接可能被未登录用户打开，不需要后台样式。 */
	private static function render_page( $title, $body ) {
		nocache_headers();
		status_header( 200 );
		header( 'Content-Type: text/html; charset=utf-8' );
		?><!DOCTYPE html>
<html lang="zh-CN">
<head>
	<meta charset="utf-8">
	<meta name="viewport" content="width=device-width, initial-scale=1">
	<title><?php echo esc_html( $title ); ?></title>
</head>
<body style="margin:0;background:#f6f7f7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#1d2327;">
	<main style="max-width:520px;margin:10vh auto;padding:28px 32px;background:#fff;border:1px solid #dcdcde;border-radius:10px;">
		<h1 style="margin-top:0;font-size:20px;"><?php echo esc_html( $title ); ?></h1>
		<?php echo $body; // phpcs:ignore WordPress.Security.EscapeOutput.OutputNotEscaped -- 片段在调用处已转义。 ?>
	</main>
</body>
</html><?php
		exit;
	}

	// ------------------------------------------------------------------
	// 个人资料页
	// ------------------------------------------------------------------

	public static function render_card( $user ) {
		$user_id = isset( $user->ID ) ? (int) $user->ID : 0;
		if ( $user_id <= 0 || ! current_user_can( 'edit_user', $user_id ) ) {
			return;
		}

		$verified = self::is_verified( $user_id );
		$email    = isset( $user->user_email ) ? (string) $user->user_email : '';
		$is_self  = get_current_user_id() === $user_id;
		?>
		<h2>邮箱验证</h2>
		<div class="l2d-ai-email-card" style="max-width:640px;padding:16px 20px;border:1px solid #dcdcde;border-radius:8px;background:#fff;">
			<?php self::render_notice( $user_id ); ?>

			<?php if ( $verified ) : ?>
				<p style="margin:0 0 8px;">
					<strong>状态：</strong>已验证
					<?php if ( '' !== $email ) : ?>
						（<code><?php echo esc_html( $email ); ?></code>）
					<?php endif; ?>
				</p>
				<p style="margin:0;color:#646970;">
					<?php echo esc_html( '小助手、QQ 绑定和额度继承都可以正常使用。' ); ?>
				</p>
			<?php else : ?>
				<p style="margin:0 0 8px;color:#b32d2e;">
					<strong>状态：</strong>未验证
				</p>
				<p style="margin:0 0 12px;color:#646970;">
					<?php echo esc_html( '验证邮箱后才能使用小助手、生成 QQ 绑定码和继承注销账号的额度。' ); ?>
					<?php if ( '' !== $email ) : ?>
						<?php echo esc_html( '验证邮件会发到：' ); ?>
						<code><?php echo esc_html( $email ); ?></code>
					<?php else : ?>
						<?php echo esc_html( '这个账号还没有填写邮箱，请先补上邮箱。' ); ?>
					<?php endif; ?>
				</p>

				<?php if ( $is_self && '' !== $email ) : ?>
					<?php
					self::$footer_user_id      = $user_id;
					self::$need_resend_form    = true;
					?>
					<p style="margin:0 0 12px;">
						<button
							type="submit"
							class="button button-primary"
							form="l2d-ai-email-resend-form"
						>
							<?php echo esc_html( '重新发送验证邮件' ); ?>
						</button>
					</p>
				<?php endif; ?>

				<?php if ( current_user_can( 'manage_options' ) ) : ?>
					<?php
					self::$footer_user_id          = $user_id;
					self::$need_admin_verify_form = true;
					?>
					<p style="margin:0;">
						<button
							type="submit"
							class="button"
							form="l2d-ai-email-admin-verify-form"
							onclick="return confirm('确认这个邮箱已经通过其他方式核实？');"
						>
							<?php echo esc_html( '手动标记为已验证' ); ?>
						</button>
					</p>
				<?php endif; ?>
			<?php endif; ?>
		</div>
		<?php
	}

	/**
	 * 在页面底部输出资料卡片按钮要提交的表单。
	 *
	 * WordPress 资料页本身是一个 `<form>`，卡片里再套 `<form>` 会破坏
	 * 外层表单，导致底部「更新个人资料」点了没反应。这里把容器放到
	 * admin_footer（外层表单之外），卡片按钮通过 `form` 属性关联。
	 */
	public static function render_forms() {
		$user_id = (int) self::$footer_user_id;
		if ( $user_id <= 0 || ( ! self::$need_resend_form && ! self::$need_admin_verify_form ) ) {
			return;
		}

		if ( self::$need_resend_form ) {
			?>
			<form
				id="l2d-ai-email-resend-form"
				method="post"
				action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
				style="display:none;"
			>
				<input type="hidden" name="action" value="l2d_ai_resend_verification">
				<?php wp_nonce_field( self::NONCE_ACTION ); ?>
			</form>
			<?php
		}

		if ( self::$need_admin_verify_form ) {
			?>
			<form
				id="l2d-ai-email-admin-verify-form"
				method="post"
				action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
				style="display:none;"
			>
				<input type="hidden" name="action" value="l2d_ai_admin_verify_email">
				<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>">
				<?php wp_nonce_field( self::NONCE_ACTION ); ?>
			</form>
			<?php
		}
	}

	public static function handle_resend() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录。', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		$user_id = get_current_user_id();
		if ( self::is_verified( $user_id ) ) {
			self::notice( $user_id, '这个邮箱已经验证过了。', false );
			self::redirect_profile();
		}

		$result = self::send_verification( $user_id, false );
		if ( is_wp_error( $result ) ) {
			self::notice( $user_id, $result->get_error_message(), true );
		} else {
			self::notice( $user_id, '验证邮件已发送，请查收（也看看垃圾箱）。', false );
		}

		self::redirect_profile();
	}

	/** 管理员在资料页手动验证：SMTP 不通或用户已通过其他方式核实时的兜底。 */
	public static function handle_admin_verify() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录。', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		// 手动验证是管理员兜底，不能让用户给自己点一下绕过验证。
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '无权执行这个操作。', '无权限', array( 'response' => 403 ) );
		}

		$user_id = isset( $_POST['user_id'] ) ? absint( $_POST['user_id'] ) : 0;
		if ( $user_id <= 0 || ! current_user_can( 'edit_user', $user_id ) ) {
			wp_die( '无权修改这个用户。', '无权限', array( 'response' => 403 ) );
		}

		self::mark_verified( $user_id );
		self::notice( $user_id, '已手动标记为邮箱已验证。', false );

		$target = ( get_current_user_id() === $user_id )
			? admin_url( 'profile.php' )
			: admin_url( 'user-edit.php?user_id=' . $user_id );
		wp_safe_redirect( $target );
		exit;
	}

	private static function redirect_profile() {
		wp_safe_redirect( admin_url( 'profile.php' ) );
		exit;
	}

	private static function notice( $user_id, $message, $is_error ) {
		set_transient(
			self::NOTICE_PREFIX . (int) $user_id,
			array(
				'message' => (string) $message,
				'error'   => (bool) $is_error,
			),
			60
		);
	}

	private static function render_notice( $user_id ) {
		$key    = self::NOTICE_PREFIX . (int) $user_id;
		$notice = get_transient( $key );
		if ( ! is_array( $notice ) ) {
			return;
		}
		delete_transient( $key );

		$message = isset( $notice['message'] ) ? (string) $notice['message'] : '';
		if ( '' === $message ) {
			return;
		}
		$color = ! empty( $notice['error'] ) ? '#b32d2e' : '#1d2327';
		?>
		<p style="margin:0 0 12px;color:<?php echo esc_attr( $color ); ?>;">
			<?php echo esc_html( $message ); ?>
		</p>
		<?php
	}

	public static function login_message( $message ) {
		$status = ( isset( $_GET['l2d_ai_email'] ) && is_string( $_GET['l2d_ai_email'] ) )
			? sanitize_key( wp_unslash( $_GET['l2d_ai_email'] ) )
			: '';

		if ( 'verified' === $status ) {
			$message = '<p class="message">邮箱验证成功，请登录。</p>' . $message;
		}

		return $message;
	}

	// ------------------------------------------------------------------
	// 升级迁移与邮件故障提示
	// ------------------------------------------------------------------

	/**
	 * 升级前就存在的用户没有验证过邮箱，不能让他们突然全部被拦。
	 *
	 * 迁移只跑一次；之后新注册的用户走正常的 on_register 流程。
	 * 如果站点希望老用户也重新验证，可以挂
	 * `l2d_ai_email_verification_grandfather` 返回 false。
	 */
	public static function maybe_migrate_existing_users() {
		if ( self::MIGRATION_VERSION === (string) get_option( self::MIGRATION_OPTION, '' ) ) {
			return;
		}

		if ( apply_filters( 'l2d_ai_email_verification_grandfather', true ) ) {
			$user_ids = get_users(
				array(
					'fields' => 'ID',
					'number' => -1,
				)
			);
			foreach ( (array) $user_ids as $user_id ) {
				$user_id = (int) $user_id;
				if ( $user_id <= 0 ) {
					continue;
				}
				if ( (int) get_user_meta( $user_id, self::META_VERIFIED, true ) <= 0 ) {
					update_user_meta( $user_id, self::META_VERIFIED, time() );
				}
				delete_user_meta( $user_id, self::META_TOKEN );
				delete_user_meta( $user_id, self::META_EXPIRES );
				delete_user_meta( $user_id, self::META_SENT_AT );
			}
		}

		update_option( self::MIGRATION_OPTION, self::MIGRATION_VERSION, false );
	}

	public static function record_mail_failure( $error ) {
		if ( ! self::$sending || ! is_wp_error( $error ) ) {
			return;
		}
		// 只存错误文本，不存收件人；后台提示里也只给管理员看。
		set_transient( self::MAIL_ERROR_KEY, $error->get_error_message(), HOUR_IN_SECONDS );
	}

	public static function render_mail_failure_notice() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$message = get_transient( self::MAIL_ERROR_KEY );
		if ( ! $message ) {
			return;
		}
		delete_transient( self::MAIL_ERROR_KEY );

		echo '<div class="notice notice-error"><p>'
			. esc_html( '邮箱验证邮件发送失败，请检查 SMTP 设置：' . $message )
			. '</p></div>';
	}
}
