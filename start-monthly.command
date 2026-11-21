#!/bin/bash
# Double-click to start the card-discovery server (monthly scan, developer panel) and open it
# in the browser. Close this window, or press Ctrl+C, to stop the server.
HERE="$(cd "$(dirname "$0")" && pwd)"
MAIN="/Users/shubhamkadam/Documents/Projects/cc-automatic-card-discovery-vai-mail"
cd "$HERE" || exit 1

# One server at a time on port 8790: stop an earlier one first.
OLD=$(lsof -ti tcp:8790 -sTCP:LISTEN)
if [ -n "$OLD" ]; then
  echo "Stopping the server already running on port 8790"
  kill $OLD 2>/dev/null
  sleep 2
fi

export CD_SCAN_MODE=monthly
export CD_DATABASE_URL="postgresql://shubhamkadam@localhost:5432/carddiscovery"
export CD_HOME="$MAIN/.local"                       # vault keys shared with the main checkout
export CD_GOOGLE_CLIENT="$MAIN/credentials.json"    # Google sign-in client
export CARDDISCOVERY_DEV=1                          # this laptop only: "?user=<name>" test sign-ins

LOG="$(mktemp -t cdserver)"
"$MAIN/.venv/bin/python" -u -m cdserver dev 2>&1 | tee "$LOG" &
SERVER=$!
trap 'kill $SERVER 2>/dev/null' EXIT

# Open the app once the server prints its address (it includes the access token).
for _ in $(seq 1 30); do
  URL=$(grep -o 'http://127.0.0.1:8790/?t=[A-Za-z0-9_-]*' "$LOG" | head -1)
  [ -n "$URL" ] && break
  sleep 1
done
if [ -n "$URL" ]; then
  open "$URL"
else
  echo "The server didn't start; see the messages above."
fi
wait $SERVER
