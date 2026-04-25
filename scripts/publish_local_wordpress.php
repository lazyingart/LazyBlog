<?php
/**
 * Import LazyBlog Markdown posts into the local Docker WordPress site.
 *
 * Run through WP-CLI:
 *   wp eval-file publish_local_wordpress.php
 */

if (!defined('ABSPATH')) {
    fwrite(STDERR, "This script must run inside WordPress through WP-CLI.\n");
    exit(1);
}

function lazyblog_log(string $message): void
{
    if (class_exists('WP_CLI')) {
        WP_CLI::log($message);
        return;
    }

    echo $message . PHP_EOL;
}

function lazyblog_unquote(string $value): string
{
    $value = trim($value);
    if ($value === '') {
        return '';
    }

    $first = $value[0];
    $last = $value[strlen($value) - 1];
    if (($first === "'" && $last === "'") || ($first === '"' && $last === '"')) {
        $value = substr($value, 1, -1);
    }

    return str_replace("''", "'", $value);
}

function lazyblog_parse_markdown_file(string $path): array
{
    $text = file_get_contents($path);
    if ($text === false) {
        throw new RuntimeException("Unable to read Markdown file: {$path}");
    }

    if (!str_starts_with($text, "---\n")) {
        return [[], $text];
    }

    $parts = explode("\n---\n", $text, 2);
    if (count($parts) !== 2) {
        return [[], $text];
    }

    $front_matter = [];
    $current_list_key = null;
    $lines = explode("\n", $parts[0]);
    array_shift($lines);

    foreach ($lines as $line) {
        if (preg_match('/^\s+-\s*(.+?)\s*$/u', $line, $matches) && $current_list_key !== null) {
            $front_matter[$current_list_key][] = lazyblog_unquote($matches[1]);
            continue;
        }

        if (preg_match('/^([A-Za-z0-9_-]+):\s*(.*?)\s*$/u', $line, $matches)) {
            $key = $matches[1];
            $value = $matches[2];
            if ($value === '') {
                $front_matter[$key] = [];
                $current_list_key = $key;
            } else {
                $front_matter[$key] = lazyblog_unquote($value);
                $current_list_key = null;
            }
            continue;
        }

        $current_list_key = null;
    }

    return [$front_matter, ltrim($parts[1], "\n")];
}

function lazyblog_inline_markdown_to_html(string $text): string
{
    $tokens = [];
    $tokenized = preg_replace_callback(
        '/\\\\\((.*?)\\\\\)/u',
        static function (array $matches) use (&$tokens): string {
            $token = '%%LAZYBLOG_INLINE_' . count($tokens) . '%%';
            $tokens[$token] = '[math]' . esc_html($matches[1]) . '[/math]';
            return $token;
        },
        $text
    );
    $tokenized = preg_replace_callback(
        '/!\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/u',
        static function (array $matches) use (&$tokens): string {
            $token = '%%LAZYBLOG_INLINE_' . count($tokens) . '%%';
            $tokens[$token] = '<img alt="' . esc_attr($matches[1]) . '" src="' . esc_url($matches[2]) . '" />';
            return $token;
        },
        $tokenized ?? $text
    );
    $tokenized = preg_replace_callback(
        '/(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/u',
        static function (array $matches) use (&$tokens): string {
            $token = '%%LAZYBLOG_INLINE_' . count($tokens) . '%%';
            $tokens[$token] = '<a href="' . esc_url($matches[2]) . '">' . esc_html($matches[1]) . '</a>';
            return $token;
        },
        $tokenized ?? $text
    );

    $escaped = esc_html($tokenized ?? $text);
    $escaped = preg_replace('/`([^`]+)`/u', '<code>$1</code>', $escaped);
    $escaped = preg_replace('/\*\*([^*]+)\*\*/u', '<strong>$1</strong>', $escaped);
    $escaped = preg_replace('/\*([^*]+)\*/u', '<em>$1</em>', $escaped);

    return strtr($escaped ?? esc_html($text), $tokens);
}

function lazyblog_markdown_body_to_html(string $body): string
{
    $lines = preg_split('/\R/u', $body) ?: [];
    $output = [];
    $paragraph = [];
    $list_items = [];
    $ordered_items = [];
    $code_lines = [];
    $math_lines = [];
    $in_code = false;
    $math_end = null;

    $flush_paragraph = static function () use (&$output, &$paragraph): void {
        if ($paragraph === []) {
            return;
        }
        $converted = array_map('lazyblog_inline_markdown_to_html', $paragraph);
        $output[] = '<p>' . implode("<br />\n", $converted) . '</p>';
        $paragraph = [];
    };

    $flush_list = static function () use (&$output, &$list_items): void {
        if ($list_items === []) {
            return;
        }
        $output[] = "<ul>\n" . implode("\n", array_map(static fn($item) => '<li>' . $item . '</li>', $list_items)) . "\n</ul>";
        $list_items = [];
    };

    $flush_ordered = static function () use (&$output, &$ordered_items): void {
        if ($ordered_items === []) {
            return;
        }
        $output[] = "<ol>\n" . implode("\n", array_map(static fn($item) => '<li>' . $item . '</li>', $ordered_items)) . "\n</ol>";
        $ordered_items = [];
    };

    foreach ($lines as $line) {
        if ($math_end !== null) {
            if (trim($line) === $math_end) {
                $output[] = "[latex]\n" . esc_html(implode("\n", $math_lines)) . "\n[/latex]";
                $math_lines = [];
                $math_end = null;
            } else {
                $math_lines[] = $line;
            }
            continue;
        }

        if (str_starts_with($line, '```')) {
            if ($in_code) {
                $output[] = '<pre><code>' . esc_html(implode("\n", $code_lines)) . '</code></pre>';
                $code_lines = [];
                $in_code = false;
            } else {
                $flush_paragraph();
                $flush_list();
                $flush_ordered();
                $in_code = true;
            }
            continue;
        }

        if ($in_code) {
            $code_lines[] = $line;
            continue;
        }

        $trimmed_line = trim($line);
        if ($trimmed_line === '\\[' || $trimmed_line === '$$') {
            $flush_paragraph();
            $flush_list();
            $flush_ordered();
            $math_end = $trimmed_line === '\\[' ? '\\]' : '$$';
            $math_lines = [];
            continue;
        }

        if (trim($line) === '') {
            $flush_paragraph();
            $flush_list();
            $flush_ordered();
            continue;
        }

        if (preg_match('/^(#{1,6})\s+(.+?)\s*$/u', $line, $matches)) {
            $flush_paragraph();
            $flush_list();
            $flush_ordered();
            $level = strlen($matches[1]);
            $output[] = '<h' . $level . '>' . lazyblog_inline_markdown_to_html($matches[2]) . '</h' . $level . '>';
            continue;
        }

        if (preg_match('/^\s*[-*]\s+(.+?)\s*$/u', $line, $matches)) {
            $flush_paragraph();
            $flush_ordered();
            $list_items[] = lazyblog_inline_markdown_to_html($matches[1]);
            continue;
        }

        if (preg_match('/^\s*\d+\.\s+(.+?)\s*$/u', $line, $matches)) {
            $flush_paragraph();
            $flush_list();
            $ordered_items[] = lazyblog_inline_markdown_to_html($matches[1]);
            continue;
        }

        if (str_starts_with($line, '> ')) {
            $flush_paragraph();
            $flush_list();
            $flush_ordered();
            $output[] = '<blockquote><p>' . lazyblog_inline_markdown_to_html(trim(substr($line, 2))) . '</p></blockquote>';
            continue;
        }

        $paragraph[] = $line;
    }

    if ($in_code) {
        $output[] = '<pre><code>' . esc_html(implode("\n", $code_lines)) . '</code></pre>';
    }

    $flush_paragraph();
    $flush_list();
    $flush_ordered();

    return implode("\n\n", $output);
}

function lazyblog_front_value(array $front_matter, string $key, string $fallback = ''): string
{
    $value = $front_matter[$key] ?? $fallback;
    return is_array($value) ? $fallback : (string) $value;
}

function lazyblog_normalize_datetime(string $value): string
{
    $value = trim($value);
    if ($value === '') {
        return current_time('mysql');
    }

    $value = str_replace('T', ' ', $value);
    $value = preg_replace('/(?:Z|[+-]\d{2}:?\d{2})$/', '', $value) ?? $value;

    return trim($value);
}

function lazyblog_prepare_media_urls(string $post_dir, int $post_id): array
{
    $image_dir = rtrim($post_dir, '/') . '/images';
    if (!is_dir($image_dir)) {
        return [];
    }

    $uploads = wp_upload_dir();
    if (!empty($uploads['error'])) {
        throw new RuntimeException((string) $uploads['error']);
    }

    $target_dir = trailingslashit($uploads['basedir']) . 'lazyblog/' . $post_id;
    if (!wp_mkdir_p($target_dir)) {
        throw new RuntimeException("Unable to create upload directory: {$target_dir}");
    }

    $base_url = trailingslashit($uploads['baseurl']) . 'lazyblog/' . $post_id . '/';
    $urls = [];
    foreach (glob($image_dir . '/*') ?: [] as $source_path) {
        if (!is_file($source_path)) {
            continue;
        }

        $filename = basename($source_path);
        $target_path = $target_dir . '/' . $filename;
        if (!copy($source_path, $target_path)) {
            throw new RuntimeException("Unable to copy media file: {$source_path}");
        }

        $urls[$filename] = $base_url . rawurlencode($filename);
    }

    return $urls;
}

function lazyblog_rewrite_local_image_paths(string $body, array $media_urls): string
{
    if ($media_urls === []) {
        return $body;
    }

    return preg_replace_callback(
        '/(!\[[^\]]*\]\()\s*(?:\.\.\/)?images\/([^)\s]+)((?:\s+"[^"]*")?\))/u',
        static function (array $matches) use ($media_urls): string {
            $filename = basename(rawurldecode($matches[2]));
            if (!isset($media_urls[$filename])) {
                return $matches[0];
            }

            return $matches[1] . $media_urls[$filename] . $matches[3];
        },
        $body
    ) ?? $body;
}

function lazyblog_decode_terms(array $terms): array
{
    $decoded = [];
    foreach ($terms as $term) {
        $name = html_entity_decode((string) $term, ENT_QUOTES | ENT_HTML5, 'UTF-8');
        $name = trim($name);
        if ($name !== '') {
            $decoded[] = $name;
        }
    }

    return array_values(array_unique($decoded));
}

function lazyblog_term_id(string $taxonomy, string $name): int
{
    $existing = term_exists($name, $taxonomy);
    if (is_array($existing) && isset($existing['term_id'])) {
        return (int) $existing['term_id'];
    }
    if (is_int($existing)) {
        return $existing;
    }

    $created = wp_insert_term($name, $taxonomy);
    if (is_wp_error($created)) {
        throw new RuntimeException($created->get_error_message());
    }

    return (int) $created['term_id'];
}

function lazyblog_clear_local_content(): array
{
    global $wpdb;

    $deleted = [
        'posts' => 0,
        'attachments' => 0,
        'revisions' => 0,
        'categories' => 0,
        'tags' => 0,
    ];

    $ids = $wpdb->get_col(
        "SELECT ID FROM {$wpdb->posts} WHERE post_type IN ('post', 'attachment', 'revision') ORDER BY ID ASC"
    );

    foreach ($ids as $id) {
        $post = get_post((int) $id);
        if (!$post instanceof WP_Post) {
            continue;
        }
        wp_delete_post((int) $id, true);
        if ($post->post_type === 'attachment') {
            $deleted['attachments']++;
        } elseif ($post->post_type === 'revision') {
            $deleted['revisions']++;
        } else {
            $deleted['posts']++;
        }
    }

    $default_category = (int) get_option('default_category');
    $categories = get_terms(['taxonomy' => 'category', 'hide_empty' => false]);
    if (!is_wp_error($categories)) {
        foreach ($categories as $category) {
            if ((int) $category->term_id === $default_category) {
                continue;
            }
            wp_delete_term((int) $category->term_id, 'category');
            $deleted['categories']++;
        }
    }

    $tags = get_terms(['taxonomy' => 'post_tag', 'hide_empty' => false]);
    if (!is_wp_error($tags)) {
        foreach ($tags as $tag) {
            wp_delete_term((int) $tag->term_id, 'post_tag');
            $deleted['tags']++;
        }
    }

    return $deleted;
}

function lazyblog_translation_payload(string $path, array $media_urls): array
{
    [$front_matter, $body] = lazyblog_parse_markdown_file($path);
    $body = lazyblog_rewrite_local_image_paths($body, $media_urls);
    $language = lazyblog_front_value($front_matter, 'language', pathinfo($path, PATHINFO_FILENAME));

    return [
        $language,
        [
            'title' => lazyblog_front_value($front_matter, 'title', pathinfo($path, PATHINFO_FILENAME)),
            'content' => lazyblog_markdown_body_to_html($body),
            'excerpt' => lazyblog_front_value($front_matter, 'excerpt'),
            'updated_at' => current_time('mysql', true),
        ],
    ];
}

function lazyblog_import_post(string $post_dir, string $status_override): array
{
    $manifest_path = $post_dir . '/lazyblog.json';
    $post_path = $post_dir . '/post.md';
    if (!is_file($manifest_path) || !is_file($post_path)) {
        throw new RuntimeException("Missing manifest or post.md in {$post_dir}");
    }

    $manifest = json_decode((string) file_get_contents($manifest_path), true);
    if (!is_array($manifest)) {
        throw new RuntimeException("Invalid manifest: {$manifest_path}");
    }

    [$front_matter, $body] = lazyblog_parse_markdown_file($post_path);
    $post_id = (int) ($manifest['post_id'] ?? basename($post_dir));
    $media_urls = lazyblog_prepare_media_urls($post_dir, $post_id);
    $body = lazyblog_rewrite_local_image_paths($body, $media_urls);
    $source_language = (string) ($manifest['source_language'] ?? lazyblog_front_value($front_matter, 'source_language', 'en'));
    $categories = lazyblog_decode_terms(is_array($manifest['categories'] ?? null) ? $manifest['categories'] : ($front_matter['categories'] ?? []));
    $tags = lazyblog_decode_terms(is_array($manifest['tags'] ?? null) ? $manifest['tags'] : ($front_matter['tags'] ?? []));
    $category_ids = [];

    foreach ($categories as $category_name) {
        $category_ids[] = lazyblog_term_id('category', $category_name);
    }
    if ($category_ids === []) {
        $category_ids[] = (int) get_option('default_category');
    }

    $title = lazyblog_front_value($front_matter, 'title', 'Post ' . $post_id);
    $slug = rawurldecode(lazyblog_front_value($front_matter, 'slug', sanitize_title($title)));
    $status = $status_override !== '' ? $status_override : lazyblog_front_value($front_matter, 'status', 'publish');

    $post_data = [
        'import_id' => $post_id,
        'post_author' => 1,
        'post_type' => 'post',
        'post_status' => $status,
        'post_title' => $title,
        'post_name' => $slug,
        'post_content' => lazyblog_markdown_body_to_html($body),
        'post_excerpt' => lazyblog_front_value($front_matter, 'excerpt'),
        'post_date' => lazyblog_normalize_datetime(lazyblog_front_value($front_matter, 'date')),
        'post_modified' => lazyblog_normalize_datetime(lazyblog_front_value($front_matter, 'modified')),
    ];

    $new_id = wp_insert_post(wp_slash($post_data), true);
    if (is_wp_error($new_id)) {
        throw new RuntimeException("Post {$post_id}: " . $new_id->get_error_message());
    }
    if ((int) $new_id !== $post_id) {
        global $wpdb;
        if (get_post($post_id) instanceof WP_Post) {
            throw new RuntimeException("Post {$post_id}: target ID already exists, cannot remap imported post {$new_id}");
        }

        $updated = $wpdb->update($wpdb->posts, ['ID' => $post_id, 'guid' => home_url('/?p=' . $post_id)], ['ID' => (int) $new_id], ['%d', '%s'], ['%d']);
        if ($updated === false) {
            throw new RuntimeException("Post {$post_id}: failed to remap imported post {$new_id}");
        }
        clean_post_cache((int) $new_id);
        clean_post_cache($post_id);
    }

    wp_set_post_categories($post_id, $category_ids, false);
    wp_set_post_tags($post_id, $tags, false);
    update_post_meta($post_id, '_lazyblog_source_language', wp_slash($source_language));
    update_post_meta($post_id, '_lazyblog_original_id', (string) $post_id);

    $translations = [];
    $translation_dir = $post_dir . '/translations';
    foreach (glob($translation_dir . '/*.md') ?: [] as $translation_path) {
        [$language, $payload] = lazyblog_translation_payload($translation_path, $media_urls);
        $translations[$language] = $payload;
    }
    update_post_meta($post_id, '_lazyblog_translations', wp_slash($translations));

    return [
        'post_id' => $post_id,
        'title' => $title,
        'source_language' => $source_language,
        'categories' => $categories,
        'translations' => array_keys($translations),
        'media_files' => count($media_urls),
    ];
}

if (!function_exists('is_plugin_active')) {
    require_once ABSPATH . 'wp-admin/includes/plugin.php';
}

$script_args = isset($args) && is_array($args) ? $args : [];
$content_root = getenv('LAZYBLOG_LOCAL_CONTENT_DIR') ?: ($script_args[0] ?? '/lazyblog/content/posts');
$status_override = getenv('LAZYBLOG_LOCAL_STATUS') ?: ($script_args[1] ?? 'publish');

if (!is_dir($content_root)) {
    throw new RuntimeException("Content directory not found: {$content_root}");
}

$plugin = 'lazyblog-translations/lazyblog-translations.php';
if (!is_plugin_active($plugin)) {
    $result = activate_plugin($plugin);
    if (is_wp_error($result)) {
        throw new RuntimeException($result->get_error_message());
    }
}

lazyblog_log('Clearing local Docker posts, attachments, tags, and non-default categories.');
$deleted = lazyblog_clear_local_content();

$post_dirs = glob(rtrim($content_root, '/') . '/*', GLOB_ONLYDIR) ?: [];
usort($post_dirs, static fn($a, $b) => (int) basename($a) <=> (int) basename($b));

$summary = [
    'content_root' => $content_root,
    'status' => $status_override,
    'deleted' => $deleted,
    'posts' => 0,
    'translations' => 0,
    'media_files' => 0,
    'categories' => [],
    'imported_ids' => [],
];

foreach ($post_dirs as $index => $post_dir) {
    $imported = lazyblog_import_post($post_dir, $status_override);
    $summary['posts']++;
    $summary['translations'] += count($imported['translations']);
    $summary['media_files'] += $imported['media_files'];
    $summary['imported_ids'][] = $imported['post_id'];
    foreach ($imported['categories'] as $category) {
        $summary['categories'][$category] = true;
    }

    if ($summary['posts'] === 1 || $summary['posts'] % 25 === 0 || $summary['posts'] === count($post_dirs)) {
        lazyblog_log(sprintf('[%d/%d] imported post %d: %s', $summary['posts'], count($post_dirs), $imported['post_id'], $imported['title']));
    }
}

$summary['categories'] = array_values(array_keys($summary['categories']));
sort($summary['categories'], SORT_NATURAL | SORT_FLAG_CASE);

flush_rewrite_rules(false);

if (class_exists('WP_CLI')) {
    WP_CLI::line(wp_json_encode($summary, JSON_PRETTY_PRINT | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES));
}
