# LazyBlog Studio Chat And Post Model

LazyBlog Studio should treat chat sessions and posts as separate objects.

## Current Buttons

- `Send & Store`: saves the user message into `content/chat/<session>/messages/` and asks the reply tool to answer. It is chat memory, not a WordPress post action.
- `Draft Post`: runs the heavier task tool and writes Markdown locally. Today it writes under `content/drafts/<session>/`; it does not publish to WordPress.
- `Draft and Publish`: sends the selected/latest Markdown draft to WordPress. The status dropdown controls the WordPress target state:
  - `draft`: create/update a WordPress draft.
  - `publish`: create/update a public post.
  - `private`: create/update a private WordPress post.

## Target Model

The cleaner model is:

- `ChatSession`: durable memory and context only.
- `PostProject`: independent article object that can be drafted, revised, published, private, or updated many times.
- `WordPressPost`: the remote WordPress entity, optionally linked to a `PostProject` by `post_id`.

One chat session can create many post projects. One post project can use one or more chat sessions as background. A post should not be owned by a chat session.

## Proposed Storage

```text
content/chat/<session-id>/
  session.json
  messages/*.md

content/studio-posts/<post-project-id>/
  post.json
  drafts/<revision>.md
  publish-events/<timestamp>.json
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

## Target API

- `POST /api/posts`: create a new post project from a chat session or direct prompt.
- `GET /api/posts`: list/manage post projects independent of chat sessions.
- `GET /api/post?id=...`: load one post project and its current draft.
- `POST /api/post/draft`: generate or revise Markdown for a selected post project.
- `POST /api/post/publish`: create a new WordPress post or update an existing `post_id` with status `draft`, `publish`, or `private`.
- `POST /api/post/link`: attach an existing WordPress post to a local post project.
- `POST /api/category`: create or reuse taxonomy terms, with optional parent category.

## Target UI

The publish panel should have a post selector:

- `New post from this chat`
- existing post projects
- existing WordPress post id/manual link

Then each action is explicit:

- `Draft selected post`
- `Publish as new WordPress post`
- `Update linked WordPress post`
- `Change status: draft / publish / private`

This makes it possible to write a post at any stage of a conversation, create multiple posts from one chat, and later update posts created from other chat sessions.
