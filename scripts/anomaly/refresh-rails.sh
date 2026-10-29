#!/usr/bin/env bash
# Fetch into an isolated bare cache; export a fresh source-only snapshot per build.
set -Eeuo pipefail
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
output="$repo_root/build/anomaly"
cache="$output/rails.git"
remote=${ANOMALY_RAILS_REPOSITORY:-${ANOMALY_RAILS_URL:-https://github.com/ManchesterMakerspace/makerspace-rails-2026.git}}
ref=${ANOMALY_RAILS_REF:-refs/heads/master}
requested_commit=${ANOMALY_RAILS_COMMIT:-}
if [[ -z "$remote" || "$remote" == -* || "$remote" =~ [[:cntrl:]] || "$ref" == -* || "$ref" =~ [[:cntrl:]] ]]; then
    printf 'Invalid Rails repository or ref.\n' >&2
    exit 2
fi
# The repository address is recorded in image provenance. Credentials must come
# from Git's credential helper or SSH agent, never from this recorded address.
reject_credential_url() {
    printf 'Rails repository URLs must omit credentials, query strings, and fragments; use a credential helper or SSH agent.\n' >&2
    exit 2
}
if [[ "$remote" =~ ^([A-Za-z][A-Za-z0-9+.-]*)://([^/]*) ]]; then
    scheme=${BASH_REMATCH[1],,}
    authority=${BASH_REMATCH[2]}
    [[ "$remote" != *\?* && "$remote" != *\#* ]] || reject_credential_url
    if [[ "$authority" == *@* ]]; then
        [[ "$scheme" != http && "$scheme" != https ]] || reject_credential_url
        userinfo=${authority%@*}
        # Permit ssh://git@host, but no password or encoded userinfo delimiters.
        [[ "$userinfo" =~ ^[A-Za-z0-9._~-]+$ ]] || reject_credential_url
    fi
elif [[ "$remote" =~ ^([^/]+)@([^/:]+): ]]; then
    # Git's ordinary SCP-style SSH spelling, e.g. git@host:org/repository.git.
    [[ "${BASH_REMATCH[1]}" =~ ^[A-Za-z0-9._~-]+$ ]] || reject_credential_url
elif [[ "$remote" =~ ^[A-Za-z][A-Za-z0-9+.-]*:: ]]; then
    # Transport-helper wrappers can hide an inner credential-bearing URL.
    reject_credential_url
fi
if [[ -n "$requested_commit" && ! "$requested_commit" =~ ^([0-9a-f]{40}|[0-9a-f]{64})$ ]]; then
    printf 'ANOMALY_RAILS_COMMIT must be a full lowercase Git object ID.\n' >&2
    exit 2
fi
command -v git >/dev/null
command -v tar >/dev/null
mkdir -p -- "$output"
# The cache is always within this ignored build directory; never operate on ../Rails.
if [[ ! -d "$cache" ]]; then git init --bare --quiet "$cache"; fi
if [[ $(git --git-dir="$cache" rev-parse --is-bare-repository) != true ]]; then
    printf 'Rails schema cache must be a bare repository.\n' >&2
    exit 2
fi
printf 'Refreshing Rails schema source...\n'
git -c core.hooksPath=/dev/null --git-dir="$cache" fetch --quiet --force --no-tags --depth=1 -- "$remote" "$ref"
if [[ -n "$requested_commit" ]]; then
    # Still refresh the configured upstream on each build, then fetch the exact pin.
    git -c core.hooksPath=/dev/null --git-dir="$cache" fetch --quiet --force --no-tags --depth=1 -- "$remote" "$requested_commit"
fi
commit=$(git --git-dir="$cache" rev-parse --verify 'FETCH_HEAD^{commit}')
[[ "$commit" =~ ^[0-9a-f]{40,64}$ ]]
[[ -z "$requested_commit" || "$commit" == "$requested_commit" ]]
snapshot=$(mktemp -d "$output/snapshot.XXXXXXXX")
cleanup() { [[ -z "${snapshot:-}" ]] || rm -rf -- "$snapshot"; }
trap cleanup EXIT
git --git-dir="$cache" archive "$commit" app/models config/initializers/inflections.rb \
    docs/mongodb-collections.MD Gemfile.lock | tar -xf - -C "$snapshot"
# Do not permit a source symlink to redirect the parser outside its snapshot.
if [[ -n $(find "$snapshot" -type l -print -quit) ]]; then
    printf 'Schema source contains a symlink; review it before building.\n' >&2
    exit 2
fi
printf '%s\n' "$commit" > "$snapshot/rails.commit"
printf '%s\n' "$remote" > "$snapshot/rails.repository"
printf '%s\n' "$ref" > "$snapshot/rails.ref"
# Replacement is confined to build/anomaly; extraction never sees a partial tree.
rm -rf -- "$output/rails.previous"
if [[ -d "$output/rails" ]]; then mv -- "$output/rails" "$output/rails.previous"; fi
mv -- "$snapshot" "$output/rails"
snapshot=''
rm -rf -- "$output/rails.previous"
printf 'Rails schema source commit: %s\n' "$commit"
