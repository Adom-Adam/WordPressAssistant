<?php
/**
 * 定期对账：清理已经在 WordPress 里删除、但后端还留着数据的账号。
 *
 * 为什么不能只靠 `deleted_user` 钩子：钩子只会收到“刚刚被删的 ID”，
 * 如果当时后端不可用、或者数据是在插件启用前删的，就没人再记起这些 ID。
 * 这里每天对比“上次见过的用户名单”和“现在还在的账号”，差集就是
 * 被删掉的账号，逐个通知后端清理解绑与记忆。
 *
 * 第一次运行只记名单、不删任何东西：没有历史快照时，分不清“新用户”
 * 和“早就删掉的人”。之后快照里才包含真正需要跟踪的 ID。
 *
 * 安全阀：如果 WordPress 返回空用户列表而上次名单非空，说明查询可能
 * 出了问题；这时宁可跳过一轮，也不要清空整站数据。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Reconcile {

	const HOOK               = 'l2d_ai_reconcile_users';
	const OPTION_KNOWN_USERS = 'l2d_ai_known_user_ids';

	public static function init() {
		// 每次请求都检查一次计划任务是否存在：插件是直接覆盖文件部署的，
		// 没有可靠的“启用插件”时机可以只挂一次。
		add_action( 'init', array( __CLASS__, 'ensure_schedule' ) );
		add_action( self::HOOK, array( __CLASS__, 'run' ) );
	}

	public static function ensure_schedule() {
		if ( ! wp_next_scheduled( self::HOOK ) ) {
			// 错开一小时，避免刚部署就立刻打一批后端请求。
			wp_schedule_event( time() + HOUR_IN_SECONDS, 'daily', self::HOOK );
		}
	}

	public static function deactivate() {
		$timestamp = wp_next_scheduled( self::HOOK );
		if ( $timestamp ) {
			wp_unschedule_event( $timestamp, self::HOOK );
		}
	}

	public static function run() {
		$previous = array_map( 'intval', (array) get_option( self::OPTION_KNOWN_USERS, array() ) );
		$current  = self::current_user_ids();

		if ( empty( $current ) && ! empty( $previous ) ) {
			error_log( 'L2D AI 对账跳过：WordPress 返回空用户列表，疑似查询异常。' );
			return;
		}

		$failed = array();
		foreach ( array_values( array_diff( $previous, $current ) ) as $user_id ) {
			if ( ! self::purge_user( (int) $user_id ) ) {
				$failed[] = (int) $user_id;
			}
		}

		// 失败的用户留在名单里，明天再试；当前用户永远在名单里。
		update_option(
			self::OPTION_KNOWN_USERS,
			array_values( array_unique( array_merge( $current, $failed ) ) ),
			false
		);
	}

	/** 当前全部用户 ID；`get_users` 正常总是返回数组，防御性处理一下。 */
	private static function current_user_ids() {
		$ids = get_users(
			array(
				'fields' => 'ID',
				'number' => -1,
			)
		);
		if ( ! is_array( $ids ) ) {
			return array();
		}
		return array_values( array_unique( array_map( 'intval', $ids ) ) );
	}

	/** 清掉一个已删账号的后端数据；任意一步失败返回 false，留给下一轮重试。 */
	public static function purge_user( $user_id ) {
		$ok = true;

		$unbound = L2D_AI_Plugin::system_backend_request(
			'DELETE',
			'/admin/users/' . $user_id . '/bind'
		);
		if ( is_wp_error( $unbound ) ) {
			error_log( 'L2D AI 删除用户后解绑失败 user=' . $user_id . ': ' . $unbound->get_error_message() );
			$ok = false;
		}

		$purged = L2D_AI_Plugin::system_backend_request(
			'DELETE',
			'/admin/users/' . $user_id . '/memory'
		);
		if ( is_wp_error( $purged ) ) {
			error_log( 'L2D AI 删除用户后清理记忆失败 user=' . $user_id . ': ' . $purged->get_error_message() );
			$ok = false;
		}

		return $ok;
	}
}
