#!/usr/bin/env bash
# Download competition data needed for local development.
#
# Usage: scripts/fetch_data.sh <dest_dir> [split_file ...]
#
# Fetches everything except snapshots, then only the snapshots listed in the
# given split files. The full snapshot set is 21.5 GB (fastapi alone is 16.5 GB),
# so we pull per-task files instead of the whole competition archive.
# Competition data must not be redistributed, so keep <dest_dir> outside the repo.
set -euo pipefail

COMP=gemma-4-developer-agent
dest=${1:?usage: fetch_data.sh <dest_dir> [split_file ...]}
shift
mkdir -p "$dest"

list_files() {
    local token="" out
    while :; do
        out=$(kaggle competitions files "$COMP" --page-size 200 ${token:+--page-token "$token"})
        echo "$out" | awk '$1 !~ /^(Next|name|-)/ {print $1}'
        token=$(echo "$out" | sed -n 's/^Next Page Token = //p')
        [ -z "$token" ] && break
    done
}

fetch() {
    local f=$1
    [ -e "$dest/$f" ] && return 0
    # Some embedding files are legitimately 0 bytes (no graph data for that repo),
    # hence -e above and the touch below.
    kaggle competitions download "$COMP" -f "$f" -p "$dest/$(dirname "$f")" -q >/dev/null
    touch "$dest/$f"
}

list_files | grep -v '^snapshots/' | while read -r f; do fetch "$f"; done

for split in "$@"; do
    while read -r id; do
        [ -n "$id" ] && fetch "snapshots/$id.tgz"
    done < "$split"
done
echo "done: $(du -sh "$dest" | cut -f1) in $dest"
