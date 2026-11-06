# The Ledger prompt library

The bot ships with **23 JSON files and 79 prompt variations** in [`ledger/prompts`](../ledger/prompts). Each file owns one message type. Each variation pairs a `system` prompt with a `user` prompt and identifies its personality and attitude. The default voices are a measured archivist, a practical mentor, and a dry-witted grimoire. Sensitive messages such as opt-out confirmations and corrections stay sober in all three voices.

Every generation also receives the shared [XML/Markdown Prompt Matrix Template](PROMPT_MATRIX.md). It defines game rules, role limits, personality, privacy, and DM/channel conduct. `LEDGER_PROMPT_MATRIX_DOC_URL` optionally supplies its contents from a Google Doc on startup or admin reload. The matrix and its content hash are saved with each new prompt reservation; changing it does not reroll voices or rewrite reserved deliveries.

For each new delivery, the composer excludes the **two most recently selected variation IDs** and randomly selects a complete pair with equal probability among the remaining choices. One shared history covers channel announcements, public kudos, project posts, and channel conversation replies, regardless of the member being discussed. Each recipient has a separate DM history covering all message types and participation states. A private message does not consume shared history or another member's history.

History uses stable variation IDs across message types and template publications, so the shipped `archivist`, `mentor`, and `wry_grimoire` voices do not repeat on consecutive new compositions within a scope. Reuse an ID across types when it represents the same voice; preserve it when revising that voice. If a custom set has too few alternatives, the oldest exclusion is relaxed first: two variations alternate, while a legacy single-variation template necessarily repeats. Removed IDs never prevent selection.

The history update and chosen prompt/settings snapshot are reserved together in the delivery job's Mongo transaction **before** calling the AI. Concurrent workers therefore see reserved choices, and a restarted worker resumes its reserved pair even if templates changed. The selected text, variation ID, personality, attitude, scope, and template version are then saved before Slack delivery. Delivery retries reuse that saved composition, including a saved canned fallback; they do not advance history or call the AI again. A crash before text is saved may require generation again, using the same reserved prompts.

History is bounded to two IDs per scope in `ledger_context`, contains no member message text, and expires after thirty days without new selections. It starts accumulating when this behavior is deployed. Reservations count even if generation falls back or a later authorization check cancels delivery; broken templates that cannot select a variation do not advance history. Spacing follows reservation order: delayed deliveries/retries can arrive out of order, and canned fallback wording can repeat. Administrative prompt previews are isolated from delivery history.

This library changes narration, not delivery policy. Automatic shared announcements still cover only major achievements. Public kudos and member-directed conversations use their existing explicit paths. Consent, XP decisions, rank attribution, actions, and original kudos bodies remain application-controlled.

## Files

| Purpose | Files in `ledger/prompts/` |
| --- | --- |
| Participation | `onboarding.json`, `return.json`, `opt_out.json`, `invitation.json` |
| Learning and service | `checkout_earned.json`, `checkout_granted.json`, `volunteer_credit.json`, `challenge.json`, `first_build.json`, `mentoring.json`, `develop_mentor.json` |
| Major achievements | `rank_up.json`, `shop_complete.json`, `boss.json`, `stewardship.json` |
| Community recognition | `kudos.json`, `recruitment.json` |
| Conversation and collaboration | `conversation.json`, `status.json`, `delivery.json`, `project.json`, `quest.json`, `correction.json` |

## Editing a file

Use [`rank_up.json`](../ledger/prompts/rank_up.json) as a complete example. Every file has:

- `schema_version: 1`, its exact message `type`, and a positive integer `version`. Increment `version` when publishing file edits.
- `temperature` between 0 and 2 and `max_tokens` between 32 and 1,024.
- An `audiences` object with `member`, `shared`, `recipient`, and `nonparticipant`. Each has a literal `audience_instruction` and a literal canned `fallback`. Defining an audience does not enable a new delivery route.
- Between three and twenty `variations`. Each needs a unique lowercase `id`, a descriptive `personality` and `attitude`, and nonblank `system` and `user` prompts. Express the desired voice in `system`; personality and attitude also appear in the composition audit.

For example, this is **one entry** in the variations array, not a complete file:

```json
{
  "id": "archivist",
  "personality": "Measured archivist",
  "attitude": "Dignified, precise, quietly proud",
  "system": "You are The Ledger, an old leatherbound grimoire with a calm, exact voice. Celebrate verified learning in two concise sentences.",
  "user": "{audience_instruction}\nAnnounce that {member_full_name} (Slack ID {member_slack_id}) advanced from {old_rank} to {new_rank}. Their deepest cleared skill is {highest_skill}. Omit unavailable details and do not imply new tool permissions."
}
```

Ordinary rank-up recognition may use configured rank details according to its audience and policy. Automatic rank-channel transitions use a separate constrained composition: shared variants receive only the member's name and a generic stage summary, and must not identify or hint at any rank. The prior-channel ascent and new-channel welcome reuse the selected checked-in `rank_up.json` variation set while saving separate choices/text for retry.

## Substitutions

Placeholders work in **both** `system` and `user`. String values are inserted as JSON-quoted data, so `Name: {member_full_name}` becomes `Name: "Joe Maker"`. Quotes, line breaks, and braces inside member data remain literal data. Substitution runs once: a name containing `{new_rank}` cannot expand a second placeholder. Use `{{` and `}}` for literal braces in authored prompts. There are no expressions, attribute access, format specifiers, or executable templates.

| Variable | Source and availability |
| --- | --- |
| `member_full_name` | Recipient/member's projected first and last name, when available |
| `member_slack_id`, `member_mention` | Valid, non-invalidated Slack mapping; mention is formatted as `<@U…>` |
| `current_rank` | Current configured rank name for an active participant; withheld from kudos and invitations |
| `old_rank`, `new_rank` | Rank-transition evidence; current display names are resolved through stable slot IDs before first composition |
| `highest_skill`, `highest_skill_shop`, `highest_skill_depth` | Derived from current clearance/prerequisite data for rank advancement, returning-member summaries, earned checkouts, and conversations |
| `xp_change`, `xp_total` | Decimal strings for an award delta and recorded total; the total can also be supplied for active participant updates |
| `shop_name`, `tool_name` | Relevant catalog labels for a checkout, shop milestone, or selected kudos context |
| `volunteer_credits` | Approved credit quantity, including fractional amounts, for a volunteer-credit award |
| `challenge_title` | Reviewed challenge title or linked volunteer opportunity title when present |
| `project_title`, `quest_title` | Title when the originating project/quest notification supplies one |
| `giver_full_name`, `giver_slack_id` | Kudos giver's projected name and validated Slack mapping |
| `recipient_full_name`, `recipient_slack_id` | Kudos recipient's projected name and validated Slack mapping |
| `recipient_mention` | Validated kudos recipient Slack mention, including sender delivery acknowledgments and receipts |
| `delivery_status` | Recorded overall kudos delivery result: queued, pending, partial, delivered, failed or cancelled |
| `dm_status`, `public_status` | Recorded destination status; public status is `not requested` when sharing is off |
| `xp_result` | Once-only kudos XP result: `17 XP awarded` or `0 XP`; a receipt never awards XP again |
| `sponsor_full_name`, `sponsor_slack_id` | Invitation sponsor's projected name and validated Slack mapping |
| `summary` | Application-authored summary, when supplied by the originating message |
| `message_type`, `audience` | Current routing type and audience |
| `audience_instruction` | Trusted audience instructions from the template, inserted as instructions rather than quoted member data |
| `facts` | JSON object containing only allowlisted event facts and metrics; supports older custom prompts |

Availability depends on the event. Missing or empty details become the quoted value `"not recorded"`; every generation is instructed to omit unavailable details rather than invent them or print that phrase. Do not require facts that an event cannot establish. For a coalesced announcement, named variables come from the matching achievement type; `{facts}` retains the authorized list of achievements.

“Highest skill” is a display convention, not a claim of mastery: select the member's non-revoked clearance with the greatest prerequisite depth among enabled tools in enabled shops. A root has depth zero; a tool's depth is one plus its deepest prerequisite. Ties sort by tool name, then stable tool ID. Open-access tools are not candidates. Cycles, missing prerequisites, and paths depending on them are omitted. If no candidate can be established, the value is unavailable. This never grants a clearance or changes progression requirements.

Only projected identity and allowlisted game data enter these substitutions. Billing details, access codes, and internal notes are excluded. Kudos generation receives names and optional shop/tool context, **never the original kudos body or retained rank/XP**. Both deliveries append the original Slack-formatted body unchanged, and the application checks participation before displaying rank attribution.

## Validation and publication

Validate file structure, audience settings, and every placeholder locally:

```powershell
python -m ledger.prompt_library
python -m pytest tests/test_prompts.py tests/test_messages.py
```

The loader requires at least three variations in each packaged file. Unknown placeholders, duplicate variation IDs, malformed JSON, and invalid settings fail validation. A broken or missing deployment file falls back to built-in canned copy at runtime instead of blocking notification delivery. API failures continue to use the selected audience's canned fallback without exposing errors to members.

Files are included in the Python wheel and Docker image and cached for the process lifetime. Rebuild/redeploy the application after editing them, restarting the processes that preview or compose messages. File defaults take effect immediately in the restarted process **only when no published database override exists** for that type/audience. Each file composition records its declared version and SHA-256 content digest.

Admins and board members have two publication paths:

1. `/ledger-admin template <type> <audience>` edits the variation JSON array, audience instructions, fallback, temperature, and token budget. The modal previews every system/user pair with sample facts before publication. Each Slack input is limited to 3,000 characters. Larger sets use one JSON input per paired variation; oversized individual pairs use the file workflow.
2. `/ledger-admin template-library <type> <audience>` previews the deployed JSON file's variations and publishes them as a new immutable database version. Use this to adopt updated file defaults over a prior custom override, or to publish larger sets. No override changes until **Publish** is submitted.

For example, `/ledger-admin template-library rank_up shared` adopts the shared rank-up configuration. Audiences publish independently. `/ledger-admin template-test rank_up shared` queues one live generation preview in the administrator's DM and reports the selected variation. `/ledger-admin template-history` lists versions; `/ledger-admin template-rollback <template-id>` republishes a prior version without changing already composed jobs.

Existing database versions with a single `system`/`prompt` pair remain readable as one `legacy` variation; they are not silently overwritten. Adopt the library to enable its packaged voices for those overrides. Database version IDs identify published configurations; library-adoption records also retain the source file digest. Original event/audit rank labels stay historical even when current names are used in newly composed text.

`rank_up.json` version 3 retains its paired variations for shared-channel ascent and welcome messages. Prior-rank announcements receive stage-limited facts and must never identify the next rank; Python checks generated text and falls back to a safe sentence if it does. The other four completion files `shop_complete`, `quest`, `volunteer_credit`, and `develop_mentor` remain version 2 with five paired variations. New Achievement framing applies only to verified completion, never acceptance, pending evidence, or corrections. Humor is optional under matrix version 21. All member-facing model text uses The Ledger/The System; original member-authored content is preserved. New game result summaries use `status.json`; new giver receipts use `delivery.json`.

`delivery.json` version 3 and `status.json` version 2 each provide five paired variations: four primarily plain and one restrained System flavor. For consolidated results, Python renders the factual paragraph or lines; Qwen supplies only an optional short sentence that does not repeat recipient, destinations, XP, or milestones. Pending/partial/failure kudos results bypass inference. Receipt identity and metadata exclude original authored content and recipient progress. Destination delivery does not confirm reading. Older reserved acknowledgements and receipts retain their saved policy and text.

Customize with `/ledger-admin template delivery member` or `/ledger-admin template status member`, or explicitly adopt each file with `template-library`. Preview with `/ledger-admin template-test delivery member`. Stable paired voice IDs share the sender's existing DM history; reserved prompts and saved text survive retries without rerolling. Existing database overrides require explicit publication to adopt new defaults.

Short result reservations snapshot a ten-second, at-most-128-token profile, the full validated matrix and hash, and the exact projection in canonical order. Receipt projection includes `identity`, `authority`, `kudos`, `privacy`, and `response`; game results omit `kudos`. Requested next-step guidance uses the paired `conversation` template with a ten-second, at-most-256-token profile and existing caller-only progress facts, without a tool round-trip. Invalid narration is omitted; guidance failures use deterministic suggestions or blockers. Existing reservations do not gain new profiles when policy reloads. See [result lifecycle and deployment](RESULT_SUMMARIES.md).
