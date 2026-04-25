<?php
/**
 * Configure local Docker WP QuickLaTeX to match blog.lazying.art.
 *
 * Run through WP-CLI:
 *   wp eval-file configure_local_quicklatex.php
 */

if (!defined('ABSPATH')) {
    fwrite(STDERR, "This script must run inside WordPress through WP-CLI.\n");
    exit(1);
}

$quicklatex_options = [
    'font_size' => '32',
    'font_color' => '000000',
    'bg_type' => '0',
    'bg_color' => 'ffffff',
    'latex_mode' => 0,
    'preamble' => "\\usepackage{amsmath}\r\n"
        . "\\usepackage{amsfonts}\r\n"
        . "\\usepackage{amssymb}\r\n"
        . "\r\n"
        . "\\usepackage{tensor}\r\n"
        . "\r\n"
        . "\\usepackage[makeroom]{cancel}\r\n"
        . "\r\n"
        . "\\usepackage{tikz}\r\n"
        . "\\usetikzlibrary{calc}\r\n"
        . "\\newcommand{\\tikzmark}[1]{\\tikz[baseline,remember picture] \\coordinate (#1) {};}\r\n"
        . "\r\n"
        . "\\usepackage{pgfplots}\r\n"
        . "\\usetikzlibrary{angles, quotes, arrows.meta}\r\n"
        . "\r\n"
        . "\\usepackage{MnSymbol,wasysym}\r\n"
        . "\r\n"
        . "\\usepackage{physics}",
    'use_cache' => '1',
    'show_errors' => 0,
    'add_footer_link' => 0,
    'is_preamble_corrected' => 1,
    'displayed_equations_align' => '0',
    'eqno_align' => '0',
    'latex_syntax' => 0,
    'exclude_dollars' => 0,
    'image_format' => '1',
];

update_option('quicklatex', $quicklatex_options);

if (class_exists('WP_CLI')) {
    WP_CLI::success('Configured WP QuickLaTeX with blog.lazying.art settings.');
}
