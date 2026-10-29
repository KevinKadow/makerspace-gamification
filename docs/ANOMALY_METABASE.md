# Metabase for the anomaly service

The standalone stack has two Metabase instances, built from the sibling `metabase/` fork. Each has its own persistent PostgreSQL application database. `metabase-anomaly` exposes curated reporting data and native Metabot. `metabase-raw` is an administrative MongoDB explorer with AI disabled. Neither Metabase application database is the analytics reporting database.

The curated connection uses the `anomaly_reporter` PostgreSQL login with `SELECT` access only to the `reporting` schema. The analytics worker owns its state through a separate writer login. Source MongoDB credentials belong only on the raw instance and worker. A Metabase schema filter helps discovery; database grants enforce the boundary. Never attach raw MongoDB or the worker's private state tables to the curated instance, even for administrators. Bootstrap refuses an instance with another source, including the sample database. Before every native analytics turn, the backend checks that the complete configured database catalog exactly matches the reporting allowlist, including databases the caller cannot see. Adding another source later disables analytics before history/context construction or inference; remove that source to restore it. On the analytics-only instance, the catalog and target database are checked again immediately before every Metabot query executes.

## Configuration

Use `compose.anomaly.yaml` and its documented environment template. Private hostnames must resolve from the analytics host; the Docker service name `ledger-ai` only resolves when both containers share the same network. For a separate host, set the vLLM URL to a reachable private address ending in `/v1`.

| Variable | Purpose |
| --- | --- |
| `ANOMALY_METABASE_URL` | Curated HTTP(S) origin, normally `http://metabase-anomaly:3000`. No path or embedded credentials. |
| `ANOMALY_METABASE_RAW_URL` | Optional raw explorer origin, normally `http://metabase-raw:3000`. |
| `ANOMALY_METABASE_CHAT_API_KEY` | Non-admin API key allowed to query curated data and use Metabot. |
| `ANOMALY_METABASE_CHART_API_KEY` | Non-admin API key allowed to read the report collection and run its saved questions. |
| `ANOMALY_METABASE_BOOTSTRAP_API_KEY` | Curated administrator key, supplied only to the explicit setup command. |
| `ANOMALY_METABASE_RAW_API_KEY` | Raw administrator key, supplied only to setup when a raw URL is configured. |
| `ANOMALY_REPORTING_DB_HOST`, `PORT`, `NAME`, `USER`, `PASSWORD`, `SSL` | Setup connection parameters. The fully prefixed names start with `ANOMALY_REPORTING_DB_`. Use the reporting reader, never the state writer. |
| `MLAB_URI`, `MLAB_DATABASE` | Read-only source MongoDB URI and optional database override. Bootstrap preserves URI credentials, SRV scheme and `authSource` while applying the database override. |

The curated container sets `MB_ANOMALY_ANALYTICS_ONLY=true`. This overrides the profile requested by both the native UI and API with `anomaly-analytics`. Its tools discover/read resources, construct and run notebook queries, and create conversation charts or references to existing saved questions. It has no save/delete, alert, subscription, source mutation, or dynamically loaded skill tools. Database and collection permissions still apply to every query. `MB_METABOT_QUERY_EXECUTION_ENABLED=true` enables the required query tools; keep `MB_METABOT_SQL_EXECUTION_ENABLED=false`.

Configure both `MB_LLM_METABOT_PROVIDER=vllm/<served-model>` and `MB_LLM_MINI_MODEL=vllm/<served-model>` so short tasks also use the existing private provider. Supply `MB_LLM_VLLM_API_BASE_URL`, `MB_LLM_VLLM_API_KEY`, and `MB_LLM_ALLOWED_NETWORKS=allow-private`. The local fork requires a supported context window of at least 16,384 tokens. Verify this against the existing runner's actual configuration; the Ledger narrator's smaller context configuration is insufficient for native Metabot. A second language model is not required.

The raw container sets `MB_AI_FEATURES_ENABLED=false` and receives no vLLM key. Restrict its accounts and network exposure to administrators who need source exploration. Do not put its API key in the normal worker environment.

## Explicit bootstrap

Start the analytics stack and let its reporting schema initialize. Complete first-run setup separately for each Metabase instance and create temporary setup API keys. Set those keys in the operator shell, then run:

```sh
docker compose -f compose.anomaly.yaml run --rm \
  -e ANOMALY_METABASE_BOOTSTRAP_API_KEY \
  -e ANOMALY_METABASE_RAW_API_KEY \
  ledger-anomaly python -m ledger.anomaly bootstrap-metabase
```

The keys are read from the operator environment; no secret value belongs in the command. Bootstrap creates the reporting connection, requests a schema sync if necessary, and asks for a rerun after that sync. Reruns reuse exact owned names and leave existing cards and populated dashboards intact. Duplicate names or a changed reporting connection fail instead of selecting a target silently. Configuration changes to an existing source require explicit administrative correction.

Reporting validation compares engine, host, port, database, user, TLS and the exact
`reporting` inclusion filters. Unconfigured connection overrides, such as JDBC
options or SSH tunnels, fail validation. Numeric and string ports compare equally;
the masked reporting password is not compared. Metabase may enable TLS during
connection creation even when configured false; if validation detects this, set
`ANOMALY_REPORTING_DB_SSL=true` to match the intended connection and rerun.

The raw source must use Mongo URI mode with the resolved `MLAB_URI`/`MLAB_DATABASE`
and no separate database-name or tunnel override. A visible URI mismatch fails.
The native API masks the entire URI, including its target and credentials, so a
masked value cannot establish a match. On explicit bootstrap reruns, the configured
URI is reapplied to that database ID through the authenticated update API before
accepting it. An update failure aborts bootstrap. This preserves the configured
source credentials without exposing them in results or errors; actual read-only
grants still require deployment verification.

It seeds these cards and dashboards from `reporting` only:

| Report identifier | Dashboard | Source |
| --- | --- | --- |
| `quality` | Data Quality | `findings_summary` |
| `status` | Pipeline Health | `pipeline_health` |
| `churn` | Churn | `cohort_metrics`, report `churn` |
| `revocation` | Revocation | `cohort_metrics`, report `revocation` |
| `retention_24m` | Long-term Retention | `cohort_metrics`, report `retention_24m` |
| `attendance` | Attendance | `cohort_metrics`, report `attendance` |
| `model_quality` | Model Quality | `model_quality` |

Bootstrap returns database, collection, card and dashboard IDs; the CLI persists them in local analytics state. It also sets `anomaly-analytics-database-ids` to the reporting database ID and enables the analytics-only profile. Do not set an empty `MB_ANOMALY_ANALYTICS_DATABASE_IDS` environment override: it would shadow the saved allowlist and disable analytics. The default empty allowlist denies native analytics chat and chart rendering until bootstrap completes.

Grant the steady-state API-key groups read access to the report collection and query access to the reporting database. Grant the chat group the appropriate native Metabot query/visualization permissions. Do not grant either group source administration, writeback, subscriptions, or unrestricted collection editing. Direct native Metabot users need their own appropriately scoped accounts; Slack roles do not automatically provision Metabase accounts. Revoke the temporary setup keys after setup and retain only the chat/chart keys on the worker.

## Chat and chart transport

`MetabaseClient.ask(message, conversation_id, context=None)` sends the native `POST /api/metabot/agent-streaming` request with profile `anomaly-analytics`. It reads persisted conversation history to recover the latest parent UUID after restarts. An in-progress turn, authorization failure, conflict, malformed stream, or incomplete finish fails without automatically replaying the question. The caller must persist and serialize conversation IDs by authorized Slack audience; a user-supplied conversation UUID is never trusted. Context accepts only a curated database ID and current time.

The result contains `answer`, `chartrefs`, `conversation_id`, and `assistant_message_id`. The client ignores model-supplied query bodies and URLs. It accepts at most four unique chart references, bounds SSE output to 2 MiB, and requires a completed native finish event before returning an answer.

The fork adds authenticated `POST /api/metabot/analytics/render`. Its closed request schema accepts exactly one of:

```json
{"type":"saved_card","card_id":123}
```

```json
{"type":"conversation_chart","conversation_id":"<uuid>","chart_id":"<uuid>"}
```

The endpoint checks saved-question access or conversation participation before loading chart state, then checks the reporting database allowlist and current query permissions. It resolves conversation charts only from completed, non-errored persisted turns. Inline native queries and unsupported chart types are rejected. Queries have a 30-second deadline, a 1,000-row limit, and a 30-column limit. Text is bounded per cell and per result, PNG width is 1,200 pixels, and output is capped at 8 MiB. Saved visualization options are discarded to prevent remote image URL rendering. Responses are `image/png`, `Cache-Control: no-store`, and `X-Content-Type-Options: nosniff`.

`MetabaseClient.render(ref)` downloads bytes from this fixed endpoint, checks the content type and PNG signature, and rejects redirects or oversized responses. Conversation charts use the chat key because its synthetic user owns that conversation; the independent chart key is used for saved cards. The worker uploads bytes as a Slack file only after its normal audience checks. It never creates a public Metabase URL or asks Slack to fetch a private dashboard image. Source queries, credentials and raw database rows are not part of this transport.

## Verification

Python contract tests cover conversation restart, in-flight protection, invalid references, partial/error SSE, bounded PNG transport, API-key separation, setup idempotency, database override handling and the seven reports:

```sh
python -m pytest tests/test_anomaly_metabase.py
```

The Metabase fork adds `metabase.metabot.api.analytics-test` for route authentication, ownership-before-state checks, current query permissions, fail-closed database selection, render bounds and the forced read-only tool profile. Run it using the fork's normal backend test environment before deployment. Local static syntax checks do not replace compiling or executing those native tests.

Deployment verification must cover the built image, native Metabot with the configured vLLM model, PostgreSQL reader grants, MongoDB read-only connection, reporting sync, real PNG output and Slack upload. An absent or unsupported chart must leave a useful text response without claiming that an attachment was sent. Revoke chat/chart permissions during testing to confirm access loss fails closed.


Security bootstrap adds eight aggregate dashboards through the existing `reporting.cohort_metrics` view. Python applies minimum-cohort and complementary suppression; SQL also rejects rows below the configured cohort floor. Security event, rollup, incident and delivery tables are private and receive no reporting grants. Rerun bootstrap after deployment; existing cards are reused. See [security analytics](ANOMALY_SECURITY.md).
