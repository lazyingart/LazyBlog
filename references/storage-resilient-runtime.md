# Storage-resilient Studio runtime

Keep production LazyBlog Studio and LocalSTT checkouts on a reliable service
volume that is independent of large project or archive storage. Keep the
content database, chat queue, uploads, and generated Markdown under the private
Studio runtime. LocalSTT should retain recordings, job records, and transcripts
in a separate private state directory.

The startup supervisor should start one LocalSTT user service, with a single
tmux fallback only when user systemd is unavailable, and one LazyBlog Studio
process on its loopback port. A reverse proxy should continue to target those
loopback ports so moving a checkout does not change a public URL.

Queue discovery treats a malformed or unreadable JSON record as an isolated
record failure. Other queue items continue to run. This protects the worker
from one damaged file, but it does not make an unhealthy filesystem safe: NVMe
timeouts, an aborted filesystem journal, or kernel `EIO` messages require an
offline storage repair before the affected volume is used again.

## Verification

Verify both loopback health routes and the public Studio route. An audio
verification must also exercise the authenticated Studio upload endpoint, wait
for the speech job to succeed, and confirm that the resulting user message and
assistant reply both reach `succeeded`. A health response alone is not
sufficient.
