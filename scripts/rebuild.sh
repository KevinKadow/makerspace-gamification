#!/usr/bin/env bash
# Run after updating the checkout on the deployment host.
set -Eeuo pipefail

usage() {
    cat <<'HELP'
Usage: bash scripts/rebuild.sh [--wait-timeout SECONDS]

Rebuild images without build cache, refresh configured registry images, and
recreate every enabled Compose service. Named volumes and external data survive.
The health-check deadline defaults to 1800 seconds for model startup.
HELP
}

wait_timeout=1800
while (($#)); do
    case "$1" in
        --wait-timeout)
            if (($# < 2)) || [[ ! "$2" =~ ^[1-9][0-9]*$ ]]; then
                printf 'Error: --wait-timeout requires a positive number of seconds.\n' >&2
                exit 2
            fi
            wait_timeout=$2
            shift 2
            ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 2 ;;
    esac
done

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd -- "$repo_root"
# Keep normal .env, compose.override.yaml, and COMPOSE_* settings available.
compose() { docker compose --project-directory "$repo_root" "$@"; }

phase='preflight'
restarting=false
on_error() {
    local status=$?
    trap - ERR
    printf '\nRebuild failed during %s (exit %s).\n' "$phase" "$status" >&2
    if [[ "$restarting" == true ]]; then
        compose ps --all >&2 || true
        printf 'Inspect docker compose logs and fix the cause before retrying.\n' >&2
    else
        printf 'Existing containers have not been stopped.\n' >&2
    fi
    exit "$status"
}
trap on_error ERR

if ! command -v docker >/dev/null 2>&1; then
    printf 'Error: Docker CLI is required.\n' >&2
    exit 127
fi
docker info >/dev/null
compose version >/dev/null
up_help=$(compose up --help)
pull_help=$(compose pull --help)
if [[ "$up_help" != *'--wait-timeout'* || "$pull_help" != *'--ignore-buildable'* ]]; then
    printf 'Error: update Docker Compose v2 to support --wait-timeout and --ignore-buildable.\n' >&2
    exit 2
fi
compose config --quiet

phase='image build'
printf 'Rebuilding application images without build cache...\n'
compose build --pull --no-cache
phase='image pull'
printf 'Refreshing configured registry images...\n'
compose pull --ignore-buildable

phase='shutdown'
restarting=true
printf 'Stopping services and removing old containers (preserving named volumes)...\n'
compose down --remove-orphans --timeout 60
phase='startup and health checks'
printf 'Starting all services; waiting up to %s seconds for readiness...\n' "$wait_timeout"
compose up --detach --force-recreate --remove-orphans --no-build --pull never \
    --wait --wait-timeout "$wait_timeout"

phase='database readiness'
compose exec -T ledger-web python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/ready', timeout=10).close()"
phase='service status'
compose ps --all
printf '\nAll enabled services restarted; health checks and database readiness passed.\n'
