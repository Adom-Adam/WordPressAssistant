<?php
/**
 * 允许注册时使用中文用户名。
 *
 * 背景：WordPress 的 wp_insert_user() 会用 sanitize_user( $login, true )
 * 做一次“严格模式”清洗，正则只留 ASCII：
 *
 *     preg_replace( '|[^a-z0-9 _.\-@]|i', '', $username )
 *
 * 汉字会被整段删掉，于是 validate_username() 判定“用户名含非法字符”，
 * 用户在注册页看到的就是 “This username is invalid because it uses
 * illegal characters.”。这不是主题或插件的问题，也不是数据库不支持
 * （utf8mb4 完全存得下），而是核心为了“最大可移植性”做的取舍。
 *
 * 修法只用一个官方过滤器：
 * - sanitize_user 在清洗完成后触发，拿得到原始输入和 $strict 标记；
 * - 我们只在 $strict 且原始输入含汉字时，按同样规则重新清洗一遍，
 *   把汉字保留下来；纯 ASCII 用户名原样返回，核心行为一个字都不变。
 *
 * 还有一处必须一起处理：wp_insert_user() 会用 sanitize_title() 从用户名
 * 生成 user_nicename（作者页地址）。中文会被 utf8_uri_encode() 变成
 * “%e6%a2%85%e6%9e%97”这样的长串，6 个汉字就超过 50 字符上限，
 * 报 user_nicename_too_long。所以我们把这种 nicename 换成稳定的 ASCII
 * 别名 user-xxxxxxxxxxxxxxxx；登录名、显示名、用户 ID 都不受影响。
 *
 * 不改 WordPress 核心，不直接写 users 表，也不绕过密码/邮箱校验。
 *
 * @package wp-live2d-ai
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Chinese_Usernames {

	/** nicename 别名前缀，方便在作者页 URL 里认出来。 */
	const NICENAME_PREFIX = 'user-';

	public static function init() {
		add_filter( 'sanitize_user', array( __CLASS__, 'sanitize_login' ), 10, 3 );
		add_filter( 'pre_user_nicename', array( __CLASS__, 'ascii_nicename' ) );
	}

	/**
	 * 严格模式下保留汉字。
	 *
	 * 只处理“原始输入含汉字”的情况：英文、数字、邮箱式的用户名完全走
	 * 核心逻辑，避免这个过滤器影响站点上的其它代码。
	 *
	 * 返回值和核心一样是“清洗后的结果”，不做 trim 之外的任何改写；
	 * validate_username() 仍会对比“清洗前 == 清洗后”，所以带表情符号、
	 * 全角标点、零宽字符的名字依然会被拒绝——这是有意的。
	 *
	 * @param string $username     核心清洗后的用户名。
	 * @param string $raw_username 清洗前的原始输入。
	 * @param bool   $strict       是否为严格模式。
	 * @return string
	 */
	public static function sanitize_login( $username, $raw_username, $strict ) {
		if ( ! $strict || ! is_string( $raw_username ) ) {
			return $username;
		}

		// 不含汉字就交还给核心，保持原有行为。
		if ( 1 !== preg_match( '/\p{Han}/u', $raw_username ) ) {
			return $username;
		}

		$clean = wp_strip_all_tags( $raw_username );

		// 有 intl 扩展时先做 NFKC，把全角字母数字归一成半角。
		// 没有 intl 也不影响汉字本身，直接跳过。
		if ( class_exists( 'Normalizer' ) ) {
			$normalized = Normalizer::normalize( $clean, Normalizer::FORM_KC );
			if ( is_string( $normalized ) ) {
				$clean = $normalized;
			}
		}

		// 与核心 sanitize_user() 保持同样的基础清洗顺序。
		$clean = remove_accents( $clean );
		$clean = preg_replace( '|%([a-fA-F0-9][a-fA-F0-9])|', '', $clean );
		$clean = preg_replace( '/&.+?;/', '', $clean );

		// 控制符、格式符（含零宽连接符、方向控制符）一律去掉，
		// 避免两个“看起来一样”的用户名。
		$clean = preg_replace( '/[\p{Cc}\p{Cf}]/u', '', (string) $clean );

		// 只保留汉字、ASCII 字母数字，以及核心严格模式本来就允许的
		// 空格、下划线、点、连字符、@。
		$clean = preg_replace( '/[^\p{Han}A-Za-z0-9 _.\-@]/u', '', (string) $clean );

		$clean = trim( (string) $clean );
		$clean = preg_replace( '/\s+/u', ' ', $clean );

		return is_string( $clean ) ? $clean : $username;
	}

	/**
	 * 中文用户名生成的 nicename 会是一长串 %xx，超过核心的 50 字符上限。
	 * 这里换成稳定的 ASCII 别名；同一个用户名每次得到同一个值。
	 *
	 * 只在 nicename 里含有百分号编码时才替换，原本就是 ASCII 的用户不受影响。
	 *
	 * @param string $nicename 核心由用户名生成的作者别名。
	 * @return string
	 */
	public static function ascii_nicename( $nicename ) {
		if ( ! is_string( $nicename ) || '' === $nicename ) {
			return $nicename;
		}

		if ( 1 !== preg_match( '/%[0-9a-fA-F]{2}/', $nicename ) ) {
			return $nicename;
		}

		return self::NICENAME_PREFIX . substr( md5( $nicename ), 0, 16 );
	}
}
