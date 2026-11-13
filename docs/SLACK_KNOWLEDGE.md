# Public Slack knowledge

The optional knowledge pipeline imports native Slack public-channel JSON ZIP
exports. It groups replies with their parent, screens and distills each thread
using the existing private Qwen service, and indexes the result through
LangChain. It does not import DMs or private channels. Exported public content
can inform answers in other public channels and DMs; it is historical evidence,
not current policy, safety clearance or authority. Source links are generated
from validated Slack identifiers. Existing Python authorization remains in force.

## Deployment

Provision the updated `ledger_knowledge` Mongo role grants in
[MongoDB access](MONGODB_ACCESS.md). Build the Ledger image and start the
private services with `docker compose --profile knowledge up -d --build
ledger-embeddings ledger-chroma`. The pinned CPU embedding model downloads on
first startup; the embedding cache and Chroma index have separate named
volumes. No service port is published by the supplied Compose configuration.
Pin image digests for the deployment after verifying the images and model on
the target host. A host CLI needs `pip install -e '.[knowledge]'` and private
connectivity to both services. `LEDGER_EMBEDDING_URL`, `LEDGER_CHROMA_HOST` and
`LEDGER_CHROMA_PORT` configure those connections. Do not expose them publicly.

Set `LEDGER_KNOWLEDGE_ENABLED=true` to allow conversations to search the index.
For ambient answers, set `LEDGER_KNOWLEDGE_AMBIENT_CHANNELS` to a comma-separated
list of public channel IDs and invite the bot. Existing registered Ledger-channel
ambient behavior is separate. For the new path, the bot sends a separate notice
before using an eligible member's later ambient questions; the first question is
not saved as conversation context or answered. Members can disable this path via
the observation preference. Notice, preference, configured channel, live bot
membership and source versions are rechecked before delivery. A two-minute
per-channel cooldown and a relevance threshold limit replies. Slack ingress
temporarily retains received events in the durable inbox until processing;
terminal configured-channel event payloads have text, blocks, attachments and
files removed from the event and its message/previous-message fields.

Update a Google Doc Prompt Matrix override to version 54 with the same policy,
then explicitly reload it. Existing reserved deliveries retain their saved
policy and text; no external document is published automatically.

## Import and removal

Set `SLACK_TEAM_ID` to the export's workspace ID. With the knowledge services,
Mongo and vLLM reachable, run:

```sh
ledger knowledge-import --export /path/to/export.zip
ledger knowledge-list
ledger knowledge-remove --thread-id 'T123:C123:1700000000.000001'
```

For a container CLI, mount the export read-only into the container and pass its
mounted path. Limit archive access to the operator. Imports report counts rather
than message content. `knowledge-list` shows up to 1,000 pending, quarantined
and removed identifiers and reasons; review the original archive securely when
investigating a quarantine. Screening is conservative but cannot detect every
sensitive disclosure. Suspect threads are quarantined before inference and their
content is not retained in the knowledge store. Do not treat an unflagged export
as proof that it contains no sensitive content.

Screened source messages are retained in `ledger_knowledge` so a later partial
export can add replies or edits. Unchanged threads do not invoke Qwen again;
changed threads are re-distilled, and threads missing their root stay pending.
Absence from a partial export is not deletion. A removal creates a durable
tombstone and removes vectors; later imports do not restore that thread.
Removing a thread is the operator mechanism for excluding imported content;
changing a member's observation preference does not automatically purge their
historical public exports. Remove affected threads when required.

Imports and removal serialize per thread. If an operation crashes, retry after
its 12-hour lease expires. Reimporting repairs an incomplete publication or
retries vector cleanup for quarantined and removed records. Mongo generation
checks exclude stale or incomplete vectors from answers. Monitor import counts,
service health and storage, and retain the original export under the workspace's
own retention policy. Verify live Slack, Mongo, vLLM, embedding model and Chroma
behavior on the deployment before enabling replies.

An import exits unsuccessfully if any thread encounters an error or is busy;
retry the same archive after resolving the service failure or lease. Pending
threads may need a later export containing their root. Role and subscriber
boosts use current verified identity and membership data, so a Slack identity
that cannot be verified remains unboosted.
