<?php
/**
 * 注册页补充：设置密码，并说明用户名与昵称的区别。
 *
 * WordPress 原生注册表单只收「用户名 + 邮箱」，密码由系统随机生成，
 * 用户再通过邮件里的重置链接自己设。这里在注册表单里加一个密码框，
 * 让用户注册完就能直接用自己设的密码登录；邮件验证仍然照常发送。
 *
 * 三条边界：
 * - 只有表单真的渲染了 `l2d_ai_password` 字段才校验/写入，其他插件或
 *   WooCommerce 之类自己做的注册表单不会因为缺这个字段被误伤；
 * - 密码不做 trim、不做 HTML 清洗，只检查长度，避免把合法字符改掉；
 * - 用户名 `user_login` 是登录用的唯一标识，注册后锁定；显示名字用
 *   `display_name`，注册后可在「用户 → 个人资料」改昵称或显示名，
 *   点页面底部的「更新个人资料」生效。
 *
 * @package wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Registration {

	/** 密码框的字段名。 */
	const PASSWORD_FIELD = 'l2d_ai_password';

	/** 最短密码长度。WordPress 核心没有硬性下限，这里给一个最低要求。 */
	const MIN_PASSWORD_LENGTH = 8;

	public static function init() {
		add_action( 'register_form', array( __CLASS__, 'render_fields' ) );
		add_filter( 'registration_errors', array( __CLASS__, 'validate_password' ), 20, 3 );
		add_action( 'user_register', array( __CLASS__, 'set_password' ), 5, 1 );
	}

	/**
	 * 在原生注册表单的邮箱框之后、提交按钮之前输出密码框和提示。
	 *
	 * 这里不复制用户名/邮箱字段，只追加两个区块；主题或插件如果重写了
	 * 整个注册表单，可以用 `l2d_ai_registration_password_field` 过滤器
	 * 关掉这段输出。
	 */
	public static function render_fields() {
		if ( ! apply_filters( 'l2d_ai_registration_password_field', true ) ) {
			return;
		}
		?>
		<p class="l2d-ai-register-password">
			<label for="l2d-ai-register-password">
				<?php echo esc_html( '设置密码' ); ?><br>
				<input
					type="password"
					name="<?php echo esc_attr( self::PASSWORD_FIELD ); ?>"
					id="l2d-ai-register-password"
					class="input"
					value=""
					size="20"
					autocomplete="new-password"
					minlength="<?php echo esc_attr( (string) self::MIN_PASSWORD_LENGTH ); ?>"
					required
				>
			</label>
			<span class="description">
				<?php
				echo esc_html(
					'至少 ' . self::MIN_PASSWORD_LENGTH . ' 位，注册后即可用这个密码登录。'
				);
				?>
			</span>
		</p>
		<p class="description l2d-ai-register-note" style="margin-top:12px;">
			<?php
			echo esc_html(
				'用户名是登录用的唯一标识，注册后不可更改；想改显示的名字，注册后到「用户 → 个人资料」修改昵称或显示名，点页面底部的「更新个人资料」生效。'
			);
			?>
		</p>
		<?php
	}

	/**
	 * 校验密码。只有当前请求带了 `l2d_ai_password` 才生效。
	 *
	 * 注意：`registration_errors` 的第三个参数才是邮箱；这里不重复校验
	 * 用户名和邮箱，交给 WordPress 核心。
	 *
	 * @param WP_Error $errors 注册错误集合。
	 * @param string   $login  清洗后的用户名（未使用）。
	 * @param string   $email  邮箱（未使用）。
	 * @return WP_Error
	 */
	public static function validate_password( $errors, $login = '', $email = '' ) {
		if ( ! ( $errors instanceof WP_Error ) ) {
			return $errors;
		}
		if ( ! isset( $_POST[ self::PASSWORD_FIELD ] ) ) {
			return $errors;
		}

		$password = (string) wp_unslash( $_POST[ self::PASSWORD_FIELD ] );
		if ( '' === trim( $password ) ) {
			$errors->add( 'l2d_ai_password_empty', '请设置登录密码。' );
			return $errors;
		}
		if ( strlen( $password ) < self::MIN_PASSWORD_LENGTH ) {
			$errors->add(
				'l2d_ai_password_short',
				'密码至少需要 ' . self::MIN_PASSWORD_LENGTH . ' 位。'
			);
			return $errors;
		}
		// 防止有人提交超长字符串撑大密码哈希；正常密码远不会到这个长度。
		if ( strlen( $password ) > 4096 ) {
			$errors->add( 'l2d_ai_password_long', '密码太长了，请控制在 4096 个字节以内。' );
		}

		return $errors;
	}

	/**
	 * 用户创建成功后写入注册时填的密码。
	 *
	 * `register_new_user()` 会先随机生成密码再触发 `user_register`，
	 * 所以这里覆盖掉即可。只处理字段存在且通过长度检查的请求；其他
	 * 注册入口没有这个字段，保持 WordPress 原行为。
	 *
	 * @param int $user_id 新用户 ID。
	 */
	public static function set_password( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 || ! isset( $_POST[ self::PASSWORD_FIELD ] ) ) {
			return;
		}

		$password = (string) wp_unslash( $_POST[ self::PASSWORD_FIELD ] );
		if (
			'' === trim( $password )
			|| strlen( $password ) < self::MIN_PASSWORD_LENGTH
			|| strlen( $password ) > 4096
		) {
			// 校验钩子已经报过错；这里再兜一次，避免绕过表单直接注册。
			return;
		}

		wp_set_password( $password, $user_id );
	}
}
