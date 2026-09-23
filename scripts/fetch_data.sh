#!/usr/bin/env bash
# Download competition data needed for local development.
#
# Usage: [JOBS=6] scripts/fetch_data.sh <dest_dir> [split_file ...]
#
# Fetches everything except snapshots, then only the snapshots listed in the
# given split files. The full snapshot set is 21.5 GB (fastapi alone is 16.5 GB),
# so we pull per-task files instead of the whole competition archive.
# Competition data must not be redistributed, so keep <dest_dir> outside the repo.
#
# Safe to re-run: each file is downloaded into a temp dir and moved into place
# only once complete, so existing files are always whole and are skipped.
set -euo pipefail

export COMP=gemma-4-developer-agent
dest=${1:?usage: fetch_data.sh <dest_dir> [split_file ...]}
shift
mkdir -p "$dest"
export dest

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
    local f=$1 tmp
    [ -e "$dest/$f" ] && return 0
    tmp=$(mktemp -d "$dest/.part.XXXXXX")
    local attempt ok=0
    # The API intermittently returns an empty body under parallel load; back off and retry.
    for attempt in 1 2 3 4 5; do
        if kaggle competitions download "$COMP" -f "$f" -p "$tmp" -q >/dev/null 2>&1; then
            ok=1
            break
        fi
        sleep $((attempt * attempt * 3))
    done
    if [ "$ok" = 1 ]; then
        mkdir -p "$dest/$(dirname "$f")"
        # Kaggle may serve a file zipped.
        if [ -e "$tmp/$(basename "$f").zip" ]; then
            unzip -q -o "$tmp/$(basename "$f").zip" -d "$tmp" && rm "$tmp/$(basename "$f").zip"
        fi
        # Some embedding files are legitimately 0 bytes and may not be written at all.
        [ -e "$tmp/$(basename "$f")" ] || touch "$tmp/$(basename "$f")"
        mv "$tmp/$(basename "$f")" "$dest/$f"
    else
        echo "FAILED: $f" >&2
    fi
    rm -rf "$tmp"
}
export -f fetch

{
    list_files | grep -v '^snapshots/'
    for split in "$@"; do
        sed -e '/^$/d' -e 's#^#snapshots/#' -e 's#$#.tgz#' "$split"
    done
} | xargs -P "${JOBS:-6}" -I{} bash -c 'fetch "$1"' _ {}

echo "done: $(du -sh "$dest" | cut -f1) in $dest"
