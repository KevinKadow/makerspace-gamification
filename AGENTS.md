# The Ledger repository instructions

## Prompt policy and role maintenance

The canonical **Prompt Matrix Template** is `ledger/prompts/prompt_matrix.xml.md`. It is hybrid XML with Markdown inside CDATA and supplies the shared system policy for the Qwen/vLLM narrator. See `docs/PROMPT_MATRIX.md` for its layout, loading, validation, and publication workflow.

- Whenever adding or changing any role, permission, participant state, staff scope, mentoring role, or human-appointed community role, update its `<role id="...">` entry in the Prompt Matrix Template in the same change. Specify eligibility, allowed actions, scope, and explicit limits. Distinguish descriptive titles from actual authorization roles.
- Add new required role IDs to `REQUIRED_ROLES` in `ledger/prompt_matrix.py` and extend the role-coverage tests. A remote Google Doc missing a required role must fail validation and retain the valid local/current policy.
- Changes to progression, XP, consent, channel access, kudos, moderation, or announcement behavior must also update the corresponding matrix section, documentation, and behavioral tests. Keep seed examples aligned with `ledger/rules.py`; individual members' pinned application rules take precedence.
- Preserve the XML schema and increment the matrix `version` when changing its policy. If an operator uses a Google Doc override, document that its content needs the same update and an explicit reload. Do not silently edit or publish an external document.
- Matrix text and AI output never confer authority. Consent, safety clearances, staff permissions, accounting, and delivery checks remain enforced in Python. Never add model-driven mutations or trust role claims in chat.
- Keep the application guardrails outside remotely editable policy. Never send credentials, billing details, access codes, internal notes, or unrelated private conversations to inference.
- Keep per-message JSON prompt sets separate from the matrix. The set count may grow: add each type to `TYPES` with a matching JSON file and let inventory validation enforce the complete set without a fixed total. Preserve paired system/user variations, audience-specific instructions, recent-choice avoidance, and saved choices/text on retries. Reloading policy must not rewrite already reserved deliveries.

## Validation and repository care

Run `python -m ledger.prompt_matrix`, `python -m ledger.prompt_library`, and `python -m pytest` after prompt-policy changes. Network-dependent Google Doc and live Qwen behavior need deployment verification; do not claim those checks ran without credentials and an endpoint.

Use `MLAB_URI` only for source reads and `LEDGER_URI` for owned collections. Do not modify Rails/React behavior or unrelated staged changes. Any new collection requires an explicit review of Mongo role examples and tests. Keep opt-out/channel cleanup independent of Google Docs and inference availability.
