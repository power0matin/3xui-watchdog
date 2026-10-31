#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=../lib/venv.sh
source "${ROOT_DIR}/lib/venv.sh"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
python_bin="${PYTHON:-python3}"

# 1. An existing directory without an interpreter must be recreated.
broken="${tmp_dir}/broken"
mkdir -p "$broken/bin"
printf 'partial\n' > "$broken/partial-marker"
ensure_virtualenv "$python_bin" "$broken"
test -x "$broken/bin/python"
"$broken/bin/python" -m pip --version >/dev/null
test ! -e "$broken/partial-marker"

# 2. A healthy venv is reused, even if its pip launcher is absent.
healthy="${tmp_dir}/healthy"
"$python_bin" -m venv "$healthy"
rm -f "$healthy/bin/pip" "$healthy/bin/pip3" "$healthy/bin/pip3."*
ensure_virtualenv "$python_bin" "$healthy"
test -x "$healthy/bin/python"
"$healthy/bin/python" -m pip --version >/dev/null
test ! -e "$healthy/bin/pip"

# 3. A healthy environment remains usable on a subsequent run.
marker="${healthy}/reuse-marker"
: > "$marker"
ensure_virtualenv "$python_bin" "$healthy"
test -e "$marker"

printf 'installer virtualenv regression tests: PASS\n'
