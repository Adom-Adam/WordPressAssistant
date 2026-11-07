<?php
/**
 * 个人资料页的 QQ 绑定卡片。
 *
 * 为什么绑定放在 wp-admin/profile.php：
 * 那里本来就有 WordPress 登录态，是唯一能证明“我是我”的地方。QQ 那头
 * 谁都能私聊，不能承担“证明身份”的职责，否则先发消息的人就能顶掉别人的绑定。
 *
 * 两端各存一半：
 * - WordPress 存明文绑定码（用户在资料页看得到），存在 usermeta；
 * - 后端只存 SHA-256，用 `/绑定 1234-5678` 时现算哈希去比对。
 *
 * 绑定码的同步是“打开资料页时顺手推一次”，而不是实时推送：少一个
 * 后台任务，也少一个会悄悄失败的地方。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Bind {

	/** 明文绑定码（8 位数字，不含分隔符）。 */
	const CODE_META = 'l2d_ai_qq_bind_code';

	/** 已经成功同步给后端的码；与 CODE_META 相同就跳过 PUT。 */
	const SYNC_META = 'l2d_ai_qq_bind_code_synced';

	const NONCE_ACTION = 'l2d_ai_bind';

	/**
	 * 需要放到资料页底部（外层资料表单之外）的表单：类型 => 用户 ID。
	 *
	 * 资料页本身是一个 `<form>`，卡片里再嵌 `<form>` 会让浏览器提前关掉
	 * 外层表单，底部「更新个人资料」就点了没反应。这里只记录本次要渲染
	 * 哪几个表单，真正容器由 admin_footer 输出，卡片里的按钮用 HTML5
	 * `form` 属性关联过去。
	 */
	private static $footer_forms = array();

	public static function init() {
		add_action( 'show_user_profile', array( __CLASS__, 'render_card' ) );
		add_action( 'admin_footer', array( __CLASS__, 'render_forms' ) );
		add_action( 'admin_post_l2d_ai_bind_refresh', array( __CLASS__, 'handle_refresh' ) );
		add_action( 'admin_post_l2d_ai_bind_unbind', array( __CLASS__, 'handle_unbind' ) );
		// 有人被删号时顺手解绑，免得那个 QQ 永远占着“已绑定”。
		add_action( 'deleted_user', array( __CLASS__, 'handle_deleted_user' ) );
	}

	// ------------------------------------------------------------------
	// 渲染
	// ------------------------------------------------------------------

	/**
	 * 在个人资料页输出绑定卡片。
	 *
	 * 后端读不到时只显示提示、不渲染按钮：状态不明时让用户点按钮，
	 * 可能把“其实已绑定”的账号又解一次。
	 */
	public static function render_card( $user ) {
		$user_id = isset( $user->ID ) ? (int) $user->ID : 0;
		if ( $user_id <= 0 || ! current_user_can( 'edit_user', $user_id ) ) {
			return;
		}

		$state = L2D_AI_Plugin::user_backend_request( $user_id, 'GET', '/me/bind' );
		$qq    = self::bound_qq( $state );

		// 未验证邮箱不生成新绑定码；已经绑过的仍然显示，允许用户解绑。
		$verified = ! class_exists( 'L2D_AI_Email_Verification' )
			|| L2D_AI_Email_Verification::is_verified( $user_id );
		if ( ! $verified && '' === $qq ) {
			?>
			<h2>QQ 绑定</h2>
			<div class="l2d-ai-bind-card" style="max-width:640px;padding:16px 20px;border:1px solid #dcdcde;border-radius:8px;background:#fff;">
				<p style="margin:0;color:#b32d2e;">
					<?php echo esc_html( '请先在「邮箱验证」里完成验证，再生成绑定码。' ); ?>
				</p>
			</div>
			<?php
			return;
		}
		?>
		<h2>QQ 绑定</h2>
		<div class="l2d-ai-bind-card" style="max-width:640px;padding:16px 20px;border:1px solid #dcdcde;border-radius:8px;background:#fff;">
			<?php self::render_notice(); ?>
			<?php if ( is_wp_error( $state ) ) : ?>
				<p style="margin:0;color:#b32d2e;">
					<?php echo esc_html( '暂时无法同步绑定状态，请稍后刷新页面。' ); ?>
				</p>
				<p style="margin:8px 0 0;color:#646970;">
					<?php echo esc_html( $state->get_error_message() ); ?>
				</p>
			<?php elseif ( '' !== $qq ) : ?>
				<?php self::render_bound( $user_id, $qq ); ?>
			<?php else : ?>
				<?php self::render_unbound( $user_id ); ?>
			<?php endif; ?>
		</div>
		<?php
	}

	private static function render_notice() {
		if ( ! isset( $_GET['l2d-ai-bind'] ) ) {
			return;
		}
		$status = sanitize_key( wp_unslash( $_GET['l2d-ai-bind'] ) );
		$text   = '';
		$color  = '#1d2327';
		if ( 'refreshed' === $status ) {
			$text = '新的绑定码已生成，旧的立刻失效。';
		} elseif ( 'unbound' === $status ) {
			$text = '已解绑。这个 QQ 现在和账号没有关系了。';
		} elseif ( 'error' === $status ) {
			$text  = '操作没有完成，请稍后再试一次。';
			$color = '#b32d2e';
		} elseif ( 'unverified' === $status ) {
			$text  = '请先完成邮箱验证，再生成绑定码。';
			$color = '#b32d2e';
		}
		if ( '' === $text ) {
			return;
		}
		?>
		<p style="margin:0 0 12px;color:<?php echo esc_attr( $color ); ?>;">
			<?php echo esc_html( $text ); ?>
		</p>
		<?php
	}

	private static function render_bound( $user_id, $qq_uin ) {
		?>
		<p style="margin:0 0 8px;">
			<strong>状态：</strong>已绑定 QQ <code><?php echo esc_html( $qq_uin ); ?></code>
		</p>
		<p style="margin:0 0 12px;color:#646970;">
			<?php echo esc_html( '在群里 @梅林 或者在私聊里直接说话，我都能听到。' ); ?>
		</p>
		<p style="margin:0 0 12px;color:#646970;">
			<?php echo esc_html( '如需换绑，请先解绑，再刷新绑定码用新的 QQ 绑定。' ); ?>
		</p>
		<?php self::$footer_forms['unbind'] = $user_id; ?>
		<button
			type="submit"
			class="button"
			form="l2d-ai-bind-unbind-form"
			onclick="return confirm('解绑后这个 QQ 将无法再和我聊天，确定吗？');"
		><?php echo esc_html( '解绑这个 QQ' ); ?></button>
		<?php
	}

	private static function render_unbound( $user_id ) {
		$code        = self::ensure_code( $user_id );
		$sync_error  = self::sync_code( $user_id, $code );
		$display     = self::format_code( $code );
		?>
		<p style="margin:0 0 8px;">
			<strong>状态：</strong>还没有绑定 QQ
		</p>
		<p style="margin:0 0 12px;color:#646970;">
			<?php echo esc_html( '在 QQ 里私聊梅林，发送下面这条命令就行：' ); ?>
		</p>
		<p style="margin:0 0 12px;font-size:16px;">
			<code style="font-size:16px;padding:4px 8px;">/绑定 <?php echo esc_html( $display ); ?></code>
			<button
				type="button"
				class="button"
				data-l2d-bind-copy="<?php echo esc_attr( '/绑定 ' . $display ); ?>"
			><?php echo esc_html( '复制' ); ?></button>
		</p>
		<p style="margin:0 0 4px;">
			<strong>绑定码：</strong><code><?php echo esc_html( $display ); ?></code>
			<?php echo esc_html( '（只有你能看到，发给梅林即可）' ); ?>
		</p>
		<?php if ( '' !== $sync_error ) : ?>
			<p style="margin:8px 0 0;color:#b32d2e;">
				<?php echo esc_html( '绑定码暂时没能同步到 AI 服务，稍后刷新页面会自动重试：' . $sync_error ); ?>
			</p>
		<?php endif; ?>
		<?php self::$footer_forms['refresh'] = $user_id; ?>
		<p style="margin-top:12px;">
			<button
				type="submit"
				class="button"
				form="l2d-ai-bind-refresh-form"
			><?php echo esc_html( '刷新绑定码' ); ?></button>
		</p>
		<script>
		(function () {
			var buttons = document.querySelectorAll('[data-l2d-bind-copy]');
			for (var i = 0; i < buttons.length; i++) {
				buttons[i].addEventListener('click', function () {
					var text = this.getAttribute('data-l2d-bind-copy');
					if (!navigator.clipboard) {
						window.prompt('请手动复制这条命令：', text);
						return;
					}
					navigator.clipboard.writeText(text).then(function () {
						// 复制成功时给一点反馈，避免用户不确定有没有生效。
						window.alert('已复制，去 QQ 私聊梅林粘贴发出去吧。');
					}, function () {
						window.prompt('请手动复制这条命令：', text);
					});
				});
			}
		})();
		</script>
		<?php
	}

	/**
	 * 在页面底部输出卡片按钮真正要提交的表单容器。
	 *
	 * 只有 render_card 确实渲染了对应按钮时才输出；容器放在资料页外层
	 * 表单之外，避免嵌套。表单本身用 display:none 隐藏，用户只会看到
	 * 卡片里的按钮。
	 */
	public static function render_forms() {
		if ( empty( self::$footer_forms ) ) {
			return;
		}

		foreach ( self::$footer_forms as $kind => $user_id ) {
			$user_id = (int) $user_id;
			if ( $user_id <= 0 ) {
				continue;
			}

			if ( 'unbind' === $kind ) {
				$form_id = 'l2d-ai-bind-unbind-form';
				$action  = 'l2d_ai_bind_unbind';
			} elseif ( 'refresh' === $kind ) {
				$form_id = 'l2d-ai-bind-refresh-form';
				$action  = 'l2d_ai_bind_refresh';
			} else {
				continue;
			}
			?>
			<form
				id="<?php echo esc_attr( $form_id ); ?>"
				method="post"
				action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>"
				style="display:none;"
			>
				<input type="hidden" name="action" value="<?php echo esc_attr( $action ); ?>">
				<input type="hidden" name="user_id" value="<?php echo esc_attr( (string) $user_id ); ?>">
				<?php wp_nonce_field( self::NONCE_ACTION ); ?>
			</form>
			<?php
		}
	}

	// ------------------------------------------------------------------
	// 操作处理
	// ------------------------------------------------------------------

	public static function handle_refresh() {
		$user_id = self::authorize_request();

		if (
			class_exists( 'L2D_AI_Email_Verification' )
			&& ! L2D_AI_Email_Verification::is_verified( $user_id )
		) {
			self::redirect( 'unverified', $user_id );
		}

		$code = self::generate_code();
		update_user_meta( $user_id, self::CODE_META, $code );
		delete_user_meta( $user_id, self::SYNC_META );

		$result = L2D_AI_Plugin::user_backend_request(
			$user_id,
			'PUT',
			'/me/bind-code',
			array( 'code' => self::format_code( $code ) )
		);
		if ( is_wp_error( $result ) ) {
			self::redirect( 'error', $user_id );
		}

		update_user_meta( $user_id, self::SYNC_META, $code );
		self::redirect( 'refreshed', $user_id );
	}

	public static function handle_unbind() {
		$user_id = self::authorize_request();

		$result = L2D_AI_Plugin::user_backend_request( $user_id, 'DELETE', '/me/bind' );
		if ( is_wp_error( $result ) ) {
			self::redirect( 'error', $user_id );
		}

		// 解绑同时作废旧码：换绑必须是一次干净的开始。
		delete_user_meta( $user_id, self::CODE_META );
		delete_user_meta( $user_id, self::SYNC_META );
		self::redirect( 'unbound', $user_id );
	}

	/**
	 * WordPress 删除用户后，把后端的绑定一并清掉。
	 *
	 * 删除动作本身不能被解绑失败阻断（用户已经删了），所以这里只记日志。
	 * WP-CLI 或非管理员触发的删除不强求同步：后端在下次绑定时会
	 * 用 qq_taken / account_taken 挡住重复绑定，不会出现两个账号共用一个 QQ。
	 */
	public static function handle_deleted_user( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 || ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
			return;
		}

		$result = L2D_AI_Plugin::request_backend(
			'DELETE',
			'/admin/users/' . $user_id . '/bind'
		);
		if ( is_wp_error( $result ) ) {
			error_log( 'L2D AI 删除用户后解绑 QQ 失败: ' . $result->get_error_message() );
		}

		// 账号已经删除，记忆不能留在后端。失败只记日志：删除动作本身
		// 已经完成，不能让一次网络错误把后台流程弄崩。
		$purged = L2D_AI_Plugin::request_backend(
			'DELETE',
			'/admin/users/' . $user_id . '/memory'
		);
		if ( is_wp_error( $purged ) ) {
			error_log( 'L2D AI 删除用户后清理记忆失败: ' . $purged->get_error_message() );
		}
	}

	/**
	 * 校验 nonce 与权限，返回目标用户 ID；不通过直接结束请求。
	 *
	 * 两个表单都只提交 user_id，不提交任何“绑定码”，所以不存在
	 * 把绑定码从浏览器回传后写库的路径。
	 */
	private static function authorize_request() {
		if ( ! is_user_logged_in() ) {
			wp_die( '请先登录', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( self::NONCE_ACTION );

		$user_id = isset( $_POST['user_id'] ) ? absint( $_POST['user_id'] ) : 0;
		if ( $user_id <= 0 || ! current_user_can( 'edit_user', $user_id ) ) {
			wp_die( '无权修改这个用户', '无权限', array( 'response' => 403 ) );
		}

		return $user_id;
	}

	private static function redirect( $status, $user_id ) {
		wp_safe_redirect(
			add_query_arg(
				array(
					'l2d-ai-bind' => sanitize_key( $status ),
					'user_id'     => (int) $user_id,
				),
				admin_url( 'profile.php' )
			)
		);
		exit;
	}

	// ------------------------------------------------------------------
	// 绑定码
	// ------------------------------------------------------------------

	/** 取本地码；没有或格式不对就生成一个新的（老数据迁移也走这里）。 */
	private static function ensure_code( $user_id ) {
		$code = (string) get_user_meta( $user_id, self::CODE_META, true );
		if ( ! preg_match( '/^\d{8}$/', $code ) ) {
			$code = self::generate_code();
			update_user_meta( $user_id, self::CODE_META, $code );
			delete_user_meta( $user_id, self::SYNC_META );
		}

		return $code;
	}

	/**
	 * 把当前码推给后端；返回空字符串表示成功，否则是可展示的错误。
	 * 已经同步过就跳过，避免每看一次资料页都打一次后端。
	 */
	private static function sync_code( $user_id, $code ) {
		$synced = (string) get_user_meta( $user_id, self::SYNC_META, true );
		if ( $synced === $code ) {
			return '';
		}

		$result = L2D_AI_Plugin::user_backend_request(
			$user_id,
			'PUT',
			'/me/bind-code',
			array( 'code' => self::format_code( $code ) )
		);
		if ( is_wp_error( $result ) ) {
			// 不把绑定码写进任何日志：日志比数据库更容易被打包外传。
			return $result->get_error_message();
		}

		update_user_meta( $user_id, self::SYNC_META, $code );

		return '';
	}

	private static function generate_code() {
		// 前导零要保留：绑定码是 8 位，不是 8 位以内的数字。
		return str_pad( (string) wp_rand( 0, 99999999 ), 8, '0', STR_PAD_LEFT );
	}

	private static function format_code( $code ) {
		return substr( $code, 0, 4 ) . '-' . substr( $code, 4, 4 );
	}

	/** GET /me/bind 的响应里取 QQ；没绑定时是 null。 */
	private static function bound_qq( $state ) {
		if ( ! is_array( $state ) || ! isset( $state['qq_uin'] ) || null === $state['qq_uin'] ) {
			return '';
		}

		return (string) $state['qq_uin'];
	}
}
