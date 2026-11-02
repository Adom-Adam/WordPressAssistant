<?php
/**
 * 账号自助：改用户名、注销账号，以及注销后的额度继承。
 *
 * ==================================================================
 * 改用户名
 * ==================================================================
 * WordPress 资料页默认不提供改 user_login 的入口，但 wp_update_user()
 * 本身支持。这里只补三件核心没做的事：
 * - 校验当前密码，避免别人借走已登录的浏览器直接改名；
 * - 检查新用户名是否已被占用（核心更新时不会检查唯一性）；
 * - 改完重新签发登录 cookie，否则旧 cookie 里还是旧用户名，会立刻掉线。
 *
 * 中文用户名的放行仍由 L2D_AI_Chinese_Usernames 的过滤器负责。
 *
 * ==================================================================
 * 注销与额度继承
 * ==================================================================
 * 额度按 WordPress 用户 ID 存在 Python 后端，邮箱只存在 WordPress。
 * 用户删号再注册会拿到新的用户 ID，后端不可能自己知道“这两个 ID
 * 是同一个邮箱”。所以 WordPress 在删号时记一份“邮箱哈希 → 旧 ID”
 * 的墓碑；新账号第一次换 token / 打开资料页时，插件用专用短时 JWT
 * 把旧 ID 交给后端合并，成功后删除墓碑。
 *
 * 墓碑里不存邮箱明文，只存带站点 salt 的 HMAC，后台被拖走也不能
 * 直接拿去撞库。继承只搬额度（每日已用、额外额度、会员、流水、
 * 兑换记录），不继承 QQ 绑定、管理员身份和备注。
 *
 * 额度继承要求新账号的邮箱已经验证：验证由 L2D_AI_Email_Verification
 * 负责，只有能打开该邮箱验证链接的人才能把旧额度领走。墓碑默认保留
 * 365 天，可用 l2d_ai_quota_inherit 过滤器关闭。
 *
 * @package wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Account {

	const NONCE_ACTION = 'l2d_ai_account';

	/** 墓碑存在一个 options 数组里：邮箱哈希 => {user_ids, deleted_at}。 */
	const TOMBSTONE_OPTION = 'l2d_ai_quota_tombstones';
	const TOMBSTONE_TTL    = 31536000; // 365 天。
	const MAX_SOURCES      = 20;

	/** 删号请求内暂存邮箱：delete_user 在删之前触发，deleted_user 在删之后触发。 */
	private static $pending_emails = array();

	/** 删号前这个账号的邮箱是否已验证；未验证的账号不留可继承的墓碑。 */
	private static $pending_verified = array();

	/**
	 * 只有“用户自己注销”才留墓碑。
	 *
	 * 管理员删号可能是在处理垃圾账号/违规账号；如果也留墓碑，对方用同一个
	 * 邮箱重新注册就能把额度领回去，等于绕过管理动作。这里只认本类发起的
	 * 自助注销。
	 */
	private static $self_deleted = array();

	public static function init() {
		add_action( 'show_user_profile', array( __CLASS__, 'render_card' ) );
		add_action( 'admin_post_l2d_ai_change_username', array( __CLASS__, 'handle_change_username' ) );
		add_action( 'admin_post_l2d_ai_delete_account', array( __CLASS__, 'handle_delete_account' ) );
		add_action( 'delete_user', array( __CLASS__, 'capture_deletion' ), 10, 3 );
		add_action( 'deleted_user', array( __CLASS__, 'record_tombstone' ), 10, 3 );
	}

	// ------------------------------------------------------------------
	// 资料页卡片
	// ------------------------------------------------------------------

	public static function render_card( $user ) {
		$user_id = isset( $user->ID ) ? (int) $user->ID : 0;
		if (
			$user_id <= 0
			|| $user_id !== get_current_user_id()
			|| ! current_user_can( 'edit_user', $user_id )
		) {
			return;
		}

		// 资料页本身可能不加载小助手，在这里补一次继承检查。
		self::inherit_quota( $user_id );

		$is_admin = user_can( $user_id, 'manage_options' );
		?>
		<h2>账号设置</h2>
		<div class="l2d-ai-account-card" style="max-width:640px;padding:16px 20px;border:1px solid #dcdcde;border-radius:8px;background:#fff;">
			<?php self::render_notice(); ?>

			<h3 style="margin-top:0;">更改用户名</h3>
			<p style="margin:0 0 12px;color:#646970;">
				<?php echo esc_html( '用户名用于登录。没单独设过昵称时，显示名会一起改；设过就保持原昵称。' ); ?>
			</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_change_username">
				<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>">
				<?php wp_nonce_field( self::NONCE_ACTION ); ?>
				<p style="margin:0 0 8px;">
					<label for="l2d-ai-new-login"><strong>新用户名</strong></label><br>
					<input
						type="text"
						id="l2d-ai-new-login"
						name="new_login"
						class="regular-text"
						autocomplete="username"
						placeholder="<?php echo esc_attr( (string) $user->user_login ); ?>"
						required
					>
				</p>
				<p style="margin:0 0 12px;">
					<label for="l2d-ai-username-password"><strong>当前密码</strong></label><br>
					<input
						type="password"
						id="l2d-ai-username-password"
						name="current_password"
						class="regular-text"
						autocomplete="current-password"
						required
					>
				</p>
				<button type="submit" class="button button-primary">保存新用户名</button>
			</form>

			<hr style="margin:24px 0;">

			<h3>注销账号</h3>
			<?php if ( $is_admin ) : ?>
				<p style="margin:0;color:#646970;">
					<?php echo esc_html( '管理员账号不能在这里自行注销，请让另一位管理员在后台「用户」页删除。' ); ?>
				</p>
			<?php else : ?>
				<p style="margin:0 0 8px;color:#b32d2e;">
					<?php echo esc_html( '注销后无法用这个账号登录；名下文章会转交给站长，不会跟着删除。' ); ?>
				</p>
				<p style="margin:0 0 12px;color:#646970;">
					<?php echo esc_html( '如果你之后用同一个邮箱重新注册，今日已用次数、额外额度和会员会继承过来；QQ 绑定需要重新绑。' ); ?>
				</p>
				<form
					method="post"
					action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
					onsubmit="return confirm('这是不可撤销的操作，确定要注销吗？');"
				>
					<input type="hidden" name="action" value="l2d_ai_delete_account">
					<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>">
					<?php wp_nonce_field( self::NONCE_ACTION ); ?>
					<p style="margin:0 0 8px;">
						<label for="l2d-ai-delete-password"><strong>当前密码</strong></label><br>
						<input
							type="password"
							id="l2d-ai-delete-password"
							name="delete_password"
							class="regular-text"
							autocomplete="current-password"
							required
						>
					</p>
					<p style="margin:0 0 12px;">
						<label for="l2d-ai-delete-confirm">
							<strong>确认</strong>：输入「注销」两个字
						</label><br>
						<input
							type="text"
							id="l2d-ai-delete-confirm"
							name="confirm_delete"
							class="regular-text"
							autocomplete="off"
							required
						>
					</p>
					<button type="submit" class="button button-link-delete">永久注销账号</button>
				</form>
			<?php endif; ?>
		</div>
		<?php
	}

	private static function render_notice() {
		$key = 'l2d_ai_account_notice_' . get_current_user_id();
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

	// ------------------------------------------------------------------
	// 改用户名
	// ------------------------------------------------------------------

	public static function handle_change_username() {
		$user_id = self::authorize_self();
		$user    = get_userdata( $user_id );
		if ( ! $user instanceof WP_User ) {
			self::fail( '找不到当前账号。' );
		}

		$password = isset( $_POST['current_password'] )
			? (string) wp_unslash( $_POST['current_password'] )
			: '';
		if ( '' === $password || ! wp_check_password( $password, $user->user_pass, $user_id ) ) {
			self::fail( '当前密码不正确，用户名没有修改。' );
		}

		$new_login = isset( $_POST['new_login'] )
			? trim( (string) wp_unslash( $_POST['new_login'] ) )
			: '';
		if ( '' === $new_login ) {
			self::fail( '请填写新用户名。' );
		}
		if ( strlen( $new_login ) > 60 ) {
			self::fail( '用户名太长，最多 60 个字节。' );
		}
		if ( ! validate_username( $new_login ) ) {
			self::fail( '这个用户名包含不允许的字符。' );
		}
		// validate_username() 用的是非严格清洗，会把 UTF-8 高位字节都放过；
		// 这里再用核心更新时真正使用的严格模式比一次，表情符号、全角标点
		// 就不会出现“校验通过、保存时被悄悄删掉”的情况。
		if ( sanitize_user( $new_login, true ) !== $new_login ) {
			self::fail( '这个用户名包含不允许的字符。' );
		}
		if ( $new_login === $user->user_login ) {
			self::fail( '新用户名和现在一样。' );
		}

		$existing = username_exists( $new_login );
		if ( $existing && (int) $existing !== $user_id ) {
			self::fail( '这个用户名已经被占用了。' );
		}

		// 和注册流程一样尊重 illegal_user_logins，避免用户改成保留名。
		$illegal = array_map(
			'strtolower',
			array_map( 'strval', (array) apply_filters( 'illegal_user_logins', array() ) )
		);
		if ( in_array( strtolower( $new_login ), $illegal, true ) ) {
			self::fail( '这个用户名不可用。' );
		}

		$args = array(
			'ID'         => $user_id,
			'user_login' => $new_login,
		);
		// 大多数人从没单独设过昵称，显示名默认等于用户名；这种情况下
		// 一起改掉，否则评论区会继续显示旧名字，用户会以为没生效。
		if ( $user->display_name === $user->user_login ) {
			$args['display_name'] = $new_login;
		}

		$result = wp_update_user( $args );
		if ( is_wp_error( $result ) ) {
			self::fail( $result->get_error_message() );
		}

		// 登录 cookie 里带着用户名，不重新签发的话下一次请求就会掉线。
		// 先清缓存，确保新 cookie 用的是新用户名，而不是旧对象。
		clean_user_cache( $user_id );
		wp_set_auth_cookie( $user_id, true, is_ssl() );
		wp_set_current_user( $user_id );

		self::notice( '用户名已改为 ' . $new_login . '，下次登录请使用新用户名。' );
		self::redirect();
	}

	// ------------------------------------------------------------------
	// 注销
	// ------------------------------------------------------------------

	public static function handle_delete_account() {
		$user_id = self::authorize_self();
		$user    = get_userdata( $user_id );
		if ( ! $user instanceof WP_User ) {
			self::fail( '找不到当前账号。' );
		}

		// 管理员自行注销可能把站点锁死；统一交给另一位管理员处理。
		if ( user_can( $user_id, 'manage_options' ) ) {
			self::fail( '管理员账号不能在这里自行注销，请让另一位管理员在后台删除。' );
		}

		$password = isset( $_POST['delete_password'] )
			? (string) wp_unslash( $_POST['delete_password'] )
			: '';
		if ( '' === $password || ! wp_check_password( $password, $user->user_pass, $user_id ) ) {
			self::fail( '当前密码不正确，账号没有删除。' );
		}

		$confirm = isset( $_POST['confirm_delete'] )
			? trim( (string) wp_unslash( $_POST['confirm_delete'] ) )
			: '';
		if ( '注销' !== $confirm ) {
			self::fail( '请在确认框里完整输入「注销」两个字。' );
		}

		// wp_delete_user() 不传第二个参数会连作者文章一起删掉。这里把内容
		// 转交给另一位管理员，注销只影响账号，不带走多年文章。
		$reassign = self::reassign_target( $user_id );
		if ( $reassign <= 0 ) {
			self::fail( '站点没有可接收内容的另一位管理员，请联系站长先处理。' );
		}

		// 先解绑 QQ：删号之后再解绑就找不到用户了，而且 QQ 会一直
		// 占着“已绑定”，新账号无法重新绑定。失败就停止注销，让用户重试。
		$unbind = L2D_AI_Plugin::user_backend_request( $user_id, 'DELETE', '/me/bind' );
		if ( is_wp_error( $unbind ) ) {
			self::fail(
				'暂时无法解绑 QQ，账号没有删除。请稍后再试：' . $unbind->get_error_message()
			);
		}

		// 标记“这次是自助注销”，deleted_user 钩子据此决定要不要留墓碑。
		self::$self_deleted[ $user_id ] = true;

		require_once ABSPATH . 'wp-admin/includes/user.php';
		if ( ! wp_delete_user( $user_id, $reassign ) ) {
			unset( self::$self_deleted[ $user_id ] );
			self::fail( '账号删除失败，请联系管理员。' );
		}

		wp_logout();
		wp_safe_redirect( add_query_arg( 'l2d-ai-account', 'deleted', home_url( '/' ) ) );
		exit;
	}

	/**
	 * 注销后文章转交给谁。
	 *
	 * 默认交给第一位管理员（排除自己）；站点可以用过滤器指定别的账号。
	 * 找不到可接收的人就不允许自助注销，避免 wp_delete_user() 把文章删掉。
	 */
	private static function reassign_target( $user_id ) {
		$target = (int) apply_filters( 'l2d_ai_delete_reassign_user', 0, $user_id );
		if ( $target > 0 && $target !== (int) $user_id && user_can( $target, 'manage_options' ) ) {
			return $target;
		}

		$admins = get_users(
			array(
				'role'    => 'administrator',
				'number'  => 1,
				'exclude' => array( (int) $user_id ),
				'fields'  => 'ID',
			)
		);
		if ( empty( $admins ) ) {
			return 0;
		}

		return (int) $admins[0];
	}

	// ------------------------------------------------------------------
	// 墓碑与额度继承
	// ------------------------------------------------------------------

	/** 删除前抓住邮箱：deleted_user 触发时用户数据已经不在了。 */
	public static function capture_deletion( $user_id, $reassign = null, $user = null ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return;
		}

		if ( ! $user instanceof WP_User ) {
			$user = get_userdata( $user_id );
		}
		if ( ! $user instanceof WP_User ) {
			return;
		}

		$email = strtolower( trim( (string) $user->user_email ) );
		if ( '' !== $email ) {
			self::$pending_emails[ $user_id ] = $email;
		}

		// 用户 meta 在 deleted_user 时已经没了，必须在这里读。
		self::$pending_verified[ $user_id ] = class_exists( 'L2D_AI_Email_Verification' )
			&& L2D_AI_Email_Verification::is_verified( $user_id );
	}

	/** 删除成功后写墓碑；删除失败不会触发 deleted_user，也就不会留下假记录。 */
	public static function record_tombstone( $user_id, $reassign = null, $user = null ) {
		$user_id = (int) $user_id;
		$email   = isset( self::$pending_emails[ $user_id ] )
			? (string) self::$pending_emails[ $user_id ]
			: '';
		unset( self::$pending_emails[ $user_id ] );

		if ( '' === $email && $user instanceof WP_User ) {
			$email = strtolower( trim( (string) $user->user_email ) );
		}

		$is_self_delete = ! empty( self::$self_deleted[ $user_id ] );
		unset( self::$self_deleted[ $user_id ] );

		$was_verified = ! empty( self::$pending_verified[ $user_id ] );
		unset( self::$pending_verified[ $user_id ] );

		// 未验证账号的邮箱本来就没有被证明属于本人，不留可继承的记录。
		if ( '' === $email || ! $is_self_delete || ! $was_verified ) {
			return;
		}

		$key = self::email_key( $email );
		if ( '' === $key ) {
			return;
		}

		$all = get_option( self::TOMBSTONE_OPTION, array() );
		if ( ! is_array( $all ) ) {
			$all = array();
		}

		$now = time();
		foreach ( $all as $hash => $entry ) {
			$deleted_at = is_array( $entry ) && isset( $entry['deleted_at'] )
				? (int) $entry['deleted_at']
				: 0;
			if ( $deleted_at <= 0 || ( $now - $deleted_at ) > self::TOMBSTONE_TTL ) {
				unset( $all[ $hash ] );
			}
		}

		$ids = array();
		if ( isset( $all[ $key ]['user_ids'] ) && is_array( $all[ $key ]['user_ids'] ) ) {
			foreach ( $all[ $key ]['user_ids'] as $id ) {
				$id = (int) $id;
				if ( $id > 0 ) {
					$ids[] = $id;
				}
			}
		}
		if ( ! in_array( $user_id, $ids, true ) ) {
			$ids[] = $user_id;
		}
		$ids = array_slice( array_values( array_unique( $ids ) ), -self::MAX_SOURCES );

		$all[ $key ] = array(
			'user_ids'   => $ids,
			'deleted_at' => $now,
		);
		update_option( self::TOMBSTONE_OPTION, $all, false );
	}

	/**
	 * 如果当前邮箱有注销记录，请后端把旧账号的额度并过来。
	 *
	 * 返回 true 表示这次真的完成了继承；失败时保留墓碑，下次换 token
	 * 或打开资料页会自动重试，不阻塞用户正常聊天。
	 */
	public static function inherit_quota( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return false;
		}

		// 没有验证邮箱就没有“同一个邮箱属于同一个人”的依据。
		if (
			! class_exists( 'L2D_AI_Email_Verification' )
			|| ! L2D_AI_Email_Verification::is_verified( $user_id )
		) {
			return false;
		}

		// 站点可以按需关掉继承，例如运营活动明确要求“注销即清零”。
		if ( ! apply_filters( 'l2d_ai_quota_inherit', true, $user_id ) ) {
			return false;
		}

		$user = get_userdata( $user_id );
		if ( ! $user instanceof WP_User ) {
			return false;
		}

		$key = self::email_key( (string) $user->user_email );
		if ( '' === $key ) {
			return false;
		}

		$all = get_option( self::TOMBSTONE_OPTION, array() );
		if ( ! is_array( $all ) || ! isset( $all[ $key ] ) || ! is_array( $all[ $key ] ) ) {
			return false;
		}

		$ids = array();
		if ( isset( $all[ $key ]['user_ids'] ) && is_array( $all[ $key ]['user_ids'] ) ) {
			foreach ( $all[ $key ]['user_ids'] as $id ) {
				$id = (int) $id;
				if ( $id > 0 && $id !== $user_id ) {
					$ids[] = $id;
				}
			}
		}
		$ids = array_slice( array_values( array_unique( $ids ) ), -self::MAX_SOURCES );

		if ( empty( $ids ) ) {
			unset( $all[ $key ] );
			update_option( self::TOMBSTONE_OPTION, $all, false );
			return false;
		}

		$result = L2D_AI_Plugin::user_backend_request(
			$user_id,
			'POST',
			'/me/inherit-quota',
			null,
			array( 'transfer_from' => $ids )
		);
		if ( is_wp_error( $result ) ) {
			// 只记用户 ID 和错误，不记邮箱。
			error_log(
				'L2D AI 额度继承失败 user=' . $user_id . ': ' . $result->get_error_message()
			);
			return false;
		}

		unset( $all[ $key ] );
		update_option( self::TOMBSTONE_OPTION, $all, false );

		if ( isset( $result['conflicts'] ) && is_array( $result['conflicts'] ) && $result['conflicts'] ) {
			error_log(
				'L2D AI 额度继承冲突 user=' . $user_id
				. ' from=' . implode( ',', array_map( 'intval', $result['conflicts'] ) )
			);
		}

		return true;
	}

	/** 邮箱哈希：带站点 salt 的 HMAC，墓碑被拖走也不能直接反查邮箱。 */
	private static function email_key( $email ) {
		$email = strtolower( trim( (string) $email ) );
		if ( '' === $email ) {
			return '';
		}
		return hash_hmac( 'sha256', 'l2d-ai-quota|' . $email, wp_salt( 'auth' ) );
	}

	// ------------------------------------------------------------------
	// 表单辅助
	// ------------------------------------------------------------------

	private static function authorize_self() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		$user_id = isset( $_POST['user_id'] ) ? absint( $_POST['user_id'] ) : 0;
		if (
			$user_id <= 0
			|| $user_id !== get_current_user_id()
			|| ! current_user_can( 'edit_user', $user_id )
		) {
			wp_die( '只能修改自己的账号', '无权限', array( 'response' => 403 ) );
		}

		return $user_id;
	}

	private static function notice( $message, $is_error = false ) {
		set_transient(
			'l2d_ai_account_notice_' . get_current_user_id(),
			array(
				'message' => (string) $message,
				'error'   => (bool) $is_error,
			),
			60
		);
	}

	private static function fail( $message ) {
		self::notice( $message, true );
		self::redirect( 'error' );
	}

	private static function redirect( $status = 'updated' ) {
		wp_safe_redirect(
			add_query_arg(
				'l2d-ai-account',
				sanitize_key( $status ),
				admin_url( 'profile.php' )
			)
		);
		exit;
	}
}
