<?php
/**
 * Plugin Name: LazyBlog Translations
 * Plugin URI: https://lazying.art
 * Description: Stores post translations managed by LazyBlog Markdown workflows, renders a lightweight language switcher, and handles local math rendering.
 * Version: 0.4.8
 * Requires at least: 6.5
 * Requires PHP: 7.4
 * Author: LazyingArt LLC
 * Author URI: https://lazying.art
 * License: GPL-2.0-or-later
 * License URI: https://www.gnu.org/licenses/gpl-2.0.html
 * Text Domain: lazyblog-translations
 * Domain Path: /languages
 * Update URI: https://github.com/LazyingArt/lazyblog-translations
 */

if (!defined('ABSPATH')) {
    exit;
}

final class LazyBlog_Translations
{
    private const PLUGIN_VERSION = '0.4.8';
    private const PLUGIN_REPO_URL = 'https://github.com/lazyingart/lazyblog-translations';
    private const LAZYBLOG_REPO_URL = 'https://github.com/lazyingart/LazyBlog';
    private const LAZYBLOG_INSTALL_SCRIPT_URL = 'https://github.com/lazyingart/lazyblog-translations/blob/main/tools/install_lazyblog_translation_api.sh';
    private const LAZYBLOG_INSTALL_SCRIPT_RAW_URL = 'https://raw.githubusercontent.com/lazyingart/lazyblog-translations/main/tools/install_lazyblog_translation_api.sh';
    private const PROVIDER_LAZYBLOG = 'lazyblog';
    private const PROVIDER_OPENAI = 'openai';
    private const PROVIDER_DEEPSEEK = 'deepseek';
    private const DEFAULT_PROVIDER = self::PROVIDER_LAZYBLOG;
    private const META_SOURCE_LANGUAGE = '_lazyblog_source_language';
    private const META_TRANSLATIONS = '_lazyblog_translations';
    private const META_TRANSLATION_JOBS = '_lazyblog_translation_jobs';
    private const OPTION_LANGUAGES = 'lazyblog_translation_languages';
    private const OPTION_PROVIDER = 'lazyblog_translation_provider';
    private const OPTION_API_ENDPOINT = 'lazyblog_translation_api_endpoint';
    private const OPTION_API_TOKEN = 'lazyblog_translation_api_token';
    private const OPTION_API_MOCK = 'lazyblog_translation_api_mock';
    private const OPTION_API_MODEL = 'lazyblog_translation_api_model';
    private const OPTION_API_REASONING = 'lazyblog_translation_api_reasoning';
    private const OPTION_OPENAI_ENDPOINT = 'lazyblog_translation_openai_endpoint';
    private const OPTION_OPENAI_API_KEY = 'lazyblog_translation_openai_api_key';
    private const OPTION_OPENAI_MODEL = 'lazyblog_translation_openai_model';
    private const OPTION_DEEPSEEK_ENDPOINT = 'lazyblog_translation_deepseek_endpoint';
    private const OPTION_DEEPSEEK_API_KEY = 'lazyblog_translation_deepseek_api_key';
    private const OPTION_DEEPSEEK_MODEL = 'lazyblog_translation_deepseek_model';
    private const DEFAULT_API_MODEL = 'gpt-5.4';
    private const DEFAULT_API_REASONING = 'low';
    private const DEFAULT_OPENAI_ENDPOINT = 'https://api.openai.com/v1/chat/completions';
    private const DEFAULT_OPENAI_MODEL = 'gpt-4o';
    private const DEFAULT_DEEPSEEK_ENDPOINT = 'https://api.deepseek.com/chat/completions';
    private const DEFAULT_DEEPSEEK_MODEL = 'deepseek-v4-flash';
    private const TRANSLATION_SIGNATURE_TTL = 3600;

    private static ?self $instance = null;

    private ?string $current_language = null;
    private bool $content_filter_active = false;
    private int $language_prefixed_post_id = 0;

    public static function instance(): self
    {
        if (self::$instance === null) {
            self::$instance = new self();
        }

        return self::$instance;
    }

    private function __construct()
    {
        add_action('plugins_loaded', [$this, 'load_textdomain']);
        add_action('plugins_loaded', [$this, 'capture_language_prefix'], 0);
        add_action('init', [$this, 'register_meta']);
        add_action('init', [$this, 'register_rewrite_rules']);
        add_action('init', [$this, 'disable_quicklatex_parser'], 100);
        add_action('add_meta_boxes', [$this, 'add_source_language_metabox']);
        add_action('save_post_post', [$this, 'save_source_language_metabox'], 10, 2);
        add_action('admin_menu', [$this, 'add_settings_page']);
        add_action('admin_init', [$this, 'register_settings']);
        add_action('rest_api_init', [$this, 'register_rest_routes']);
        add_action('wp_enqueue_scripts', [$this, 'enqueue_styles'], 100);

        add_shortcode('math', [$this, 'render_inline_math_shortcode']);
        add_shortcode('latex', [$this, 'render_display_math_shortcode']);

        add_filter('query_vars', [$this, 'register_query_vars']);
        add_filter('request', [$this, 'filter_request_query_vars'], 0);
        add_filter('the_title', [$this, 'filter_title'], 20, 2);
        add_filter('the_content', [$this, 'filter_listing_content'], 5);
        add_filter('the_content', [$this, 'filter_content'], 20);
        add_filter('language_attributes', [$this, 'filter_language_attributes'], 20);
        add_filter('redirect_canonical', [$this, 'filter_redirect_canonical'], 20, 2);
    }

    public function load_textdomain(): void
    {
        load_plugin_textdomain(
            'lazyblog-translations',
            false,
            dirname(plugin_basename(__FILE__)) . '/languages'
        );
    }

    public static function activate(): void
    {
        self::instance()->register_rewrite_rules();
        flush_rewrite_rules();
    }

    public static function deactivate(): void
    {
        flush_rewrite_rules();
    }

    public function capture_language_prefix(): void
    {
        if (is_admin()) {
            return;
        }

        $request_uri = $_SERVER['REQUEST_URI'] ?? '';
        $path = parse_url($request_uri, PHP_URL_PATH);
        if (!is_string($path) || $path === '') {
            return;
        }

        $trimmed_path = trim($path, '/');
        if ($trimmed_path === '') {
            return;
        }

        if (preg_match('#^(wp-admin|wp-json|wp-content|wp-includes)(/|$)#', $trimmed_path)) {
            return;
        }

        $segments = explode('/', $trimmed_path);
        $language = $this->normalize_language($segments[0] ?? '');
        if ($language === null) {
            return;
        }

        $this->current_language = $language;
        $this->capture_language_permalink_parts($segments);
        $remaining = array_slice($segments, 1);
        $new_path = '/' . implode('/', $remaining);
        if ($new_path === '/') {
            $new_path = '/';
        }

        $query = parse_url($request_uri, PHP_URL_QUERY);
        $_SERVER['REQUEST_URI'] = $new_path . ($query ? '?' . $query : '');
        $_SERVER['REDIRECT_URL'] = $new_path;
        $_SERVER['PATH_INFO'] = $new_path;
    }

    public function disable_quicklatex_parser(): void
    {
        if (is_admin()) {
            return;
        }

        foreach (['the_content', 'comment_text', 'the_title', 'the_excerpt', 'thesis_comment_text'] as $filter) {
            remove_filter($filter, 'quicklatex_parser', 7);
        }
        remove_action('wp_print_scripts', 'quicklatex_frontend_scripts');
    }

    public function register_meta(): void
    {
        register_post_meta('post', self::META_SOURCE_LANGUAGE, [
            'type' => 'string',
            'single' => true,
            'show_in_rest' => false,
            'auth_callback' => static fn() => current_user_can('edit_posts'),
        ]);

        register_post_meta('post', self::META_TRANSLATIONS, [
            'type' => 'object',
            'single' => true,
            'show_in_rest' => false,
            'auth_callback' => static fn() => current_user_can('edit_posts'),
        ]);

        register_post_meta('post', self::META_TRANSLATION_JOBS, [
            'type' => 'object',
            'single' => true,
            'show_in_rest' => false,
            'auth_callback' => static fn() => current_user_can('edit_posts'),
        ]);
    }

    public function register_rewrite_rules(): void
    {
        $slugs = [];
        foreach ($this->languages() as $code => $settings) {
            $slugs[] = preg_quote((string) ($settings['slug'] ?? $code), '#');
        }

        $language_pattern = implode('|', array_filter($slugs));
        if ($language_pattern === '') {
            return;
        }

        add_rewrite_rule(
            '^(' . $language_pattern . ')/html/(.+?)/([0-9]+)/([^/]+)\.html/?$',
            'index.php?category_name=$matches[2]&p=$matches[3]&name=$matches[4]&lazyblog_lang=$matches[1]',
            'top'
        );
        add_rewrite_rule(
            '^(' . $language_pattern . ')/?$',
            'index.php?lazyblog_lang=$matches[1]',
            'top'
        );
    }

    public function add_source_language_metabox(): void
    {
        add_meta_box(
            'lazyblog-source-language',
            __('LazyBlog Original Language', 'lazyblog-translations'),
            [$this, 'render_source_language_metabox'],
            'post',
            'side',
            'default'
        );
    }

    public function render_source_language_metabox(WP_Post $post): void
    {
        $source_language = $this->get_source_language($post->ID);
        $languages = $this->languages();

        wp_nonce_field('lazyblog_save_source_language', 'lazyblog_source_language_nonce');
        echo '<p><label for="lazyblog_source_language">' . esc_html__('Original writing language', 'lazyblog-translations') . '</label></p>';
        echo '<select id="lazyblog_source_language" name="lazyblog_source_language" style="width:100%">';
        foreach ($languages as $language => $settings) {
            printf(
                '<option value="%s"%s>%s</option>',
                esc_attr((string) $language),
                selected($source_language, (string) $language, false),
                esc_html((string) ($settings['label'] ?? strtoupper((string) $language)))
            );
        }
        echo '</select>';
        echo '<p class="description">' . esc_html__('Controls the Original switcher option and the untranslated post language.', 'lazyblog-translations') . '</p>';
    }

    public function save_source_language_metabox(int $post_id, WP_Post $post): void
    {
        if (defined('DOING_AUTOSAVE') && DOING_AUTOSAVE) {
            return;
        }

        if (wp_is_post_revision($post_id) || $post->post_type !== 'post') {
            return;
        }

        $nonce = $_POST['lazyblog_source_language_nonce'] ?? '';
        if (!is_string($nonce) || !wp_verify_nonce($nonce, 'lazyblog_save_source_language')) {
            return;
        }

        if (!current_user_can('edit_post', $post_id)) {
            return;
        }

        $source_language = isset($_POST['lazyblog_source_language'])
            ? $this->normalize_language((string) wp_unslash($_POST['lazyblog_source_language']))
            : null;

        if ($source_language !== null) {
            update_post_meta($post_id, self::META_SOURCE_LANGUAGE, $source_language);
        }
    }

    public function add_settings_page(): void
    {
        add_options_page(
            __('LazyBlog Translations', 'lazyblog-translations'),
            __('LazyBlog Translations', 'lazyblog-translations'),
            'manage_options',
            'lazyblog-translations',
            [$this, 'render_settings_page']
        );
    }

    public function register_settings(): void
    {
        register_setting('lazyblog_translations', self::OPTION_PROVIDER, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_translation_provider'],
            'default' => self::DEFAULT_PROVIDER,
        ]);
        register_setting('lazyblog_translations', self::OPTION_API_ENDPOINT, [
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => '',
        ]);
        register_setting('lazyblog_translations', self::OPTION_API_TOKEN, [
            'type' => 'string',
            'sanitize_callback' => 'sanitize_text_field',
            'default' => '',
        ]);
        register_setting('lazyblog_translations', self::OPTION_API_MOCK, [
            'type' => 'boolean',
            'sanitize_callback' => static fn($value): bool => (bool) $value,
            'default' => false,
        ]);
        register_setting('lazyblog_translations', self::OPTION_API_MODEL, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_translation_model'],
            'default' => self::DEFAULT_API_MODEL,
        ]);
        register_setting('lazyblog_translations', self::OPTION_API_REASONING, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_translation_reasoning'],
            'default' => self::DEFAULT_API_REASONING,
        ]);
        register_setting('lazyblog_translations', self::OPTION_OPENAI_ENDPOINT, [
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => self::DEFAULT_OPENAI_ENDPOINT,
        ]);
        register_setting('lazyblog_translations', self::OPTION_OPENAI_API_KEY, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_api_secret'],
            'default' => '',
        ]);
        register_setting('lazyblog_translations', self::OPTION_OPENAI_MODEL, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_model_name'],
            'default' => self::DEFAULT_OPENAI_MODEL,
        ]);
        register_setting('lazyblog_translations', self::OPTION_DEEPSEEK_ENDPOINT, [
            'type' => 'string',
            'sanitize_callback' => 'esc_url_raw',
            'default' => self::DEFAULT_DEEPSEEK_ENDPOINT,
        ]);
        register_setting('lazyblog_translations', self::OPTION_DEEPSEEK_API_KEY, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_api_secret'],
            'default' => '',
        ]);
        register_setting('lazyblog_translations', self::OPTION_DEEPSEEK_MODEL, [
            'type' => 'string',
            'sanitize_callback' => [$this, 'sanitize_model_name'],
            'default' => self::DEFAULT_DEEPSEEK_MODEL,
        ]);
    }

    public function sanitize_translation_provider($value): string
    {
        $provider = strtolower(sanitize_key((string) $value));
        return in_array($provider, [self::PROVIDER_LAZYBLOG, self::PROVIDER_OPENAI, self::PROVIDER_DEEPSEEK], true)
            ? $provider
            : self::DEFAULT_PROVIDER;
    }

    public function sanitize_translation_model($value): string
    {
        $model = sanitize_text_field((string) $value);
        return $model !== '' ? $model : self::DEFAULT_API_MODEL;
    }

    public function sanitize_model_name($value): string
    {
        return sanitize_text_field((string) $value);
    }

    public function sanitize_api_secret($value): string
    {
        return sanitize_text_field((string) $value);
    }

    public function sanitize_translation_reasoning($value): string
    {
        $reasoning = strtolower(sanitize_text_field((string) $value));
        return in_array($reasoning, ['low', 'medium', 'high', 'xhigh'], true) ? $reasoning : self::DEFAULT_API_REASONING;
    }

    public function render_settings_page(): void
    {
        if (!current_user_can('manage_options')) {
            return;
        }

        echo '<div class="wrap">';
        echo '<h1>' . esc_html__('LazyBlog Translations', 'lazyblog-translations') . '</h1>';
        $this->render_settings_intro();
        echo '<form method="post" action="options.php">';
        settings_fields('lazyblog_translations');
        echo '<table class="form-table" role="presentation">';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_PROVIDER) . '">' . esc_html__('Translation provider', 'lazyblog-translations') . '</label></th><td>';
        echo '<select id="' . esc_attr(self::OPTION_PROVIDER) . '" name="' . esc_attr(self::OPTION_PROVIDER) . '">';
        foreach ($this->translation_providers() as $provider => $label) {
            printf(
                '<option value="%s"%s>%s</option>',
                esc_attr($provider),
                selected($this->translation_provider(), $provider, false),
                esc_html($label)
            );
        }
        echo '</select>';
        echo '<p class="description">' . esc_html__('Codex uses the local LazyBlog API. OpenAI and DeepSeek call their hosted APIs directly and do not need the local service.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th colspan="2"><h2>' . esc_html__('Codex / LazyBlog local API', 'lazyblog-translations') . '</h2></th></tr>';
        echo '<tr><th scope="row">' . esc_html__('Codex setup script', 'lazyblog-translations') . '</th><td>';
        printf(
            '<p><a class="button button-secondary" href="%1$s" target="_blank" rel="noopener">%2$s</a></p>',
            esc_url(self::LAZYBLOG_INSTALL_SCRIPT_URL),
            esc_html__('Open setup script on GitHub', 'lazyblog-translations')
        );
        printf(
            '<p class="description">%s</p><pre class="lazyblog-admin-code"><code>git clone %s LazyBlog
cd LazyBlog
scripts/install_lazyblog_translation_api.sh</code></pre>',
            esc_html__('Use this only for the Codex provider. OpenAI and DeepSeek do not need the local API service.', 'lazyblog-translations'),
            esc_html(self::LAZYBLOG_REPO_URL)
        );
        echo '</td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_API_ENDPOINT) . '">' . esc_html__('LazyBlog API endpoint', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="url" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="http://host.docker.internal:8765/api/translate/jobs">',
            esc_attr(self::OPTION_API_ENDPOINT),
            esc_attr($this->api_endpoint())
        );
        echo '<p class="description">' . esc_html__('POST endpoint for LazyBlog Studio translation jobs. If you choose Codex, install the local API with scripts/install_lazyblog_translation_api.sh in the LazyBlog repo.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_API_TOKEN) . '">' . esc_html__('LazyBlog bearer token', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="password" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" autocomplete="new-password">',
            esc_attr(self::OPTION_API_TOKEN),
            esc_attr($this->api_token())
        );
        echo '<p class="description">' . esc_html__('Stored server-side only. The browser receives only scoped signed post/language requests.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_API_MODEL) . '">' . esc_html__('Translation model', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="text" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="%3$s">',
            esc_attr(self::OPTION_API_MODEL),
            esc_attr($this->api_model()),
            esc_attr(self::DEFAULT_API_MODEL)
        );
        echo '<p class="description">' . esc_html__('Model sent to LazyBlog Studio for on-demand translations. Default: gpt-5.4.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_API_REASONING) . '">' . esc_html__('Translation reasoning', 'lazyblog-translations') . '</label></th><td>';
        echo '<select id="' . esc_attr(self::OPTION_API_REASONING) . '" name="' . esc_attr(self::OPTION_API_REASONING) . '">';
        foreach (['low', 'medium', 'high', 'xhigh'] as $reasoning) {
            printf(
                '<option value="%s"%s>%s</option>',
                esc_attr($reasoning),
                selected($this->api_reasoning(), $reasoning, false),
                esc_html($reasoning)
            );
        }
        echo '</select>';
        echo '<p class="description">' . esc_html__('Reasoning effort sent to LazyBlog Studio for on-demand translations. Default: low.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row">' . esc_html__('Mock API calls', 'lazyblog-translations') . '</th><td><label>';
        printf(
            '<input type="checkbox" name="%s" value="1"%s> %s',
            esc_attr(self::OPTION_API_MOCK),
            checked($this->api_mock_enabled(), true, false),
            esc_html__('Send mock=true to LazyBlog Studio for local smoke tests.', 'lazyblog-translations')
        );
        echo '</label></td></tr>';
        echo '<tr><th colspan="2"><h2>' . esc_html__('OpenAI direct API', 'lazyblog-translations') . '</h2></th></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_OPENAI_ENDPOINT) . '">' . esc_html__('OpenAI chat endpoint', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="url" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="%3$s">',
            esc_attr(self::OPTION_OPENAI_ENDPOINT),
            esc_attr($this->openai_endpoint()),
            esc_attr(self::DEFAULT_OPENAI_ENDPOINT)
        );
        echo '<p class="description">' . esc_html__('Default is the OpenAI Chat Completions endpoint.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_OPENAI_API_KEY) . '">' . esc_html__('OpenAI API key', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="password" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" autocomplete="new-password">',
            esc_attr(self::OPTION_OPENAI_API_KEY),
            esc_attr($this->openai_api_key())
        );
        echo '<p class="description">' . esc_html__('Stored server-side. The browser never receives this key.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_OPENAI_MODEL) . '">' . esc_html__('OpenAI model', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="text" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="%3$s">',
            esc_attr(self::OPTION_OPENAI_MODEL),
            esc_attr($this->openai_model()),
            esc_attr(self::DEFAULT_OPENAI_MODEL)
        );
        echo '<p class="description">' . esc_html__('Default: gpt-4o. Direct mode is best for short and medium posts; use Codex/LazyBlog for long polishing workflows.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th colspan="2"><h2>' . esc_html__('DeepSeek direct API', 'lazyblog-translations') . '</h2></th></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_DEEPSEEK_ENDPOINT) . '">' . esc_html__('DeepSeek chat endpoint', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="url" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="%3$s">',
            esc_attr(self::OPTION_DEEPSEEK_ENDPOINT),
            esc_attr($this->deepseek_endpoint()),
            esc_attr(self::DEFAULT_DEEPSEEK_ENDPOINT)
        );
        echo '<p class="description">' . esc_html__('Default is the DeepSeek OpenAI-compatible chat endpoint.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_DEEPSEEK_API_KEY) . '">' . esc_html__('DeepSeek API key', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="password" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" autocomplete="new-password">',
            esc_attr(self::OPTION_DEEPSEEK_API_KEY),
            esc_attr($this->deepseek_api_key())
        );
        echo '<p class="description">' . esc_html__('Stored server-side. The browser never receives this key.', 'lazyblog-translations') . '</p></td></tr>';
        echo '<tr><th scope="row"><label for="' . esc_attr(self::OPTION_DEEPSEEK_MODEL) . '">' . esc_html__('DeepSeek model', 'lazyblog-translations') . '</label></th><td>';
        printf(
            '<input type="text" class="regular-text code" id="%1$s" name="%1$s" value="%2$s" placeholder="%3$s">',
            esc_attr(self::OPTION_DEEPSEEK_MODEL),
            esc_attr($this->deepseek_model()),
            esc_attr(self::DEFAULT_DEEPSEEK_MODEL)
        );
        echo '<p class="description">' . esc_html__('Default: deepseek-v4-flash.', 'lazyblog-translations') . '</p></td></tr>';
        echo '</table>';
        submit_button();
        echo '</form></div>';
    }

    private function render_settings_intro(): void
    {
        echo '<style>
.lazyblog-admin-hero{position:relative;margin:16px 0 22px;padding:24px;border:1px solid #cbd5e1;border-radius:18px;background:linear-gradient(135deg,#0f766e 0%,#0f172a 56%,#7c2d12 100%);color:#fff;box-shadow:0 18px 40px rgba(15,23,42,.18);overflow:hidden}
.lazyblog-admin-hero:after{content:"";position:absolute;right:-70px;top:-90px;width:240px;height:240px;border-radius:999px;background:rgba(255,255,255,.12)}
.lazyblog-admin-hero h2{margin:0 0 8px;color:#fff;font-size:26px;line-height:1.2}
.lazyblog-admin-hero p{max-width:760px;margin:0 0 14px;color:#ecfeff;font-size:14px}
.lazyblog-admin-actions{display:flex;flex-wrap:wrap;gap:10px;margin-top:14px}
.lazyblog-admin-actions a{display:inline-flex;align-items:center;min-height:32px;padding:0 14px;border-radius:999px;background:#fff;color:#0f172a;text-decoration:none;font-weight:600}
.lazyblog-admin-actions a:hover{background:#ccfbf1;color:#0f172a}
.lazyblog-admin-code{display:inline-block;max-width:100%;margin:8px 0 0;padding:12px 14px;border-radius:12px;background:#0f172a;color:#d1fae5;overflow:auto}
</style>';
        echo '<div class="lazyblog-admin-hero">';
        echo '<h2>' . esc_html__('One switcher, three translation engines.', 'lazyblog-translations') . '</h2>';
        echo '<p>' . esc_html__('Choose Codex/LazyBlog for local automation, OpenAI for hosted GPT translation, or DeepSeek for an OpenAI-compatible hosted path. The frontend language switcher keeps the same clean reader experience.', 'lazyblog-translations') . '</p>';
        echo '<div class="lazyblog-admin-actions">';
        printf(
            '<a href="%1$s" target="_blank" rel="noopener">%2$s</a>',
            esc_url(self::PLUGIN_REPO_URL),
            esc_html__('Plugin GitHub', 'lazyblog-translations')
        );
        printf(
            '<a href="%1$s" target="_blank" rel="noopener">%2$s</a>',
            esc_url(self::LAZYBLOG_INSTALL_SCRIPT_URL),
            esc_html__('Codex setup script', 'lazyblog-translations')
        );
        printf(
            '<a href="%1$s" target="_blank" rel="noopener">%2$s</a>',
            esc_url(self::LAZYBLOG_INSTALL_SCRIPT_RAW_URL),
            esc_html__('Raw installer', 'lazyblog-translations')
        );
        echo '</div></div>';
    }

    public function register_query_vars(array $query_vars): array
    {
        $query_vars[] = 'lazyblog_lang';
        return $query_vars;
    }

    public function filter_request_query_vars(array $query_vars): array
    {
        if ($this->current_language !== null) {
            $query_vars['lazyblog_lang'] = $this->current_language;
        }

        if ($this->language_prefixed_post_id > 0) {
            $query_vars = [
                'p' => $this->language_prefixed_post_id,
                'lazyblog_lang' => $this->current_language,
            ];
        }

        return $query_vars;
    }

    public function register_rest_routes(): void
    {
        register_rest_route('lazyblog/v1', '/posts/(?P<id>\d+)/translations', [
            [
                'methods' => WP_REST_Server::READABLE,
                'callback' => [$this, 'rest_get_translations'],
                'permission_callback' => [$this, 'can_read_post_translation'],
            ],
            [
                'methods' => WP_REST_Server::EDITABLE,
                'callback' => [$this, 'rest_update_translation_settings'],
                'permission_callback' => [$this, 'can_edit_post_translation'],
            ],
        ]);

        register_rest_route('lazyblog/v1', '/posts/(?P<id>\d+)/translations/(?P<lang>[A-Za-z0-9_-]+)', [
            [
                'methods' => WP_REST_Server::READABLE,
                'callback' => [$this, 'rest_get_translation'],
                'permission_callback' => [$this, 'can_read_post_translation'],
            ],
            [
                'methods' => WP_REST_Server::EDITABLE,
                'callback' => [$this, 'rest_put_translation'],
                'permission_callback' => [$this, 'can_edit_post_translation'],
            ],
            [
                'methods' => WP_REST_Server::DELETABLE,
                'callback' => [$this, 'rest_delete_translation'],
                'permission_callback' => [$this, 'can_edit_post_translation'],
            ],
        ]);

        register_rest_route('lazyblog/v1', '/posts/(?P<id>\d+)/translations/(?P<lang>[A-Za-z0-9_-]+)/ensure', [
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [$this, 'rest_ensure_translation'],
                'permission_callback' => [$this, 'can_request_translation'],
            ],
        ]);

        register_rest_route('lazyblog/v1', '/cache/purge', [
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [$this, 'rest_purge_cache'],
                'permission_callback' => [$this, 'can_manage_translation_settings'],
            ],
        ]);

        register_rest_route('lazyblog/v1', '/plugin/decouple', [
            [
                'methods' => WP_REST_Server::CREATABLE,
                'callback' => [$this, 'rest_decouple_plugin_slug'],
                'permission_callback' => [$this, 'can_manage_plugins'],
            ],
        ]);
    }

    public function can_read_post_translation(WP_REST_Request $request): bool
    {
        $post = get_post((int) $request['id']);
        if (!$post instanceof WP_Post) {
            return false;
        }

        return $post->post_status === 'publish' || current_user_can('read_post', $post->ID);
    }

    public function can_edit_post_translation(WP_REST_Request $request): bool
    {
        $post_id = (int) $request['id'];
        return $post_id > 0 && current_user_can('edit_post', $post_id);
    }

    public function can_manage_translation_settings(): bool
    {
        return current_user_can('manage_options');
    }

    public function can_manage_plugins(): bool
    {
        return current_user_can('activate_plugins');
    }

    public function can_request_translation(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $language = $this->normalize_language((string) $request['lang']);
        $post = get_post($post_id);
        if (!$post instanceof WP_Post || $post->post_type !== 'post' || $language === null) {
            return new WP_Error('lazyblog_translation_not_allowed', __('Invalid translation request.', 'lazyblog-translations'), ['status' => 404]);
        }

        if ($post->post_status !== 'publish' && !current_user_can('read_post', $post_id)) {
            return new WP_Error('lazyblog_translation_not_allowed', __('Post is not public.', 'lazyblog-translations'), ['status' => 403]);
        }

        $params = $this->json_params($request);
        $request_key = isset($params['key']) ? (string) $params['key'] : '';
        $expires = isset($params['expires']) ? (int) $params['expires'] : 0;
        $signature = isset($params['signature']) ? (string) $params['signature'] : '';
        if (!$this->verify_translation_request($post_id, $language, $request_key, $expires, $signature)) {
            return new WP_Error('lazyblog_bad_translation_signature', __('Invalid or expired translation signature.', 'lazyblog-translations'), ['status' => 403]);
        }

        if (!$this->rate_limit_ok()) {
            return new WP_Error('lazyblog_translation_rate_limited', __('Too many translation requests. Try again later.', 'lazyblog-translations'), ['status' => 429]);
        }

        return true;
    }

    public function rest_get_translations(WP_REST_Request $request): WP_REST_Response
    {
        $post_id = (int) $request['id'];

        return new WP_REST_Response([
            'post_id' => $post_id,
            'source_language' => $this->get_source_language($post_id),
            'current_language' => $this->current_language_for_post($post_id),
            'languages' => $this->languages(),
            'translations' => $this->get_translations($post_id),
        ]);
    }

    public function rest_update_translation_settings(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $params = $this->json_params($request);

        if (isset($params['source_language'])) {
            $source_language = $this->normalize_language((string) $params['source_language']);
            if ($source_language === null) {
                return new WP_Error('lazyblog_invalid_language', __('Unsupported source language.', 'lazyblog-translations'), ['status' => 400]);
            }
            update_post_meta($post_id, self::META_SOURCE_LANGUAGE, $source_language);
        }

        return $this->rest_get_translations($request);
    }

    public function rest_get_translation(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $language = $this->normalize_language((string) $request['lang']);
        if ($language === null) {
            return new WP_Error('lazyblog_invalid_language', __('Unsupported language.', 'lazyblog-translations'), ['status' => 400]);
        }

        $translation = $this->get_translation($post_id, $language);
        if ($translation === null) {
            return new WP_Error('lazyblog_translation_not_found', __('Translation not found.', 'lazyblog-translations'), ['status' => 404]);
        }

        return new WP_REST_Response([
            'post_id' => $post_id,
            'language' => $language,
            'translation' => $translation,
        ]);
    }

    public function rest_put_translation(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $language = $this->normalize_language((string) $request['lang']);
        if ($language === null) {
            return new WP_Error('lazyblog_invalid_language', __('Unsupported language.', 'lazyblog-translations'), ['status' => 400]);
        }

        $params = $this->json_params($request);
        if (isset($params['source_language'])) {
            $source_language = $this->normalize_language((string) $params['source_language']);
            if ($source_language === null) {
                return new WP_Error('lazyblog_invalid_source_language', __('Unsupported source language.', 'lazyblog-translations'), ['status' => 400]);
            }
            update_post_meta($post_id, self::META_SOURCE_LANGUAGE, $source_language);
        }

        $translations = $this->get_translations($post_id);
        $existing = $translations[$language] ?? [];
        $translations[$language] = [
            'title' => array_key_exists('title', $params) ? sanitize_text_field((string) $params['title']) : ($existing['title'] ?? ''),
            'content' => array_key_exists('content', $params) ? wp_kses_post((string) $params['content']) : ($existing['content'] ?? ''),
            'excerpt' => array_key_exists('excerpt', $params) ? wp_kses_post((string) $params['excerpt']) : ($existing['excerpt'] ?? ''),
            'updated_at' => current_time('mysql', true),
        ];

        update_post_meta($post_id, self::META_TRANSLATIONS, $translations);

        return new WP_REST_Response([
            'post_id' => $post_id,
            'language' => $language,
            'translation' => $translations[$language],
        ]);
    }

    public function rest_delete_translation(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $language = $this->normalize_language((string) $request['lang']);
        if ($language === null) {
            return new WP_Error('lazyblog_invalid_language', __('Unsupported language.', 'lazyblog-translations'), ['status' => 400]);
        }

        $translations = $this->get_translations($post_id);
        unset($translations[$language]);
        update_post_meta($post_id, self::META_TRANSLATIONS, $translations);

        return new WP_REST_Response([
            'post_id' => $post_id,
            'language' => $language,
            'deleted' => true,
        ]);
    }

    public function rest_purge_cache(): WP_REST_Response
    {
        return new WP_REST_Response(array_merge(['status' => 'ok'], $this->purge_site_caches()));
    }

    public function rest_decouple_plugin_slug()
    {
        $target_plugin = 'lazyblog-translations/lazyblog-translations.php';
        $current_plugin = plugin_basename(__FILE__);
        $target_dir = WP_PLUGIN_DIR . '/lazyblog-translations';
        $target_file = WP_PLUGIN_DIR . '/' . $target_plugin;
        $copied = false;

        if ($current_plugin !== $target_plugin) {
            if (!wp_mkdir_p($target_dir)) {
                return new WP_Error(
                    'lazyblog_plugin_mkdir_failed',
                    __('Could not create the LazyBlog plugin directory.', 'lazyblog-translations'),
                    ['status' => 500, 'target_dir' => $target_dir]
                );
            }
            @chmod($target_dir, 0775);

            if (!copy(__FILE__, $target_file)) {
                return new WP_Error(
                    'lazyblog_plugin_copy_failed',
                    __('Could not copy LazyBlog Translations into its own plugin directory.', 'lazyblog-translations'),
                    ['status' => 500, 'target_file' => $target_file]
                );
            }

            @chmod($target_file, 0664);
            $copied = true;
        }

        $active_plugins = get_option('active_plugins', []);
        if (!is_array($active_plugins)) {
            $active_plugins = [];
        }

        $active_plugins = array_values(array_diff($active_plugins, [$current_plugin]));
        if (!in_array($target_plugin, $active_plugins, true)) {
            $active_plugins[] = $target_plugin;
        }

        update_option('active_plugins', array_values(array_unique($active_plugins)));
        flush_rewrite_rules(false);
        $cache = $this->purge_site_caches();

        return new WP_REST_Response([
            'status' => 'ok',
            'current_plugin' => $current_plugin,
            'target_plugin' => $target_plugin,
            'target_file' => $target_file,
            'copied' => $copied,
            'active_plugins_updated' => true,
            'cache' => $cache,
            'message' => __('LazyBlog Translations will load from its own plugin slug on the next request.', 'lazyblog-translations'),
        ]);
    }

    public function rest_ensure_translation(WP_REST_Request $request)
    {
        $post_id = (int) $request['id'];
        $language = $this->normalize_language((string) $request['lang']);
        if ($language === null) {
            return new WP_Error('lazyblog_invalid_language', __('Unsupported language.', 'lazyblog-translations'), ['status' => 400]);
        }

        $source_language = $this->get_source_language($post_id);
        $redirect_url = $this->language_url($post_id, $language);
        if ($language === $source_language || $this->language_has_translation($post_id, $language)) {
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'status' => 'ready',
                'redirect_url' => $redirect_url,
            ]);
        }

        if ($this->translation_provider() !== self::PROVIDER_LAZYBLOG) {
            return $this->rest_ensure_direct_provider_translation($post_id, $language, $redirect_url);
        }

        $jobs = $this->get_translation_jobs($post_id);
        $job = $jobs[$language] ?? null;
        if (is_array($job) && !empty($job['job_id']) && in_array(($job['status'] ?? ''), ['queued', 'running'], true)) {
            $polled = $this->poll_lazyblog_translation_job((string) $job['job_id']);
            $response = $this->handle_translation_job_response($post_id, $language, $polled, $redirect_url);
            if ($response !== null) {
                return $response;
            }
        }

        $lock_key = $this->translation_lock_key($post_id, $language);
        if (get_transient($lock_key)) {
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'status' => 'queued',
                'message' => __('Translation is already starting.', 'lazyblog-translations'),
                'poll_after' => 2,
            ]);
        }

        set_transient($lock_key, 1, 30);
        try {
            $started = $this->start_lazyblog_translation_job($post_id, $language);
            $response = $this->handle_translation_job_response($post_id, $language, $started, $redirect_url);
            if ($response !== null) {
                return $response;
            }
        } finally {
            delete_transient($lock_key);
        }

        return new WP_REST_Response([
            'post_id' => $post_id,
            'language' => $language,
            'status' => 'queued',
            'message' => __('Translation job started.', 'lazyblog-translations'),
            'poll_after' => 2,
        ]);
    }

    private function rest_ensure_direct_provider_translation(int $post_id, string $language, string $redirect_url): WP_REST_Response
    {
        $provider = $this->translation_provider();
        $lock_key = $this->translation_lock_key($post_id, $language);
        if (get_transient($lock_key)) {
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'provider' => $provider,
                'status' => 'queued',
                'message' => __('Translation is already running.', 'lazyblog-translations'),
                'poll_after' => 2,
            ]);
        }

        set_transient($lock_key, 1, 2 * MINUTE_IN_SECONDS);
        $this->update_translation_job($post_id, $language, [
            'provider' => $provider,
            'job_id' => $provider . '-' . wp_generate_uuid4(),
            'status' => 'running',
            'model' => $this->direct_provider_model($provider),
        ]);

        try {
            $translation = $this->generate_direct_provider_translation($post_id, $language);
            if (is_wp_error($translation)) {
                $this->update_translation_job($post_id, $language, [
                    'provider' => $provider,
                    'status' => 'failed',
                    'error' => $translation->get_error_message(),
                ]);

                return new WP_REST_Response([
                    'post_id' => $post_id,
                    'language' => $language,
                    'provider' => $provider,
                    'status' => 'failed',
                    'message' => $translation->get_error_message(),
                ], 502);
            }

            $this->store_translation_output($post_id, $language, $translation);
            $this->update_translation_job($post_id, $language, [
                'provider' => $provider,
                'status' => 'succeeded',
                'model' => $this->direct_provider_model($provider),
            ]);

            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'provider' => $provider,
                'status' => 'ready',
                'redirect_url' => $redirect_url,
            ]);
        } finally {
            delete_transient($lock_key);
        }
    }

    public function filter_title(string $title, int $post_id = 0): string
    {
        if (is_admin() || $post_id <= 0 || !$this->is_translation_context()) {
            return $title;
        }

        $language = $this->effective_language_for_post($post_id);
        if ($language === $this->get_source_language($post_id)) {
            return $title;
        }

        $translation = $this->get_translation($post_id, $language);
        if (!is_array($translation) || empty($translation['title'])) {
            return $title;
        }

        return (string) $translation['title'];
    }

    public function filter_listing_content(string $content): string
    {
        if (is_admin() || is_singular('post') || !in_the_loop() || !is_main_query()) {
            return $content;
        }

        if (!is_home() && !is_archive() && !is_search()) {
            return $content;
        }

        $post = get_post();
        if (!$post instanceof WP_Post || $post->post_type !== 'post') {
            return $content;
        }

        $excerpt_source = trim((string) $post->post_excerpt);
        if ($excerpt_source === '') {
            $excerpt_source = $content;
        }

        $excerpt_source = preg_replace('/\[(?:latex|math)\b[^\]]*\].*?\[\/(?:latex|math)\]/is', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = preg_replace('/!\[.*?\]\([^)]+\)/s', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = preg_replace('/\\\\begin\{[^}]+\}.*?\\\\end\{[^}]+\}/s', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = preg_replace('/\\\\\[.*?\\\\\]/s', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = preg_replace('/\\\\\(.*?\\\\\)/s', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = preg_replace('/<img\b[^>]*>/i', ' ', $excerpt_source) ?? $excerpt_source;
        $excerpt_source = strip_shortcodes($excerpt_source);
        $excerpt = wp_trim_words(wp_strip_all_tags($excerpt_source), 55, '...');
        $permalink = get_permalink($post);

        if ($excerpt === '' || !is_string($permalink)) {
            return $content;
        }

        return sprintf(
            '<p>%s</p><p><a class="more-link" href="%s">%s</a></p>',
            esc_html($excerpt),
            esc_url($permalink),
            esc_html__('Continue reading', 'lazyblog-translations')
        );
    }

    public function filter_content(string $content): string
    {
        if ($this->content_filter_active || is_admin() || !is_singular('post') || !in_the_loop()) {
            return $content;
        }

        $post_id = get_the_ID();
        if (!$post_id) {
            return $content;
        }

        $this->content_filter_active = true;

        $language = $this->effective_language_for_post($post_id);
        $source_language = $this->get_source_language($post_id);
        $rendered_content = $content;

        if ($language !== $source_language) {
            $translation = $this->get_translation($post_id, $language);
            if (is_array($translation) && !empty($translation['content'])) {
                $rendered_content = (string) $translation['content'];
            }
        }

        $rendered_content = $this->render_math_shortcodes($rendered_content);
        $rendered_content .= $this->render_language_switcher($post_id);
        $this->content_filter_active = false;

        return $rendered_content;
    }

    public function filter_language_attributes(string $output): string
    {
        $language = $this->effective_language_for_post(get_queried_object_id() ?: 0);
        $locale = $this->language_locale($language);
        if (!$locale) {
            return $output;
        }

        $html_language = str_replace('_', '-', $locale);
        if (preg_match('/\blang="/', $output)) {
            $output = preg_replace('/\blang="[^"]*"/', 'lang="' . esc_attr($html_language) . '"', $output) ?? $output;
        } else {
            $output = trim($output . ' lang="' . esc_attr($html_language) . '"');
        }

        $direction = $this->language_direction($language);
        if (preg_match('/\bdir="/', $output)) {
            return preg_replace('/\bdir="[^"]*"/', 'dir="' . esc_attr($direction) . '"', $output) ?? $output;
        }

        return trim($output . ' dir="' . esc_attr($direction) . '"');
    }

    public function filter_redirect_canonical($redirect_url, $requested_url)
    {
        if ($this->current_language !== null || get_query_var('lazyblog_lang')) {
            return false;
        }

        return $redirect_url;
    }

    public function enqueue_styles(): void
    {
        wp_dequeue_style('wp-quicklatex-format');
        wp_dequeue_script('wp-quicklatex-frontend');

        wp_register_style('lazyblog-translations', false, [], self::PLUGIN_VERSION);
        wp_enqueue_style('lazyblog-translations');
        wp_add_inline_style('lazyblog-translations', $this->switcher_css());

        if (is_singular('post')) {
            wp_register_script('lazyblog-translations', false, [], self::PLUGIN_VERSION, true);
            wp_enqueue_script('lazyblog-translations');
            wp_add_inline_script('lazyblog-translations', $this->switcher_js());
        }

        if ($this->current_page_needs_mathjax()) {
            wp_enqueue_script(
                'lazyblog-mathjax',
                'https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js',
                [],
                '3.2.2',
                ['strategy' => 'defer']
            );
            wp_add_inline_script('lazyblog-mathjax', $this->mathjax_config(), 'before');
        }
    }

    public function render_inline_math_shortcode(array $attributes, ?string $content = null): string
    {
        return $this->math_html((string) $content, false);
    }

    public function render_display_math_shortcode(array $attributes, ?string $content = null): string
    {
        return $this->math_html((string) $content, true);
    }

    private function is_translation_context(): bool
    {
        return $this->current_language !== null || is_singular('post');
    }

    private function json_params(WP_REST_Request $request): array
    {
        $params = $request->get_json_params();
        return is_array($params) ? $params : $request->get_params();
    }

    private function capture_language_permalink_parts(array $segments): void
    {
        if (count($segments) < 5 || ($segments[1] ?? '') !== 'html') {
            return;
        }

        $post_id_segment = $segments[count($segments) - 2] ?? '';
        if (is_string($post_id_segment) && ctype_digit($post_id_segment)) {
            $this->language_prefixed_post_id = (int) $post_id_segment;
        }
    }

    private function languages(): array
    {
        $default = [
            'zh' => [
                'label' => '简体中文',
                'locale' => 'zh_CN',
                'slug' => 'zh',
            ],
            'zh-hant' => [
                'label' => '繁體中文',
                'locale' => 'zh_TW',
                'slug' => 'zh-hant',
            ],
            'en' => [
                'label' => 'English',
                'locale' => 'en_US',
                'slug' => 'en',
            ],
            'ja' => [
                'label' => '日本語',
                'locale' => 'ja',
                'slug' => 'ja',
            ],
            'ko' => [
                'label' => '한국어',
                'locale' => 'ko_KR',
                'slug' => 'ko',
            ],
            'vi' => [
                'label' => 'Tiếng Việt',
                'locale' => 'vi',
                'slug' => 'vi',
            ],
            'ar' => [
                'label' => 'العربية',
                'locale' => 'ar',
                'slug' => 'ar',
                'dir' => 'rtl',
            ],
            'fr' => [
                'label' => 'Français',
                'locale' => 'fr_FR',
                'slug' => 'fr',
            ],
            'es' => [
                'label' => 'Español',
                'locale' => 'es_ES',
                'slug' => 'es',
            ],
            'de' => [
                'label' => 'Deutsch',
                'locale' => 'de_DE',
                'slug' => 'de',
            ],
            'ru' => [
                'label' => 'Русский',
                'locale' => 'ru_RU',
                'slug' => 'ru',
            ],
        ];

        $configured = get_option(self::OPTION_LANGUAGES);
        if (is_array($configured) && $configured) {
            foreach ($configured as $code => $settings) {
                $language = $this->normalize_language((string) $code);
                if ($language === null || !is_array($settings)) {
                    continue;
                }
                $default[$language] = array_merge($default[$language] ?? [], $settings);
            }
        }

        return apply_filters('lazyblog_translation_languages', $default);
    }

    private function normalize_language(string $value): ?string
    {
        $normalized = strtolower(trim(str_replace('_', '-', $value)));
        if ($normalized === '') {
            return null;
        }

        $aliases = [
            'en-us' => 'en',
            'en' => 'en',
            'english' => 'en',
            'zh-cn' => 'zh',
            'zh-hans' => 'zh',
            'zh-sg' => 'zh',
            'zh' => 'zh',
            'cn' => 'zh',
            'jianti' => 'zh',
            'simplified' => 'zh',
            'simplified-chinese' => 'zh',
            'zh-tw' => 'zh-hant',
            'zh-hk' => 'zh-hant',
            'zh-mo' => 'zh-hant',
            'zh-hant' => 'zh-hant',
            'fanti' => 'zh-hant',
            'traditional' => 'zh-hant',
            'traditional-chinese' => 'zh-hant',
            'ja-jp' => 'ja',
            'jp' => 'ja',
            'ja' => 'ja',
            'japanese' => 'ja',
            'ko-kr' => 'ko',
            'ko' => 'ko',
            'kr' => 'ko',
            'korean' => 'ko',
            'vi-vn' => 'vi',
            'vi' => 'vi',
            'vietnamese' => 'vi',
            'ar' => 'ar',
            'arabic' => 'ar',
            'fr-fr' => 'fr',
            'fr' => 'fr',
            'french' => 'fr',
            'es-es' => 'es',
            'es' => 'es',
            'spanish' => 'es',
            'de-de' => 'de',
            'de' => 'de',
            'deutsch' => 'de',
            'german' => 'de',
            'ru-ru' => 'ru',
            'ru' => 'ru',
            'russian' => 'ru',
        ];

        if (isset($aliases[$normalized])) {
            return $aliases[$normalized];
        }

        $languages = $this->languages_without_normalization();
        foreach ($languages as $code => $settings) {
            $slug = strtolower((string) ($settings['slug'] ?? $code));
            $locale = strtolower(str_replace('_', '-', (string) ($settings['locale'] ?? $code)));
            if ($normalized === strtolower((string) $code) || $normalized === $slug || $normalized === $locale) {
                return (string) $code;
            }
        }

        return null;
    }

    private function languages_without_normalization(): array
    {
        return [
            'zh' => ['locale' => 'zh_CN', 'slug' => 'zh'],
            'zh-hant' => ['locale' => 'zh_TW', 'slug' => 'zh-hant'],
            'en' => ['locale' => 'en_US', 'slug' => 'en'],
            'ja' => ['locale' => 'ja', 'slug' => 'ja'],
            'ko' => ['locale' => 'ko_KR', 'slug' => 'ko'],
            'vi' => ['locale' => 'vi', 'slug' => 'vi'],
            'ar' => ['locale' => 'ar', 'slug' => 'ar'],
            'fr' => ['locale' => 'fr_FR', 'slug' => 'fr'],
            'es' => ['locale' => 'es_ES', 'slug' => 'es'],
            'de' => ['locale' => 'de_DE', 'slug' => 'de'],
            'ru' => ['locale' => 'ru_RU', 'slug' => 'ru'],
        ];
    }

    private function get_source_language(int $post_id): string
    {
        $source_language = get_post_meta($post_id, self::META_SOURCE_LANGUAGE, true);
        $language = $this->normalize_language(is_string($source_language) ? $source_language : '');

        return $language ?? 'en';
    }

    private function current_language_for_post(int $post_id): string
    {
        if ($this->current_language !== null) {
            return $this->current_language;
        }

        $rewrite_language = get_query_var('lazyblog_lang');
        if (is_string($rewrite_language)) {
            $language = $this->normalize_language($rewrite_language);
            if ($language !== null) {
                return $language;
            }
        }

        $query_language = isset($_GET['lazyblog_lang']) ? $this->normalize_language((string) wp_unslash($_GET['lazyblog_lang'])) : null;
        if ($query_language !== null) {
            return $query_language;
        }

        return $post_id > 0 ? $this->get_source_language($post_id) : 'en';
    }

    private function effective_language_for_post(int $post_id): string
    {
        $requested_language = $this->current_language_for_post($post_id);
        if ($post_id <= 0) {
            return $requested_language;
        }

        $source_language = $this->get_source_language($post_id);
        if ($requested_language === $source_language) {
            return $source_language;
        }

        $translation = $this->get_translation($post_id, $requested_language);
        if (is_array($translation) && (!empty($translation['content']) || !empty($translation['title']))) {
            return $requested_language;
        }

        return $source_language;
    }

    private function get_translations(int $post_id): array
    {
        $translations = get_post_meta($post_id, self::META_TRANSLATIONS, true);
        if (!is_array($translations)) {
            return [];
        }

        $normalized = [];
        foreach ($translations as $language => $translation) {
            $language = $this->normalize_language((string) $language);
            if ($language === null || !is_array($translation)) {
                continue;
            }

            $normalized[$language] = [
                'title' => (string) ($translation['title'] ?? ''),
                'content' => (string) ($translation['content'] ?? ''),
                'excerpt' => (string) ($translation['excerpt'] ?? ''),
                'updated_at' => (string) ($translation['updated_at'] ?? ''),
            ];
        }

        return $normalized;
    }

    private function get_translation(int $post_id, string $language): ?array
    {
        $translations = $this->get_translations($post_id);
        return $translations[$language] ?? null;
    }

    private function language_locale(string $language): ?string
    {
        $languages = $this->languages();
        return isset($languages[$language]['locale']) ? (string) $languages[$language]['locale'] : null;
    }

    private function language_direction(string $language): string
    {
        $languages = $this->languages();
        return isset($languages[$language]['dir']) && $languages[$language]['dir'] === 'rtl' ? 'rtl' : 'ltr';
    }

    private function render_language_switcher(int $post_id): string
    {
        $languages = $this->languages();
        $translations = $this->get_translations($post_id);
        $source_language = $this->get_source_language($post_id);
        $current_language = $this->effective_language_for_post($post_id);
        $source_label = $languages[$source_language]['label'] ?? strtoupper($source_language);
        $current_label = $current_language === $source_language
            /* translators: %s is the human-readable source language label, for example English. */
            ? sprintf(__('Original (%s)', 'lazyblog-translations'), $source_label)
            : ($languages[$current_language]['label'] ?? strtoupper($current_language));

        $items = '';
        $original_classes = ['lazyblog-ls__link', 'is-original'];
        if ($current_language === $source_language) {
            $original_classes[] = 'is-current';
            $items .= sprintf(
                '<a href="#" class="%s" onclick="event.preventDefault()" aria-current="true">%s</a>',
                esc_attr(implode(' ', $original_classes)),
                /* translators: %s is the human-readable source language label, for example English. */
                esc_html(sprintf(__('Original (%s)', 'lazyblog-translations'), $source_label))
            );
        } else {
            $items .= sprintf(
                '<a href="%s" class="%s">%s</a>',
                esc_url($this->language_url($post_id, $source_language)),
                esc_attr(implode(' ', $original_classes)),
                /* translators: %s is the human-readable source language label, for example English. */
                esc_html(sprintf(__('Original (%s)', 'lazyblog-translations'), $source_label))
            );
        }

        foreach ($languages as $language => $settings) {
            $label = (string) ($settings['label'] ?? strtoupper((string) $language));
            $is_current = $language === $current_language && $current_language !== $source_language;
            $is_available = $this->language_is_available((string) $language, $source_language, $translations);
            $classes = ['lazyblog-ls__link'];
            if ($is_current) {
                $classes[] = 'is-current';
            }
            if (!$is_available) {
                $classes[] = 'is-missing';
            }

            if ($is_current) {
                $items .= sprintf(
                    '<a href="#" class="%s" onclick="event.preventDefault()" aria-current="true">%s</a>',
                    esc_attr(implode(' ', $classes)),
                    esc_html($label)
                );
                continue;
            }

            if (!$is_available) {
                $items .= sprintf(
                    '<a href="%s" class="%s" data-lazyblog-translate="1" data-post-id="%d" data-language="%s" data-language-label="%s" data-endpoint="%s" data-key="%s" title="%s">%s<span class="lazyblog-ls__progress" aria-hidden="true"></span></a>',
                    esc_url($this->language_url($post_id, (string) $language)),
                    esc_attr(implode(' ', $classes)),
                    $post_id,
                    esc_attr((string) $language),
                    esc_attr($label),
                    esc_url(rest_url(sprintf('lazyblog/v1/posts/%d/translations/%s/ensure', $post_id, rawurlencode((string) $language)))),
                    esc_attr($this->translation_request_key($post_id, (string) $language)),
                    /* translators: %s is the human-readable target language label. */
                    esc_attr(sprintf(__('Click to generate the %s translation.', 'lazyblog-translations'), $label)),
                    esc_html($label)
                );
                continue;
            }

            $items .= sprintf(
                '<a href="%s" class="%s">%s</a>',
                esc_url($this->language_url($post_id, (string) $language)),
                esc_attr(implode(' ', $classes)),
                esc_html($label)
            );
        }

        return sprintf(
            '<nav id="lazyblog-language-switcher" class="lazyblog-language-switcher" data-no-translation aria-label="%s"><div class="lazyblog-ls__current">%s</div><div class="lazyblog-ls__list">%s<div class="lazyblog-ls__status" role="status" aria-live="polite"></div></div></nav>',
            esc_attr__('Language switcher', 'lazyblog-translations'),
            esc_html($current_label),
            $items
        );
    }

    private function language_url(int $post_id, string $language): string
    {
        $languages = $this->languages();
        $slug = $languages[$language]['slug'] ?? $language;
        $permalink = get_permalink($post_id);
        $path = parse_url($permalink, PHP_URL_PATH);
        if (!is_string($path) || $path === '') {
            $path = '/';
        }

        $path = $this->strip_language_prefix($path);

        return home_url('/' . trim((string) $slug, '/') . '/' . ltrim($path, '/'));
    }

    private function strip_language_prefix(string $path): string
    {
        $segments = explode('/', trim($path, '/'));
        if (!$segments || $segments[0] === '') {
            return '/';
        }

        if ($this->normalize_language($segments[0]) !== null) {
            $segments = array_slice($segments, 1);
        }

        return '/' . implode('/', $segments);
    }

    private function language_is_available(string $language, string $source_language, array $translations): bool
    {
        return $language === $source_language || !empty($translations[$language]['content']) || !empty($translations[$language]['title']);
    }

    private function language_has_translation(int $post_id, string $language): bool
    {
        $translation = $this->get_translation($post_id, $language);
        return is_array($translation) && (!empty($translation['content']) || !empty($translation['title']));
    }

    private function translation_signature(int $post_id, string $language, int $expires): string
    {
        return hash_hmac('sha256', $post_id . '|' . $language . '|' . $expires, wp_salt('auth'));
    }

    private function translation_request_key(int $post_id, string $language): string
    {
        return hash_hmac('sha256', 'lazyblog-translation|' . home_url('/') . '|' . $post_id . '|' . $language, wp_salt('auth'));
    }

    private function verify_translation_request(int $post_id, string $language, string $request_key, int $expires, string $signature): bool
    {
        if ($request_key !== '' && hash_equals($this->translation_request_key($post_id, $language), $request_key)) {
            return true;
        }

        return $this->verify_translation_signature($post_id, $language, $expires, $signature);
    }

    private function verify_translation_signature(int $post_id, string $language, int $expires, string $signature): bool
    {
        if ($expires < time() || $expires > time() + self::TRANSLATION_SIGNATURE_TTL + 300 || $signature === '') {
            return false;
        }

        return hash_equals($this->translation_signature($post_id, $language, $expires), $signature);
    }

    private function rate_limit_ok(): bool
    {
        $ip = $_SERVER['REMOTE_ADDR'] ?? 'unknown';
        $key = 'lazyblog_translate_rate_' . md5((string) $ip);
        $count = (int) get_transient($key);
        if ($count >= 120) {
            return false;
        }

        set_transient($key, $count + 1, 10 * MINUTE_IN_SECONDS);
        return true;
    }

    private function get_translation_jobs(int $post_id): array
    {
        $jobs = get_post_meta($post_id, self::META_TRANSLATION_JOBS, true);
        return is_array($jobs) ? $jobs : [];
    }

    private function update_translation_job(int $post_id, string $language, array $job): void
    {
        $jobs = $this->get_translation_jobs($post_id);
        $jobs[$language] = array_merge($jobs[$language] ?? [], $job, ['updated_at' => current_time('mysql', true)]);
        update_post_meta($post_id, self::META_TRANSLATION_JOBS, $jobs);
    }

    private function translation_lock_key(int $post_id, string $language): string
    {
        return 'lazyblog_translate_lock_' . $post_id . '_' . md5($language);
    }

    private function translation_providers(): array
    {
        return [
            self::PROVIDER_LAZYBLOG => __('Codex / LazyBlog local API', 'lazyblog-translations'),
            self::PROVIDER_OPENAI => __('OpenAI direct API', 'lazyblog-translations'),
            self::PROVIDER_DEEPSEEK => __('DeepSeek direct API', 'lazyblog-translations'),
        ];
    }

    private function translation_provider(): string
    {
        if (defined('LAZYBLOG_TRANSLATION_PROVIDER')) {
            return $this->sanitize_translation_provider((string) constant('LAZYBLOG_TRANSLATION_PROVIDER'));
        }

        $configured = get_option(self::OPTION_PROVIDER, self::DEFAULT_PROVIDER);
        return $this->sanitize_translation_provider(is_string($configured) ? $configured : '');
    }

    private function api_endpoint(): string
    {
        if (defined('LAZYBLOG_TRANSLATION_API_ENDPOINT')) {
            return (string) constant('LAZYBLOG_TRANSLATION_API_ENDPOINT');
        }

        $configured = get_option(self::OPTION_API_ENDPOINT);
        if (is_string($configured) && trim($configured) !== '') {
            return trim($configured);
        }

        return wp_get_environment_type() === 'local'
            ? 'http://host.docker.internal:8765/api/translate/jobs'
            : '';
    }

    private function api_token(): string
    {
        if (defined('LAZYBLOG_TRANSLATION_API_TOKEN')) {
            return (string) constant('LAZYBLOG_TRANSLATION_API_TOKEN');
        }

        $env_token = getenv('LAZYBLOG_API_TOKEN');
        if (is_string($env_token) && trim($env_token) !== '') {
            return trim($env_token);
        }

        $configured = get_option(self::OPTION_API_TOKEN);
        return is_string($configured) ? trim($configured) : '';
    }

    private function api_mock_enabled(): bool
    {
        if (defined('LAZYBLOG_TRANSLATION_API_MOCK')) {
            return (bool) constant('LAZYBLOG_TRANSLATION_API_MOCK');
        }

        return (bool) get_option(self::OPTION_API_MOCK, false);
    }

    private function api_model(): string
    {
        if (defined('LAZYBLOG_TRANSLATION_API_MODEL')) {
            return $this->sanitize_translation_model((string) constant('LAZYBLOG_TRANSLATION_API_MODEL'));
        }

        $configured = get_option(self::OPTION_API_MODEL, self::DEFAULT_API_MODEL);
        return $this->sanitize_translation_model(is_string($configured) ? $configured : '');
    }

    private function api_reasoning(): string
    {
        if (defined('LAZYBLOG_TRANSLATION_API_REASONING')) {
            return $this->sanitize_translation_reasoning((string) constant('LAZYBLOG_TRANSLATION_API_REASONING'));
        }

        $configured = get_option(self::OPTION_API_REASONING, self::DEFAULT_API_REASONING);
        return $this->sanitize_translation_reasoning(is_string($configured) ? $configured : '');
    }

    private function openai_endpoint(): string
    {
        if (defined('LAZYBLOG_OPENAI_CHAT_ENDPOINT')) {
            return (string) constant('LAZYBLOG_OPENAI_CHAT_ENDPOINT');
        }

        $configured = get_option(self::OPTION_OPENAI_ENDPOINT, self::DEFAULT_OPENAI_ENDPOINT);
        return is_string($configured) && trim($configured) !== '' ? trim($configured) : self::DEFAULT_OPENAI_ENDPOINT;
    }

    private function openai_api_key(): string
    {
        if (defined('LAZYBLOG_OPENAI_API_KEY')) {
            return (string) constant('LAZYBLOG_OPENAI_API_KEY');
        }

        $env_key = getenv('OPENAI_API_KEY');
        if (is_string($env_key) && trim($env_key) !== '') {
            return trim($env_key);
        }

        $configured = get_option(self::OPTION_OPENAI_API_KEY);
        return is_string($configured) ? trim($configured) : '';
    }

    private function openai_model(): string
    {
        if (defined('LAZYBLOG_OPENAI_MODEL')) {
            $model = $this->sanitize_model_name((string) constant('LAZYBLOG_OPENAI_MODEL'));
            return $model !== '' ? $model : self::DEFAULT_OPENAI_MODEL;
        }

        $configured = get_option(self::OPTION_OPENAI_MODEL, self::DEFAULT_OPENAI_MODEL);
        $model = $this->sanitize_model_name(is_string($configured) ? $configured : '');
        return $model !== '' ? $model : self::DEFAULT_OPENAI_MODEL;
    }

    private function deepseek_endpoint(): string
    {
        if (defined('LAZYBLOG_DEEPSEEK_CHAT_ENDPOINT')) {
            return (string) constant('LAZYBLOG_DEEPSEEK_CHAT_ENDPOINT');
        }

        $configured = get_option(self::OPTION_DEEPSEEK_ENDPOINT, self::DEFAULT_DEEPSEEK_ENDPOINT);
        return is_string($configured) && trim($configured) !== '' ? trim($configured) : self::DEFAULT_DEEPSEEK_ENDPOINT;
    }

    private function deepseek_api_key(): string
    {
        if (defined('LAZYBLOG_DEEPSEEK_API_KEY')) {
            return (string) constant('LAZYBLOG_DEEPSEEK_API_KEY');
        }

        $env_key = getenv('DEEPSEEK_API_KEY');
        if (is_string($env_key) && trim($env_key) !== '') {
            return trim($env_key);
        }

        $configured = get_option(self::OPTION_DEEPSEEK_API_KEY);
        return is_string($configured) ? trim($configured) : '';
    }

    private function deepseek_model(): string
    {
        if (defined('LAZYBLOG_DEEPSEEK_MODEL')) {
            $model = $this->sanitize_model_name((string) constant('LAZYBLOG_DEEPSEEK_MODEL'));
            return $model !== '' ? $model : self::DEFAULT_DEEPSEEK_MODEL;
        }

        $configured = get_option(self::OPTION_DEEPSEEK_MODEL, self::DEFAULT_DEEPSEEK_MODEL);
        $model = $this->sanitize_model_name(is_string($configured) ? $configured : '');
        return $model !== '' ? $model : self::DEFAULT_DEEPSEEK_MODEL;
    }

    private function lazyblog_api_url(string $path): string
    {
        $endpoint = $this->api_endpoint();
        if ($endpoint === '') {
            return '';
        }

        if ($path === 'job') {
            return preg_replace('#/translate/jobs/?$#', '/translate/job', $endpoint) ?: $endpoint;
        }

        return $endpoint;
    }

    private function start_lazyblog_translation_job(int $post_id, string $language)
    {
        $endpoint = $this->lazyblog_api_url('jobs');
        $token = $this->api_token();
        if ($endpoint === '' || $token === '') {
            return new WP_Error('lazyblog_api_not_configured', __('LazyBlog translation API is not configured.', 'lazyblog-translations'), ['status' => 503]);
        }

        $payload = $this->translation_api_payload($post_id, $language);
        $response = wp_remote_post($endpoint, [
            'timeout' => 20,
            'headers' => [
                'Authorization' => 'Bearer ' . $token,
                'Content-Type' => 'application/json',
                'Accept' => 'application/json',
            ],
            'body' => wp_json_encode($payload),
        ]);

        return $this->decode_lazyblog_api_response($response);
    }

    private function poll_lazyblog_translation_job(string $job_id)
    {
        $endpoint = $this->lazyblog_api_url('job');
        $token = $this->api_token();
        if ($endpoint === '' || $token === '') {
            return new WP_Error('lazyblog_api_not_configured', __('LazyBlog translation API is not configured.', 'lazyblog-translations'), ['status' => 503]);
        }

        $url = add_query_arg('id', rawurlencode($job_id), $endpoint);
        $response = wp_remote_get($url, [
            'timeout' => 15,
            'headers' => [
                'Authorization' => 'Bearer ' . $token,
                'Accept' => 'application/json',
            ],
        ]);

        return $this->decode_lazyblog_api_response($response);
    }

    private function direct_provider_model(string $provider): string
    {
        if ($provider === self::PROVIDER_OPENAI) {
            return $this->openai_model();
        }

        if ($provider === self::PROVIDER_DEEPSEEK) {
            return $this->deepseek_model();
        }

        return $this->api_model();
    }

    private function direct_provider_endpoint(string $provider): string
    {
        if ($provider === self::PROVIDER_OPENAI) {
            return $this->openai_endpoint();
        }

        if ($provider === self::PROVIDER_DEEPSEEK) {
            return $this->deepseek_endpoint();
        }

        return '';
    }

    private function direct_provider_api_key(string $provider): string
    {
        if ($provider === self::PROVIDER_OPENAI) {
            return $this->openai_api_key();
        }

        if ($provider === self::PROVIDER_DEEPSEEK) {
            return $this->deepseek_api_key();
        }

        return '';
    }

    private function generate_direct_provider_translation(int $post_id, string $language)
    {
        $provider = $this->translation_provider();
        $endpoint = $this->direct_provider_endpoint($provider);
        $api_key = $this->direct_provider_api_key($provider);
        if ($endpoint === '' || $api_key === '') {
            return new WP_Error('lazyblog_direct_provider_not_configured', __('Direct translation provider is not configured.', 'lazyblog-translations'), ['status' => 503]);
        }

        $payload = $this->direct_provider_payload($post_id, $language, $provider);
        $response = wp_remote_post($endpoint, [
            'timeout' => 90,
            'headers' => [
                'Authorization' => 'Bearer ' . $api_key,
                'Content-Type' => 'application/json',
                'Accept' => 'application/json',
            ],
            'body' => wp_json_encode($payload),
        ]);

        return $this->decode_direct_provider_response($response);
    }

    private function direct_provider_payload(int $post_id, string $language, string $provider): array
    {
        $payload = [
            'model' => $this->direct_provider_model($provider),
            'messages' => [
                [
                    'role' => 'system',
                    'content' => $this->direct_provider_system_prompt(),
                ],
                [
                    'role' => 'user',
                    'content' => $this->direct_provider_user_prompt($post_id, $language),
                ],
            ],
            'temperature' => 0.2,
            'response_format' => [
                'type' => 'json_object',
            ],
        ];

        if ($provider === self::PROVIDER_OPENAI) {
            $payload['max_completion_tokens'] = 4096;
        } elseif ($provider === self::PROVIDER_DEEPSEEK) {
            $payload['max_tokens'] = 8192;
        }

        return $payload;
    }

    private function direct_provider_system_prompt(): string
    {
        return 'You are a careful multilingual WordPress blog translator. Return only a JSON object with keys "title", "content", and "excerpt". Preserve the author voice, paragraph structure, HTML, Markdown, WordPress shortcodes, code blocks, math notation, links, image markup, and factual meaning. Do not add AI disclaimers, prefaces, summaries, or invented details.';
    }

    private function direct_provider_user_prompt(int $post_id, string $language): string
    {
        $payload = $this->translation_api_payload($post_id, $language);
        unset($payload['mock'], $payload['model'], $payload['reasoning']);

        return sprintf(
            "Translate this WordPress post from %s (%s) into %s (%s). Return valid JSON only.\n\n%s",
            (string) ($payload['source_label'] ?? ''),
            (string) ($payload['source_language'] ?? ''),
            (string) ($payload['target_label'] ?? ''),
            (string) ($payload['target_language'] ?? ''),
            wp_json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT)
        );
    }

    private function decode_direct_provider_response($response)
    {
        if (is_wp_error($response)) {
            return $response;
        }

        $code = (int) wp_remote_retrieve_response_code($response);
        $body = (string) wp_remote_retrieve_body($response);
        $decoded = json_decode($body, true);
        if (!is_array($decoded)) {
            return new WP_Error('lazyblog_direct_provider_bad_response', __('Direct provider returned invalid JSON.', 'lazyblog-translations'), ['status' => 502, 'body' => $body]);
        }

        if ($code < 200 || $code >= 300) {
            $message = (string) ($decoded['error']['message'] ?? __('Direct provider request failed.', 'lazyblog-translations'));
            return new WP_Error('lazyblog_direct_provider_failed', $message, ['status' => $code ?: 502, 'body' => $decoded]);
        }

        $content = (string) ($decoded['choices'][0]['message']['content'] ?? '');
        $translation = $this->decode_direct_provider_json_content($content);
        if (is_wp_error($translation)) {
            return $translation;
        }

        if (trim((string) ($translation['content'] ?? '')) === '') {
            return new WP_Error('lazyblog_direct_provider_empty_content', __('Direct provider returned an empty translation.', 'lazyblog-translations'), ['status' => 502]);
        }

        return [
            'title' => (string) ($translation['title'] ?? ''),
            'content' => (string) ($translation['content'] ?? ''),
            'excerpt' => (string) ($translation['excerpt'] ?? ''),
        ];
    }

    private function decode_direct_provider_json_content(string $content)
    {
        $trimmed = trim($content);
        $trimmed = preg_replace('/^```(?:json)?\s*/i', '', $trimmed) ?? $trimmed;
        $trimmed = preg_replace('/\s*```$/', '', $trimmed) ?? $trimmed;

        $decoded = json_decode($trimmed, true);
        if (!is_array($decoded)) {
            $start = strpos($trimmed, '{');
            $end = strrpos($trimmed, '}');
            if ($start !== false && $end !== false && $end > $start) {
                $decoded = json_decode(substr($trimmed, $start, $end - $start + 1), true);
            }
        }

        if (!is_array($decoded)) {
            return new WP_Error('lazyblog_direct_provider_unparseable_content', __('Direct provider response did not contain a valid translation JSON object.', 'lazyblog-translations'), ['status' => 502, 'content' => $content]);
        }

        return $decoded;
    }

    private function decode_lazyblog_api_response($response)
    {
        if (is_wp_error($response)) {
            return $response;
        }

        $code = (int) wp_remote_retrieve_response_code($response);
        $body = (string) wp_remote_retrieve_body($response);
        $decoded = json_decode($body, true);
        if (!is_array($decoded)) {
            return new WP_Error('lazyblog_api_bad_response', __('LazyBlog API returned invalid JSON.', 'lazyblog-translations'), ['status' => 502, 'body' => $body]);
        }

        if ($code < 200 || $code >= 300 || empty($decoded['ok'])) {
            return new WP_Error('lazyblog_api_failed', __('LazyBlog API request failed.', 'lazyblog-translations'), ['status' => $code ?: 502, 'body' => $decoded]);
        }

        return $decoded;
    }

    private function handle_translation_job_response(int $post_id, string $language, $api_response, string $redirect_url): ?WP_REST_Response
    {
        if (is_wp_error($api_response)) {
            $this->update_translation_job($post_id, $language, [
                'status' => 'failed',
                'error' => $api_response->get_error_message(),
            ]);
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'status' => 'failed',
                'message' => $api_response->get_error_message(),
            ], 502);
        }

        $job = is_array($api_response['job'] ?? null) ? $api_response['job'] : [];
        $job_id = (string) ($job['id'] ?? '');
        $status = (string) ($job['status'] ?? 'queued');
        $this->update_translation_job($post_id, $language, [
            'job_id' => $job_id,
            'status' => $status,
            'translation_key' => (string) ($api_response['translation_key'] ?? ''),
        ]);

        if ($status === 'succeeded' && is_array($api_response['output'] ?? null)) {
            $this->store_translation_output($post_id, $language, $api_response['output']);
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'status' => 'ready',
                'job_id' => $job_id,
                'redirect_url' => $redirect_url,
            ]);
        }

        if ($status === 'failed') {
            return new WP_REST_Response([
                'post_id' => $post_id,
                'language' => $language,
                'status' => 'failed',
                'job_id' => $job_id,
                'message' => (string) ($job['error'] ?? __('Translation job failed.', 'lazyblog-translations')),
            ], 502);
        }

        return new WP_REST_Response([
            'post_id' => $post_id,
            'language' => $language,
            'status' => $status ?: 'queued',
            'job_id' => $job_id,
            'poll_after' => 2,
        ]);
    }

    private function store_translation_output(int $post_id, string $language, array $output): void
    {
        $translations = $this->get_translations($post_id);
        $translations[$language] = [
            'title' => sanitize_text_field((string) ($output['title'] ?? '')),
            'content' => wp_kses_post((string) ($output['content'] ?? '')),
            'excerpt' => wp_kses_post((string) ($output['excerpt'] ?? '')),
            'updated_at' => current_time('mysql', true),
        ];
        update_post_meta($post_id, self::META_TRANSLATIONS, $translations);
        $this->update_translation_job($post_id, $language, [
            'status' => 'succeeded',
            'completed_at' => current_time('mysql', true),
        ]);
        $this->purge_site_caches();
    }

    private function purge_site_caches(): array
    {
        $page_cache_purged = null;
        $page_cache_error = '';

        try {
            if (function_exists('WP_Optimize')) {
                $wp_optimize = WP_Optimize();
                if (is_object($wp_optimize) && method_exists($wp_optimize, 'get_page_cache')) {
                    $page_cache = $wp_optimize->get_page_cache();
                    if (is_object($page_cache) && method_exists($page_cache, 'purge')) {
                        $page_cache_purged = (bool) $page_cache->purge();
                    }
                }
            }
        } catch (Throwable $error) {
            $page_cache_purged = false;
            $page_cache_error = $error->getMessage();
        }

        return [
            'object_cache_flushed' => wp_cache_flush(),
            'wp_optimize_page_cache_purged' => $page_cache_purged,
            'wp_optimize_page_cache_error' => $page_cache_error,
        ];
    }

    private function translation_api_payload(int $post_id, string $language): array
    {
        $post = get_post($post_id);
        $source_language = $this->get_source_language($post_id);
        $languages = $this->languages();

        return [
            'post_id' => $post_id,
            'site_url' => home_url('/'),
            'post_url' => get_permalink($post_id),
            'source_language' => $source_language,
            'source_label' => (string) ($languages[$source_language]['label'] ?? strtoupper($source_language)),
            'target_language' => $language,
            'target_label' => (string) ($languages[$language]['label'] ?? strtoupper($language)),
            'title' => $post instanceof WP_Post ? get_the_title($post) : '',
            'content' => $post instanceof WP_Post ? (string) $post->post_content : '',
            'excerpt' => $post instanceof WP_Post ? (string) $post->post_excerpt : '',
            'model' => $this->api_model(),
            'reasoning' => $this->api_reasoning(),
            'mock' => $this->api_mock_enabled(),
        ];
    }

    private function current_page_needs_mathjax(): bool
    {
        if (!is_singular('post')) {
            return false;
        }

        $post = get_post();
        if (!$post instanceof WP_Post) {
            return false;
        }

        if ($this->contains_math_shortcode((string) $post->post_content)) {
            return true;
        }

        foreach ($this->get_translations($post->ID) as $translation) {
            if ($this->contains_math_shortcode((string) ($translation['content'] ?? ''))) {
                return true;
            }
        }

        return false;
    }

    private function contains_math_shortcode(string $content): bool
    {
        return preg_match('/\[(?:math|latex)\b/i', $content) === 1;
    }

    private function render_math_shortcodes(string $content): string
    {
        $content = preg_replace_callback(
            '/\[latex(?:\s[^\]]*)?\](.*?)\[\/latex\]/is',
            fn(array $matches): string => $this->math_html((string) $matches[1], true),
            $content
        ) ?? $content;

        return preg_replace_callback(
            '/\[math(?:\s[^\]]*)?\](.*?)\[\/math\]/is',
            fn(array $matches): string => $this->math_html((string) $matches[1], false),
            $content
        ) ?? $content;
    }

    private function math_html(string $formula, bool $display): string
    {
        $formula = preg_replace('/<br\s*\/?>/i', "\n", $formula) ?? $formula;
        $formula = preg_replace('#</?p[^>]*>#i', "\n", $formula) ?? $formula;
        $formula = trim(wp_specialchars_decode($formula, ENT_QUOTES));
        if ($formula === '') {
            return '';
        }

        if ($display) {
            return '<div class="lazyblog-math lazyblog-math--display">\\[' . esc_html($formula) . '\\]</div>';
        }

        return '<span class="lazyblog-math lazyblog-math--inline">\\(' . esc_html($formula) . '\\)</span>';
    }

    private function mathjax_config(): string
    {
        return <<<'JS'
window.MathJax = {
  tex: {
    inlineMath: [['\\(', '\\)']],
    displayMath: [['\\[', '\\]']],
    processEscapes: true
  },
  options: {
    skipHtmlTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
  }
};
JS;
    }

    private function switcher_js(): string
    {
        return <<<'JS'
(function () {
  function statusFor(link) {
    var switcher = link.closest('.lazyblog-language-switcher');
    return switcher ? switcher.querySelector('.lazyblog-ls__status') : null;
  }

  function labelFor(link) {
    return link.dataset.languageLabel || link.textContent.replace(/\s+·\s+.*/, '').trim() || 'this language';
  }

  function setStatus(link, message, state) {
    var status = statusFor(link);
    if (status) {
      status.textContent = message;
      status.dataset.state = state || '';
    }
    link.dataset.lazyblogStatus = message;
  }

  function parseJson(response) {
    return response.text().then(function (text) {
      var body = {};
      if (text) {
        try {
          body = JSON.parse(text);
        } catch (error) {
          throw new Error('WordPress returned a non-JSON translation response.');
        }
      }
      if (!response.ok || body.code || body.message && !body.status) {
        throw new Error(body.message || 'Translation request failed.');
      }
      return body;
    });
  }

  function requestTranslation(link) {
    if (link.dataset.lazyblogBusy === '1') {
      return;
    }

    var label = labelFor(link);
    link.dataset.lazyblogBusy = '1';
    link.classList.add('is-loading');
    link.classList.remove('is-error', 'is-ready');
    link.setAttribute('aria-busy', 'true');
    setStatus(link, 'Starting ' + label + ' translation...', 'loading');

    var payload = {
      key: link.dataset.key || '',
      signature: link.dataset.signature || '',
      expires: Number(link.dataset.expires || 0)
    };

    fetch(link.dataset.endpoint, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'application/json'
      },
      body: JSON.stringify(payload)
    })
      .then(parseJson)
      .then(function (body) {
        if (body.status === 'ready') {
          link.classList.remove('is-loading');
          link.classList.add('is-ready');
          link.setAttribute('aria-busy', 'false');
          setStatus(link, label + ' translation is ready. Opening...', 'ready');
          window.location.href = body.redirect_url || link.href;
          return;
        }

        if (body.status === 'failed') {
          throw new Error(body.message || 'Translation failed.');
        }

        setStatus(link, body.message || label + ' translation is running. I will refresh automatically.', 'loading');
        window.setTimeout(function () {
          link.dataset.lazyblogBusy = '0';
          requestTranslation(link);
        }, Math.max(1000, Number(body.poll_after || 2) * 1000));
      })
      .catch(function (error) {
        link.dataset.lazyblogBusy = '0';
        link.classList.remove('is-loading');
        link.classList.add('is-error');
        link.setAttribute('aria-busy', 'false');
        link.setAttribute('title', error.message || 'Translation failed.');
        setStatus(link, error.message || 'Translation failed. Click again to retry.', 'error');
      });
  }

  document.addEventListener('click', function (event) {
    var link = event.target.closest('[data-lazyblog-translate="1"]');
    if (!link) {
      return;
    }

    event.preventDefault();
    requestTranslation(link);
  });
})();
JS;
    }

    private function switcher_css(): string
    {
        return <<<'CSS'
.lazyblog-language-switcher {
  position: fixed;
  right: 24px;
  bottom: 0;
  z-index: 99999;
  min-width: 160px;
  max-width: calc(100vw - 48px);
  color: #f5f5f5;
  font: 14px/1.4 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  text-align: left;
}

.lazyblog-ls__current,
.lazyblog-ls__link {
  display: block;
  padding: 10px 14px;
  color: #f5f5f5;
  background: #1f1f1f;
  text-decoration: none;
  box-shadow: 0 -1px 0 rgba(255, 255, 255, 0.08) inset;
}

.lazyblog-ls__current {
  border-radius: 4px 4px 0 0;
  cursor: default;
}

.lazyblog-ls__list {
  display: none;
}

.lazyblog-language-switcher:hover .lazyblog-ls__list,
.lazyblog-language-switcher:focus-within .lazyblog-ls__list {
  display: block;
}

.lazyblog-ls__link:hover,
.lazyblog-ls__link:focus {
  color: #ffffff;
  background: #303030;
}

.lazyblog-ls__link.is-current {
  pointer-events: none;
  opacity: 0.72;
}

.lazyblog-ls__link.is-missing {
  cursor: pointer;
}

.lazyblog-ls__link.is-missing::after {
  content: " · translate";
  opacity: 0.7;
}

.lazyblog-ls__link.is-loading {
  pointer-events: none;
  position: relative;
  padding-right: 38px;
}

.lazyblog-ls__link.is-loading::after {
  content: " · translating";
  opacity: 0.78;
}

.lazyblog-ls__progress {
  display: none;
}

.lazyblog-ls__link.is-loading .lazyblog-ls__progress {
  position: absolute;
  right: 12px;
  top: 50%;
  display: inline-block;
  width: 14px;
  height: 14px;
  margin-top: -7px;
  border: 2px solid rgba(255, 255, 255, 0.28);
  border-top-color: #fff;
  border-radius: 999px;
  animation: lazyblog-spin 0.8s linear infinite;
}

.lazyblog-ls__link.is-error {
  background: #5a2018;
}

.lazyblog-ls__status {
  display: none;
  max-width: 240px;
  padding: 10px 14px;
  color: #f5f5f5;
  background: #111;
  box-shadow: 0 -1px 0 rgba(255, 255, 255, 0.08) inset;
}

.lazyblog-ls__status:not(:empty) {
  display: block;
}

.lazyblog-ls__status[data-state="loading"] {
  color: #f8e8b0;
}

.lazyblog-ls__status[data-state="ready"] {
  color: #b9f6ca;
}

.lazyblog-ls__status[data-state="error"] {
  color: #ffc4ba;
}

@keyframes lazyblog-spin {
  to {
    transform: rotate(360deg);
  }
}

.lazyblog-math--display {
  display: block;
  max-width: 100%;
  margin: 1.2em 0;
  overflow-x: auto;
  overflow-y: hidden;
}

.lazyblog-math--inline {
  overflow-wrap: normal;
}

@media (max-width: 640px) {
  .lazyblog-language-switcher {
    right: 12px;
    min-width: 144px;
    max-width: calc(100vw - 24px);
  }
}
CSS;
    }
}

register_activation_hook(__FILE__, ['LazyBlog_Translations', 'activate']);
register_deactivation_hook(__FILE__, ['LazyBlog_Translations', 'deactivate']);

LazyBlog_Translations::instance();
