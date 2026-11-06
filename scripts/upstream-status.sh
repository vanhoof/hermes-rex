#!/usr/bin/env bash
# Print the state of the upstream Hermes issues and PRs this plugin tracks. Needs the gh CLI.
set -u
ITEMS="138147 138152 141194 146211 148669 149627"
command -v gh >/dev/null || { echo "gh CLI not found" >&2; exit 1; }
printf '%-7s %-6s %-7s %s\n' "number" "kind" "state" "title"
for n in $ITEMS; do
  gh api "repos/NousResearch/hermes-agent/issues/$n" \
    --jq '"\(.number)\t\(if .pull_request then "PR" else "issue" end)\t\(.state)\(if .state == "closed" and .pull_request then (if .pull_request.merged_at then " (merged)" else " (unmerged)" end) else "" end)\t\(.title)"' 2>/dev/null \
    | awk -F'\t' '{printf "%-7s %-6s %-7s %s\n", $1, $2, $3, $4}' || printf '%-7s %s\n' "$n" "(lookup failed)"
done
