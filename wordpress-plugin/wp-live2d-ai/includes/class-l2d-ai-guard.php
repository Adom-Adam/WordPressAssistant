<?php
/**
 * 注册防护：邮箱域名黑名单 + 同一 IP 的频率限制。
 *
 * 背景：站点对外开放注册后，出现了凌晨自动注册、次日改密码的账号，
 * 邮箱域名是随机拼出来的。这类账号不会用聊天功能，但会占用户 ID、
 * 干扰后台统计，也可能被用来试探弱口令。
 *
 * 这里只做三件低风险的事：
 * 1. 黑名单域名直接拒绝（一次性邮箱常用域名）；
 * 2. 同一 IP 每小时/每天最多注册几个；
 * 3. 同一 IP 两次注册之间至少隔多少秒。
 *
 * 不做的事：不删号、不禁用账号、不引入验证码。那些动作要么不可逆，
 * 要么会给正常访客增加负担；拦截记录留在后台页，由站长大决定怎么处理。
 *
 * 拦截提示统一为「该邮箱暂不支持注册」，不出现「机器人」「垃圾」这类
 * 字样：一是避免教对手绕过，二是防止误伤真实用户时的尴尬。
 *
 * @package wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Guard {

	const OPTION_DOMAINS = 'l2d_ai_blocked_email_domains';
	const OPTION_LIMIT   = 'l2d_ai_register_limit';
	const OPTION_LOG     = 'l2d_ai_block_log';

	/** 拦截记录最多保留多少条，超出后丢掉最旧的。 */
	const LOG_MAX = 50;

	/** 记到用户身上的元数据，后台用户列表会用到。 */
	const META_IP = 'l2d_ai_reg_ip';
	const META_AT = 'l2d_ai_reg_at';

	/** 默认频率限制：每小时 3 个、每天 10 个、两次间隔 30 秒。 */
	const DEFAULT_LIMIT = array(
		'per_ip_hour'        => 3,
		'per_ip_day'         => 10,
		'min_interval_seconds' => 30,
	);

	/** 对外的统一文案：不解释具体命中了哪条规则。 */
	const BLOCK_MESSAGE = '该邮箱暂不支持注册。';

	public static function init() {
		// 4 个参数：$errors, $sanitize_user, $user_login, $user_email。
		add_filter( 'registration_errors', array( __CLASS__, 'check_registration' ), 10, 4 );
		add_action( 'user_register', array( __CLASS__, 'record_registration' ) );
	}

	/**
	 * 读取频率限制，缺项或非法值回退默认值。
	 *
	 * 用数组选项而不是三个独立选项：后台保存时一次写完整组，
	 * 不会出现「小时数改了、天数没改」的半套配置。
	 */
	public static function register_limit() {
		$saved   = get_option( self::OPTION_LIMIT, array() );
		$saved   = is_array( $saved ) ? $saved : array();
		$default = self::DEFAULT_LIMIT;
		$limit   = array();

		foreach ( $default as $key => $fallback ) {
			$value = isset( $saved[ $key ] ) ? (int) $saved[ $key ] : $fallback;
			// 0 表示不限；负数当作不合法，回退默认值。
			$limit[ $key ] = $value >= 0 ? $value : $fallback;
		}

		return $limit;
	}

	/** 黑名单域名列表：一行一个，去掉空行与注释行（以 # 开头）。 */
	public static function blocked_domains() {
		$raw    = (string) get_option( self::OPTION_DOMAINS, '' );
		$lines  = preg_split( '/\r\n|\r|\n/', $raw );
		$result = array();

		foreach ( (array) $lines as $line ) {
			$line = trim( $line );
			if ( '' === $line || 0 === strpos( $line, '#' ) ) {
				continue;
			}
			$result[] = strtolower( ltrim( $line, '@' ) );
		}

		return array_values( array_unique( $result ) );
	}

	/**
	 * 注册校验。
	 *
	 * 任何一条规则命中都会往 $errors 里加一条错误；WordPress 会因此
	 * 取消这次注册，并把消息显示给访客。
	 *
	 * @param WP_Error $errors     注册错误集合。
	 * @param string   $login      用户名（未使用，保留签名用）。
	 * @param string   $email      邮箱。
	 * @return WP_Error
	 */
	public static function check_registration( $errors, $login, $email ) {
		if ( ! ( $errors instanceof WP_Error ) ) {
			return $errors;
		}

		$email = strtolower( trim( (string) $email ) );
		if ( '' === $email || false === strpos( $email, '@' ) ) {
			// 邮箱本身的格式由 WordPress 校验，这里不重复报错。
			return $errors;
		}

		$domain = substr( $email, strrpos( $email, '@' ) + 1 );
		$reason = '';

		if ( self::domain_is_blocked( $domain ) ) {
			$reason = '邮箱域名在黑名单中：' . $domain;
		} else {
			$reason = self::ip_rejection_reason();
		}

		if ( '' === $reason ) {
			return $errors;
		}

		self::log_block( $email, $reason );
		// 提示文案保持中立，具体原因只写进后台日志。
		$errors->add( 'l2d_ai_blocked', self::BLOCK_MESSAGE );

		return $errors;
	}

	/** 域名是否命中黑名单；子域也算命中（mail.example.com 命中 example.com）。 */
	private static function domain_is_blocked( $domain ) {
		foreach ( self::blocked_domains() as $blocked ) {
			if ( $domain === $blocked || substr( $domain, - strlen( '.' . $blocked ) ) === '.' . $blocked ) {
				return true;
			}
		}
		return false;
	}

	/**
	 * 按 IP 判断是否该拒绝，返回原因；放行时返回空串。
	 *
	 * IP 只取 REMOTE_ADDR：X-Forwarded-For 可以被客户端伪造，
	 * 拿它做限制等于让对手自己决定用哪个计数桶。站点前面有 Nginx 时，
	 * REMOTE_ADDR 是 Nginx 的地址——那种部署下这条限制会变成全站共享，
	 * 此时把对应项设成 0 即关闭该项限制。
	 */
	private static function ip_rejection_reason() {
		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? (string) $_SERVER['REMOTE_ADDR'] : '';
		if ( '' === $ip || ! filter_var( $ip, FILTER_VALIDATE_IP ) ) {
			$ip = 'unknown';
		}

		$limit = self::register_limit();
		$key   = md5( $ip );

		// 两次注册之间的最小间隔。unknown 是拿不到 IP 时的兜底桶，
		// 对它用间隔限制会把互不相关的访客串在一起，所以只做全天计数。
		if ( 'unknown' !== $ip && $limit['min_interval_seconds'] > 0 ) {
			$last = (int) get_transient( 'l2d_ai_reg_' . $key . '_t' );
			if ( $last > 0 && ( time() - $last ) < $limit['min_interval_seconds'] ) {
				return '同一 IP 两次注册间隔太短';
			}
		}

		if ( $limit['per_ip_hour'] > 0 ) {
			$hour = self::count_window( 'l2d_ai_reg_' . $key . '_h', 3600 );
			if ( $hour >= $limit['per_ip_hour'] ) {
				return '同一 IP 一小时内注册次数已达上限';
			}
		}

		if ( $limit['per_ip_day'] > 0 ) {
			$day = self::count_window( 'l2d_ai_reg_' . $key . '_d', DAY_IN_SECONDS );
			if ( $day >= $limit['per_ip_day'] ) {
				return '同一 IP 当天注册次数已达上限';
			}
		}

		return '';
	}

	/**
	 * 读取一个滑动窗口内的计数，并把这次注册记进去。
	 *
	 * 直接 set_transient( $key, $n + 1, $ttl ) 会把过期时间往后推，
	 * 高频访问下窗口永远不过期（等于变成累计计数）。所以这里存
	 * 「窗口起点 + 次数」，过期时间由起点算，窗口是固定的。
	 */
	private static function count_window( $key, $seconds ) {
		$now      = time();
		$data     = get_transient( $key );
		$start    = is_array( $data ) && isset( $data['start'] ) ? (int) $data['start'] : $now;
		$count    = is_array( $data ) && isset( $data['count'] ) ? (int) $data['count'] : 0;
		$elapsed  = $now - $start;
		$remaining = $seconds - $elapsed;

		if ( $remaining <= 0 ) {
			// 旧窗口已经过期（transient 可能因对象缓存被清而残留）。
			$start     = $now;
			$count     = 0;
			$remaining = $seconds;
		}

		$count++;
		set_transient(
			$key,
			array(
				'start' => $start,
				'count' => $count,
			),
			$remaining
		);

		return $count;
	}

	/** 注册成功后记录 IP 与时间，后台用户列表据此判断来源。 */
	public static function record_registration( $user_id ) {
		$user_id = (int) $user_id;
		if ( $user_id <= 0 ) {
			return;
		}

		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? (string) $_SERVER['REMOTE_ADDR'] : '';
		if ( '' === $ip || ! filter_var( $ip, FILTER_VALIDATE_IP ) ) {
			$ip = 'unknown';
		}

		update_user_meta( $user_id, self::META_IP, $ip );
		update_user_meta( $user_id, self::META_AT, current_time( 'mysql' ) );

		// 记录最后一次注册时间，供上面的最小间隔检查使用。
		set_transient( 'l2d_ai_reg_' . md5( $ip ) . '_t', time(), DAY_IN_SECONDS );
	}

	/** 写一条拦截记录（最新在前，最多 LOG_MAX 条）。 */
	private static function log_block( $email, $reason ) {
		$log = get_option( self::OPTION_LOG, array() );
		$log = is_array( $log ) ? $log : array();

		$ip = isset( $_SERVER['REMOTE_ADDR'] ) ? (string) $_SERVER['REMOTE_ADDR'] : '';
		if ( '' === $ip || ! filter_var( $ip, FILTER_VALIDATE_IP ) ) {
			$ip = 'unknown';
		}

		array_unshift(
			$log,
			array(
				'time'   => current_time( 'mysql' ),
				'email'  => (string) $email,
				'ip'     => $ip,
				'reason' => (string) $reason,
			)
		);

		// 只留最近的记录：这个选项每次都被整条读取，不设上限会越滚越大。
		$log = array_slice( $log, 0, self::LOG_MAX );
		// autoload=no：只有后台页用到，不要让它跟着每个请求一起加载。
		update_option( self::OPTION_LOG, $log, false );
	}
}
