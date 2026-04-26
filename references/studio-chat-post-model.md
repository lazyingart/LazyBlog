# LazyBlog Studio Chat And Post Model

LazyBlog Studio should treat chat sessions and posts as separate objects.

## Current Buttons

- `Send & Store`: saves the user message into `content/chat/<session>/messages/` and asks the reply tool to answer. It is chat memory, not a WordPress post action.
- `Draft Post`: runs the heavier task tool and writes Markdown locally. Today it writes under `content/drafts/<session>/`; it does not publish to WordPress.
- `Draft and Publish`: sends the selected/latest Markdown draft to WordPress. The status dropdown controls the WordPress target state:
  - `draft`: create/update a WordPress draft.
  - `publish`: create/update a public post.
  - `private`: create/update a private WordPress post.

## Implemented Model

LazyBlog Studio now keeps the writing workflow deliberately split:

- `ChatSession`: durable memory and context only.
- `PostProject`: independent article object that can be drafted, revised, published, private, or updated many times.
- `WordPressPost`: the remote WordPress entity, optionally linked to a `PostProject` by `post_id`.
- `CategoryMirror`: local snapshot of WordPress categories used by the reply and draft tools.

One chat session can create many post projects. One post project can use one or more chat sessions as background. A post should not be owned by a chat session.

The practical rule is: chat can suggest or remember, but post/category mutations go through explicit APIs. This prevents the reply tool from doing open-ended site maintenance or claiming an action was completed when no controlled endpoint ran.

## Storage

```text
content/chat/<session-id>/
  session.json
  messages/*.md

content/studio-posts/<post-project-id>/
  post.json
  drafts/<revision>.md
  publish-events/<timestamp>.json

content/taxonomy/categories.json
```

`post.json` should include:

```json
{
  "id": "20260426-example",
  "title": "Example",
  "source_language": "en",
  "categories": ["Writing", "Journals"],
  "tags": [],
  "source_sessions": ["20260425-152731-20783ef7"],
  "current_draft": "drafts/20260426T001000Z.md",
  "wordpress": {
    "post_id": null,
    "status": "local_draft",
    "link": ""
  }
}
```

`content/taxonomy/categories.json` is the category mirror. It is refreshed from WordPress and includes `term_id`, `slug`, `name`, `parent`, `description`, `count`, and `link`.

## Controlled APIs

- `GET /api/categories?search=&sync=1`: search the category mirror; `sync=1` pulls fresh terms from WordPress first.
- `POST /api/categories/sync`: refresh `content/taxonomy/categories.json`.
- `POST /api/category`: create or reuse a category. Payload: `name`, optional `parent`, `slug`, `description`.
- `POST /api/category/update`: update category fields. Payload: `category` or `id`, plus `name`, `slug`, `parent`, `description`.
- `POST /api/category/delete`: delete a category by `category`, `id`, `slug`, or `name`.
- `POST /api/posts`: create a local post project from a chat session or instruction.
- `GET /api/posts?session_id=`: list post projects, with active/current-session projects sorted first.
- `GET /api/post?id=...`: load one post project and its current draft.
- `POST /api/post/select`: set or clear the active post project for a chat session.
- `POST /api/post/select-source`: resolve a pasted URL, post id, or search words to a WordPress post, pull/sync the local mirror, create or update a linked `PostProject`, and select it for the current chat.
- `POST /api/post/draft`: generate or revise Markdown for the selected post project. If no post is selected, it creates a new post project from the session context.
- `POST /api/post/publish`: publish the selected post project. If it already has `wordpress.post_id`, it updates that post; otherwise it creates a new WordPress post.
- `POST /api/post/link`: attach an existing WordPress post id to a local post project.

Legacy `/api/draft` and `/api/publish` still exist for compatibility, but the Studio UI uses the post-project endpoints.

## Prompt Tools

- `web-action-router.txt` runs on `gpt-5.3-codex-spark` with medium reasoning. It classifies chat messages into one bounded action: `select_post`, `create_category`, `sync_categories`, or `no_op`.
- `web-git-commit-push.txt` also uses `gpt-5.3-codex-spark` medium through `codex exec`. The backend generates an allowlisted shell script with exact paths, then Codex runs only that script. This keeps commit/push deterministic while still using the Codex prompt tool.

If a chat message contains a blog/WordPress post URL, the action router selects the post object before the reply tool answers. The publish panel then shows the linked `PostProject` and its local mirror path so the next chat/draft action edits the selected object.

## UI Behavior

The publish panel has a post selector. Selecting a post makes it the active `PostProject` for the current chat. Choosing the blank `Auto new post from this chat` option clears the active post; the next `Draft Post` call will create a new project from the current chat.

Button behavior:

- `Send & Store`: writes a chat message and runs the reply tool only.
- `New Post`: creates an empty managed `PostProject` from the current chat and extra instruction.
- `Draft Post`: drafts or revises the selected `PostProject`; if none is selected, creates one first.
- `Publish Selected`: creates or updates the linked WordPress post using the selected status.
- `Force Redraft`: drafts again before publishing.
- `Sync` in Categories: refreshes the local category mirror from WordPress.

The selected post card should show the post project id, title, WordPress id/status/link, category chips, source language, source chat count, and local mirror path. This makes the selected editing target explicit before any draft or publish action runs.

The extra instruction field is intentionally part of the controlled draft/publish call. For example, writing `make this a journal, category Journals` is passed to the task tool and can affect title, category, tone, and whether the post revises the selected project or becomes a new one.

## Philosophy

The app should stay flexible but not autonomous in an unsafe way. Codex tools are allowed to draft, analyze, and suggest. Site mutations happen through a small, named API surface: post create/select/draft/publish/link and category sync/create/update/delete. This keeps the system easy to reason about when a chat says something like “create a journal from today” or “add this under Writing/Journals”: the chat provides context, the post selector identifies the managed article, and the API call performs exactly one bounded action.
