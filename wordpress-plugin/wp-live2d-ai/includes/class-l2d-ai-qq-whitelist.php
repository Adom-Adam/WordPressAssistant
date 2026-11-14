<?php
/**
 * QQ 白名单：让没注册博客的熟人也能被记住。
 *
 * ==================================================================
 * 名单不是“绑定”
 * ==================================================================
 * 加进名单的 QQ 仍然以 `qq:<号>` 的身份存在，不会挂到任何 WP 账号上；
 * 它只是换来“按有身份的人对待”——记忆、关系、主动消息、普通额度。
 * 这一页不负责绑定，绑定在个人资料页。
 *
 * ==================================================================
 * 两种删除，意图完全不同
 * ==================================================================
 * - 移出名单：不再特殊对待，但历史数据留着休眠。以后加回来还能接着聊，
 *   所以它不该被写成“删除”。
 * - 彻底删除：连着这个 QQ 的记忆、事件、关系一起清掉。这是隐私要求，
 *   也是唯一一个删完就找不回来的按钮，必须二次确认。
 *
 * 已经绑定过账号的 QQ 不能加进来（后端返回 409）：身份来自账号，
 * 名单里再加一行会出现两个互相矛盾的角色设置。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_QQ_Whitelist {

	const PAGE_SLUG = 'l2d-ai-qq-whitelist';
	const TRANSIENT = 'l2d_ai_qq_whitelist_notice_';
	const PER_PAGE  = 20;

	/** 与后端 `QQWhitelistAdd.role` 的默认值保持一致。 */
	const DEFAULT_ROLE = 'special';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_qq_whitelist_add', array( __CLASS__, 'handle_add' ) );
		add_action( 'admin_post_l2d_ai_qq_whitelist_update', array( __CLASS__, 'handle_update' ) );
		add_action( 'admin_post_l2d_ai_qq_whitelist_remove', array( __CLASS__, 'handle_remove' ) );
		add_action( 'admin_post_l2d_ai_qq_whitelist_purge', array( __CLASS__, 'handle_purge' ) );
	}

	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · QQ 白名单',
			'Live2D 小助手 · QQ 白名单',
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

	private static function notice_transient() {
		return self::TRANSIENT . get_current_user_id();
	}

	private static function redirect_with_notice( $message, $is_error = false, array $extra = array() ) {
		set_transient(
			self::notice_transient(),
			array(
				'message' => (string) $message,
				'error'   => (bool) $is_error,
			),
			60
		);
		wp_safe_redirect( self::page_url( $extra ) );
		exit;
	}

	/** 当前搜索词。空串表示不过滤。 */
	private static function search() {
		return isset( $_GET['s'] ) ? sanitize_text_field( wp_unslash( $_GET['s'] ) ) : '';
	}

	/**
	 * 跳转时把搜索和页码带回去，避免改完一个人就回到第一页。
	 *
	 * 表单提交到 admin-post.php，那个请求里没有本页的查询参数，
	 * 所以搜索词和页码由表单的隐藏字段带过来。
	 */
	private static function back_args() {
		$args = array();
		if ( isset( $_POST['back_s'] ) && '' !== $_POST['back_s'] ) {
			$args['s'] = sanitize_text_field( wp_unslash( $_POST['back_s'] ) );
		}
		if ( isset( $_POST['back_paged'] ) ) {
			$paged = absint( $_POST['back_paged'] );
			if ( $paged > 1 ) {
				$args['paged'] = $paged;
			}
		}
		return $args;
	}

	/** 后端的 409 要翻译成“去角色名单里设置”，否则站长不知道下一步去哪。 */
	private static function error_message( WP_Error $error ) {
		$data   = $error->get_error_data();
		$status = is_array( $data ) && isset( $data['status'] ) ? (int) $data['status'] : 0;
		if ( 409 === $status ) {
			return '这个 QQ 已经绑定了账号，请到「关系角色」页里设置它的角色。';
		}
		return $error->get_error_message();
	}

	private static function qq_from_request() {
		$qq = isset( $_POST['qq_uin'] ) ? sanitize_text_field( wp_unslash( $_POST['qq_uin'] ) ) : '';
		if ( ! preg_match( '/^\d{5,12}$/', $qq ) ) {
			self::redirect_with_notice( 'QQ 号必须是 5 到 12 位数字。', true );
		}
		return $qq;
	}

	// ------------------------------------------------------------------
	// 操作处理
	// ------------------------------------------------------------------

	public static function handle_add() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_qq_whitelist' );

		$qq   = self::qq_from_request();
		$role = isset( $_POST['role'] ) ? sanitize_key( wp_unslash( $_POST['role'] ) ) : self::DEFAULT_ROLE;
		$note = isset( $_POST['note'] ) ? sanitize_text_field( wp_unslash( $_POST['note'] ) ) : '';

		$result = L2D_AI_Plugin::request_backend(
			'POST',
			'/admin/qq-whitelist',
			array(
				'qq_uin' => $qq,
				'role'   => $role,
				'note'   => '' !== $note ? $note : null,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( self::error_message( $result ), true, self::back_args() );
		}

		self::redirect_with_notice( sprintf( 'QQ %s 已加入白名单。', $qq ), false, self::back_args() );
	}

	public static function handle_update() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_qq_whitelist' );

		$qq   = self::qq_from_request();
		$role = isset( $_POST['role'] ) ? sanitize_key( wp_unslash( $_POST['role'] ) ) : '';
		$note = isset( $_POST['note'] ) ? sanitize_text_field( wp_unslash( $_POST['note'] ) ) : '';

		$result = L2D_AI_Plugin::request_backend(
			'PUT',
			'/admin/qq-whitelist/' . rawurlencode( $qq ),
			array(
				'role' => $role,
				// 空串会被后端当成“不要改备注”，所以这里传 null 表意更清楚。
				'note' => '' !== $note ? $note : null,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( self::error_message( $result ), true, self::back_args() );
		}

		self::redirect_with_notice( sprintf( 'QQ %s 已更新。', $qq ), false, self::back_args() );
	}

	public static function handle_remove() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_qq_whitelist' );

		$qq     = self::qq_from_request();
		$result = L2D_AI_Plugin::request_backend(
			'DELETE',
			'/admin/qq-whitelist/' . rawurlencode( $qq )
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		self::redirect_with_notice(
			sprintf( 'QQ %s 已移出名单，历史记忆保留；再加回来还能接着聊。', $qq ),
			false,
			self::back_args()
		);
	}

	public static function handle_purge() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_qq_whitelist' );

		$qq     = self::qq_from_request();
		$result = L2D_AI_Plugin::request_backend(
			'DELETE',
			'/admin/qq-whitelist/' . rawurlencode( $qq ) . '/data'
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		self::redirect_with_notice(
			sprintf( 'QQ %s 的全部数据已删除，且已移出名单。', $qq ),
			false,
			self::back_args()
		);
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------

	/** 当前搜索词和页码，供表单的隐藏字段使用。 */
	private static $current = array(
		's'     => '',
		'paged' => 1,
	);

	private static function hidden_back_fields() {
		printf(
			'<input type="hidden" name="back_s" value="%s" />',
			esc_attr( self::$current['s'] )
		);
		printf(
			'<input type="hidden" name="back_paged" value="%d" />',
			(int) self::$current['paged']
		);
	}

	/** 给每条名单补上已绑定账号的用户名和昵称（未绑定就是空串）。 */
	private static function with_bound_accounts( array $entries ) {
		foreach ( $entries as $index => $entry ) {
			$login = '';
			$name  = '';
			$bound = isset( $entry['bound_user_id'] ) ? (int) $entry['bound_user_id'] : 0;
			if ( $bound > 0 ) {
				$user = get_userdata( $bound );
				if ( $user ) {
					$login = (string) $user->user_login;
					$name  = (string) $user->display_name;
				}
			}
			$entries[ $index ]['bound_user_login']   = $login;
			$entries[ $index ]['bound_display_name'] = $name;
		}
		return $entries;
	}

	/** 模糊匹配：任意一个字段包含搜索词就算命中。 */
	private static function any_contains( array $values, $search ) {
		foreach ( $values as $value ) {
			if ( false !== stripos( (string) $value, $search ) ) {
				return true;
			}
		}
		return false;
	}

	private static function filter_entries( array $entries, $search ) {
		if ( '' === $search ) {
			return $entries;
		}
		$filtered = array();
		foreach ( $entries as $entry ) {
			if ( self::any_contains(
				array(
					isset( $entry['qq_uin'] ) ? $entry['qq_uin'] : '',
					isset( $entry['note'] ) ? $entry['note'] : '',
					isset( $entry['bound_user_login'] ) ? $entry['bound_user_login'] : '',
					isset( $entry['bound_display_name'] ) ? $entry['bound_display_name'] : '',
				),
				$search
			) ) {
				$filtered[] = $entry;
			}
		}
		return $filtered;
	}

	/** 好感显示：有状态就显示数值，没有就标明是角色的初始值。 */
	private static function affection_text( array $entry ) {
		if ( isset( $entry['affection'] ) && null !== $entry['affection'] ) {
			return number_format_i18n( (float) $entry['affection'], 1 );
		}
		$initial = isset( $entry['affection_initial'] ) ? $entry['affection_initial'] : '—';
		return '初始 ' . $initial;
	}

	public static function render_page() {
		$search = self::search();
		$paged  = isset( $_GET['paged'] ) ? max( 1, absint( $_GET['paged'] ) ) : 1;

		$data       = L2D_AI_Plugin::request_backend( 'GET', '/admin/qq-whitelist' );
		$backend_ok = ! is_wp_error( $data );
		$entries    = $backend_ok && isset( $data['entries'] ) ? (array) $data['entries'] : array();

		$entries = self::filter_entries( self::with_bound_accounts( $entries ), $search );
		$total   = count( $entries );
		$pages   = max( 1, (int) ceil( $total / self::PER_PAGE ) );
		$paged   = min( $paged, $pages );
		$rows    = array_slice( $entries, ( $paged - 1 ) * self::PER_PAGE, self::PER_PAGE );

		self::$current = array(
			's'     => $search,
			'paged' => $paged,
		);

		$notice = get_transient( self::notice_transient() );
		delete_transient( self::notice_transient() );
		?>
		<div class="wrap">
			<h1>QQ 白名单</h1>

			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( ! $backend_ok ) : ?>
				<div class="notice notice-error">
					<p>后端不可达，白名单暂时读不到：<?php echo esc_html( $data->get_error_message() ); ?></p>
				</div>
			<?php endif; ?>

			<p class="description">
				名单里的 QQ 不需要注册博客，也能被记住、被主动找。
				它们仍然以 QQ 号作为身份，不会挂到任何账号上。
			</p>

			<h2>加入白名单</h2>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_qq_whitelist_add" />
				<?php wp_nonce_field( 'l2d_ai_qq_whitelist' ); ?>
				<?php self::hidden_back_fields(); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="l2d-ai-whitelist-qq">QQ 号</label></th>
						<td><input type="text" id="l2d-ai-whitelist-qq" name="qq_uin" class="regular-text" placeholder="5 到 12 位数字" /></td>
					</tr>
					<tr>
						<th scope="row">角色</th>
						<td>
							<?php L2D_AI_Roles::render_role_select( self::DEFAULT_ROLE ); ?>
							<p class="description">角色决定好感上限，只影响之后的增长，不会扣当前好感。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">备注</th>
						<td><input type="text" name="note" class="regular-text" placeholder="比如：站长的备用号" /></td>
					</tr>
				</table>
				<?php submit_button( '加入白名单', 'secondary' ); ?>
			</form>

			<h2>名单</h2>

			<form method="get" action="<?php echo esc_url( admin_url( 'options-general.php' ) ); ?>" style="margin: 1em 0;">
				<input type="hidden" name="page" value="<?php echo esc_attr( self::PAGE_SLUG ); ?>" />
				<input type="search" name="s" value="<?php echo esc_attr( $search ); ?>" placeholder="搜索 QQ 号 / 备注 / 绑定账号" />
				<?php submit_button( '搜索', 'secondary', '', false ); ?>
			</form>

			<table class="widefat striped">
				<thead>
					<tr>
						<th>QQ</th>
						<th>用户名</th>
						<th>昵称</th>
						<th>角色</th>
						<th>好感</th>
						<th>好感上限</th>
						<th>备注</th>
						<th>添加时间</th>
						<th>绑定状态</th>
						<th>操作</th>
					</tr>
				</thead>
				<tbody>
				<?php if ( empty( $rows ) ) : ?>
					<tr><td colspan="10"><?php echo $backend_ok ? ( '' !== $search ? '没有匹配的白名单条目。' : '名单里还没有人。' ) : '—'; ?></td></tr>
				<?php endif; ?>
				<?php foreach ( $rows as $entry ) : ?>
					<?php
					$qq       = isset( $entry['qq_uin'] ) ? (string) $entry['qq_uin'] : '';
					$role     = isset( $entry['role'] ) ? (string) $entry['role'] : 'normal';
					$cap      = isset( $entry['cap'] ) ? $entry['cap'] : '—';
					$note     = isset( $entry['note'] ) ? (string) $entry['note'] : '';
					$added_at = isset( $entry['added_at'] ) ? (string) $entry['added_at'] : '';
					$bound    = isset( $entry['bound_user_id'] ) ? $entry['bound_user_id'] : null;
					$login    = isset( $entry['bound_user_login'] ) ? (string) $entry['bound_user_login'] : '';
					$name     = isset( $entry['bound_display_name'] ) ? (string) $entry['bound_display_name'] : '';
					?>
					<tr>
						<td><code><?php echo esc_html( $qq ); ?></code></td>
						<td><?php echo '' !== $login ? esc_html( $login ) : '没有'; ?></td>
						<td><?php echo '' !== $name ? esc_html( $name ) : '没有'; ?></td>
						<td><?php echo esc_html( L2D_AI_Roles::role_label( $role ) ); ?></td>
						<td><?php echo esc_html( self::affection_text( $entry ) ); ?></td>
						<td><?php echo esc_html( (string) $cap ); ?></td>
						<td><?php echo '' !== $note ? esc_html( $note ) : '—'; ?></td>
						<td><?php echo '' !== $added_at ? esc_html( $added_at ) : '—'; ?></td>
						<td>
							<?php
							if ( null === $bound ) {
								echo '未绑定';
							} else {
								echo esc_html( sprintf( '已绑定到用户 #%d', (int) $bound ) );
							}
							?>
						</td>
						<td>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-bottom: 8px;">
								<input type="hidden" name="action" value="l2d_ai_qq_whitelist_update" />
								<input type="hidden" name="qq_uin" value="<?php echo esc_attr( $qq ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_qq_whitelist' ); ?>
								<?php self::hidden_back_fields(); ?>
								<?php L2D_AI_Roles::render_role_select( $role ); ?>
								<input type="text" name="note" value="<?php echo esc_attr( $note ); ?>" placeholder="备注" class="regular-text" />
								<button type="submit" class="button button-small">保存</button>
							</form>

							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block; margin-right: 8px;">
								<input type="hidden" name="action" value="l2d_ai_qq_whitelist_remove" />
								<input type="hidden" name="qq_uin" value="<?php echo esc_attr( $qq ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_qq_whitelist' ); ?>
								<?php self::hidden_back_fields(); ?>
								<button type="submit" class="button button-small">移出名单</button>
							</form>

							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block;">
								<input type="hidden" name="action" value="l2d_ai_qq_whitelist_purge" />
								<input type="hidden" name="qq_uin" value="<?php echo esc_attr( $qq ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_qq_whitelist' ); ?>
								<?php self::hidden_back_fields(); ?>
								<button type="submit" class="button button-small button-link-delete" onclick="return confirm('这会删除这个 QQ 的全部记忆和关系，无法恢复。确定吗？');">彻底删除数据</button>
							</form>
						</td>
					</tr>
				<?php endforeach; ?>
				</tbody>
			</table>

			<?php if ( $pages > 1 ) : ?>
				<p class="tablenav-pages">
					<?php if ( $paged > 1 ) : ?>
						<a class="button" href="<?php echo esc_url( add_query_arg( array( 'page' => self::PAGE_SLUG, 's' => $search, 'paged' => $paged - 1 ), admin_url( 'options-general.php' ) ) ); ?>">上一页</a>
					<?php endif; ?>
					<span class="description">
						第 <?php echo esc_html( (string) $paged ); ?> / <?php echo esc_html( (string) $pages ); ?> 页，
						共 <?php echo esc_html( (string) $total ); ?> 条
					</span>
					<?php if ( $paged < $pages ) : ?>
						<a class="button" href="<?php echo esc_url( add_query_arg( array( 'page' => self::PAGE_SLUG, 's' => $search, 'paged' => $paged + 1 ), admin_url( 'options-general.php' ) ) ); ?>">下一页</a>
					<?php endif; ?>
				</p>
			<?php endif; ?>
		</div>
		<?php
	}
}
