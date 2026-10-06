# Studio Atelier design and persistent login

## Purpose and boundaries

The October 2026 refresh gives the writing workspace a warm editorial identity:
ivory paper, ink typography, vermilion actions, and a light sage user-message
surface. Display headings use local serif fonts; controls use system sans-serif.
No remote font, new framework, or additional proxy route is required.

This changes Studio, not WordPress themes or the translation plugin. Model
routing, publishing permissions, audio processing, Markdown storage, and the
SQLite message ledger retain their existing contracts.

## Implementation

- `web/studio-theme.css` contains the scoped visual layer. The server reads it
  once at startup and embeds it in both workspace and login HTML. Deploy this
  file together with `scripts/lazyblog_webapp.py`, then restart Studio.
- Desktop retains conversation / writing / publishing columns. At tablet widths
  the publishing tools move into a drawer. Phones have a fixed-height workspace,
  scrollable transcript, bottom composer, and history/publishing drawers.
- The blank conversation has three starter chips. They only populate the input
  and trigger normal draft autosave; they never send or publish automatically.
- Categories and background activity live in expandable sections. Settings,
  attachments, both microphone modes, quote actions, and artifact previews keep
  their existing IDs and API contracts.
- A session-view revision prevents late history/autoload/background refresh
  responses from replacing a new or differently selected conversation.
- The PWA shell cache is `lazyblog-studio-v9-atelier-icons`. Root HTML, login, and API
  responses remain outside the service-worker cache; auth responses additionally
  use `Cache-Control: no-store`.

### App icons

`web/icons/lazyblog.svg` is the canonical ivory-on-vermilion `l.` monogram.
The opaque 192px and 512px PNGs are built from that vector, not drawn separately.
The mark stays inside the central maskable safe circle; the operating system
supplies its own rounded-square/circular mask. The server reads the committed
PNGs and caches those bytes in memory, so it needs no imaging dependencies.

Rebuild with `bash scripts/build_studio_icons.sh` (ImageMagick is needed only
on the build machine). Commit all three assets with any source change.

The manifest icons, shortcut icons, favicon, login page, and workspace reference
the same `APP_ICON_VERSION` query value. Both HTML pages use a PNG
`apple-touch-icon` rather than the formerly unsupported SVG reference. Existing
proxy paths stay unchanged. The manifest URL, start URL, and app identity stay
stable; the manifest is network-first with an offline cached fallback, and its
HTTP response requires revalidation. The new service worker reloads its shell
assets and removes previous cache versions.

Deploy the `web/icons/` directory with the server. Verify the public manifest and
each versioned icon URL against local bytes. Existing OS home-screen shortcuts
may retain their previous artwork after the website has updated; icon assets
being live is not proof that a particular phone has refreshed its shortcut.
Do not clear site data to force an icon update: it may remove unsynced local
drafts and login state.

## Compact phone workspace (October 6 refinement)

The mobile surface prioritizes the conversation, not explanatory UI text. At
720px and below there is one header with the conversation title, history menu,
artifact folder, and settings. The repeated brand header, message-count/path
metadata, model badge, welcome illustration, and introductory paragraphs are
not shown on the main phone surface. History and model configuration remain
available through their existing controls. Desktop keeps its three-column layout.

The composer is a two-row CSS grid using the existing input and handlers:

- Attach, auto-growing message input, and a 44px send arrow.
- Native dictation, audio message, short sync status, Draft, and Posts.

The textarea retains a 16px font to avoid focus zoom, starts at 44px, and grows
up to 124px. Message text remains 14px. A visual-viewport resize/scroll listener
keeps the phone shell within the visible viewport, with safe-area padding;
pinch-zoom changes are not treated as keyboard resizing. No polling or new
network requests are added by this layout.

Quote previews and horizontally scrollable attachment chips/thumbnails appear
above the input only when needed. Message-level quote controls remain; the
duplicate composer shortcut is hidden on phones. Attachment details (MIME,
analysis status, notes) are expandable instead of repeated below every preview.
The raw Markdown draft is collapsed in the Posts drawer; post selection,
WordPress ID/status, categories, and publishing controls retain their behavior.
Draft and Posts are not renamed publish actions: Draft prepares Markdown; Posts
opens the existing publishing controls without submitting anything.

Short status is not a claim that every draft reached the server. “On device”
means a local-only/recovered copy; “Saved” is used for the normal server-save
result. Tap the status to read its full text. Errors automatically expand the
full explanation and use “Check sync”; that automatic expansion closes after
recovery, while a manually opened detail panel stays open. Autosave, queueing,
audio, publishing, and authentication endpoints are unchanged.

Browser measurements at **390 × 844**, empty composer, no safe-area inset:

| Area | Previous | Compact |
| --- | ---: | ---: |
| Header area | 127px | 57px |
| Composer | 207px | 101px |
| Conversation viewport | 510px | 686px |

This is about 35% more vertical conversation space. Actual device keyboards,
safe areas, open attachment/quote previews, and longer input change the numbers.
The synthetic screenshot [compact mobile workspace](../demos/studio-compact-mobile.png)
shows the revised layout; it contains no personal chat data.

Regression coverage includes compact/local-only/error status behavior, manual
detail expansion, real DOM send/reply, quote/clear, multiple file selections and
removal, textarea growth/shrink, autosave failure followed by reload/recovery,
dictation-language long press, centered settings, and drawer open/close. Browser
viewports tested: 320×568, 390×844, 430×932, 720×844, 768×844, 1024×768, 1500×940,
390×430, and 667×375. The short viewports exercise keyboard-like/landscape space
constraints; they are not a claim of physical iOS/Android keyboard testing.

Deploy the Python template and embedded CSS together, as for the original
Atelier release. Root HTML is not service-worker cached, so reload the app once;
do not clear site data or uninstall the PWA, which could discard unsynced drafts.

## Persistent login

“Keep me signed in on this device” is checked by default.

| Choice | Browser cookie | Server expiry | Renewal |
| --- | --- | --- | --- |
| Keep signed in | `Max-Age=7776000` (90 days) | 90 days | Successful HTML/JSON responses extend it after at least one day |
| Unchecked | Session cookie; no `Max-Age` | 24 hours | None |

The signed payload contains the account, expiry, and persistence mode. Changing
any field invalidates the HMAC. Existing three-part cookies from the previous
30-day scheme remain valid and are upgraded on the next successful response.
An already authenticated visitor to `/login` is redirected to the workspace.

The signing key is the existing `LAZYBLOG_STUDIO_LOGIN_TOKEN`; it must stay
stable across app restarts and deployments. **Do not regenerate it during a
routine restart.** Changing the key invalidates all existing sessions.

The login secret is never stored in JavaScript localStorage. Authentication uses
an HttpOnly, host-only, SameSite=Lax cookie. Keep
`LAZYBLOG_STUDIO_SECURE_COOKIE=true` on public HTTPS deployments and retain
cookie forwarding through the reverse proxy. Local HTTP previews can explicitly
disable Secure without changing production.

The sidebar has a Sign out control. It saves unsent text before clearing this
browser's auth cookie, and reports a sync failure instead of pretending to have
signed out. Logout must not emit a renewal cookie in the same response. Local
draft copies are retained for recovery, so prefer a personal device. This
stateless-cookie design does not remotely revoke a copied cookie on logout;
rotate the signing key to revoke all sessions if a device or token is compromised.

Clearing browser data, private browsing, expiry, and account-key changes can still
require login. Separate browsers or an installed PWA may have separate cookie
stores; persistence does not promise automatic cross-browser sign-in. Browsers
with session restoration can also restore session cookies, so sign out explicitly
on shared devices instead of relying on closing the window.

## Verification

Run:

```bash
python3 -m unittest discover -s tests -p 'test_lazyblog_webapp*.py'
python3 -m py_compile scripts/lazyblog_webapp.py
git diff --check
./blogctl doctor
```

Auth tests cover legacy-cookie migration, persistence mode, renewal, signature
tampering, expiry, key rotation, HTTP login/redirect/logout, and no-store headers.
UI tests check unique IDs, event-handler targets, inline JavaScript syntax, and
stale asynchronous history responses.

Browser review uses the actual Studio server with a separate temporary content
root, synthetic notes, `--mock-codex`, and `--no-commit-push`. It must not load the
production `.env`, use real chat history, or publish to WordPress. Review checks:

1. Login, remembered-cookie attributes, return to `/login`, logout, and unchecked
   session-cookie login.
2. Close the entire browser and relaunch the same dedicated profile: remembered
   login should still open the workspace.
3. New conversation, starter chip, Send & Store, and deterministic mock reply.
4. Markdown/formula rendering, history selection, centered action modal,
   settings, and opening/closing the publishing drawer without publishing.
5. Widths 320, 390, 720, 768, 1024, 1200, and 1500: no horizontal overflow and
   Send remains in the viewport.

The screenshots in `demos/studio-atelier-*.png` contain synthetic review content
only. They are not production chat captures.

## Release and rollback

Release code, tests, documentation, and synthetic screenshots together. Preserve
unrelated work and keep `.env`, uploads, browser profiles, SQLite databases, and
raw conversation history out of release artifacts.

Apply the same scoped commit to the live Studio checkout. Before restarting,
check for active jobs/children, preserve its `.env` and content directory, and
reuse its existing tmux pane and start script. Do not restart the WordPress
translation service or the reverse-tunnel services for a visual change.

Afterward, test the public HTTPS login page, authenticated workspace, health,
cookie flags, and anonymous API denial. A rollback must restore the Python file
and theme together. Older code cannot read the new four-part cookie, so rolling
back to the previous release requires a fresh login.
