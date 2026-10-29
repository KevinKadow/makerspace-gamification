#!/usr/bin/env bash
# CI-only disposable database. The workflow supplies an immutable image reference.
set -Eeuo pipefail
: "${MONGO_TEST_IMAGE:?Set MONGO_TEST_IMAGE to the verified Mongo image digest}"

failure() {
    status=$?
    trap - ERR
    printf 'Mongo startup failed (command status %s). Container state and logs follow.\n' "$status" >&2
    docker inspect --format '{{json .State}}' ledger-mongo >&2 || true
    docker logs --timestamps --tail=200 ledger-mongo >&2 || true
    exit "$status"
}
trap failure ERR

running() {
    if [[ "$(docker inspect --format '{{.State.Running}}' ledger-mongo)" != true ]]; then
        printf 'Mongo container exited during startup.\n' >&2
        return 1
    fi
}

wait_for() {
    local description="$1" expression="$2" attempt
    for attempt in $(seq 1 30); do
        running || return 1
        if docker exec ledger-mongo mongosh --quiet --eval "$expression"; then
            return 0
        fi
        sleep 1
    done
    printf 'Timed out waiting for Mongo %s.\n' "$description" >&2
    return 1
}

docker run -d --name ledger-mongo -p 127.0.0.1:27017:27017 \
    "$MONGO_TEST_IMAGE" --replSet rs0 --bind_ip_all
wait_for ping 'if (db.adminCommand({ping:1}).ok !== 1) { quit(1); }'
docker exec ledger-mongo mongosh --quiet --eval \
    'if (rs.initiate({_id:"rs0",members:[{_id:0,host:"localhost:27017"}]}).ok !== 1) { quit(1); }'
wait_for primary 'if (!db.hello().isWritablePrimary) { quit(1); }'
