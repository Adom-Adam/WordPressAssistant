<?php
/**
 * 表情库：梅林主动发表情时用的素材。
 *
 * 三类数据：
 * - 素材：文件在服务器 data/stickers/library，后台只管标签和启用状态；
 * - 标签：情绪（受控词表）+ 自由标签，多对多；
 * - 出处：原神、鸣潮……单独一张词表，可加别名并合并。
 *
 * 色彩（tone）单独一列：warm / neutral / cold。冲突或冷淡时只会选 cold，
 * 所以“真正的冷漠”不能和可爱的白眼混在一起。
 */

defined( 'ABSPATH' ) || exit;

final class L2D_AI_Stickers {

	const PAGE_SLUG = 'l2d-ai-stickers';
	const TRANSIENT = 'l2d_ai_stickers_notice_';
	const PER_PAGE  = 40;
	const MAX_BYTES = 5242880; // 5 MB

	const TONES = array(
		'warm'    => '温暖',
		'neutral' => '中性',
		'cold'    => '冷漠',
	);

	public static function init() {
		add_action( 'admin_menu', array( __CLASS__, 'add_page' ) );
		add_action( 'admin_post_l2d_ai_sticker_upload', array( __CLASS__, 'handle_upload' ) );
		add_action( 'admin_post_l2d_ai_sticker_update', array( __CLASS__, 'handle_update' ) );
		add_action( 'admin_post_l2d_ai_sticker_delete', array( __CLASS__, 'handle_delete' ) );
		add_action( 'admin_post_l2d_ai_sticker_origin_save', array( __CLASS__, 'handle_origin_save' ) );
		add_action( 'admin_post_l2d_ai_sticker_origin_delete', array( __CLASS__, 'handle_origin_delete' ) );
		add_action( 'admin_post_l2d_ai_sticker_settings', array( __CLASS__, 'handle_settings' ) );
	}

	public static function add_page() {
		add_options_page(
			'Live2D 小助手 · 表情库',
			'Live2D 小助手 · 表情库',
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

	private static function back_args() {
		$args = array();
		foreach ( array( 'q', 'tone', 'enabled' ) as $key ) {
			$field = 'back_' . $key;
			if ( isset( $_POST[ $field ] ) && '' !== $_POST[ $field ] ) {
				$args[ $key ] = sanitize_text_field( wp_unslash( $_POST[ $field ] ) );
			}
		}
		foreach ( array( 'paged' ) as $key ) {
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

	private static $current = array(
		'q'       => '',
		'tone'    => '',
		'enabled' => '',
		'paged'   => 1,
	);

	private static function hidden_back_fields() {
		foreach ( array( 'q', 'tone', 'enabled' ) as $key ) {
			printf(
				'<input type="hidden" name="back_%s" value="%s" />',
				esc_attr( $key ),
				esc_attr( (string) self::$current[ $key ] )
			);
		}
		printf(
			'<input type="hidden" name="back_paged" value="%d" />',
			(int) self::$current['paged']
		);
	}

	private static function tone_label( $tone ) {
		return isset( self::TONES[ $tone ] ) ? self::TONES[ $tone ] : '中性';
	}

	private static function render_tone_select( $selected = 'neutral', $name = 'tone' ) {
		echo '<select name="' . esc_attr( $name ) . '">';
		foreach ( self::TONES as $value => $label ) {
			printf(
				'<option value="%s" %s>%s</option>',
				esc_attr( $value ),
				selected( $value, (string) $selected, false ),
				esc_html( $label )
			);
		}
		echo '</select>';
	}

	private static function render_origin_select( $origins, $selected_id = 0, $name = 'origin' ) {
		echo '<select name="' . esc_attr( $name ) . '">';
		echo '<option value="">（不设出处）</option>';
		foreach ( $origins as $origin ) {
			printf(
				'<option value="%s" %s>%s</option>',
				esc_attr( (string) $origin['name'] ),
				selected( (int) $selected_id, (int) $origin['id'], false ),
				esc_html( (string) $origin['name'] )
			);
		}
		echo '</select>';
	}

	private static function parse_tags( $raw ) {
		$parts = preg_split( '/[,，、;；\n]+/u', (string) $raw );
		$tags  = array();
		foreach ( (array) $parts as $part ) {
			$tag = trim( $part );
			if ( '' !== $tag && ! in_array( $tag, $tags, true ) ) {
				$tags[] = $tag;
			}
		}
		return array_slice( $tags, 0, 30 );
	}

	// ------------------------------------------------------------------
	// 操作
	// ------------------------------------------------------------------
	public static function handle_upload() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		if ( empty( $_FILES['sticker_file']['tmp_name'] ) ) {
			self::redirect_with_notice( '请选择一张图片。', true, self::back_args() );
		}
		$file = $_FILES['sticker_file'];
		if ( ! empty( $file['error'] ) ) {
			self::redirect_with_notice( '上传失败（错误码 ' . (int) $file['error'] . '）。', true, self::back_args() );
		}
		if ( (int) $file['size'] > self::MAX_BYTES ) {
			self::redirect_with_notice( '图片太大，单张上限 5MB。', true, self::back_args() );
		}
		if ( ! is_uploaded_file( $file['tmp_name'] ) ) {
			self::redirect_with_notice( '上传文件校验失败。', true, self::back_args() );
		}

		$data = file_get_contents( $file['tmp_name'] );
		if ( false === $data ) {
			self::redirect_with_notice( '读取上传文件失败。', true, self::back_args() );
		}

		$tone = isset( $_POST['tone'] ) ? sanitize_key( wp_unslash( $_POST['tone'] ) ) : 'neutral';
		if ( ! isset( self::TONES[ $tone ] ) ) {
			$tone = 'neutral';
		}
		$origin = isset( $_POST['origin'] ) ? sanitize_text_field( wp_unslash( $_POST['origin'] ) ) : '';

		$result = L2D_AI_Plugin::request_backend(
			'POST',
			'/admin/stickers',
			array(
				'content_base64' => base64_encode( $data ),
				'filename'       => sanitize_file_name( (string) $file['name'] ),
				'description'    => isset( $_POST['description'] ) ? sanitize_textarea_field( wp_unslash( $_POST['description'] ) ) : '',
				'tone'           => $tone,
				'origin'         => '' !== $origin ? $origin : null,
				'tags'           => self::parse_tags( isset( $_POST['tags'] ) ? wp_unslash( $_POST['tags'] ) : '' ),
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}

		$duplicate = ! empty( $result['duplicate'] );
		self::redirect_with_notice(
			$duplicate ? '这张图已经在表情库里了。' : '已加入表情库。',
			false,
			self::back_args()
		);
	}

	public static function handle_update() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		$id = isset( $_POST['sticker_id'] ) ? absint( $_POST['sticker_id'] ) : 0;
		if ( $id <= 0 ) {
			self::redirect_with_notice( '参数不完整。', true, self::back_args() );
		}
		$tone = isset( $_POST['tone'] ) ? sanitize_key( wp_unslash( $_POST['tone'] ) ) : 'neutral';
		if ( ! isset( self::TONES[ $tone ] ) ) {
			$tone = 'neutral';
		}
		$origin = isset( $_POST['origin'] ) ? sanitize_text_field( wp_unslash( $_POST['origin'] ) ) : '';
		$enabled = ! empty( $_POST['enabled'] );

		$payload = array(
			'tone'        => $tone,
			'enabled'     => $enabled,
			'tags'        => self::parse_tags( isset( $_POST['tags'] ) ? wp_unslash( $_POST['tags'] ) : '' ),
			'origin'      => '' !== $origin ? $origin : null,
			'description' => isset( $_POST['description'] ) ? sanitize_textarea_field( wp_unslash( $_POST['description'] ) ) : '',
		);

		$result = L2D_AI_Plugin::request_backend( 'PUT', '/admin/stickers/' . $id, $payload );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}
		self::redirect_with_notice( '表情 #' . $id . ' 已更新。', false, self::back_args() );
	}

	public static function handle_delete() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		$id = isset( $_POST['sticker_id'] ) ? absint( $_POST['sticker_id'] ) : 0;
		if ( $id <= 0 ) {
			self::redirect_with_notice( '参数不完整。', true, self::back_args() );
		}
		$result = L2D_AI_Plugin::request_backend( 'DELETE', '/admin/stickers/' . $id );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}
		self::redirect_with_notice( '表情 #' . $id . ' 已删除。', false, self::back_args() );
	}

	public static function handle_origin_save() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		$origin_id = isset( $_POST['origin_id'] ) ? absint( $_POST['origin_id'] ) : 0;
		$name      = isset( $_POST['name'] ) ? sanitize_text_field( wp_unslash( $_POST['name'] ) ) : '';
		$aliases   = self::parse_tags( isset( $_POST['aliases'] ) ? wp_unslash( $_POST['aliases'] ) : '' );
		if ( '' === $name ) {
			self::redirect_with_notice( '出处名称不能为空。', true, self::back_args() );
		}

		$path = $origin_id > 0
			? '/admin/sticker-origins/' . $origin_id
			: '/admin/sticker-origins';
		$result = L2D_AI_Plugin::request_backend(
			$origin_id > 0 ? 'PUT' : 'POST',
			$path,
			array(
				'name'    => $name,
				'aliases' => $aliases,
			)
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}
		self::redirect_with_notice( '出处已保存。', false, self::back_args() );
	}

	public static function handle_origin_delete() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		$origin_id = isset( $_POST['origin_id'] ) ? absint( $_POST['origin_id'] ) : 0;
		if ( $origin_id <= 0 ) {
			self::redirect_with_notice( '参数不完整。', true, self::back_args() );
		}
		$result = L2D_AI_Plugin::request_backend( 'DELETE', '/admin/sticker-origins/' . $origin_id );
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}
		self::redirect_with_notice( '出处已删除，相关表情变回“不设出处”。', false, self::back_args() );
	}

	public static function handle_settings() {
		if ( ! current_user_can( 'manage_options' ) ) {
			wp_die( '需要管理员权限', '无权限', array( 'response' => 403 ) );
		}
		check_admin_referer( 'l2d_ai_stickers' );

		$raw = isset( $_POST['sticker_library_max'] ) ? trim( (string) wp_unslash( $_POST['sticker_library_max'] ) ) : '';
		if ( '' === $raw || ! preg_match( '/^\d+$/', $raw ) ) {
			self::redirect_with_notice( '容量上限必须是非负整数。', true, self::back_args() );
		}
		$result = L2D_AI_Plugin::request_backend(
			'PUT',
			'/admin/settings',
			array( 'sticker_library_max' => (int) $raw )
		);
		if ( is_wp_error( $result ) ) {
			self::redirect_with_notice( $result->get_error_message(), true, self::back_args() );
		}
		self::redirect_with_notice( '表情库容量上限已保存。', false, self::back_args() );
	}

	// ------------------------------------------------------------------
	// 页面
	// ------------------------------------------------------------------
	private static function matches( array $sticker, $query ) {
		if ( '' === $query ) {
			return true;
		}
		$haystack = array(
			isset( $sticker['description'] ) ? $sticker['description'] : '',
			isset( $sticker['origin'] ) ? $sticker['origin'] : '',
			isset( $sticker['proposed_origin'] ) ? $sticker['proposed_origin'] : '',
			implode( ' ', isset( $sticker['tags'] ) ? (array) $sticker['tags'] : array() ),
		);
		foreach ( $haystack as $value ) {
			if ( false !== stripos( (string) $value, $query ) ) {
				return true;
			}
		}
		return false;
	}

	public static function render_page() {
		$query   = isset( $_GET['q'] ) ? sanitize_text_field( wp_unslash( $_GET['q'] ) ) : '';
		$tone    = isset( $_GET['tone'] ) ? sanitize_key( wp_unslash( $_GET['tone'] ) ) : '';
		$enabled = isset( $_GET['enabled'] ) ? sanitize_key( wp_unslash( $_GET['enabled'] ) ) : '';
		$paged   = isset( $_GET['paged'] ) ? max( 1, absint( $_GET['paged'] ) ) : 1;

		$data       = L2D_AI_Plugin::request_backend( 'GET', '/admin/stickers' );
		$backend_ok = ! is_wp_error( $data );
		$stickers   = $backend_ok && isset( $data['stickers'] ) ? (array) $data['stickers'] : array();
		$origins    = $backend_ok && isset( $data['origins'] ) ? (array) $data['origins'] : array();
		$library_max = $backend_ok && isset( $data['library_max'] ) ? (int) $data['library_max'] : 2000;

		$filtered = array();
		foreach ( $stickers as $sticker ) {
			if ( '' !== $tone && ( isset( $sticker['tone'] ) ? $sticker['tone'] : '' ) !== $tone ) {
				continue;
			}
			if ( 'on' === $enabled && empty( $sticker['enabled'] ) ) {
				continue;
			}
			if ( 'off' === $enabled && ! empty( $sticker['enabled'] ) ) {
				continue;
			}
			if ( ! self::matches( $sticker, $query ) ) {
				continue;
			}
			$filtered[] = $sticker;
		}

		$total = count( $filtered );
		$pages = max( 1, (int) ceil( $total / self::PER_PAGE ) );
		$paged = min( $paged, $pages );
		$rows  = array_slice( $filtered, ( $paged - 1 ) * self::PER_PAGE, self::PER_PAGE );

		self::$current = array(
			'q'       => $query,
			'tone'    => $tone,
			'enabled' => $enabled,
			'paged'   => $paged,
		);

		$notice = get_transient( self::notice_transient() );
		delete_transient( self::notice_transient() );
		?>
		<div class="wrap">
			<h1>表情库</h1>

			<?php if ( is_array( $notice ) ) : ?>
				<div class="notice <?php echo ! empty( $notice['error'] ) ? 'notice-error' : 'notice-success'; ?> is-dismissible">
					<p><?php echo esc_html( (string) $notice['message'] ); ?></p>
				</div>
			<?php endif; ?>

			<?php if ( ! $backend_ok ) : ?>
				<div class="notice notice-error">
					<p>后端不可达，表情库暂时读不到：<?php echo esc_html( $data->get_error_message() ); ?></p>
				</div>
			<?php endif; ?>

			<p class="description">
				梅林主动发表情时会从这里选。色彩「冷漠」只会在冲突或冷淡时使用；
				出处可以自由添加和合并别名。当前共 <?php echo esc_html( (string) $total ); ?> 张。
			</p>

			<form method="get" action="<?php echo esc_url( admin_url( 'options-general.php' ) ); ?>" style="margin: 1em 0;">
				<input type="hidden" name="page" value="<?php echo esc_attr( self::PAGE_SLUG ); ?>" />
				<input type="search" name="q" value="<?php echo esc_attr( $query ); ?>" placeholder="搜索描述 / 标签 / 出处" />
				<select name="tone">
					<option value="">全部色彩</option>
					<?php foreach ( self::TONES as $value => $label ) : ?>
						<option value="<?php echo esc_attr( $value ); ?>" <?php selected( $tone, $value ); ?>><?php echo esc_html( $label ); ?></option>
					<?php endforeach; ?>
				</select>
				<select name="enabled">
					<option value="">全部状态</option>
					<option value="on" <?php selected( $enabled, 'on' ); ?>>已启用</option>
					<option value="off" <?php selected( $enabled, 'off' ); ?>>已停用</option>
				</select>
				<?php submit_button( '筛选', 'secondary', '', false ); ?>
			</form>

			<h2>上传素材</h2>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" enctype="multipart/form-data">
				<input type="hidden" name="action" value="l2d_ai_sticker_upload" />
				<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
				<?php self::hidden_back_fields(); ?>
				<table class="form-table" role="presentation">
					<tr>
						<th scope="row"><label for="l2d-ai-sticker-file">图片</label></th>
						<td>
							<input type="file" id="l2d-ai-sticker-file" name="sticker_file" accept="image/png,image/gif,image/jpeg,image/webp" />
							<p class="description">PNG / GIF / JPG / WebP，单张不超过 5MB。后缀不要求正确，按内容识别。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">色彩</th>
						<td><?php self::render_tone_select( 'neutral' ); ?></td>
					</tr>
					<tr>
						<th scope="row">出处</th>
						<td><?php self::render_origin_select( $origins ); ?></td>
					</tr>
					<tr>
						<th scope="row">标签</th>
						<td>
							<input type="text" name="tags" class="regular-text" placeholder="开心, 猫, 暗中观察" />
							<p class="description">逗号分隔；情绪词会原样保存，检索和主动选择都会用。</p>
						</td>
					</tr>
					<tr>
						<th scope="row">描述</th>
						<td><textarea name="description" class="regular-text" rows="2"></textarea></td>
					</tr>
				</table>
				<?php submit_button( '上传到表情库', 'secondary' ); ?>
			</form>

			<h2>素材（<?php echo esc_html( (string) $total ); ?>）</h2>
			<table class="widefat striped">
				<thead>
					<tr>
						<th style="width:80px;">预览</th>
						<th>ID</th>
						<th>出处</th>
						<th>色彩</th>
						<th>标签</th>
						<th>状态 / 来源</th>
						<th>操作</th>
					</tr>
				</thead>
				<tbody>
				<?php if ( empty( $rows ) ) : ?>
					<tr><td colspan="7"><?php echo $backend_ok ? '没有符合条件的表情。' : '—'; ?></td></tr>
				<?php endif; ?>
				<?php foreach ( $rows as $sticker ) : ?>
					<?php
					$id       = isset( $sticker['id'] ) ? (int) $sticker['id'] : 0;
					$tone_v   = isset( $sticker['tone'] ) ? (string) $sticker['tone'] : 'neutral';
					$origin_n = isset( $sticker['origin'] ) ? (string) $sticker['origin'] : '';
					$proposed = isset( $sticker['proposed_origin'] ) ? (string) $sticker['proposed_origin'] : '';
					$tags     = isset( $sticker['tags'] ) ? (array) $sticker['tags'] : array();
					$enabled_v = ! empty( $sticker['enabled'] );
					$source   = isset( $sticker['source'] ) ? (string) $sticker['source'] : '';
					$image    = L2D_AI_Plugin::backend_public_url( '/api/stickers/' . $id . '/image' );
					?>
					<tr>
						<td><img src="<?php echo esc_url( $image ); ?>" alt="" style="max-width:64px;max-height:64px;" loading="lazy" /></td>
						<td><?php echo esc_html( (string) $id ); ?></td>
						<td>
							<?php echo '' !== $origin_n ? esc_html( $origin_n ) : '—'; ?>
							<?php if ( '' !== $proposed ) : ?>
								<br /><span class="description">待确认：<?php echo esc_html( $proposed ); ?></span>
							<?php endif; ?>
						</td>
						<td><?php echo esc_html( self::tone_label( $tone_v ) ); ?></td>
						<td><?php echo esc_html( implode( '、', $tags ) ); ?></td>
						<td><?php echo $enabled_v ? '已启用' : '已停用'; ?> / <?php echo esc_html( $source ); ?></td>
						<td>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" style="margin-bottom:8px;">
								<input type="hidden" name="action" value="l2d_ai_sticker_update" />
								<input type="hidden" name="sticker_id" value="<?php echo esc_attr( (string) $id ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
								<?php self::hidden_back_fields(); ?>
								<?php self::render_tone_select( $tone_v ); ?>
								<?php self::render_origin_select( $origins, isset( $sticker['origin_id'] ) ? (int) $sticker['origin_id'] : 0 ); ?>
								<input type="text" name="tags" value="<?php echo esc_attr( implode( ', ', $tags ) ); ?>" class="regular-text" placeholder="标签" />
								<input type="text" name="description" value="<?php echo esc_attr( isset( $sticker['description'] ) ? (string) $sticker['description'] : '' ); ?>" class="large-text" placeholder="描述" />
								<label><input type="checkbox" name="enabled" value="1" <?php checked( $enabled_v ); ?> /> 启用</label>
								<button type="submit" class="button button-small">保存</button>
							</form>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" onsubmit="return confirm('删除后素材文件也会一起删除，确定吗？');">
								<input type="hidden" name="action" value="l2d_ai_sticker_delete" />
								<input type="hidden" name="sticker_id" value="<?php echo esc_attr( (string) $id ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
								<?php self::hidden_back_fields(); ?>
								<button type="submit" class="button button-small button-link-delete">删除</button>
							</form>
						</td>
					</tr>
				<?php endforeach; ?>
				</tbody>
			</table>

			<?php if ( $pages > 1 ) : ?>
				<p class="tablenav-pages">
					<?php if ( $paged > 1 ) : ?>
						<a class="button" href="<?php echo esc_url( add_query_arg( array( 'page' => self::PAGE_SLUG, 'q' => $query, 'tone' => $tone, 'enabled' => $enabled, 'paged' => $paged - 1 ), admin_url( 'options-general.php' ) ) ); ?>">上一页</a>
					<?php endif; ?>
					<span class="description">第 <?php echo esc_html( (string) $paged ); ?> / <?php echo esc_html( (string) $pages ); ?> 页</span>
					<?php if ( $paged < $pages ) : ?>
						<a class="button" href="<?php echo esc_url( add_query_arg( array( 'page' => self::PAGE_SLUG, 'q' => $query, 'tone' => $tone, 'enabled' => $enabled, 'paged' => $paged + 1 ), admin_url( 'options-general.php' ) ) ); ?>">下一页</a>
					<?php endif; ?>
				</p>
			<?php endif; ?>

			<hr />
			<h2>出处管理</h2>
			<p class="description">模型提议的新出处会显示在素材行里，确认后在这里加进来。别名可以填“Genshin, 原神启动”。</p>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_sticker_origin_save" />
				<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
				<?php self::hidden_back_fields(); ?>
				<input type="text" name="name" placeholder="出处名称" class="regular-text" />
				<input type="text" name="aliases" placeholder="别名，逗号分隔" class="regular-text" />
				<?php submit_button( '新增出处', 'secondary', '', false ); ?>
			</form>

			<table class="widefat striped" style="margin-top:12px;">
				<thead><tr><th>出处</th><th>操作</th></tr></thead>
				<tbody>
				<?php if ( empty( $origins ) ) : ?>
					<tr><td colspan="2">还没有出处。</td></tr>
				<?php endif; ?>
				<?php foreach ( $origins as $origin ) : ?>
					<tr>
						<td>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
								<input type="hidden" name="action" value="l2d_ai_sticker_origin_save" />
								<input type="hidden" name="origin_id" value="<?php echo esc_attr( (string) $origin['id'] ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
								<?php self::hidden_back_fields(); ?>
								<input type="text" name="name" value="<?php echo esc_attr( (string) $origin['name'] ); ?>" class="regular-text" />
								<input type="text" name="aliases" value="<?php echo esc_attr( implode( ', ', (array) $origin['aliases'] ) ); ?>" class="regular-text" placeholder="别名，逗号分隔" />
								<button type="submit" class="button button-small">保存</button>
							</form>
						</td>
						<td>
							<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>" onsubmit="return confirm('删除出处？相关表情会变成“不设出处”。');">
								<input type="hidden" name="action" value="l2d_ai_sticker_origin_delete" />
								<input type="hidden" name="origin_id" value="<?php echo esc_attr( (string) $origin['id'] ); ?>" />
								<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
								<?php self::hidden_back_fields(); ?>
								<button type="submit" class="button button-small button-link-delete">删除</button>
							</form>
						</td>
					</tr>
				<?php endforeach; ?>
				</tbody>
			</table>

			<hr />
			<h2>设置</h2>
			<form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
				<input type="hidden" name="action" value="l2d_ai_sticker_settings" />
				<?php wp_nonce_field( 'l2d_ai_stickers' ); ?>
				<?php self::hidden_back_fields(); ?>
				<label for="l2d-ai-sticker-max">表情库容量上限</label>
				<input type="number" id="l2d-ai-sticker-max" name="sticker_library_max" value="<?php echo esc_attr( (string) $library_max ); ?>" min="0" max="100000" step="1" class="small-text" />
				张
				<p class="description">自动收藏到这个数量后停止；手动上传不受限制。0 表示不设上限。</p>
				<?php submit_button( '保存设置', 'secondary', '', false ); ?>
			</form>
		</div>
		<?php
	}
}
