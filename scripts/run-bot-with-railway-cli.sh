#!/usr/bin/env bash
set -euo pipefail

railway_bin="${HOME}/.railway/bin/railway"

if [[ ! -x "${railway_bin}" ]] || ! "${railway_bin}" api --help >/dev/null 2>&1; then
    curl -fsSL https://railway.com/install.sh \
        | sh -s -- --force --bin-dir "${HOME}/.railway/bin"
fi

export PATH="${HOME}/.railway/bin:${PATH}"
export RAILWAY_CLI_PATH="${RAILWAY_CLI_PATH:-${railway_bin}}"

exec python main.py
