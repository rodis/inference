#!/bin/sh
# Point git at the tracked hooks directory. Run once per clone.
#
# ABSOLUTE on purpose: `core.hooksPath` lives in the shared config, so a relative `.githooks`
# resolves against EACH worktree's own root — and a linked worktree checked out at a commit
# without the directory would then have no hook, which is exactly the case this polices.
set -eu
cd "$(dirname "$0")/.."
root=$(git rev-parse --show-toplevel)
git config core.hooksPath "$root/.githooks"
echo "core.hooksPath -> $(git config --get core.hooksPath)"
