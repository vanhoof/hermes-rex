#!/usr/bin/env bash
# End-to-end demo / smoke test of the Hermes x Rex plugin, meant to be run (and recorded)
# from inside a Rex pane. It opens Hermes in a pane to the right, talks to it through the
# rex CLI, and prints the Rex status records as they change.
#
#   scripts/demo.sh            run the full demo, close the Hermes pane at the end
#   scripts/demo.sh --keep     leave the Hermes pane open
#   scripts/demo.sh --tui      use the Hermes TUI instead of the classic CLI
#   scripts/demo.sh --no-question   skip the clarify step (no blocked/question state)
#   scripts/demo.sh --no-split      skip the step where Hermes splits its own pane
#
# Layout while it runs:   left, top: this script        right, top: Hermes
#                         left, bottom: the pane Hermes  right, bottom: live Rex status of the
#                                       opens for uptime                Hermes pane
# Requires: a Rex pane (REX_SESSION/REX_BLOCK set), the rex CLI on PATH, hermes on PATH.
set -u

# Internal: `demo.sh --monitor BLOCK` runs in the bottom-right pane and redraws that pane's status.
if [ "${1:-}" = "--monitor" ]; then
  BLOCK=$2; bold=$'\e[1m'; dim=$'\e[2m'; reset=$'\e[0m'
  while true; do
    printf '\e[H\e[2J%sRex program status%s  %spane %s%s\n\n' "$bold" "$reset" "$dim" "${BLOCK#block:}" "$reset"
    rex block call -s "$REX_SESSION" -b "$BLOCK" program_status 2>/dev/null | python3 -c '
import json,sys
recs=json.load(sys.stdin).get("records",[])
if not recs: print("  (no record yet)")
for x in recs:
    state=x.get("state","?"); kind=x.get("kind") or ""; msg=(x.get("msg") or "")[:70]
    app=x.get("effective_app") or x.get("app") or ""; at=(x.get("updated_at") or "")[11:19]
    glyph={"working":"\u25cf","blocked":"\u25a0","done":"\u2713","error":"\u2717","idle":"\u25cb"}.get(state,"?")
    print("  %s %s%s" % (glyph, state, " (%s)" % kind if kind else ""))
    print("    app   " + app)
    if msg: print("    msg   " + msg)
    print("    at    " + at)'
    sleep 1
  done
fi

KEEP=0; UI=hermes; ASK=1; SPLIT=1
for arg in "$@"; do
  case "$arg" in
    --keep) KEEP=1 ;;
    --tui) UI="hermes --tui" ;;
    --no-question) ASK=0 ;;
    --no-split) SPLIT=0 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

bold=$'\e[1m'; dim=$'\e[2m'; green=$'\e[32m'; yellow=$'\e[33m'; red=$'\e[31m'; cyan=$'\e[36m'; magenta=$'\e[35m'; reset=$'\e[0m'
say()  { printf '\n%s\n' "${bold}${cyan}$*${reset}"; }
ok()   { printf '%s\n' "  ${green}✓${reset} $*"; [ $RECORD -eq 1 ] && RESULTS+=("✓|$*"); return 0; }
bad()  { printf '%s\n' "  ${red}✗${reset} $*"; [ $RECORD -eq 1 ] && RESULTS+=("✗|$*"); return 0; }
note() { printf '%s\n' "  ${dim}$*${reset}"; }
RESULTS=(); RECORD=0   # only the numbered steps go into the summary
T0=$(date +%s)
banner() {
  local w=44 line="  Hermes × Rex  live demo" pad
  pad=$(printf '%*s' $((w - ${#line})) '')   # ${#line} counts characters, printf %-*s counts bytes
  printf '\n%s╭%s╮%s\n' "$magenta" "$(printf '─%.0s' $(seq 1 $w))" "$reset"
  printf '%s│%s%s%s%s%s%s│%s\n' "$magenta" "$reset" "$bold" "$line" "$pad" "$reset" "$magenta" "$reset"
  printf '%s╰%s╯%s\n' "$magenta" "$(printf '─%.0s' $(seq 1 $w))" "$reset"
}
summary() {
  local secs=$(( $(date +%s) - T0 )) passed=0 failed=0 r
  for r in "${RESULTS[@]}"; do case "$r" in ✓*) passed=$((passed+1)) ;; *) failed=$((failed+1)) ;; esac; done
  printf '\n%s%-48s%s\n' "$bold" "Summary" "$reset"
  for r in "${RESULTS[@]}"; do
    case "$r" in ✓*) printf '  %s✓%s %s\n' "$green" "$reset" "${r#✓|}" ;; *) printf '  %s✗%s %s\n' "$red" "$reset" "${r#✗|}" ;; esac
  done
  printf '  %s%d passed, %d failed, %ds%s\n' "$dim" "$passed" "$failed" "$secs" "$reset"
}

# --- prerequisites ---------------------------------------------------------------
banner
say "Checking prerequisites"
[ -n "${REX_SESSION:-}" ] && [ -n "${REX_BLOCK:-}" ] || { bad "not inside a Rex pane (REX_SESSION/REX_BLOCK unset)"; exit 1; }
command -v rex >/dev/null || { bad "rex CLI not on PATH"; exit 1; }
command -v hermes >/dev/null || { bad "hermes not on PATH"; exit 1; }
ok "Rex session ${REX_SESSION#session:}"
ok "rex CLI $(rex whoami --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("rex_version","found"))' 2>/dev/null || echo found)"
ok "$(hermes --version 2>/dev/null | head -1)"
if hermes plugins list 2>/dev/null | grep -qE '^\│ rex +\│ enabled'; then ok "plugin rex enabled"; else bad "plugin rex is not enabled (hermes plugins enable rex)"; exit 1; fi

status() {  # prints "state[/kind] msg" for the Hermes pane, or "-" when no record
  rex block call -s "$REX_SESSION" -b "$1" program_status 2>/dev/null | python3 -c '
import json,sys
r=json.load(sys.stdin).get("records",[])
print(" | ".join(f"{x["state"]}{"/"+x["kind"] if x.get("kind") else ""}  {(x.get("msg") or "")[:60]}" for x in r) or "-")'
}

watch() {  # watch <block> <seconds> [stop-state] [baseline]: print status changes until stop-state
           # or timeout. The baseline (default: the current record) is stale and does not count.
  local block=$1 secs=$2 stop=${3:-done} i=0 s initial last
  initial=${4:-$(status "$block")}; last=$initial
  printf '  %s%3ss%s  %s\n' "$dim" 0 "$reset" "$initial"
  while [ $i -lt "$secs" ]; do
    sleep 1; i=$((i+1))
    s=$(status "$block")
    if [ "$s" != "$last" ]; then printf '  %s%3ss%s  %s\n' "$dim" "$i" "$reset" "$s"; last=$s; fi
    [ "$s" = "$initial" ] && continue
    case "$s" in "$stop"*) return 0 ;; esac
  done
  return 1
}

wait_for_prompt() {  # wait until the pane shows a prompt
  local block=$1 i=0
  while [ $i -lt 40 ]; do
    if rex capture -s "$REX_SESSION" -b "$block" --trim --unwrap 2>/dev/null | grep -qE '❯'; then return 0; fi
    sleep 1; i=$((i+1))
  done
  return 1
}

send() { rex block send -s "$REX_SESSION" -b "$1" "$2"; sleep 1; rex block send-key -s "$REX_SESSION" -b "$1" enter; }

# --- open Hermes --------------------------------------------------------------------
say "Opening Hermes ($UI) in a pane to the right"
BLOCK=$(rex split -s "$REX_SESSION" -b "$REX_BLOCK" --split=right --ratio 55 --label "hermes demo" --focus=false --keep-open --json -- $UI \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["block_ids"][0])') || { bad "could not open the pane"; exit 1; }
ok "pane ${BLOCK#block:}"
say "Opening a live status monitor below it"
MON=$(rex split -s "$REX_SESSION" -b "$BLOCK" --split=below --ratio 30 --label "rex status" --focus=false --keep-open --json -- "$0" --monitor "$BLOCK" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["block_ids"][0])') || MON=""
[ -n "$MON" ] && ok "monitor pane ${MON#block:}" || bad "could not open the monitor pane (continuing)"
cleanup() {
  if [ $KEEP -eq 0 ]; then
    rex block close -s "$REX_SESSION" "$BLOCK" ${MON:+"$MON"} >/dev/null 2>&1 && note "closed the Hermes and monitor panes"
  else
    note "panes left open (cmd+w closes a pane)"
  fi
}
trap cleanup EXIT
wait_for_prompt "$BLOCK" && ok "Hermes is ready" || { bad "Hermes did not show a prompt in time"; exit 1; }

# --- 1. context: does Hermes know where it is? ----------------------------------------
RECORD=1
say "1. Asking Hermes where it runs (context injection + rex skill)"
send "$BLOCK" "Which Rex session and pane are you running in? State the REX_SESSION and REX_BLOCK ids you were given, confirm them with rex whoami --json, and tell me this session's label. Be brief."
watch "$BLOCK" 90 done || note "no done state within 90s"
if rex capture -s "$REX_SESSION" -b "$BLOCK" --trim --unwrap | grep -q "${BLOCK}"; then ok "Hermes named its own pane id"; else bad "Hermes did not mention its pane id"; fi
if rex capture -s "$REX_SESSION" -b "$BLOCK" --trim --unwrap | grep -q "${REX_SESSION}"; then ok "Hermes named its own session id"; else bad "Hermes did not mention its session id"; fi

# --- 2. blocked/question --------------------------------------------------------------
if [ $ASK -eq 1 ]; then
  say "2. Asking Hermes to ask us something (blocked / question state)"
  send "$BLOCK" "Use your clarify tool to ask me one question: tabs or spaces? Offer both as choices. Wait for my answer, then reply with it in one line."
  if watch "$BLOCK" 60 "blocked/question"; then
    ok "Rex shows blocked with kind question"
    note "answering with the highlighted choice (Enter)"
    BASE=$(status "$BLOCK")
    rex block send-key -s "$REX_SESSION" -b "$BLOCK" enter; sleep 2
    case "$(status "$BLOCK")" in blocked*) rex block send-key -s "$REX_SESSION" -b "$BLOCK" enter ;; esac
    watch "$BLOCK" 60 done "$BASE" || note "no done state within 60s"
  else
    bad "no blocked/question state seen"
  fi
fi

# --- 3. Hermes drives Rex: split its own pane, run a command, read it back ---------------
if [ $SPLIT -eq 1 ]; then
  say "3. Asking Hermes to open a pane below this one with the rex CLI, run uptime there, and read it back"
  BEFORE=$(rex block ls -s "$REX_SESSION" --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["blocks"]))')
  send "$BLOCK" "Using the rex CLI, open a pane below the pane $REX_BLOCK (not your own): run rex split -s $REX_SESSION -b $REX_BLOCK --split=below --ratio 30 --keep-open --wait --json -- uptime, read the new pane's output with rex capture -s $REX_SESSION -b <new block id>, tell me the load averages in one line, then close that pane with rex block close -s $REX_SESSION <new block id>. Be brief."
  watch "$BLOCK" 120 done || note "no done state within 120s"
  AFTER=$(rex block ls -s "$REX_SESSION" --json | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["blocks"]))')
  if rex capture -s "$REX_SESSION" -b "$BLOCK" --trim --unwrap | grep -qiE 'load average'; then ok "Hermes reported the load averages from its own split"; else bad "no load averages in the reply"; fi
  if [ "$AFTER" -le "$BEFORE" ]; then ok "Hermes closed the pane it opened ($BEFORE blocks before, $AFTER after)"; else note "a pane Hermes opened is still there ($BEFORE -> $AFTER blocks)"; fi
fi

# --- 4. wrap up -----------------------------------------------------------------------
say "4. Rex's final status record for the Hermes pane"
note "$(status "$BLOCK")"
if [ "${DEMO_SHOW_PANES:-0}" = 1 ]; then
  [ -n "${MON:-}" ] && { say "   Monitor pane"; rex capture -s "$REX_SESSION" -b "$MON" --trim --unwrap | grep -vE '^\s*$' | tail -8 | sed "s/^/  ${dim}│${reset} /"; }
  say "   Hermes pane"; rex capture -s "$REX_SESSION" -b "$BLOCK" --trim --unwrap | grep -vE '^\s*$|^[─╭╰│ ]+$' | tail -12 | cut -c1-100 | sed "s/^/  ${dim}│${reset} /"
fi
summary
