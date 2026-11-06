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
| `/kudos` | Thanks for linked members with or without opt-in; optional emoji, public sharing and invitation |
| `/ledger-project` | Project gallery and feedback threads |
| `/ledger-admin` | Opted-in authorized staff/delegates only; role-scoped help and actions |

All modals, buttons, checkboxes, and external shop/tool/member dropdowns use the interaction endpoint. They are interaction payloads, not additional event subscriptions or commands.

`/ledger-skills` hashes the exact accessible text equivalent and caches the generated PNG per member in `ledger_files`, along with the text checksum and cache timestamp. A later request reuses the Slack file through a Block Kit `slack_file` image when the text is unchanged and Slack still recognizes the file; changed text or a missing file triggers a new render and upload. Rank icons use the same collection and are checked before display; a permanently missing Slack file is evicted and replaced from the bundled PNG. Slack file retention or manual deletion therefore causes a fresh upload on the next request.

On promotion, no rank-up announcement is sent to shared Ledge Chat. The prior-rank channel receives a short ascent message that does not name or imply the next rank. The bot invites the member to the new-rank channel, then removes them from the prior-rank channel and posts the welcome after the invite succeeds. The shipped `rank_up.json` shared-audience variations generate these private channel messages from a generic stage summary and the member's name; the next-rank label is not passed into those generations.

## Saved opt-in and chat replies

The consent modal saves `ledger_participants.opted_in` synchronously and displays **Opt-in saved** after the transaction succeeds. Repeating `/ledger join` or clicking an old invitation while still opted in shows **Already opted in**, current rank/XP, and any pending history import. It does not reset consent, rules, progress, or invitations. Opting out and joining again still shows the consent form. Older images always reopened consent even for a saved participant; that alone did not mean consent was lost.

HTTP 200 for a chat event confirms receipt, not that a reply has been generated. The complete path is:

`ledger-web → ledger_inbox → ledger-accounting → ledger_outbox → ledger-interactive → Slack`

`ledger-channels` separately handles invitations/removals, `ledger-results` handles consolidated results, and `ledger-delivery` handles routine notices and artwork. Run these alongside accounting, interactive, and web. If accounting is absent, chat events and history imports remain queued; welcome messages wait for the missing import. Current workers report `HistoryImportPending`, defer the welcome without spending delivery retries, and log `check ledger-accounting`. Older images reported only `RuntimeError` and could exhaust their retries. Already-failed jobs need the targeted retry procedure in [operations](OPERATIONS.md#recovery); do not erase consent or re-award XP.

```sh
docker compose up -d --build --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive
docker compose ps --all
docker compose logs --since=5m ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive
```

Accounting logs `Slack event processed ... outcome=reply_queued` when it queues a reply; otherwise it reports a fixed reason such as `ignored_unlinked_identity`, `ignored_unjoined_channel`, or `ignored_unaddressed_channel_message`. No message body is logged. Pending `slack_event` inbox jobs point to accounting; pending `conversation`/`guidance` jobs point to interactive; `summary_flush`/`summary_delivery` jobs point to results. Check workers use the same `LEDGER_URI`/`LEDGER_DATABASE` as web if queued jobs never appear to them. Check Slack errors such as `missing_scope` or `invalid_auth` in the responsible worker logs, and reinstall the Slack app after scope changes.

Linked human members may chat in DMs without opting in. In any channel the bot has joined, address The Ledger by name or Slack mention, or continue a thread containing a bot reply or a bot-authored root post. Only registered Ledger channels examine unaddressed messages containing `?` or recognize self-directed progress requests. Unrelated ambient messages in other channels are neither saved as chat context nor sent to inference; old ambient jobs are rejected before inference, and unrequested stored history is excluded from addressed replies there. Ordinary unaddressed chatter receives no reply. Nonparticipants may ask about The Ledger and XP generally and known shops/tools, with an optional `/ledger join` journey invitation; rules, specific ranks/quests and retained personal progress stay unavailable. Participants receive accurate current/lower rank details and next-rank requirements, with higher-rank names/details and inaccessible quests excluded from conversation facts. Unknown answers, including unavailable shop/tool facts, say "I don't know." Chat narration does not itself execute administrative commands or award XP; use the command/forms for actions. Ensure `message.im` and `app_mention` subscriptions, `message.groups` for private-channel replies and `message.channels` for joined public channels, and the matching manifest scopes are installed. `SLACK_BOT_USER_ID` must be the bot's `U...` user ID.

## Troubleshooting "the app did not respond"

Slack needs an HTTP acknowledgment within three seconds of invoking a command ([Slack's command guide](https://docs.slack.dev/interactivity/implementing-slash-commands/)). Healthy `/health` and `/ready` responses do not verify this path: readiness checks Mongo connectivity and Ledger replica-set topology, not collection read/write authorization, callback reachability, or worker delivery.

1. In the Slack app's **Slash Commands**, check each command's Request URL is exactly `https://YOUR_HOST/slack/commands`. Event subscriptions and interactivity have their own URLs above. Leave Socket Mode disabled. Editing the repository manifest alone does not update an installed Slack app.
2. Send an **unsigned** probe through the public hostname (replace `YOUR_HOST`):

   ```sh
   curl -i --max-time 5 -X POST 'https://YOUR_HOST/slack/commands' \
     -H 'Content-Type: application/x-www-form-urlencoded' --data 'command=%2Fledger'
   ```

   Expect **401** from Bolt; this request cannot run a command. A redirect/login, challenge, 404, or gateway error indicates a routing/access issue. Cloudflared's origin is `http://ledger-web:3000`, with paths preserved; Slack must reach callback paths without an Access login or browser challenge. A 401 proves unsigned traffic reaches verification, not that Slack's signing secret is correct.
3. Reproduce one real `/ledger` invocation while watching:

   ```sh
   docker compose logs --follow --since=5m ledger-web cloudflared
   ```

   The supplied image logs method, path, HTTP status, and duration, without query strings, headers, or bodies. Callback failures and responses taking at least 2.5 seconds also produce a warning. Listener errors record exception class and numeric Mongo error code without the potentially sensitive exception text. To install these diagnostics from an older image, run `docker compose up -d --build --no-deps ledger-web` first.

   | Result for the real Slack request | Next check |
   | --- | --- |
   | No web access-log entry | Slack Request URL, Socket Mode, Cloudflare route/Access/WAF; ensure the image includes access logging |
   | 401 | `SLACK_SIGNING_SECRET` from this app's Basic Information, host clock, and unmodified request body |
   | 403 with `Workspace not allowed` | `SLACK_TEAM_ID` must match the installed workspace's `T...` ID |
   | 404 | Exact callback path; remove trailing slash or unintended path prefix |
   | 500/503 | Safe listener/ingress error record; `OperationFailure code=13` means Mongo authorization failed. Check source reads and Ledger writes using [the role examples](MONGODB_ACCESS.md), and complete initialization/bootstrap |
   | 200 taking around three seconds or more | Database lookup/transaction latency or synchronous Slack modal API calls; AI generation is outside ingress |
   | Prompt 200, but no later DM | Inspect `ledger-accounting` and `ledger-delivery` logs and the inbox/outbox; acknowledgment succeeded |

After changing `.env`, recreate the affected containers with `docker compose up -d --no-deps ledger-web ledger-accounting ledger-delivery ledger-channels ledger-results ledger-interactive`; a plain restart does not apply changed Compose environment values. Do not disable signature verification or acknowledge failed persistence to hide the error. Share only redacted diagnostic lines; keep request payloads, signing secrets, tokens, Mongo URIs, and `response_url` private.

## Event subscriptions

| Bot event | Handler purpose | Scope used |
| --- | --- | --- |
| `app_home_opened` | Publish the first-open placeholder and queue the personalized Character Sheet | No additional event scope |
| `app_mention` | Threaded responses when The Ledger is addressed | `app_mentions:read` |
| `message.im` | Onboarding/opt-out DMs and conversations; message edits/deletions | `im:history` |
| `message.groups` | Joined private-channel conversations and context edits/deletions | `groups:history` |
| `message.channels` | Joined public-channel questions, mentions and existing bot threads | `channels:history` |
| `member_joined_channel` | Check consent/rank and remove unauthorized game-channel joins | `groups:read` for private channels |
| `member_left_channel` | Record voluntary departure and respect re-invitation rules | `groups:read` for private channels |
| `user_change` | Refresh active human identity/deactivation state | `users:read` |

Slack delivers `message.im` and `message.groups` as `type: "message"`; `message_changed` and `message_deleted` are subtypes, not separate manifest entries. Channel events require the bot to belong to the channel. The worker filters channel processing to registered Ledger private channels. See Slack's [channel membership event contract](https://docs.slack.dev/reference/events/member_joined_channel/) and [App Home event contract](https://docs.slack.dev/reference/events/app_home_opened/).

The first Home-tab open publishes a brief processing placeholder and queues a personalized Character Sheet. Generated views are bound to the current member record and consent generation; an identity reassignment or rejoin causes a rebuild before the saved view is reused. Verified milestones, rank changes, and staff rank corrections queue Home refreshes. Ordinary XP ticks do not.

## Bot permissions

| Scope | Calls or events that need it |
| --- | --- |
| `commands` | All slash commands and their interactive flows |
| `app_mentions:read` | `app_mention` event |
| `chat:write` | `chat.postMessage` for DMs, notifications, kudos, project threads, and replies |
| `channels:read` | `conversations.info` verifies the bot is a member of a public channel before chat delivery |
| `channels:history` | Receives `message.channels` in public channels the bot has joined |
| `groups:read` | `conversations.info`, `conversations.members`, private-channel membership events |
| `groups:history` | `message.groups`, including edits/deletions; private project thread visibility |
| `groups:write` | `conversations.create` with `is_private`, `conversations.invite`, `conversations.kick` |
| `im:history` | `message.im`, including edits/deletions |
| `im:write` | `conversations.open` for recipient DMs and file delivery |
| `users:read` | `users.info` for human/active checks, `user_change`, and complete bounded `users.list` name/alias reads for quest-inspiration redaction |
| `files:write` | Skill-tree image/text and rank art via `files.getUploadURLExternal` / `files.completeUploadExternal` (`files_upload_v2`); reusable skill trees and rank icons are retained by Slack file ID for later Block Kit `slack_file` display |

`groups:write` is needed for [private-channel removal](https://docs.slack.dev/reference/methods/conversations.kick/) as well as [invitations](https://docs.slack.dev/reference/methods/conversations.invite/); invite-only permission would not cover opt-out cleanup. [External file uploads](https://docs.slack.dev/reference/methods/files.getUploadURLExternal/) require `files:write`. `views.open`, `views.update`, `views.publish`, and `chat.getPermalink` need an authenticated bot but no extra scopes beyond this set for the supported flows.

No user-token scopes, public-channel history/management, workspace administration, email lookup, reaction write, incoming webhooks, or app-level Socket Mode token are needed. Custom emoji shortcodes render in Slack without calling `emoji.list`. Ordinary app tokens do not bypass workspace policy on creating private channels or removing members; verify those permissions with workspace administrators during the pilot.

The bot must be invited to existing private game channels before `ledger bootstrap`. Bootstrap creates new private channels if none are supplied. Test join/leave reconciliation with real members: native manual invitations can briefly expose a private channel before removal, as documented in the accepted design.

## Progress and quest interactions

`/ledger stats`, `/ledger progress`, `/ledger preferences`, and `/ledger achievements` open deterministic private views. `/ledger-quests list` uses external-select title options, with hash-protected updates and revalidated selections/actions. `/ledger-quests create` provides editable asynchronous Help draft with The Ledger suggestions; interaction acknowledgments never wait for inference. All authored member-facing copy uses The Ledger/The System, preserving original member text. See [member routes and deployment checks](ENGAGEMENT_QUESTS.md).

## Kudos emoji and consent

The picker begins with silver star (`:kudo:`), gold star (`:kudos:`), fixed it! (`:fix_parrot:`), First place (`:first_place_medal:`), fistbump! (`:fistbump:`), used the force (`:duct_tape:`), Thank You (`:thankyou:`), and Teamwork! (`:teamwork:`). Custom shortcodes refer to the workspace's existing emoji.

The `/kudos` sending modal offers an optional emoji picker. Its selection is saved with the submission and survives shop/recipient changes and retries. Negative/offensive selections (including `:poop:`, `:-1:`, `:middle_finger:`, `:clown_face:`, aliases and tone variants) and unknown picker values are silently dropped. The selected emoji and a space precede "You have received kudos from <SENDER>" in the recipient DM; SENDER is a validated Slack mention. The public Ledge Chat header includes the same emoji, recipient and sender. Authored formatting and emoji in the message body remain untouched and are never sent to inference.

Both sending and receiving are available without game opt-in. Recipient participation still controls XP; one giver/recipient/week and five recipient/day qualifying XP caps still apply, and extra thanks still deliver. Nonparticipant senders can request public delivery and an invitation, but do not become recruitment sponsors. Requested acknowledgments/receipts are delivered without opt-in. Public-channel scopes and subscriptions have changed: update the manifest and reinstall the app before verifying joined-channel chat.

New kudos submissions send one giver result after requested destinations finish, or a plain pending/partial result at 60 seconds that later edits in place. Python renders recipient, destination outcomes, and actual once-only XP in a compact paragraph. Successful results may append a restrained sentence from `delivery.json`; the original kudos body and recipient progression are excluded from inference. Existing acknowledgement/receipt jobs retain their reserved flow. Customize with `/ledger-admin template delivery member` or explicitly adopt the updated file via `/ledger-admin template-library delivery member`. Saved choices/text remain stable on retries. Game results and the progress modal offer caller-only “Suggest a next step”; result replies use that DM's thread. See [result summaries](RESULT_SUMMARIES.md) and [prompt variables](PROMPTS.md).

Review notifications post and update through `chat.postMessage`/`chat.update`, both covered by existing `chat:write`. `LEDGER_QUEST_REVIEW_CHANNEL_ID` must name a private, non-external staff channel separate from game channels, with the bot invited. A deleted saved review message is replaced only when update returns `message_not_found`; permission and rate-limit failures use outbox retry.

Review reconciliation selects pending activities, unresolved failed/cancelled notice jobs, channel-mismatched addresses and indexed dirty notices closed while channel configuration was absent, including legacy addresses nested in contributions. Current fingerprints and already queued deliveries cause no activity transaction or rewrite. Resolved failed jobs retain their audit records with a reconciliation marker. New parent channel/dirty markers and indexes support selective recovery; run `python -m ledger init` after updating to initialize the indexes before normal service operation. Closed historical activities with no prior notice are not backfilled.

`/ledger-admin review` also lists pending cooperative contributions with quest IDs, member mentions and `verify-quest` commands, plus completion-ready shared projects with `complete-quest` commands. Each row requires the caller's current completion capability/scope and independence from contributors. Shared readiness requires two eligible verified contributors covering every discipline; current prerequisites and quest availability still apply. Delegates can discover authorized work without joining the private review channel; listing grants no additional authority or channel membership.

## Administrative access and invitations

Application Home/help, command responses, and generated private chat show administrative commands only to eligible opted-in humans. Source MLAB members role admin enables /ledger-admin at every rank, including slot zero. Board members retain global authority, resource managers retain shop scope, and active delegates retain granted review scope; all require current participation for this command. Rank or a role claim in chat never grants access. Ineligible invocations return an account-unavailable response without administrative help. Queued help is rechecked before delivery.

Use /ledger-admin help for available actions and /ledger-admin invite to open a recipient picker, optional sender name, and optional personal message. The picker excludes opted-in, revoked/suspended, merged and invalid human identities, and submission/delivery recheck eligibility. A blank sender uses the admin source first/last name. A supplied sender replaces that real name in all invitation text and blocks; no admin mention or sponsorship identifier is sent to the recipient. Personal text is delivered without AI rewriting. This is a consent invitation, never automatic enrollment or XP. The recipient still reviews the notice and explicitly opts in.

The invitation picker searches source first/last-name tokens case-insensitively, in either order, or a Slack user ID. It reads up to 500 matching source members, checks Slack identity uniqueness and participation in batches, and returns at most 100 eligible options; narrow the search if needed. Autocomplete database operations share a two-second [PyMongo deadline](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/connection-options/csot/). Database failures return empty options and log only the error class. A repeated “Nothing could be found” may indicate a database failure; check `Slack options lookup failed` logs and the source identity link.

`/ledger invite @member [chat|rank:<slot>]` is the existing invitation to a game channel for an opted-in member. Bare `/ledger invite` returns that usage immediately. To invite someone to opt in, an eligible admin uses `/ledger-admin invite`; a participant can use `/ledger sponsor @member`. Picker requests use `/slack/interactions`; slash commands use `/slack/commands`.

Source-role admins who opt in are invited to LEDGER_QUEST_REVIEW_CHANNEL_ID; opting out removes them. Board/resource/delegation roles are not automatically added. The destination must be private, unshared, contain The Ledger and be separate from game channels. Reconciliation backfills opted-in admins and cleans up role/identity/configuration changes. Membership confers no additional review authority.

Slack itself registers slash commands workspace-wide and does not offer application-controlled per-member command discovery. The app hides all its own administrative help for ineligible members and uses a generic registered description, but Slack's slash-command picker can still show /ledger-admin. See [Slack slash-command accessibility](https://docs.slack.dev/slack-marketplace/slack-marketplace-app-guidelines-and-requirements/#slash-commands).
