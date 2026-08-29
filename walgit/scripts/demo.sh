#!/usr/bin/env bash
# End-to-end demo: real git, real S3, real compaction.
#
# The point of this script is the moment near the end where it deletes the
# server's entire on-disk state and clones the repository anyway.
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8099}"
REPO="${REPO:-demo}"
CACHE="$PWD/.walgit-cache"
WORK="$(mktemp -d)"
URL="http://127.0.0.1:$PORT/$REPO"

export WALGIT_CACHE_DIR="$CACHE"
export GIT_AUTHOR_NAME=demo GIT_AUTHOR_EMAIL=demo@example.com
export GIT_COMMITTER_NAME=demo GIT_COMMITTER_EMAIL=demo@example.com

step() { printf '\n\033[1;36m=== %s\033[0m\n' "$*"; }
run()  { printf '\033[2m$ %s\033[0m\n' "$*"; eval "$@"; }

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  rm -rf "$WORK"
}
trap cleanup EXIT

step "0. clean slate"
run "uv run python -m walgit destroy-repo $REPO" || true

step "1. start the walgit server"
uv run python -m walgit serve --port "$PORT" >"$WORK/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 40); do
  curl -sf "http://127.0.0.1:$PORT/$REPO/info/refs?service=git-upload-pack" >/dev/null && break
  sleep 0.25
done
echo "serving on http://127.0.0.1:$PORT (pid $SERVER_PID)"

step "2. an ordinary git repo pushes to it"
run "git init --quiet -b main '$WORK/src'"
for i in 1 2 3; do
  echo "line $i" >>"$WORK/src/README.md"
  git -C "$WORK/src" add README.md
  git -C "$WORK/src" commit --quiet -m "commit $i"
  run "git -C '$WORK/src' push --quiet $URL main"
done

step "3. what is actually in S3"
run "uv run python -m walgit show $REPO"
echo
echo "Three pushes -> three WAL entries -> three packfiles. Each push became"
echo "visible only when its pointer won a compare-and-swap on index.json."

step "4. clone it back"
run "git clone --quiet $URL '$WORK/clone1'"
run "git -C '$WORK/clone1' log --oneline"

step "5. compact: three packs collapse into one base"
run "uv run python -m walgit compact $REPO --min-entries 2"
run "uv run python -m walgit show $REPO"

step "6. sweep the superseded packs"
run "uv run python -m walgit gc $REPO --grace 0"

step "7. delete the server's entire local state"
run "rm -rf '$CACHE/$REPO.git'"
echo "There is now no copy of this repository anywhere except S3."

step "8. clone again -- the WAL rebuilds the repo on demand"
run "git clone --quiet $URL '$WORK/clone2'"
run "git -C '$WORK/clone2' log --oneline"
run "git -C '$WORK/clone2' fsck --no-progress"

step "9. and it still accepts pushes"
echo "line 4" >>"$WORK/src/README.md"
git -C "$WORK/src" add README.md
git -C "$WORK/src" commit --quiet -m "commit 4"
run "git -C '$WORK/src' push $URL main"
run "uv run python -m walgit show $REPO"

printf '\n\033[1;32mdone.\033[0m server log: %s\n' "$WORK/server.log"
printf 'MinIO console: http://127.0.0.1:9001 (walgit / walgitwalgit)\n'
# `|| true`: with no terminal attached (make demo in CI, or stdin redirected) read hits
# EOF and returns 1, which set -e would turn into a failed demo after every step passed.
read -rp $'\npress enter to shut down the server and clean up... ' || true
