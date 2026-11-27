#!/usr/bin/env bash
# The approval flow through the HTTP API, with curl (PLAN.md Phase 5, "Manual check").
#
#   1. Sign in as a private demo copy of Dana, whose $349.99 headphones arrived cracked.
#   2. Ask for a refund, then confirm it. Over $100, it waits for an operator.
#   3. Sign the same browser in as the demo operator, and approve it.
#   4. The agent tells Dana in the chat; the live event stream shows every step.
#
# Needs the stack running (make up, make seed) with ANTHROPIC_API_KEY and RELAY_DEMO_MODE=true in
# .env. Costs a few cents of model calls. The model decides its own wording and pace: if it asks
# a question instead of previewing the refund, the script says so and stops.
set -euo pipefail

BASE=${BASE:-http://localhost:8080/api}
ORIGIN=${ORIGIN:-http://localhost:8080}
work=$(mktemp -d)
jar="$work/cookies"
events="$work/events.log"
trap 'kill "${stream:-}" 2>/dev/null || true; rm -rf "$work"' EXIT

call() {  # call METHOD PATH [JSON]
  local args=(--silent --show-error --fail-with-body -b "$jar" -c "$jar" -X "$1" "$BASE$2"
    -H "Origin: $ORIGIN" -H "Content-Type: application/json")
  if [[ $# -ge 3 ]]; then args+=(--data "$3"); fi
  curl "${args[@]}"
}
json() {  # json EXPRESSION: evaluate a Python expression on the JSON from stdin, bound to `d`
  python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"
}
turns_done() { grep -c '^event: turn.completed' "$events" || true; }
wait_for_turn() {  # wait until the stream has shown $1 completed turns
  for _ in $(seq 1 120); do
    if [[ $(turns_done) -ge $1 ]]; then return; fi
    sleep 1
  done
  echo "No reply after two minutes." >&2; exit 1
}
say() {  # say TEXT: send a customer message and print the agent's reply
  local before; before=$(turns_done)
  echo "dana>  $1"
  call POST "/conversations/$conversation/messages" \
    "$(python3 -c 'import json, sys; print(json.dumps({"text": sys.argv[1]}))' "$1")" > /dev/null
  wait_for_turn $((before + 1))
  call GET "/conversations/$conversation" | json 'd["messages"][-1]["text"]' | sed 's/^/agent> /'
}

echo "== 1. Sign in as Dana (a private demo copy)"
call POST /auth/demo '{"persona": "dana"}' | json 'd["customer"]'
conversation=$(call POST /conversations | json 'd["id"]')
echo "conversation $conversation"
curl --silent --no-buffer -b "$jar" "$BASE/conversations/$conversation/events" > "$events" &
stream=$!
sleep 1

echo "== 2. Ask for the refund, then confirm it"
say "Hi, the headphones I ordered arrived with a cracked headband. Can I get a refund?"
say "Yes, please go ahead with the full refund to my card."

echo "== 3. Approve it as the demo operator (same browser, so it sees Dana's request)"
call POST /auth/demo-operator | json 'd["operator"]'
queue=$(call GET "/console/approvals?status=pending")
approval=$(json 'd[0]["approval_number"] if d else ""' <<< "$queue")
if [[ -z $approval ]]; then
  echo "Nothing is waiting for approval: the agent didn't submit the refund in two turns." >&2
  echo "Continue by hand, or run the script again." >&2
  exit 1
fi
json '"%s: %s ($%.2f) for %s" % (d[0]["approval_number"], d[0]["summary"], d[0]["amount_cents"] / 100, d[0]["customer_name"])' <<< "$queue"
before=$(turns_done)
call POST "/console/approvals/$approval/decision" \
  '{"decision": "approve", "note": "Photo of the crack checks out."}' | json 'd["status"], d["outcome"]'

echo "== 4. The agent resumes and tells Dana"
wait_for_turn $((before + 1))
call GET "/conversations/$conversation" | json 'd["messages"][-1]["text"]' | sed 's/^/agent> /'

echo "== The event stream (deltas left out)"
grep '^event:' "$events" | grep -v 'message.delta' | sed 's/^event: /  /'
echo "== Audit and trace: open http://localhost:6006 (Phoenix) and find the session $conversation"
