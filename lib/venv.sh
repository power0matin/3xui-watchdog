#!/usr/bin/env bash
# Helpers for creating and repairing the installer's dedicated virtualenv.

ensure_virtualenv() {
    local python_bin="$1"
    local venv_dir="$2"
    local venv_python="${venv_dir}/bin/python"

    if [[ -x "$venv_python" ]]; then
        if "$venv_python" -m pip --version >/dev/null 2>&1; then
            return 0
        fi

        # A valid venv may only be missing pip; try the stdlib bootstrap first.
        if "$venv_python" -m ensurepip --upgrade >/dev/null 2>&1 \
            && "$venv_python" -m pip --version >/dev/null 2>&1; then
            return 0
        fi
    fi

    # The directory exists but is unusable (missing/broken interpreter or pip).
    # It is installer-managed, so recreate it rather than leaving a broken state.
    rm -rf -- "$venv_dir"
    if ! "$python_bin" -m venv "$venv_dir"; then
        printf '[fail] failed to create virtualenv at %s\n' "$venv_dir" >&2
        return 1
    fi

    venv_python="${venv_dir}/bin/python"
    if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
        if ! "$venv_python" -m ensurepip --upgrade >/dev/null 2>&1 \
            || ! "$venv_python" -m pip --version >/dev/null 2>&1; then
            printf '[fail] virtualenv was created but pip is unavailable: %s\n' "$venv_dir" >&2
            return 1
        fi
    fi
}
