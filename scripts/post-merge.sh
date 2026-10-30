#!/usr/bin/env bash
set -euo pipefail

upm install --quiet

python -m compileall -q \
  config.py \
  main.py \
  core \
  handlers \
  services \
  utils