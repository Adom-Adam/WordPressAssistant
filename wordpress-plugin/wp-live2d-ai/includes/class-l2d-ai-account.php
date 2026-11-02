<?php
/**
 * 账号自助：改昵称、注销账号，以及注销后的额度继承。
 *
 * ==================================================================
 * 用户名与昵称
 * ==================================================================
 * WordPress 里有两个容易混淆的字段：
 * - `user_login`（用户名）是登录凭据，全局唯一，还参与登录 cookie、
 *   作者页别名等；改它风险高，注册后直接锁定；
 * - `display_name`（显示名）是评论、导航、助手面板里给别人看的名字，
 *   可以随时改。昵称/显示名不另做表单，沿用资料页自带字段；保存时如果
 *   原来的显示名就是昵称，会跟着新昵称走，避免两个地方各显示一个名字。
 *
 * 中文用户名的放行仍由 L2D_AI_Chinese_Usernames 的过滤器负责，
 * 但只用于注册，不再提供改名入口。
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

	/** 同步 display_name 时避免 wp_update_user() 再次触发 profile_update 造成递归。 */
	private static $syncing_display_name = false;

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
		add_action( 'admin_footer', array( __CLASS__, 'render_delete_form' ) );
		add_action( 'profile_update', array( __CLASS__, 'sync_display_name' ), 10, 2 );
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

			<h3 style="margin-top:0;">账号信息</h3>
			<p style="margin:0 0 8px;">
				<strong>用户名</strong>：
				<code><?php echo esc_html( (string) $user->user_login ); ?></code>
				<span style="color:#646970;">（登录用，注册后不可更改）</span>
			</p>
			<p style="margin:0 0 16px;color:#646970;">
				<?php
				echo esc_html(
					'昵称和显示名请使用上方个人设置里的「昵称」「显示名」字段，改完点页面底部的「更新个人资料」；当前显示名：'
				);
				?>
				<strong><?php echo esc_html( (string) $user->display_name ); ?></strong>
			</p>

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
				<p style="margin:0 0 8px;">
					<label for="l2d-ai-delete-password"><strong>当前密码</strong></label><br>
					<input
						type="password"
						id="l2d-ai-delete-password"
						name="delete_password"
						class="regular-text"
						form="l2d-ai-delete-account-form"
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
						form="l2d-ai-delete-account-form"
						autocomplete="off"
						required
					>
				</p>
				<button
					type="submit"
					class="button button-link-delete"
					form="l2d-ai-delete-account-form"
				>永久注销账号</button>
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
	// 资料页更新：昵称跟随显示名
	// ------------------------------------------------------------------

	/**
	 * 用户点「更新个人资料」后，如果原来的显示名就是昵称，就让它跟着新昵称走。
	 *
	 * WordPress 的「昵称」和「显示名」是两个字段：只改昵称时，公开显示名
	 * 不会自动变化。这里只在“显示名原本等于昵称”时同步，用户如果主动在
	 * 「显示名」下拉框里选了别的值，就尊重他的选择，不覆盖。
	 *
	 * @param int     $user_id       用户 ID。
	 * @param WP_User $old_user_data 更新前的用户对象。
	 */
	public static function sync_display_name( $user_id, $old_user_data ) {
		if ( self::$syncing_display_name || ! isset( $_POST['nickname'] ) ) {
			return;
		}

		$user_id = (int) $user_id;
		$user    = get_userdata( $user_id );
		if ( $user_id <= 0 || ! $user instanceof WP_User ) {
			return;
		}

		$nickname = trim( sanitize_text_field( wp_unslash( $_POST['nickname'] ) ) );
		if ( '' === $nickname ) {
			return;
		}

		$old_display  = isset( $old_user_data->display_name )
			? (string) $old_user_data->display_name
			: '';
		$old_nickname = isset( $old_user_data->nickname )
			? (string) $old_user_data->nickname
			: '';

		// 原来的显示名不是昵称，说明用户一直用别的公开名字；不擅自改。
		if ( $old_display !== $old_nickname ) {
			return;
		}

		// 用户在「显示名」下拉框里主动选了别的值：保留他的选择。
		$posted_display = isset( $_POST['display_name'] )
			? sanitize_text_field( wp_unslash( $_POST['display_name'] ) )
			: null;
		if (
			null !== $posted_display
			&& $posted_display !== $old_display
			&& $posted_display !== $nickname
		) {
			return;
		}

		if ( $user->display_name === $nickname ) {
			return;
		}

		self::$syncing_display_name = true;
		wp_update_user(
			array(
				'ID'           => $user_id,
				'display_name' => $nickname,
			)
		);
		self::$syncing_display_name = false;
	}

	// ------------------------------------------------------------------
	// 注销表单的容器
	// ------------------------------------------------------------------

	/**
	 * 资料页的「更新个人资料」本身是一个 `<form>`，注销表单不能再嵌一个
	 * `<form>`，否则浏览器会提前关掉外层表单，底部的「更新个人资料」
	 * 按钮就会点了没反应。
	 *
	 * 这里把注销表单的容器放到页面底部（外层表单之外），卡片里的密码框、
	 * 确认框和按钮用 HTML5 的 `form="..."` 属性关联过去。这样两个表单
	 * 各走各的，互不嵌套。
	 */
	public static function render_delete_form() {
		if ( ! is_admin() || ! function_exists( 'get_current_screen' ) ) {
			return;
		}
		$screen = get_current_screen();
		if ( ! $screen || 'profile' !== $screen->id ) {
			return;
		}

		$user_id = get_current_user_id();
		if ( $user_id <= 0 || user_can( $user_id, 'manage_options' ) ) {
			return;
		}
		?>
		<form
			id="l2d-ai-delete-account-form"
			method="post"
			action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
			onsubmit="return confirm('这是不可撤销的操作，确定要注销吗？');"
			style="display:none;"
		>
			<input type="hidden" name="action" value="l2d_ai_delete_account">
			<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>">
			<?php wp_nonce_field( self::NONCE_ACTION ); ?>
		</form>
		<?php
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
