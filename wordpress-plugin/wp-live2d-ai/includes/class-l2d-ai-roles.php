<?php
/**
 * 关系角色名单：后台给每个账号定“对我有多重要”。
 *
 * ==================================================================
 * 角色不是权限
 * ==================================================================
 * owner / exception / special / normal 只决定好感的天花板和成长速度，
 * 不影响能不能聊天、能用多少条。改角色也不会把已经攒下的好感收回去——
 * 这一点必须在页面上写清楚，否则站长会以为改成 normal 就是“降级处罚”。
 *
 * 上限从后端当前人设里读（`GET /admin/roles` 的 `role_caps`），不在这里
 * 写死：人设文件改过之后，后台显示的数字要跟着变。
 *
 * ==================================================================
 * 为什么可以直接设一个没来过的用户
 * ==================================================================
 * 站长的常见动作是“先把这个号设成 exception，等他哪天来聊”。后端对这种
 * 请求会先建一行再改角色，所以页面上提供一个手填 WP 用户 ID 的入口，
 * 不必等对方产生档案。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Roles {

	const PAGE_SLUG    = 'l2d-ai-roles';
	const TRANSIENT    = 'l2d_ai_roles_notice_';

	/** 角色名 => 页面上的中文说法。顺序就是下拉框的顺序。 */
	const ROLE_LABELS = array(
		'owner'     => '主人',
		'exception' => '例外',
		'special'   => '特别的人',
		'normal'    => '普通人',
	);

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_save_role', array( __CLASS__, 'handle_save' ) );
		add_action( 'admin_post_l2d_ai_reset_role', array( __CLASS__, 'handle_reset' ) );
	}

	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · 关系角色',
			'Live2D 小助手 · 关系角色',
			'manage_options',
			self::PAGE_SLUG,
			array( __CLASS__, 'render_page' )
		);
	}

	public static function role_label( $role ) {
		return isset( self::ROLE_LABELS[ $role ] ) ? self::ROLE_LABELS[ $role ] : '普通人';
	}

	/**
	 * 角色下拉框。QQ 白名单页也要用（那里同样能设角色），
	 * 所以标签和顺序只在这里定义一次。
	 */
	public static function render_role_select( $selected = 'normal', $name = 'role' ) {
		echo '<select name="' . esc_attr( $name ) . '">';
		foreach ( self::ROLE_LABELS as $role => $label ) {
			printf(
				'<option value="%s" %s>%s</option>',
				esc_attr( $role ),
				selected( $role, (string) $selected, false ),
				esc_html( $label )
			);
		}
		echo '</select>';
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

	private static function valid_role( $role ) {
		return isset( self::ROLE_LABELS[ $role ] );
	}

	// ------------------------------------------------------------------
	// 操作处理
	// ------------------------------------------------------------------

	public static function handle_save() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_roles' );

		$user_id = isset( $_POST['wp_user_id'] ) ? absint( $_POST['wp_user_id'] ) : 0;
		$role    = isset( $_POST['role'] ) ? sanitize_key( wp_unslash( $_POST['role'] ) ) : '';

		if ( $user_id <= 0 ) {
			self::redirect_with_notice( '请填写有效的 WP 用户 ID。', true );
		}
		if ( ! self::valid_role( $role ) ) {
			self::redirect_with_notice( '请选择有效的角色。', true );
		}

		$result = L2D_AI_Plugin::request_backend(
			'PUT',
			'/admin/roles',
			array(
				'wp_user_id' => $user_id,
				'role'       => $role,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice(
			sprintf(
				'用户 #%d 已设为「%s」。',
				$user_id,
				self::role_label( $role )
			),
			false,
			array( 's' => self::search() )
		);
	}

	public static function handle_reset() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_roles' );

		$user_id = isset( $_POST['wp_user_id'] ) ? absint( $_POST['wp_user_id'] ) : 0;
		if ( $user_id <= 0 ) {
			self::redirect_with_notice( '参数不完整，未做任何修改。', true );
		}

		$result = L2D_AI_Plugin::request_backend( 'DELETE', '/admin/roles/' . $user_id );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice(
			sprintf( '用户 #%d 已恢复为普通人。', $user_id ),
			false,
			array( 's' => self::search() )
		);
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------

	public static function render_page() {
		$search = self::search();
		$path   = '/admin/roles';
		if ( '' !== $search ) {
			$path .= '?query=' . rawurlencode( $search );
		}

		$data     = L2D_AI_Plugin::request_backend( 'GET', $path );
		$backend_ok = ! is_wp_error( $data );
		$roles    = $backend_ok && isset( $data['roles'] ) ? (array) $data['roles'] : array();
		$caps     = $backend_ok && isset( $data['role_caps'] ) ? (array) $data['role_caps'] : array();

		$notice = get_transient( self::notice_transient() );
		delete_transient( self::notice_transient() );
		?>
		<div class="wrap">
			<h1>关系角色</h1>

			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( ! $backend_ok ) : ?>
				<div class="notice notice-error">
					<p>后端不可达，角色名单暂时读不到：<?php echo esc_html( $data->get_error_message() ); ?></p>
				</div>
			<?php endif; ?>

			<p class="description">
				角色只影响好感的<b>上限</b>和之后的增长，<b>不会扣减</b>已经攒下的好感。
				管理员自己永远是「主人」，不受这里设置影响。
			</p>

			<?php if ( $backend_ok && $caps ) : ?>
				<p class="description">
					当前上限：
					<?php
					$parts = array();
					foreach ( self::ROLE_LABELS as $role => $label ) {
						if ( isset( $caps[ $role ] ) ) {
							$parts[] = $label . ' ' . $caps[ $role ];
						}
					}
					echo esc_html( implode( '，', $parts ) );
					?>
				</p>
			<?php endif; ?>

			<form method="get" action="<?php echo esc_url( admin_url( 'options-general.php' ) ); ?>" style="margin: 1em 0;">
				<input type="hidden" name="page" value="<?php echo esc_attr( self::PAGE_SLUG ); ?>" />
				<input type="search" name="s" value="<?php echo esc_attr( $search ); ?>" placeholder="搜索昵称或 WP 用户 ID" />
				<?php submit_button( '搜索', 'secondary', '', false ); ?>
			</form>

			<?php self::render_table( $roles, $caps, $backend_ok ); ?>

			<hr />

			<h2>直接设置一个用户</h2>
			<p class="description">对方还没来聊过也可以先设好，档案会在第一次接触时建立。</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_save_role" />
				<?php wp_nonce_field( 'l2d_ai_roles' ); ?>
				<input type="number" name="wp_user_id" min="1" class="small-text" placeholder="WP 用户 ID" />
				<select name="role">
					<?php foreach ( self::ROLE_LABELS as $role => $label ) : ?>
						<option value="<?php echo esc_attr( $role ); ?>"><?php echo esc_html( $label ); ?></option>
					<?php endforeach; ?>
				</select>
				<?php submit_button( '保存角色', 'secondary', '', false ); ?>
			</form>
		</div>
		<?php
	}

	private static function render_table( array $roles, array $caps, $backend_ok ) {
		?>
		<table class="widefat striped">
			<thead>
				<tr>
					<th>WP 用户 ID</th>
					<th>昵称</th>
					<th>角色</th>
					<th>好感上限</th>
					<th>操作</th>
				</tr>
			</thead>
			<tbody>
			<?php if ( empty( $roles ) ) : ?>
				<tr><td colspan="5"><?php echo $backend_ok ? '还没有人产生过关系档案。' : '—'; ?></td></tr>
			<?php endif; ?>
			<?php foreach ( $roles as $entry ) : ?>
				<?php
				$user_id = isset( $entry['wp_user_id'] ) ? (int) $entry['wp_user_id'] : 0;
				$role    = isset( $entry['role'] ) ? (string) $entry['role'] : 'normal';
				$cap     = isset( $entry['cap'] ) ? $entry['cap'] : ( isset( $caps[ $role ] ) ? $caps[ $role ] : '—' );
				$name    = isset( $entry['display_name'] ) && '' !== (string) $entry['display_name']
					? (string) $entry['display_name']
					: '（没有昵称）';
				?>
				<tr>
					<td><?php echo esc_html( (string) $user_id ); ?></td>
					<td><?php echo esc_html( $name ); ?></td>
					<td><?php echo esc_html( self::role_label( $role ) ); ?></td>
					<td><?php echo esc_html( (string) $cap ); ?></td>
					<td>
						<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block; margin-right: 8px;">
							<input type="hidden" name="action" value="l2d_ai_save_role" />
							<input type="hidden" name="wp_user_id" value="<?php echo esc_attr( (string) $user_id ); ?>" />
							<?php wp_nonce_field( 'l2d_ai_roles' ); ?>
							<select name="role">
								<?php foreach ( self::ROLE_LABELS as $value => $label ) : ?>
									<option value="<?php echo esc_attr( $value ); ?>" <?php selected( $value, $role ); ?>><?php echo esc_html( $label ); ?></option>
								<?php endforeach; ?>
							</select>
							<button type="submit" class="button button-small">保存</button>
						</form>
						<?php if ( 'normal' !== $role ) : ?>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block;">
								<input type="hidden" name="action" value="l2d_ai_reset_role" />
								<input type="hidden" name="wp_user_id" value="<?php echo esc_attr( (string) $user_id ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_roles' ); ?>
								<button type="submit" class="button button-small">恢复普通人</button>
							</form>
						<?php endif; ?>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php
	}
}
