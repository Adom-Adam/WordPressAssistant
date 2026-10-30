<?php
/**
 * 用户与额度：后台的一站式管理页。
 *
 * ==================================================================
 * 数据从两个地方拼起来
 * ==================================================================
 * - WordPress：账号、邮箱、昵称、注册时间、注册 IP；
 * - Python 后端：等级、会员到期、额外额度、今日已用。
 *
 * 两边用 WordPress 用户 ID 对齐。后端只返回“有人动过”的用户档案
 * （升过级、发过额度、写过备注），所以这里必须用 WordPress 的用户列表
 * 作为主表，再拿后端快照去补列——反过来做，从没被管理过的用户就不见了。
 *
 * 后端连不上时表格照常显示 WordPress 那几列，额度列显示「—」，
 * 并在页顶给出提示：管理页不能因为后端挂了就打不开。
 *
 * ==================================================================
 * 为什么操作走 admin-post.php
 * ==================================================================
 * 等级、额度、兑换码都存在 Python 后端里，不是 WordPress 选项，
 * 所以没法用 options.php 的 register_setting。统一提交到 admin-post.php，
 * 校验 nonce 后带短时管理员 JWT 调后端，再跳回本页。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Users {

	const PAGE_SLUG   = 'l2d-ai-users';
	const PER_PAGE    = 30;

	/** 生成的兑换码只在跳转后显示一次，放 transient 里带过去。 */
	const TRANSIENT_CODES  = 'l2d_ai_new_codes_';
	const TRANSIENT_NOTICE = 'l2d_ai_users_notice_';

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_set_tier', array( __CLASS__, 'handle_set_tier' ) );
		add_action( 'admin_post_l2d_ai_grant_credits', array( __CLASS__, 'handle_grant_credits' ) );
		add_action( 'admin_post_l2d_ai_create_codes', array( __CLASS__, 'handle_create_codes' ) );
	}

	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · 用户与额度',
			'Live2D 小助手 · 用户与额度',
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
		return self::TRANSIENT_NOTICE . get_current_user_id();
	}

	/** 把提示存进 transient，跳回列表页后显示一次。 */
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

	/**
	 * 拉取后端用户快照。
	 *
	 * @return array|WP_Error 形如 {users: [...], day: '...', limits: {user, member}}
	 */
	private static function fetch_snapshot() {
		return L2D_AI_Plugin::request_backend( 'GET', '/admin/users' );
	}

	// ------------------------------------------------------------------
	// 操作处理
	// ------------------------------------------------------------------

	public static function handle_set_tier() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_users' );

		$user_id = isset( $_POST['user_id'] ) ? absint( $_POST['user_id'] ) : 0;
		$tier    = isset( $_POST['tier'] ) ? sanitize_key( wp_unslash( $_POST['tier'] ) ) : '';
		$days    = isset( $_POST['duration_days'] ) ? absint( $_POST['duration_days'] ) : 0;
		$note    = isset( $_POST['note'] ) ? sanitize_text_field( wp_unslash( $_POST['note'] ) ) : '';

		if ( $user_id <= 0 || ! in_array( $tier, array( 'user', 'member' ), true ) ) {
			self::redirect_with_notice( '参数不完整，未做任何修改。', true );
		}
		if ( 'member' === $tier && $days <= 0 ) {
			// 后端也会拒绝，但先在这里拦一道，省一次往返。
			self::redirect_with_notice( '设为会员时必须填写天数。', true );
		}

		$body = array(
			'tier' => $tier,
			'note' => $note,
		);
		if ( 'member' === $tier ) {
			$body['duration_days'] = $days;
		}

		$result = L2D_AI_Plugin::request_backend(
			'PATCH',
			'/admin/users/' . $user_id,
			$body
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice( sprintf( '用户 #%d 已更新。', $user_id ), false, self::filter_args() );
	}

	public static function handle_grant_credits() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_users' );

		$user_id = isset( $_POST['user_id'] ) ? absint( $_POST['user_id'] ) : 0;
		$delta   = isset( $_POST['delta'] ) ? (int) $_POST['delta'] : 0;
		$reason  = isset( $_POST['reason'] ) ? sanitize_text_field( wp_unslash( $_POST['reason'] ) ) : '';

		if ( $user_id <= 0 || 0 === $delta ) {
			self::redirect_with_notice( '请填写非零的额度数量。', true );
		}
		if ( '' === $reason ) {
			$reason = '后台发放';
		}

		$result = L2D_AI_Plugin::request_backend(
			'POST',
			'/admin/users/' . $user_id . '/credits',
			array(
				'delta'  => $delta,
				'reason' => $reason,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice(
			sprintf( '用户 #%d 的额外额度现在是 %d 条。', $user_id, (int) $result['credit'] ),
			false,
			self::filter_args()
		);
	}

	public static function handle_create_codes() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_users' );

		$kind    = isset( $_POST['kind'] ) ? sanitize_key( wp_unslash( $_POST['kind'] ) ) : '';
		$value   = isset( $_POST['value'] ) ? absint( $_POST['value'] ) : 0;
		$count   = isset( $_POST['count'] ) ? absint( $_POST['count'] ) : 1;
		$max     = isset( $_POST['max_uses'] ) ? absint( $_POST['max_uses'] ) : 1;
		$expires = isset( $_POST['expires'] ) ? sanitize_text_field( wp_unslash( $_POST['expires'] ) ) : '';
		$note    = isset( $_POST['note'] ) ? sanitize_text_field( wp_unslash( $_POST['note'] ) ) : '';

		if ( ! in_array( $kind, array( 'credit', 'member' ), true ) || $value <= 0 ) {
			self::redirect_with_notice( '请选择类型并填写面值。', true );
		}

		$body = array(
			'kind'     => $kind,
			'value'    => $value,
			'count'    => max( 1, min( 50, $count ) ),
			'max_uses' => max( 1, min( 1000, $max ) ),
		);
		if ( '' !== $note ) {
			$body['note'] = $note;
		}
		if ( '' !== $expires ) {
			// date 控件给的是 YYYY-MM-DD；按站点时区的当天 23:59:59 算，
			// 这样“有效到 11-05”在 11-05 当天仍然能用。
			$timestamp = strtotime( $expires . ' 23:59:59' );
			if ( false !== $timestamp ) {
				$body['expires_at'] = wp_date( 'c', $timestamp, wp_timezone() );
			}
		}

		$result = L2D_AI_Plugin::request_backend( 'POST', '/admin/codes', $body );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		set_transient(
			self::TRANSIENT_CODES . get_current_user_id(),
			(array) $result['codes'],
			120
		);
		self::redirect_with_notice( sprintf( '已生成 %d 个兑换码。', count( (array) $result['codes'] ) ) );
	}

	/** 跳转时保留当前的搜索与筛选，避免操作后回到第一页。 */
	private static function filter_args() {
		$args = array();
		if ( isset( $_POST['back_s'] ) && '' !== $_POST['back_s'] ) {
			$args['s'] = sanitize_text_field( wp_unslash( $_POST['back_s'] ) );
		}
		if ( isset( $_POST['back_tier'] ) && '' !== $_POST['back_tier'] ) {
			$args['tier'] = sanitize_key( wp_unslash( $_POST['back_tier'] ) );
		}
		if ( isset( $_POST['back_paged'] ) ) {
			$paged = absint( $_POST['back_paged'] );
			if ( $paged > 1 ) {
				$args['paged'] = $paged;
			}
		}
		return $args;
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------

	public static function render_page() {
		if ( ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$paged  = isset( $_GET['paged'] ) ? max( 1, absint( $_GET['paged'] ) ) : 1;
		$search = isset( $_GET['s'] ) ? sanitize_text_field( wp_unslash( $_GET['s'] ) ) : '';
		$tier_filter = isset( $_GET['tier'] ) ? sanitize_key( wp_unslash( $_GET['tier'] ) ) : '';

		$snapshot = self::fetch_snapshot();
		$backend_ok = ! is_wp_error( $snapshot );

		$profiles = array();
		$limits   = array( 'user' => 0, 'member' => 0 );
		if ( $backend_ok ) {
			foreach ( (array) ( $snapshot['users'] ?? array() ) as $row ) {
				$profiles[ (int) $row['user_id'] ] = $row;
			}
			$limits = array(
				'user'   => (int) ( $snapshot['limits']['user'] ?? 0 ),
				'member' => (int) ( $snapshot['limits']['member'] ?? 0 ),
			);
		}

		// 会员筛选：后端快照里 tier=member 的 ID 就是全部会员，
		// 因为等级只存在后端。普通用户筛选用 exclude 反向取。
		$member_ids = array();
		foreach ( $profiles as $user_id => $row ) {
			if ( isset( $row['tier'] ) && 'member' === $row['tier'] ) {
				$member_ids[] = (int) $user_id;
			}
		}

		$query_args = array(
			'number'         => self::PER_PAGE,
			'paged'          => $paged,
			'orderby'        => 'registered',
			'order'          => 'DESC',
			'count_total'    => true,
			'search_columns' => array( 'user_login', 'user_email', 'display_name' ),
		);
		if ( '' !== $search ) {
			// 前后加 * 表示“包含”，否则 WordPress 默认按前缀匹配。
			$query_args['search'] = '*' . $search . '*';
		}
		if ( 'member' === $tier_filter ) {
			// include 为空数组时 WordPress 会返回空结果，这正是“没有会员”的正确表现。
			$query_args['include'] = $member_ids ? $member_ids : array( 0 );
		} elseif ( 'user' === $tier_filter && $member_ids ) {
			$query_args['exclude'] = $member_ids;
		}

		$query = new WP_User_Query( $query_args );
		$users = $query->get_results();
		$total = (int) $query->get_total();
		$pages = (int) ceil( $total / self::PER_PAGE );

		$notice = get_transient( self::notice_transient() );
		delete_transient( self::notice_transient() );

		$new_codes = get_transient( self::TRANSIENT_CODES . get_current_user_id() );
		delete_transient( self::TRANSIENT_CODES . get_current_user_id() );
		?>
		<div class="wrap">
			<h1>用户与额度</h1>

			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( ! $backend_ok ) : ?>
				<div class="notice notice-error">
					<p>后端不可达，额度信息暂不可用：<?php echo esc_html( $snapshot->get_error_message() ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( is_array( $new_codes ) && $new_codes ) : ?>
				<h2>刚生成的兑换码</h2>
				<p class="description">只在这里显示一次，请现在复制保存。</p>
				<textarea readonly rows="<?php echo esc_attr( (string) min( 10, count( $new_codes ) + 1 ) ); ?>" class="large-text code"><?php echo esc_textarea( implode( "\n", $new_codes ) ); ?></textarea>
			<?php endif; ?>

			<form method="get" action="<?php echo esc_url( admin_url( 'options-general.php' ) ); ?>" style="margin: 1em 0;">
				<input type="hidden" name="page" value="<?php echo esc_attr( self::PAGE_SLUG ); ?>" />
				<input type="search" name="s" value="<?php echo esc_attr( $search ); ?>" placeholder="搜索用户名 / 邮箱 / 昵称" />
				<select name="tier">
					<option value="" <?php selected( '', $tier_filter ); ?>>全部身份</option>
					<option value="member" <?php selected( 'member', $tier_filter ); ?>>会员</option>
					<option value="user" <?php selected( 'user', $tier_filter ); ?>>普通用户</option>
				</select>
				<?php submit_button( '筛选', 'secondary', '', false ); ?>
				<?php if ( $backend_ok ) : ?>
					<span class="description">
						今日上限：普通 <?php echo esc_html( (string) $limits['user'] ); ?> 条，会员 <?php echo esc_html( (string) $limits['member'] ); ?> 条
					</span>
				<?php endif; ?>
			</form>

			<?php self::render_table( $users, $profiles, $limits, $backend_ok, $search, $tier_filter, $paged ); ?>

			<?php self::render_pagination( $pages, $paged, $search, $tier_filter ); ?>

			<hr />

			<?php self::render_codes_section(); ?>
		</div>
		<?php
	}

	private static function render_table( $users, array $profiles, array $limits, $backend_ok, $search, $tier_filter, $paged ) {
		?>
		<table class="widefat striped">
			<thead>
				<tr>
					<th>ID</th>
					<th>昵称</th>
					<th>邮箱</th>
					<th>注册时间</th>
					<th>注册 IP</th>
					<th>等级</th>
					<th>会员到期</th>
					<th>今日已用</th>
					<th>今日剩余</th>
					<th>额外额度</th>
					<th>QQ</th>
					<th>操作</th>
				</tr>
			</thead>
			<tbody>
			<?php if ( empty( $users ) ) : ?>
				<tr><td colspan="12">没有符合条件的用户。</td></tr>
			<?php endif; ?>
			<?php foreach ( $users as $user ) : ?>
				<?php
				$user_id  = (int) $user->ID;
				$profile  = isset( $profiles[ $user_id ] ) ? $profiles[ $user_id ] : array();
				$tier     = isset( $profile['tier'] ) ? (string) $profile['tier'] : 'user';
				$expires  = isset( $profile['tier_expires_at'] ) ? (string) $profile['tier_expires_at'] : '';
				$credit   = isset( $profile['credit'] ) ? (int) $profile['credit'] : 0;
				$used     = isset( $profile['daily_used'] ) ? (int) $profile['daily_used'] : 0;
				$note     = isset( $profile['note'] ) ? (string) $profile['note'] : '';
				$reg_ip   = (string) get_user_meta( $user_id, L2D_AI_Guard::META_IP, true );
				$is_admin = user_can( $user_id, 'manage_options' );

				$limit = 'member' === $tier ? $limits['member'] : $limits['user'];
				?>
				<tr>
					<td><?php echo esc_html( (string) $user_id ); ?></td>
					<td><?php echo esc_html( $user->display_name ); ?></td>
					<td><?php echo esc_html( $user->user_email ); ?></td>
					<td><?php echo esc_html( $user->user_registered ); ?></td>
					<td><?php echo '' !== $reg_ip ? esc_html( $reg_ip ) : '—'; ?></td>
					<td>
						<?php echo esc_html( 'member' === $tier ? '会员' : '普通' ); ?>
						<?php if ( $is_admin ) : ?>
							<br /><span class="description">管理员（不限量）</span>
						<?php endif; ?>
					</td>
					<td><?php echo '' !== $expires ? esc_html( $expires ) : '—'; ?></td>
					<td><?php echo $backend_ok ? esc_html( (string) $used ) : '—'; ?></td>
					<td>
						<?php
						if ( ! $backend_ok ) {
							echo '—';
						} elseif ( $is_admin ) {
							echo '不限量';
						} else {
							echo esc_html( (string) max( 0, $limit - $used ) );
						}
						?>
					</td>
					<td><?php echo $backend_ok ? esc_html( (string) $credit ) : '—'; ?></td>
					<td>—</td>
					<td>
						<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-bottom: 8px;">
							<input type="hidden" name="action" value="l2d_ai_set_tier" />
							<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>" />
							<input type="hidden" name="back_s" value="<?php echo esc_attr( $search ); ?>" />
							<input type="hidden" name="back_tier" value="<?php echo esc_attr( $tier_filter ); ?>" />
							<input type="hidden" name="back_paged" value="<?php echo esc_attr( (string) $paged ); ?>" />
							<?php wp_nonce_field( 'l2d_ai_users' ); ?>
							<select name="tier">
								<option value="user" <?php selected( 'user', $tier ); ?>>普通</option>
								<option value="member" <?php selected( 'member', $tier ); ?>>会员</option>
							</select>
							<input type="number" name="duration_days" min="1" max="3650" value="30" class="small-text" title="会员天数" /> 天
							<br />
							<input type="text" name="note" value="<?php echo esc_attr( $note ); ?>" placeholder="备注（带空保存会清空）" class="regular-text" />
							<button type="submit" class="button button-small">保存身份</button>
						</form>

						<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
							<input type="hidden" name="action" value="l2d_ai_grant_credits" />
							<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>" />
							<input type="hidden" name="back_s" value="<?php echo esc_attr( $search ); ?>" />
							<input type="hidden" name="back_tier" value="<?php echo esc_attr( $tier_filter ); ?>" />
							<input type="hidden" name="back_paged" value="<?php echo esc_attr( (string) $paged ); ?>" />
							<?php wp_nonce_field( 'l2d_ai_users' ); ?>
							<input type="number" name="delta" value="10" class="small-text" /> 条
							<input type="text" name="reason" value="后台发放" class="regular-text" />
							<button type="submit" class="button button-small">发放额度</button>
						</form>
					</td>
				</tr>
			<?php endforeach; ?>
			</tbody>
		</table>
		<?php
	}

	private static function render_pagination( $pages, $paged, $search, $tier_filter ) {
		if ( $pages <= 1 ) {
			return;
		}

		$base = array(
			'page' => self::PAGE_SLUG,
			's'    => $search,
			'tier' => $tier_filter,
		);
		?>
		<p class="tablenav-pages">
			<?php if ( $paged > 1 ) : ?>
				<a class="button" href="<?php echo esc_url( add_query_arg( array_merge( $base, array( 'paged' => $paged - 1 ) ), admin_url( 'options-general.php' ) ) ); ?>">上一页</a>
			<?php endif; ?>
			<span class="description">第 <?php echo esc_html( (string) $paged ); ?> / <?php echo esc_html( (string) $pages ); ?> 页</span>
			<?php if ( $paged < $pages ) : ?>
				<a class="button" href="<?php echo esc_url( add_query_arg( array_merge( $base, array( 'paged' => $paged + 1 ) ), admin_url( 'options-general.php' ) ) ); ?>">下一页</a>
			<?php endif; ?>
		</p>
		<?php
	}

	private static function render_codes_section() {
		$codes = L2D_AI_Plugin::request_backend( 'GET', '/admin/codes' );
		?>
		<h2>兑换码</h2>

		<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
			<input type="hidden" name="action" value="l2d_ai_create_codes" />
			<?php wp_nonce_field( 'l2d_ai_users' ); ?>
			<table class="form-table" role="presentation">
				<tr>
					<th scope="row">类型</th>
					<td>
						<select name="kind">
							<option value="credit">额外额度（面值 = 条数）</option>
							<option value="member">会员（面值 = 天数）</option>
						</select>
					</td>
				</tr>
				<tr>
					<th scope="row">面值</th>
					<td><input type="number" name="value" min="1" value="10" class="small-text" /></td>
				</tr>
				<tr>
					<th scope="row">数量</th>
					<td><input type="number" name="count" min="1" max="50" value="1" class="small-text" /></td>
				</tr>
				<tr>
					<th scope="row">每个码可用次数</th>
					<td>
						<input type="number" name="max_uses" min="1" max="1000" value="1" class="small-text" />
						<p class="description">默认 1：每个码只能被一个人用一次。设成 2 表示可以被两个人各用一次（同一个账号仍然只能用一次）。</p>
					</td>
				</tr>
				<tr>
					<th scope="row">有效期</th>
					<td><input type="date" name="expires" value="" /> 留空表示永久有效</td>
				</tr>
				<tr>
					<th scope="row">备注</th>
					<td><input type="text" name="note" value="" class="regular-text" /></td>
				</tr>
			</table>
			<?php submit_button( '生成兑换码', 'secondary' ); ?>
		</form>

		<h3>最近 50 个兑换码</h3>
		<?php if ( is_wp_error( $codes ) ) : ?>
			<p>兑换码列表暂时读取不到：<?php echo esc_html( $codes->get_error_message() ); ?></p>
		<?php elseif ( empty( $codes['codes'] ) ) : ?>
			<p>还没有生成过兑换码。</p>
		<?php else : ?>
			<table class="widefat striped">
				<thead>
					<tr>
						<th>码</th>
						<th>类型</th>
						<th>面值</th>
						<th>已用</th>
						<th>上限</th>
						<th>有效期</th>
						<th>备注</th>
					</tr>
				</thead>
				<tbody>
					<?php foreach ( (array) ( $codes['codes'] ?? array() ) as $code ) : ?>
						<tr>
							<td><code><?php echo esc_html( (string) $code['code'] ); ?></code></td>
							<td><?php echo esc_html( 'member' === $code['kind'] ? '会员天数' : '额外额度' ); ?></td>
							<td><?php echo esc_html( (string) $code['value'] ); ?></td>
							<td><?php echo esc_html( (string) $code['used_count'] ); ?></td>
							<td><?php echo esc_html( (string) $code['max_uses'] ); ?></td>
							<td><?php echo ! empty( $code['expires_at'] ) ? esc_html( (string) $code['expires_at'] ) : '永久'; ?></td>
							<td><?php echo ! empty( $code['note'] ) ? esc_html( (string) $code['note'] ) : '—'; ?></td>
						</tr>
					<?php endforeach; ?>
				</tbody>
			</table>
		<?php endif; ?>
		<?php
	}
}
