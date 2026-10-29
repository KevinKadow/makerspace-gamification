# CI test jobs

The Ledger tests workflow runs two independent jobs on `ubuntu-24.04`:

- `test` runs prompt validators, wheel/Compose checks, and `python -m pytest -m "not mongo_integration" --strict-markers`.
- `mongo-integration` starts a disposable replica set and runs `python -m pytest -m mongo_integration --strict-markers` with `LEDGER_TEST_MONGO_URI`.

New tests that need a real MongoDB server must carry `@pytest.mark.mongo_integration`. Keep their missing-URI skip for local full-suite runs. CI supplies the URI explicitly. The jobs have no dependency on each other, so a Mongo startup failure cannot prevent ordinary tests from running. Repository branch protection should require both jobs; workflow changes do not update branch protection automatically.

The Mongo job pins the official `mongo:8.0.34-noble` image to manifest digest `sha256:c2d54fddfb3738fb27fe117f51692e3f44e640a3e7aeb8da3937ee9cd33a650b`. Its tag/digest and Linux AMD64 manifest were verified against Docker Registry during this change. This is registry verification, not proof of a successful runtime on the GitHub host. Image updates should verify the replacement tag/digest and pass the integration job before merging.

`scripts/ci/start-mongo.sh` requires `MONGO_TEST_IMAGE`, checks the container remains running during readiness polling, and requires successful ping, replica-set initialization and writable-primary election. Failure prints Docker state (including exit code and OOM status) and the last 200 timestamped log lines. The workflow also captures diagnostics on failure before always removing the container and its anonymous volumes. No external database credentials are needed; the host port binds only to loopback.

To reproduce on a Docker-equipped Linux host, set `MONGO_TEST_IMAGE` to the exact workflow value, run `bash scripts/ci/start-mongo.sh`, and run the marked tests with `LEDGER_TEST_MONGO_URI=mongodb://localhost:27017/?replicaSet=rs0`. Remove the CI-only server afterward with `docker rm -fv ledger-mongo`. Never point these tests at production: they create and drop disposable test databases.
