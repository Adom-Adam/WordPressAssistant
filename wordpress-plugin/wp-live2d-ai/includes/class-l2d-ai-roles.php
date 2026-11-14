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
 * 为什么只列“有过记忆”的账号
 * ==================================================================
 * 名单的主体是后端 `persons` 表里已经有档案的账号。没聊过天的账号没有
 * 好感、没有关系阶段，列进来只是一堆“初始 0”；要预先把某个号设成
 * exception，用下面的「直接设置一个用户」即可。
 *
 * 用户名和昵称从 WordPress 现取：后端只知道第一次建档时记下的昵称，
 * 博主改名之后后台还显示旧名字。账号被删除时回退到后端记下的昵称。
 *
 * ==================================================================
 * 上下两张表
 * ==================================================================
 * 上表是 WordPress 账号（有记忆的），下表是 QQ 白名单里的身份。
 * 白名单 QQ 没有账号，用户名/昵称显示「没有」；万一它已经绑定账号，
 * 就显示绑定的那个账号，方便核对是不是同一个人。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Roles {

	const PAGE_SLUG    = 'l2d-ai-roles';
	const TRANSIENT    = 'l2d_ai_roles_notice_';
	const PER_PAGE     = 20;

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
		add_action( 'admin_post_l2d_ai_save_qq_role', array( __CLASS__, 'handle_save_qq_role' ) );
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

	/**
	 * 当前页面的搜索词和两张表的页码。
	 *
	 * 表单提交到 admin-post.php，那个请求里没有本页的查询参数，
	 * 所以搜索词和页码要由表单的隐藏字段带过来。
	 */
	private static $current = array(
		's'      => '',
		'paged'  => 1,
		'qpaged' => 1,
	);

	/** 跳转时把搜索词和页码带回去，否则改完一个人就回到第一页。 */
	private static function back_args() {
		$args = array();
		if ( isset( $_POST['back_s'] ) && '' !== $_POST['back_s'] ) {
			$args['s'] = sanitize_text_field( wp_unslash( $_POST['back_s'] ) );
		}
		foreach ( array( 'paged', 'qpaged' ) as $key ) {
			$field = 'back_' . $key;
			if ( isset( $_POST[ $field ] ) ) {
				$value = absint( $_POST[ $field ] );
				if ( $value > 1 ) {
					$args[ $key ] = $value;
				}
			}
		}
		return $args;
	}

	/** 三个隐藏字段：搜索词 + 两张表各自的页码。 */
	private static function hidden_back_fields() {
		printf(
			'<input type="hidden" name="back_s" value="%s" />',
			esc_attr( self::$current['s'] )
		);
		printf(
			'<input type="hidden" name="back_paged" value="%d" />',
			(int) self::$current['paged']
		);
		printf(
			'<input type="hidden" name="back_qpaged" value="%d" />',
			(int) self::$current['qpaged']
		);
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
			self::redirect_with_notice( '请填写有效的 WP 用户 ID。', true, self::back_args() );
		}
		if ( ! self::valid_role( $role ) ) {
			self::redirect_with_notice( '请选择有效的角色。', true, self::back_args() );
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
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		self::redirect_with_notice(
			sprintf(
				'用户 #%d 已设为「%s」。',
				$user_id,
				self::role_label( $role )
			),
			false,
			self::back_args()
		);
	}

	/**
	 * QQ 白名单里的角色。白名单页也能改，这里再放一份是因为
	 * 「关系角色」页上看到两张表，站长会顺手在这一页改。
	 */
	public static function handle_save_qq_role() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_roles' );

		$qq   = isset( $_POST['qq_uin'] ) ? sanitize_text_field( wp_unslash( $_POST['qq_uin'] ) ) : '';
		$role = isset( $_POST['role'] ) ? sanitize_key( wp_unslash( $_POST['role'] ) ) : '';

		if ( ! preg_match( '/^\d{5,12}$/', $qq ) ) {
			self::redirect_with_notice( 'QQ 号格式不对。', true, self::back_args() );
		}
		if ( ! self::valid_role( $role ) ) {
			self::redirect_with_notice( '请选择有效的角色。', true, self::back_args() );
		}

		$result = L2D_AI_Plugin::request_backend(
			'PUT',
			'/admin/qq-whitelist/' . rawurlencode( $qq ),
			array(
				'role' => $role,
				// null = 不改备注，这里只动角色。
				'note' => null,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		self::redirect_with_notice(
			sprintf( 'QQ %s 已设为「%s」。', $qq, self::role_label( $role ) ),
			false,
			self::back_args()
		);
	}

	public static function handle_reset() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_roles' );

		$user_id = isset( $_POST['wp_user_id'] ) ? absint( $_POST['wp_user_id'] ) : 0;
		if ( $user_id <= 0 ) {
			self::redirect_with_notice( '参数不完整，未做任何修改。', true, self::back_args() );
		}

		$result = L2D_AI_Plugin::request_backend( 'DELETE', '/admin/roles/' . $user_id );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		self::redirect_with_notice(
			sprintf( '用户 #%d 已恢复为普通人。', $user_id ),
			false,
			self::back_args()
		);
	}

	// ------------------------------------------------------------------
	// 数据整理
	// ------------------------------------------------------------------

	/**
	 * 给每个后端角色行补上 WordPress 的用户名和当前昵称。
	 *
	 * 用一次 `WP_User_Query` 批量取，不逐行 `get_userdata`：
	 * 名单长的时候逐行查会明显变慢。
	 */
	private static function with_accounts( array $roles ) {
		$ids = array();
		foreach ( $roles as $entry ) {
			if ( isset( $entry['wp_user_id'] ) ) {
				$ids[] = (int) $entry['wp_user_id'];
			}
		}

		$accounts = array();
		if ( $ids ) {
			$query = new WP_User_Query(
				array(
					'include' => $ids,
					'number'  => -1,
					'fields'  => array( 'ID', 'user_login', 'display_name' ),
				)
			);
			foreach ( $query->get_results() as $row ) {
				$accounts[ (int) $row->ID ] = array(
					'user_login'   => (string) $row->user_login,
					'display_name' => (string) $row->display_name,
				);
			}
		}

		foreach ( $roles as $index => $entry ) {
			$user_id = isset( $entry['wp_user_id'] ) ? (int) $entry['wp_user_id'] : 0;
			$roles[ $index ]['user_login'] = isset( $accounts[ $user_id ] )
				? $accounts[ $user_id ]['user_login']
				: '';
			// 账号被删掉时，退回后端建档时记下的昵称。
			$roles[ $index ]['wp_display_name'] = isset( $accounts[ $user_id ] )
				? $accounts[ $user_id ]['display_name']
				: ( isset( $entry['display_name'] ) ? (string) $entry['display_name'] : '' );
		}

		return $roles;
	}

	/** 给白名单行补上已绑定账号的用户名和昵称（没绑定就是空串）。 */
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
			$entries[ $index ]['bound_user_login']    = $login;
			$entries[ $index ]['bound_display_name']  = $name;
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

	private static function filter_wp_rows( array $rows, $search ) {
		if ( '' === $search ) {
			return $rows;
		}
		$filtered = array();
		foreach ( $rows as $row ) {
			if ( self::any_contains(
				array(
					isset( $row['wp_user_id'] ) ? $row['wp_user_id'] : '',
					isset( $row['user_login'] ) ? $row['user_login'] : '',
					isset( $row['wp_display_name'] ) ? $row['wp_display_name'] : '',
					isset( $row['display_name'] ) ? $row['display_name'] : '',
				),
				$search
			) ) {
				$filtered[] = $row;
			}
		}
		return $filtered;
	}

	private static function filter_qq_rows( array $rows, $search ) {
		if ( '' === $search ) {
			return $rows;
		}
		$filtered = array();
		foreach ( $rows as $row ) {
			if ( self::any_contains(
				array(
					isset( $row['qq_uin'] ) ? $row['qq_uin'] : '',
					isset( $row['note'] ) ? $row['note'] : '',
					isset( $row['bound_user_login'] ) ? $row['bound_user_login'] : '',
					isset( $row['bound_display_name'] ) ? $row['bound_display_name'] : '',
				),
				$search
			) ) {
				$filtered[] = $row;
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

	/** 把一页数据切出来，并返回总页数。 */
	private static function paginate( array $rows, $paged ) {
		$total = count( $rows );
		$pages = max( 1, (int) ceil( $total / self::PER_PAGE ) );
		$paged = min( max( 1, (int) $paged ), $pages );
		return array(
			'rows'  => array_slice( $rows, ( $paged - 1 ) * self::PER_PAGE, self::PER_PAGE ),
			'page'  => $paged,
			'pages' => $pages,
			'total' => $total,
		);
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------

	public static function render_page() {
		$search = self::search();
		$paged  = isset( $_GET['paged'] ) ? absint( $_GET['paged'] ) : 1;
		$qpaged = isset( $_GET['qpaged'] ) ? absint( $_GET['qpaged'] ) : 1;

		$data       = L2D_AI_Plugin::request_backend( 'GET', '/admin/roles' );
		$backend_ok = ! is_wp_error( $data );
		$roles      = $backend_ok && isset( $data['roles'] ) ? (array) $data['roles'] : array();
		$caps       = $backend_ok && isset( $data['role_caps'] ) ? (array) $data['role_caps'] : array();

		$wp_rows  = self::filter_wp_rows( self::with_accounts( $roles ), $search );
		$wp_page  = self::paginate( $wp_rows, $paged );

		$qq_data    = L2D_AI_Plugin::request_backend( 'GET', '/admin/qq-whitelist' );
		$qq_ok      = ! is_wp_error( $qq_data );
		$qq_entries = $qq_ok && isset( $qq_data['entries'] ) ? (array) $qq_data['entries'] : array();
		$qq_rows    = self::filter_qq_rows( self::with_bound_accounts( $qq_entries ), $search );
		$qq_page    = self::paginate( $qq_rows, $qpaged );

		self::$current = array(
			's'      => $search,
			'paged'  => $wp_page['page'],
			'qpaged' => $qq_page['page'],
		);

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
				<input type="search" name="s" value="<?php echo esc_attr( $search ); ?>" placeholder="搜索用户名 / 昵称 / WP 用户 ID / QQ 号 / 备注" />
				<?php submit_button( '搜索', 'secondary', '', false ); ?>
			</form>

			<h2>WordPress 账号</h2>
			<p class="description">只列出已经有关系档案的账号。没聊过天的账号不会出现在这里，可以用页面底部的表单预先设置。</p>
			<?php self::render_wp_table( $wp_page['rows'], $caps, $backend_ok ); ?>
			<?php
			self::render_pager(
				$wp_page,
				'paged',
				array(
					'page' => self::PAGE_SLUG,
					's'    => $search,
					'qpaged' => $qq_page['page'],
				)
			);
			?>

			<h2>QQ 白名单</h2>
			<p class="description">
				名单里的 QQ 没有博客账号，用户名和昵称显示「没有」；角色在这里改，
				备注和移出名单请到「QQ 白名单」页。
			</p>
			<?php self::render_qq_table( $qq_page['rows'], $qq_ok ); ?>
			<?php
			self::render_pager(
				$qq_page,
				'qpaged',
				array(
					'page' => self::PAGE_SLUG,
					's'    => $search,
					'paged' => $wp_page['page'],
				)
			);
			?>

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

	private static function render_wp_table( array $rows, array $caps, $backend_ok ) {
		?>
		<table class="widefat striped">
			<thead>
				<tr>
					<th>WP 用户 ID</th>
					<th>用户名</th>
					<th>昵称</th>
					<th>角色</th>
					<th>好感</th>
					<th>好感上限</th>
					<th>操作</th>
				</tr>
			</thead>
			<tbody>
			<?php if ( empty( $rows ) ) : ?>
				<tr><td colspan="7"><?php echo $backend_ok ? '还没有人产生过关系档案。' : '—'; ?></td></tr>
			<?php endif; ?>
			<?php foreach ( $rows as $entry ) : ?>
				<?php
				$user_id = isset( $entry['wp_user_id'] ) ? (int) $entry['wp_user_id'] : 0;
				$role    = isset( $entry['role'] ) ? (string) $entry['role'] : 'normal';
				$cap     = isset( $entry['cap'] ) ? $entry['cap'] : ( isset( $caps[ $role ] ) ? $caps[ $role ] : '—' );
				$login   = isset( $entry['user_login'] ) ? (string) $entry['user_login'] : '';
				$name    = isset( $entry['wp_display_name'] ) ? (string) $entry['wp_display_name'] : '';
				?>
				<tr>
					<td><?php echo esc_html( (string) $user_id ); ?></td>
					<td><?php echo '' !== $login ? esc_html( $login ) : '（账号已删除）'; ?></td>
					<td><?php echo '' !== $name ? esc_html( $name ) : '（没有昵称）'; ?></td>
					<td><?php echo esc_html( self::role_label( $role ) ); ?></td>
					<td><?php echo esc_html( self::affection_text( $entry ) ); ?></td>
					<td><?php echo esc_html( (string) $cap ); ?></td>
					<td>
						<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block; margin-right: 8px;">
							<input type="hidden" name="action" value="l2d_ai_save_role" />
							<input type="hidden" name="wp_user_id" value="<?php echo esc_attr( (string) $user_id ); ?>" />
							<?php wp_nonce_field( 'l2d_ai_roles' ); ?>
							<?php self::hidden_back_fields(); ?>
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
								<?php self::hidden_back_fields(); ?>
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

	private static function render_qq_table( array $rows, $backend_ok ) {
		?>
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
					<th>操作</th>
				</tr>
			</thead>
			<tbody>
			<?php if ( empty( $rows ) ) : ?>
				<tr><td colspan="8"><?php echo $backend_ok ? '白名单里还没有人。' : '—'; ?></td></tr>
			<?php endif; ?>
			<?php foreach ( $rows as $entry ) : ?>
				<?php
				$qq       = isset( $entry['qq_uin'] ) ? (string) $entry['qq_uin'] : '';
				$role     = isset( $entry['role'] ) ? (string) $entry['role'] : 'normal';
				$cap      = isset( $entry['cap'] ) ? $entry['cap'] : '—';
				$note     = isset( $entry['note'] ) ? (string) $entry['note'] : '';
				$login    = isset( $entry['bound_user_login'] ) ? (string) $entry['bound_user_login'] : '';
				$name     = isset( $entry['bound_display_name'] ) ? (string) $entry['bound_display_name'] : '';
				?>
				<tr>
					<td><code><?php echo esc_html( $qq ); ?></code></td>
					<td><?php echo '' !== $login ? esc_html( $login ) : '没有'; ?></td>
					<td><?php echo '' !== $name ? esc_html( $name ) : '没有'; ?></td>
					<td><?php echo esc_html( self::role_label( $role ) ); ?></td>
					<td><?php echo esc_html( self::affection_text( $entry ) ); ?></td>
					<td><?php echo esc_html( (string) $cap ); ?></td>
					<td><?php echo '' !== $note ? esc_html( $note ) : '—'; ?></td>
					<td>
						<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="display: inline-block;">
							<input type="hidden" name="action" value="l2d_ai_save_qq_role" />
							<input type="hidden" name="qq_uin" value="<?php echo esc_attr( $qq ); ?>" />
							<?php wp_nonce_field( 'l2d_ai_roles' ); ?>
							<?php self::hidden_back_fields(); ?>
							<select name="role">
								<?php foreach ( self::ROLE_LABELS as $value => $label ) : ?>
									<option value="<?php echo esc_attr( $value ); ?>" <?php selected( $value, $role ); ?>><?php echo esc_html( $label ); ?></option>
								<?php endforeach; ?>
							</select>
							<button type="submit" class="button button-small">保存</button>
						</form>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php
	}

	/**
	 * 上一页 / 下一页。两张表各自翻页，所以另一张表当前的页码要带着，
	 * 否则翻上表会把下表重置回第一页。
	 */
	private static function render_pager( array $page, $param, array $base ) {
		if ( $page['pages'] <= 1 ) {
			return;
		}
		$prev = array_merge( $base, array( $param => $page['page'] - 1 ) );
		$next = array_merge( $base, array( $param => $page['page'] + 1 ) );
		?>
		<p class="tablenav-pages">
			<?php if ( $page['page'] > 1 ) : ?>
				<a class="button" href="<?php echo esc_url( add_query_arg( $prev, admin_url( 'options-general.php' ) ) ); ?>">上一页</a>
			<?php endif; ?>
			<span class="description">
				第 <?php echo esc_html( (string) $page['page'] ); ?> / <?php echo esc_html( (string) $page['pages'] ); ?> 页，
				共 <?php echo esc_html( (string) $page['total'] ); ?> 条
			</span>
			<?php if ( $page['page'] < $page['pages'] ) : ?>
				<a class="button" href="<?php echo esc_url( add_query_arg( $next, admin_url( 'options-general.php' ) ) ); ?>">下一页</a>
			<?php endif; ?>
		</p>
		<?php
	}
}
