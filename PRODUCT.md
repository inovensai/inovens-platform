# INOVENS'AI

INOVENS'AI is a Turkish web and Telegram workspace for an invited community pilot. It combines private personal conversations, shared community work, board records, controlled Google actions, document delivery, usage accounting, and operational monitoring.

## People and permissions

- The project owner controls invitations, roles, quotas, budgets, service actions, audit records, and the incident center.
- The president, vice president, and board members can use the shared community workspace, manage community work and board decisions, and approve their own outbound Google actions.
- Members can use their private workspace and public community information. They cannot read board documents, Google content, other users' conversations, or management records.
- Web and Telegram use the same identity, quota, conversation history, and permissions. Each user's personal content remains isolated.

## Core workflows

- Users can chat naturally, upload supported documents and images, receive generated files directly in web or Telegram, and download saved files from the panel.
- The archive separates personal, community, board, and bot memory. Course and exam files carry academic year, grade, semester, course, and exam metadata; member submissions enter board review before community publication.
- Personal notes, ideas, and dated reminders remain private. A user can share an owned personal document with one selected active user; the recipient must accept before access opens. The associated message thread is quiet and creates no unsolicited notification.
- Current or dated research must use web tools and cite sources. Requested PDF, DOCX, spreadsheet, presentation, and image outputs must be created, inspected, and attached before completion is reported.
- Community work supports date ranges, account-based assignees, due-date reminders, notes, visibility, status, editing, and deletion. Board decisions have a dated archive and implementation status.
- Gmail sending and Drive writes create a preview and require the requesting authorized user's approval. Read access is limited to authorized board roles.
- The owner-only GitHub broker is isolated from AI-Ege's account and can inspect or create INOVENS repositories, issues, pull requests, comments, and Actions records.

## Routing and privacy

- General, non-sensitive content uses the fast route: Antigravity Gemini with Muse Contributor fallback.
- Sensitive or uncertain content uses a pinned Antigravity Gemini route that contains no Contributor model. The application verifies the configured providers and models but does not claim provider zero retention.
- Credentials, access tokens, authorization headers, and system secrets are blocked from all model calls and masked in operational logs.
- Conversations are retained for 30 days, usage for 90 days, and audit records for 180 days. Server backups retain deleted data for up to 7 days; encrypted disaster-recovery copies retain it for up to 30 days.
- The pilot consent text states that Telegram and model providers may process data outside Türkiye. Legal controller details and the final school-wide privacy notice must be completed before a broad launch.

## Reliability

- Failed tool calls are classified, surfaced in the owner incident center, retried only when safe, and verified with health and model probes.
- Uploads are inspected by file signature and bounded parsing before storage. Generated documents are rendered and visually checked before delivery.
- Runtime changes pass the full server test suite and frontend production build before deployment.
- Encrypted backups are authenticated by a real disposable-database restore, copied to the owner's Mac, and stored offsite without the encryption key.
- Personal project folders are synchronized bidirectionally between the Mac and home server; rebuildable caches, dependencies, logs, and repository metadata are excluded.
- Telegram requires a separate button-based processing notice before accepting chat or files. Its command menu exposes personal, community, board, archive, idea, reminder, sharing, quota, model, and job controls.

There is no payment flow in the current invited pilot.
