#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
mkdir -p dist/.openai dist/server
cp .openai/hosting.json dist/.openai/hosting.json
cat lib/journal-checkpoint.mjs lib/journal-storage.mjs lib/access.mjs lib/oauth.mjs lib/bridge.mjs lib/native-call.mjs lib/ha.mjs lib/discovery.mjs lib/events.mjs lib/grok.mjs lib/agent-inbox.mjs lib/transport.mjs lib/decisions.mjs worker/index.js | sed '/^import .* from /d' > dist/server/index.js
