# The Ledger prompt library

The bot ships with **23 JSON files and 69 prompt variations** in [`ledger/prompts`](../ledger/prompts). Each file owns one message type. Each variation pairs a `system` prompt with a `user` prompt and identifies its personality and attitude. The default voices are a measured archivist, a practical mentor, and a dry-witted grimoire. Sensitive messages such as opt-out confirmations and corrections stay sober in all three voices.

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

The shipped rank-up archivist uses both ranks and the skill. The mentor uses the new rank and skill while omitting the old rank. The grimoire uses the new rank while omitting both the old rank and skill. A variation may use any subset of the available variables.

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

1. `/ledger-admin template <type> <audience>` edits the variation JSON array, audience instructions, fallback, temperature, and token budget. The modal previews every system/user pair with sample facts before publication. Slack's JSON input has a 3,000-character limit; use file edits for longer sets.
2. `/ledger-admin template-library <type> <audience>` previews the deployed JSON file's variations and publishes them as a new immutable database version. Use this to adopt updated file defaults over a prior custom override, or to publish larger sets. No override changes until **Publish** is submitted.

For example, `/ledger-admin template-library rank_up shared` adopts the shared rank-up configuration. Audiences publish independently. `/ledger-admin template-test rank_up shared` queues one live generation preview in the administrator's DM and reports the selected variation. `/ledger-admin template-history` lists versions; `/ledger-admin template-rollback <template-id>` republishes a prior version without changing already composed jobs.

Existing database versions with a single `system`/`prompt` pair remain readable as one `legacy` variation; they are not silently overwritten. Adopt the library to enable its three voices for those overrides. Database version IDs identify published configurations; library-adoption records also retain the source file digest. Original event/audit rank labels stay historical even when current names are used in newly composed text.
