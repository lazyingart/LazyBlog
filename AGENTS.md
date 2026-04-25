# Repository Guidelines

## Purpose
This is the LazyBlog toolkit. It contains reusable publishing, translation,
local WordPress testing, and WordPress plugin code.

## Repository Rules
- Keep `.env`, exported posts, generated chat logs, and local job state ignored.
- Use placeholders in examples. Do not commit real WordPress usernames, app
  passwords, API keys, bearer tokens, SSH usernames, or server IPs.
- Keep docs focused on reusable workflows and local setup.

## Validation
- Run `python3 -m py_compile scripts/*.py` after Python changes.
- Run `bash -n lazypub scripts/*.sh` after shell changes.
- Check for accidental secrets before publishing changes.
