#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
mkdir -p dist/.openai dist/server
cp .openai/hosting.json dist/.openai/hosting.json
cat lib/access.mjs lib/oauth.mjs lib/bridge.mjs lib/events.mjs worker/index.js | sed '/^import .* from /d' > dist/server/index.js
