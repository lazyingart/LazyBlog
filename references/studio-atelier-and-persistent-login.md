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
- The PWA shell cache is `lazyblog-studio-v8-atelier`. Root HTML, login, and API
  responses remain outside the service-worker cache; auth responses additionally
  use `Cache-Control: no-store`.

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
