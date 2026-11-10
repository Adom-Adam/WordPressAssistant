<?php
/**
 * QQ 群白名单与每个群的三个开关。
 *
 * ==================================================================
 * 名单和策略是两层
 * ==================================================================
 * 名单回答“这个群允不允许她说话”，策略回答“在这个群里怎么说话”
 * （什么时候理人、回不回、会不会主动开口）。移出名单只关掉第一层，
 * 策略行留着——重新加回来时不用再配一遍。
 *
 * 名单以运行时表为准（后端启动时从环境变量播种一次，之后后台改的就是
 * 真值），所以这里加群之后不必重启服务。
 *
 * ==================================================================
 * 三个开关的含义
 * ==================================================================
 * - trigger：什么算在叫她（只回应 @ / 回应关键词 / 全都回应）；
 * - reply_policy：被叫到就回，还是只记不说；
 * - proactive：会不会在群里主动找话题。
 * 默认值从后端 `QQ_TRIGGER` 推导，第一次打开页面看到的就是当前真实生效的行为。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Groups {

	const PAGE_SLUG = 'l2d-ai-groups';
	const TRANSIENT = 'l2d_ai_groups_notice_';

	const TRIGGER_LABELS = array(
		'mention' => '仅 @ 她时',
		'keyword' => '@ 或提到关键词',
		'all'     => '每句话都看',
	);

	const REPLY_LABELS = array(
		'always' => '被叫到就回',
		'silent' => '只看不回',
	);

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_group_add', array( __CLASS__, 'handle_add' ) );
		add_action( 'admin_post_l2d_ai_group_save', array( __CLASS__, 'handle_save' ) );
		add_action( 'admin_post_l2d_ai_group_remove', array( __CLASS__, 'handle_remove' ) );
	}

	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · QQ 群',
			'Live2D 小助手 · QQ 群',
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

	private static function redirect_with_notice( $message, $is_error = false ) {
		set_transient(
			self::notice_transient(),
			array(
				'message' => (string) $message,
				'error'   => (bool) $is_error,
			),
			60
		);
		wp_safe_redirect( self::page_url() );
		exit;
	}

	private static function group_id_from_request() {
		$group_id = isset( $_POST['group_id'] ) ? absint( $_POST['group_id'] ) : 0;
		if ( $group_id <= 0 ) {
			self::redirect_with_notice( '群号必须是正整数。', true );
		}
		return $group_id;
	}

	// ------------------------------------------------------------------
	// 操作处理
	// ------------------------------------------------------------------

	public static function handle_add() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_groups' );

		$group_id = self::group_id_from_request();
		$result   = L2D_AI_Plugin::request_backend(
			'POST',
			'/admin/qq-groups',
			array( 'group_id' => $group_id )
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice( sprintf( '群 %d 已加入白名单。', $group_id ) );
	}

	public static function handle_save() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_groups' );

		$group_id     = self::group_id_from_request();
		$trigger      = isset( $_POST['trigger'] ) ? sanitize_key( wp_unslash( $_POST['trigger'] ) ) : '';
		$reply_policy = isset( $_POST['reply_policy'] ) ? sanitize_key( wp_unslash( $_POST['reply_policy'] ) ) : '';

		if ( ! isset( self::TRIGGER_LABELS[ $trigger ] ) ) {
			self::redirect_with_notice( '请选择有效的触发方式。', true );
		}
		if ( ! isset( self::REPLY_LABELS[ $reply_policy ] ) ) {
			self::redirect_with_notice( '请选择有效的回复方式。', true );
		}

		$result = L2D_AI_Plugin::request_backend(
			'PUT',
			'/admin/qq-groups/' . $group_id,
			array(
				'trigger'      => $trigger,
				'reply_policy' => $reply_policy,
				// 未勾选的复选框在 POST 里不存在，只能按“存在即 true”处理。
				'proactive'    => ! empty( $_POST['proactive'] ),
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice( sprintf( '群 %d 的设置已保存。', $group_id ) );
	}

	public static function handle_remove() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_groups' );

		$group_id = self::group_id_from_request();
		$result   = L2D_AI_Plugin::request_backend( 'DELETE', '/admin/qq-groups/' . $group_id );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true );
		}

		self::redirect_with_notice(
			sprintf( '群 %d 已移出白名单，策略已保留；重新加回来时沿用。', $group_id )
		);
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------

	public static function render_page() {
		$data       = L2D_AI_Plugin::request_backend( 'GET', '/admin/qq-groups' );
		$backend_ok = ! is_wp_error( $data );
		$groups     = $backend_ok && isset( $data['groups'] ) ? (array) $data['groups'] : array();

		$notice = get_transient( self::notice_transient() );
		delete_transient( self::notice_transient() );
		?>
		<div class="wrap">
			<h1>QQ 群</h1>

			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( ! $backend_ok ) : ?>
				<div class="notice notice-error">
					<p>后端不可达，群名单暂时读不到：<?php echo esc_html( $data->get_error_message() ); ?></p>
				</div>
			<?php endif; ?>

			<p class="description">
				名单之外的群她会完全沉默。移出名单不会删掉这个群的设置，
				之后再加回来还是原来的配置。
			</p>

			<h2>加入一个群</h2>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_group_add" />
				<?php wp_nonce_field( 'l2d_ai_groups' ); ?>
				<input type="number" name="group_id" min="1" class="regular-text" placeholder="QQ 群号" />
				<?php submit_button( '加入白名单', 'secondary', '', false ); ?>
			</form>

			<h2>群设置</h2>
			<table class="widefat striped">
				<thead>
					<tr>
						<th>群号</th>
						<th>什么算在叫她</th>
						<th>被叫到之后</th>
						<th>主动开口</th>
						<th>来源</th>
						<th>操作</th>
					</tr>
				</thead>
				<tbody>
				<?php if ( empty( $groups ) ) : ?>
					<tr><td colspan="6"><?php echo $backend_ok ? '白名单里还没有群。' : '—'; ?></td></tr>
				<?php endif; ?>
				<?php foreach ( $groups as $group ) : ?>
					<?php
					$group_id     = isset( $group['group_id'] ) ? (int) $group['group_id'] : 0;
					$trigger      = isset( $group['trigger'] ) ? (string) $group['trigger'] : 'mention';
					$reply_policy = isset( $group['reply_policy'] ) ? (string) $group['reply_policy'] : 'always';
					$proactive    = ! empty( $group['proactive'] );
					$source       = isset( $group['source'] ) && 'db' === $group['source'] ? '后台设置' : '默认值';
					?>
					<tr>
						<td><code><?php echo esc_html( (string) $group_id ); ?></code></td>
						<td colspan="4">
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
								<input type="hidden" name="action" value="l2d_ai_group_save" />
								<input type="hidden" name="group_id" value="<?php echo esc_attr( (string) $group_id ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_groups' ); ?>
								<select name="trigger">
									<?php foreach ( self::TRIGGER_LABELS as $value => $label ) : ?>
										<option value="<?php echo esc_attr( $value ); ?>" <?php selected( $value, $trigger ); ?>><?php echo esc_html( $label ); ?></option>
									<?php endforeach; ?>
								</select>
								<select name="reply_policy">
									<?php foreach ( self::REPLY_LABELS as $value => $label ) : ?>
										<option value="<?php echo esc_attr( $value ); ?>" <?php selected( $value, $reply_policy ); ?>><?php echo esc_html( $label ); ?></option>
									<?php endforeach; ?>
								</select>
								<label>
									<input type="checkbox" name="proactive" value="1" <?php checked( $proactive ); ?> />
									会主动开口
								</label>
								<button type="submit" class="button button-small">保存</button>
								<span class="description">当前：<?php echo esc_html( $source ); ?></span>
							</form>
						</td>
						<td>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
								<input type="hidden" name="action" value="l2d_ai_group_remove" />
								<input type="hidden" name="group_id" value="<?php echo esc_attr( (string) $group_id ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_groups' ); ?>
								<button type="submit" class="button button-small">移出名单</button>
							</form>
						</td>
					</tr>
				<?php endforeach; ?>
				</tbody>
			</table>
		</div>
		<?php
	}
}
