#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")/.."
node scripts/build-dashboard.mjs
mkdir -p dist/.openai dist/server
cp .openai/hosting.json dist/.openai/hosting.json
node scripts/bundle-worker.mjs
