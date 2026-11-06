# Slack application manifest and installation

[slack-manifest.json](../slack-manifest.json) is the importable example manifest for The Ledger's single-workspace HTTP app. Replace every `LEDGER_HOST` with the public Cloudflare Tunnel hostname, without a scheme or path (for example `ledger.example.org`). It contains no credentials. Slack's [manifest reference](https://docs.slack.dev/reference/app-manifest/) defines the JSON format.

Check `/kudos` availability before installing; slash-command names can collide with other installed apps. Create an app **from a manifest** in the intended workspace, paste the modified JSON, review permissions, and install it. Save the bot OAuth token as `SLACK_BOT_TOKEN`, the Basic Information signing secret as `SLACK_SIGNING_SECRET`, the workspace ID as `SLACK_TEAM_ID`, and the bot's Slack user ID as `SLACK_BOT_USER_ID`. The last value is a `U...` user ID, not an `A...` app ID or `B...` bot ID; the bot token's `auth.test` response provides `team_id` and `user_id`.

Start web ingress and the tunnel using [deployment](DEPLOYMENT.md), then verify the Events API Request URL in Slack. Bolt responds to Slack's challenge. Enable Events and Interactivity as represented in the manifest; leave Socket Mode disabled. If updating an installed app's scopes, reinstall/re-authorize it before testing. The service uses a supplied bot token and does not implement an OAuth installation/refresh endpoint, so automatic token rotation is disabled in this example.

## Bot icon

Upload [icons/ledger-bot-512.png](../icons/ledger-bot-512.png) in the app's **Basic Information → Display Information → App icon** settings. This is The Ledger's anthropomorphic leatherbound grimoire artwork. Slack scales the uploaded icon for its clients; [icons/ledger-bot-36.png](../icons/ledger-bot-36.png) is the supplied mobile-size preview/export. The manifest sets a matching charcoal app-profile background. Icon upload is a separate installation step; it requires no additional runtime bot permissions. The [asset notes](../icons/README.md) include the original source, final prompt, and sizing guidance.

## HTTP endpoints and commands

| Configuration | HTTPS path |
| --- | --- |
| Events API Request URL | `/slack/events` |
| Every slash command | `/slack/commands` |
| Interactivity and option loading | `/slack/interactions` |

The manifest enables App Home and writable app messages. All seven commands have `should_escape: true` to preserve structured member mentions:

| Command | Function |
| --- | --- |
| `/ledger` | Consent, progress, sponsorship, channel invitations, feedback |
| `/ledger-skills` | Shop/tool skill trees and accessible text |
| `/ledger-quests` | Learning challenges, evidence, collaborative quests |
| `/ledger-mentor` | Success Buddies and verified mentoring |
| `/kudos` | Recipient-first thanks, optional public sharing and invitation |
| `/ledger-project` | Project gallery and feedback threads |
| `/ledger-admin` | Authorized configuration, verification, metrics, pause/resume |

All modals, buttons, checkboxes, and external shop/tool/member dropdowns use the interaction endpoint. They are interaction payloads, not additional event subscriptions or commands.

## Event subscriptions

| Bot event | Handler purpose | Scope used |
| --- | --- | --- |
| `app_home_opened` | Publish the participant's gallery/progress Home view | No additional event scope |
| `app_mention` | Threaded responses when The Ledger is addressed | `app_mentions:read` |
| `message.im` | Onboarding/opt-out DMs and conversations; message edits/deletions | `im:history` |
| `message.groups` | Authorized private-channel conversations and context edits/deletions | `groups:history` |
| `member_joined_channel` | Check consent/rank and remove unauthorized game-channel joins | `groups:read` for private channels |
| `member_left_channel` | Record voluntary departure and respect re-invitation rules | `groups:read` for private channels |
| `user_change` | Refresh active human identity/deactivation state | `users:read` |

Slack delivers `message.im` and `message.groups` as `type: "message"`; `message_changed` and `message_deleted` are subtypes, not separate manifest entries. Channel events require the bot to belong to the channel. The worker filters channel processing to registered Ledger private channels. See Slack's [channel membership event contract](https://docs.slack.dev/reference/events/member_joined_channel/) and [App Home event contract](https://docs.slack.dev/reference/events/app_home_opened/).

## Bot permissions

| Scope | Calls or events that need it |
| --- | --- |
| `commands` | All slash commands and their interactive flows |
| `app_mentions:read` | `app_mention` event |
| `chat:write` | `chat.postMessage` for DMs, notifications, kudos, project threads, and replies |
| `groups:read` | `conversations.info`, `conversations.members`, private-channel membership events |
| `groups:history` | `message.groups`, including edits/deletions; private project thread visibility |
| `groups:write` | `conversations.create` with `is_private`, `conversations.invite`, `conversations.kick` |
| `im:history` | `message.im`, including edits/deletions |
| `im:write` | `conversations.open` for recipient DMs and file delivery |
| `users:read` | `users.info` for human/active checks and `user_change` |
| `files:write` | Skill-tree image/text and rank art via `files.getUploadURLExternal` / `files.completeUploadExternal` (`files_upload_v2`) |

`groups:write` is needed for [private-channel removal](https://docs.slack.dev/reference/methods/conversations.kick/) as well as [invitations](https://docs.slack.dev/reference/methods/conversations.invite/); invite-only permission would not cover opt-out cleanup. [External file uploads](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/) require `files:write`. `views.open`, `views.update`, `views.publish`, and `chat.getPermalink` need an authenticated bot but no extra scopes beyond this set for the supported flows.

No user-token scopes, public-channel history/management, workspace administration, email lookup, reaction write, incoming webhooks, or app-level Socket Mode token are needed. Custom emoji shortcodes render in Slack without calling `emoji.list`. Ordinary app tokens do not bypass workspace policy on creating private channels or removing members; verify those permissions with workspace administrators during the pilot.

The bot must be invited to existing private game channels before `ledger bootstrap`. Bootstrap creates new private channels if none are supplied. Test join/leave reconciliation with real members: native manual invitations can briefly expose a private channel before removal, as documented in the accepted design.
