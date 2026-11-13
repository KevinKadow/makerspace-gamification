# Anomaly builds, source contracts, and local databases

The anomaly stack runs independently or alongside The Ledger. It reads the source
Mongo database through `MLAB_URI`, stores its own state in local PostgreSQL, and
uses the existing `LEDGER_LLM_*` endpoint for explanations. Detection does not
require inference availability. In standalone mode, use an inference URL reachable
from Docker; `http://ledger-ai:8000/v1` resolves only when that service shares the
Compose network. No GPU model is added by this Compose file.

## Build and run

Copy the anomaly section of `.env.example` into deployment `.env` and provide
separate random passwords. Passwords embedded in the default PostgreSQL URI must
be URL-safe (for example, random hexadecimal). To use other characters, set
`ANOMALY_DATABASE_URL` with a correctly percent-encoded password. Shell variables
`ANOMALY_RAILS_REPOSITORY`, `ANOMALY_RAILS_REF`, and optional `ANOMALY_RAILS_COMMIT`
configure source refresh; export them explicitly when overriding the documented
defaults. They are not secrets. The earlier `ANOMALY_RAILS_URL` spelling remains a
fallback alias for the repository URL.
Repository URLs are recorded in build provenance, so HTTP userinfo, passwords,
URL query strings/fragments, and transport-helper wrapper URLs are rejected before
Git runs. Use a Git credential helper or SSH agent; ordinary `git@host:org/repo.git`
and passwordless `ssh://git@host/org/repo.git` addresses are supported.

```sh
# Independently deployed analytics; keeps the existing bot/model deployment intact.
COMPOSE_FILE=compose.anomaly.yaml bash scripts/rebuild.sh

# Same Compose project and network as The Ledger.
COMPOSE_FILE=compose.yaml:compose.anomaly.yaml bash scripts/rebuild.sh
```

Use the same `COMPOSE_PROJECT_NAME` and file selection on subsequent commands.
On Windows, Compose uses a semicolon as the default `COMPOSE_FILE` separator;
the deployment script runs under Bash on the Linux deployment host. The sibling
`../metabase` source checkout (or `METABASE_SOURCE_DIR`) is required. Its own Dockerfile builds the Metabase
image once for both instances. `METABASE_VERSION` sets the local build tag/version;
this does not select a Git revision of the sibling checkout. The default
`v0.0.0-anomaly` is a development placeholder; use the appropriate numeric version
for the selected checkout when deploying. Compose supplies the literal string
quotes required by the Metabase build script's Clojure `-X` arguments. Set the
environment value without quotes; the image tag itself remains an ordinary tag.

The Metabase fork's builder uses Node 22 on Debian Bookworm and copies Java 25
from the official `eclipse-temurin:25-jdk-jammy` image. It no longer installs Java
through Adoptium's APT repository or upgrades every Debian package during the
build. Update the sibling Metabase checkout as well as this repository when
deploying this Dockerfile fix. The final runtime remains Temurin 25 on Alpine.

The source build normalizes the required shell scripts to LF inside the image,
invokes `bin/build.sh` through Bash, and copies the normalized runtime entrypoint
with mode `755`. It supports Windows line endings and source checkouts without
executable permissions without modifying the host checkout. If an older Dockerfile
fails with `bin/build.sh: Permission denied` (exit 126), update the Dockerfile in
`METABASE_SOURCE_DIR` before retrying the same rebuild command. This build failure
occurs before shutdown; existing services stay running.

If backend compilation fails with `Premature i18n string lookup` in
`metabase/metabot/settings.clj`, update the sibling Metabase checkout too. The
`anomaly-analytics-database-ids` JSON setting must declare `:encryption :no` because
it stores non-secret database IDs. Without that declaration, settings registration
throws a validation error whose translation is blocked during compilation,
masking the missing option. Keep the translation guard enabled; changing `.env`
or database credentials does not fix this source error.

If Metabase fails during dependency installation, check the toolchain separately
before rebuilding the full application (use the same source directory configured
for Compose):

```sh
docker build --pull --no-cache --progress=plain --target toolchain ../metabase
```

This target verifies Java, javac, Node, Clojure CLI, and uv before copying the
application source or installing frontend dependencies. Keep the first failing
command and its preceding error lines; APT exit code 100 alone does not identify
the failing repository or package. After the toolchain succeeds, rerun the normal
rebuild command above for full compilation and deployment preflight.

`scripts/rebuild.sh` discovers enabled services, refreshes Rails when analytics is
enabled, builds images, exports the exact built schema, and runs read-only source
preflight **before stopping any containers**. A running local Postgres also gets a
read-only connectivity preflight, and its retained effective collection contracts
are included in the source permission check. First deployment uses `preflight --source-only`
because the PostgreSQL volume has not yet been initialized. After recreation,
the script checks only the services present: `/ready` for Ledger ingress and the
anomaly `ready` command for analytics. Model outages do not gate anomaly readiness.

Direct `docker compose build` requires running `bash scripts/anomaly/refresh-rails.sh`
first; a missing prepared snapshot intentionally fails the Dockerfile `COPY`.
Use the rebuild script for the mandatory fresh source check on every deployment.
No refresh, extraction, or privilege failure stops the running deployment.

## How Rails becomes a schema contract

Every rebuild fetches the configured ref (default `refs/heads/master`) from
`https://github.com/ManchesterMakerspace/makerspace-rails-2026.git` into the isolated
`build/anomaly/rails.git` bare cache. It exports a fresh snapshot of models, schema
inventory, inflections, and lockfile at the resolved commit. It never resets or
modifies the sibling Rails working checkout. Fetch failure aborts the rebuild;
stale cached source is not silently substituted. Set `ANOMALY_RAILS_COMMIT` to a
full immutable commit ID when reproducing a deployment or rolling back. The script
still refreshes the configured upstream ref, fetches the exact pinned commit, and
verifies its identity before replacing the prepared source snapshot.

The Ruby 3.4.4 build stage uses Prism to parse declarations as data. It does not
load Rails, execute model methods or callbacks, evaluate default lambdas, load
initializers, read Rails environment files, install that repository's Gemfile,
or connect to a database. Its pinned ActiveSupport dependency supplies collection
inflection only. Custom inflection initializers require a reviewed extractor
adapter instead of being executed.

The extractor follows local concerns and inheritance, timestamp modules, explicit
collection names, fields, association keys, embedded documents, literal enum
constants, and supported unconditional validations/index definitions. Known
integer/ObjectId repair-ticket IDs and direct `checkins`/`notes` collections have
reviewed adapters. It preserves untyped fields, dynamic attributes, and opaque
containers. Unsupported custom/conditional business validations are diagnostics,
not invented constraints; a field default alone never makes a field required.
Structural ambiguity, unknown custom types, syntax errors, and dynamic schema
declarations abort extraction. Ruby code is never evaluated to fill a gap.

`/app/schema/manifest.json` is the generated image contract. Its format is:

```json
{
  "format_version": 1,
  "provenance": {"rails_commit": "resolved-commit", "repository": "source-url", "ref": "refs/heads/master", "extractor_version": "1", "contract_hash": "sha256-of-contracts", "build_time": "UTC-ISO8601", "source_hashes": {}},
  "collections": {
    "example": {"fields": {"_id": {"types": ["BSON::ObjectId"]}}, "required": ["_id"], "indexes": [], "references": [], "dynamic": false}
  },
  "diagnostics": []
}
```

The exact built artifact is exported to ignored `build/anomaly/manifest.json`
before live permission preflight, including on failed permission checks. New
collections enter that manifest automatically; there is no fixed collection count.
Schema updates do not broaden analytics projections, inference inputs, Slack
disclosure, or AI detector approval. Source definitions may precede the production
Rails rollout; preserve their revision and compare compatible schema generations
instead of labeling every historical record invalid after a new field appears.
Compatible additions activate independently even when another field in that
collection needs review. Removed fields, narrowed types/enums, added required
fields, changed references, and new uniqueness constraints retain their previous
effective rules until an administrator approves the exact candidate version.
Embedded schema changes follow the same rules. Renamed/removed collections remain
in the effective contract until approval, so retain their existing source grants
during that review. New collection grants are checked against the built candidate.
Mongo collection validators are not fetched by the minimal role: the current
schema contract comes from the freshly parsed Rails definitions and reviewed
direct-collection adapters, with live record drift reported separately.
After approval, a newly required field applies to post-approval inserts/replacements
and explicit changes to that field. A subsequent scan alone does not reclassify
an existing record missing the new field as a new violation. This applicability is
stored per record and survives restart and periodic snapshots. Existing type,
enum, and reference checks continue independently.

Provenance includes the resolved Rails commit, extractor version, deterministic
contract hash, UTC build timestamp, and SHA-256 hashes of model files, inflections,
the inventory, and lockfile when present. The build timestamp is excluded from
schema revision identity so rebuilding identical source does not create policy
churn. Findings expose only commit/hash/version/build-time provenance; repository
URLs, source contents, and source field values are not copied to event sinks.

## Source privileges

Each collection stream needs only `find` and `changeStream` for its exact source
namespace. The normal Ledger source role remains unchanged. Generate the updated
anomaly role from the built manifest:

```sh
node examples/mongodb-roles.js atlas anomaly makerauth makerauth build/anomaly/manifest.json > build/anomaly/source-role.json
node examples/mongodb-roles.js mongosh anomaly makerauth makerauth build/anomaly/manifest.json > build/anomaly/source-role.mongosh.json
```

An authorized database operator reviews and applies this role, then assigns it to
the read-only `MLAB_URI` database user. Atlas uses its Custom Database Roles API/UI;
the application never calls `createRole`, `updateRole`, `collMod`, or source writes.
Adding a collection changes the generated role payload. Missing access fails the
preflight before shutdown; apply the permission delta and rerun rebuilding. Do not
replace the existing role with a candidate-only payload while removed/renamed
collections await schema approval: retain their exact existing `find` and
`changeStream` grants. Full preflight outputs `required_collections` and
`retained_collections` for that review, and names inaccessible collections on
failure. Grants for retired names can be removed after approval. Do not
give analytics the optional Ledger ticket-note writer role. Collection privileges
cover whole documents; projection and inference redaction remain separate Python
enforcement boundaries. See [Mongo access](MONGODB_ACCESS.md).

## Persistent local state and Metabase separation

The `anomaly-postgres` named volume stores PostgreSQL state, including learned
baselines, serialized model coefficients/artifacts, stream checkpoints, and both
Metabase metadata databases. It is not removed by rebuilding. Back it up together
with the applicable image/schema version; do not use `down --volumes` as an upgrade
or recovery procedure. No separate model-file volume is needed by this runtime.

On a fresh PostgreSQL volume, the initialization script creates:

| Database / login | Purpose |
| --- | --- |
| `ledger_anomaly` / `anomaly_writer` | Owned analytics state; no cluster-admin or database-creation powers |
| `ledger_anomaly` / `anomaly_reporter` | Read-only connection for explicitly granted reporting views |
| `metabase_anomaly` / `metabase_anomaly` | Derived-data Metabase application metadata |
| `metabase_raw` / `metabase_raw` | Separate raw-source Metabase application metadata |

`anomaly_reporter` has read-only transactions and receives no blanket table grants.
Runtime migrations grant only named reporting views. The other Metabase instance
connects to Mongo through an explicit, read-only setup using `MLAB_URI` and
`MLAB_DATABASE`; `MB_DB_*` always describes Metabase's own PostgreSQL application
database, not the Mongo source. The raw instance has separate users, keys, database,
and encryption secret. Slack analytics tools use only the derived instance.

Metabase binds to localhost ports 3001 (derived) and 3002 (raw) by default. Configure
authenticated reverse-proxy access deliberately if needed. PostgreSQL has no host
port. Both Metabase encryption keys must be retained across restarts and backups.
Existing PostgreSQL volumes do not rerun initialization scripts; rotating passwords
or changing database roles needs an explicit database operation and coordinated
environment update.

For one-shot Metabase bootstrap, provide setup credentials only to that command;
they are intentionally absent from the steady-state Compose environment:

```sh
docker compose run --rm --no-deps \
  -e ANOMALY_METABASE_BOOTSTRAP_API_KEY -e ANOMALY_METABASE_RAW_API_KEY \
  ledger-anomaly python -m ledger.anomaly bootstrap-metabase
```

The runtime reporting connection uses `ANOMALY_REPORTING_DB_HOST/PORT/NAME/USER/PASSWORD`.
Compose maps these to `anomaly-postgres`, `5432`, `ledger_anomaly`, `anomaly_reporter`,
and `ANOMALY_REPORTING_PASSWORD`. The shorter `ANOMALY_REPORTING_USER` is retained
for the SQL view-grant configuration and follows `ANOMALY_REPORTING_DB_USER`.
All reporting connection fields can be overridden for a separately hosted
PostgreSQL instance; `ANOMALY_REPORTING_DB_PASSWORD` falls back to
`ANOMALY_REPORTING_PASSWORD`. Steady-state Metabase keys are separate
`ANOMALY_METABASE_CHAT_API_KEY` and `ANOMALY_METABASE_CHART_API_KEY` values.
`ANOMALY_METABASE_PUBLIC_URL` controls authenticated links returned in Slack and
defaults to `METABASE_ANOMALY_PUBLIC_URL`. Public sharing is disabled on both
Metabase instances. Set a unique `ANOMALY_MQTT_CLIENT_ID` if multiple deployments
use the same broker; it intentionally does not reuse the Ledger bot's client ID.

## Verification

Run `python -m pytest tests/test_anomaly_schema_build.py tests/test_anomaly_deployment.py
tests/test_rebuild_script.py tests/test_mongodb_roles.py`. Ruby extraction tests
need Ruby 3.4, Prism, and ActiveSupport; the Docker schema stage supplies those
dependencies on deployment. Tests use local fixture Git repositories, mocked
Docker, and generated role JSON. They do not contact production or claim live
Mongo, PostgreSQL, Metabase, Slack, or vLLM verification.
