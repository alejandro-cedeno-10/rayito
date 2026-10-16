#!/usr/bin/env bash
set -euo pipefail
IFS= read -r prompt || true
echo "PONG ${prompt}"
