#!/usr/bin/env bash
#
# 3xui-watchdog installer
#
# Usage (mirrors 3x-ui's own installer UX):
#   bash <(curl -Ls https://raw.githubusercontent.com/power0matin/3xui-watchdog/main/install.sh)
#   bash <(curl -Ls https://raw.githubusercontent.com/power0matin/3xui-watchdog/main/install.sh) --uninstall
#
# What this does:
#   1. Detects your package manager and installs git/python3.11+/venv if missing
#   2. Clones (or updates) the repo into /opt/3xui-watchdog
#   3. Creates an isolated virtualenv and installs the package into it
#      (never touches system Python packages, no --break-system-packages needed)
#   4. Creates a dedicated unprivileged system user to run the daemon
#   5. Lays down /etc/3xui-watchdog/config.yaml from the example (only if one
#      doesn't already exist — never overwrites your config on re-run)
#   6. Installs and enables (but does not start) the systemd service, so you
#      have a chance to edit config.yaml with real panel credentials first
#
# Safe to re-run: re-running updates the code/venv in place and leaves your
# config.yaml and any already-running service alone unless you pass --force.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (override via flags — see --help)
# ---------------------------------------------------------------------------
REPO_URL="https://github.com/power0matin/3xui-watchdog.git"
REPO_REF="main"
INSTALL_DIR="/opt/3xui-watchdog"
CONFIG_DIR="/etc/3xui-watchdog"
SERVICE_USER="xui-watchdog"
SERVICE_NAME="3xui-watchdog"
SKIP_SERVICE=false
FORCE=false
DO_UNINSTALL=false

# ---------------------------------------------------------------------------
# Pretty output (falls back to plain text if the terminal has no color)
# ---------------------------------------------------------------------------
if [[ -t 1 ]]; then
    C_RESET='\033[0m'; C_RED='\033[0;31m'; C_GREEN='\033[0;32m'
    C_YELLOW='\033[0;33m'; C_BLUE='\033[0;34m'; C_BOLD='\033[1m'
else
    C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_BOLD=''
fi

log_info()  { echo -e "${C_BLUE}[info]${C_RESET} $*"; }
log_ok()    { echo -e "${C_GREEN}[ ok ]${C_RESET} $*"; }
log_warn()  { echo -e "${C_YELLOW}[warn]${C_RESET} $*"; }
log_err()   { echo -e "${C_RED}[fail]${C_RESET} $*" >&2; }
die()       { log_err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------
print_help() {
    cat <<EOF
3xui-watchdog installer

Usage: install.sh [options]

Options:
  --ref <branch|tag>       Git ref to install (default: ${REPO_REF})
  --repo <url>             Git repo URL to install from (default: upstream)
  --install-dir <path>     Where to clone/install the code (default: ${INSTALL_DIR})
  --config-dir <path>      Where config.yaml lives (default: ${CONFIG_DIR})
  --service-user <name>    System user to run the daemon as (default: ${SERVICE_USER})
  --no-service             Skip systemd unit install (Docker/cron users)
  --force                  Overwrite an existing config.yaml too
  --uninstall              Remove the service, code, and venv (keeps config.yaml
                           and logs unless --force is also given)
  -h, --help               Show this help and exit

Examples:
  # Standard install
  bash <(curl -Ls https://raw.githubusercontent.com/power0matin/3xui-watchdog/main/install.sh)

  # Install a specific tagged release, skip systemd (you'll run via cron/Docker)
  bash <(curl -Ls .../install.sh) --ref v0.1.0 --no-service

  # Uninstall
  bash <(curl -Ls .../install.sh) --uninstall
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --ref) REPO_REF="$2"; shift 2 ;;
        --repo) REPO_URL="$2"; shift 2 ;;
        --install-dir) INSTALL_DIR="$2"; shift 2 ;;
        --config-dir) CONFIG_DIR="$2"; shift 2 ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --no-service) SKIP_SERVICE=true; shift ;;
        --force) FORCE=true; shift ;;
        --uninstall) DO_UNINSTALL=true; shift ;;
        -h|--help) print_help; exit 0 ;;
        *) die "unknown option: $1 (see --help)" ;;
    esac
done

# ---------------------------------------------------------------------------
# Preconditions
# ---------------------------------------------------------------------------
require_root() {
    if [[ "${EUID}" -ne 0 ]]; then
        die "this installer must be run as root (try: sudo bash <(curl -Ls ...))"
    fi
}

detect_os() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        OS_ID="${ID:-unknown}"
        OS_ID_LIKE="${ID_LIKE:-}"
    else
        die "cannot detect OS: /etc/os-release not found (unsupported distro)"
    fi
}

detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MGR="apt"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MGR="dnf"
    elif command -v yum >/dev/null 2>&1; then
        PKG_MGR="yum"
    elif command -v apk >/dev/null 2>&1; then
        PKG_MGR="apk"
    else
        die "no supported package manager found (need apt, dnf, yum, or apk)"
    fi
    log_info "detected package manager: ${PKG_MGR}"
}

pkg_install() {
    case "$PKG_MGR" in
        apt)
            export DEBIAN_FRONTEND=noninteractive
            apt-get update -qq
            apt-get install -y -qq "$@"
            ;;
        dnf) dnf install -y -q "$@" ;;
        yum) yum install -y -q "$@" ;;
        apk) apk add --no-cache "$@" ;;
    esac
}

# ---------------------------------------------------------------------------
# Dependencies: git, python3.11+, venv, pip
# ---------------------------------------------------------------------------
ensure_git() {
    if command -v git >/dev/null 2>&1; then
        log_ok "git already installed"
        return
    fi
    log_info "installing git..."
    pkg_install git
}

python_version_ok() {
    # returns 0 (true) if $1 is a python3 binary that's >= 3.11
    local bin="$1"
    command -v "$bin" >/dev/null 2>&1 || return 1
    "$bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null
}

ensure_python() {
    for candidate in python3.12 python3.11 python3; do
        if python_version_ok "$candidate"; then
            PYTHON_BIN="$candidate"
            log_ok "using $($candidate --version 2>&1) at $(command -v "$candidate")"
            return
        fi
    done

    log_info "no Python >= 3.11 found, attempting to install one..."
    case "$PKG_MGR" in
        apt)
            # Ubuntu 22.04 ships 3.10, 24.04 ships 3.12 — try 3.11 explicitly,
            # then fall back to whatever python3 the distro provides.
            pkg_install python3.11 python3.11-venv 2>/dev/null \
                || pkg_install python3 python3-venv
            ;;
        dnf) pkg_install python3.11 python3-pip 2>/dev/null || pkg_install python3 python3-pip ;;
        yum) pkg_install python3.11 python3-pip 2>/dev/null || pkg_install python3 python3-pip ;;
        apk) pkg_install python3 py3-pip py3-virtualenv ;;
    esac

    for candidate in python3.12 python3.11 python3; do
        if python_version_ok "$candidate"; then
            PYTHON_BIN="$candidate"
            log_ok "using $($candidate --version 2>&1) at $(command -v "$candidate")"
            return
        fi
    done

    die "could not find or install Python 3.11+. Please install it manually and re-run this script."
}

ensure_venv_module() {
    if ! "$PYTHON_BIN" -c 'import venv' 2>/dev/null; then
        log_info "installing python venv module..."
        case "$PKG_MGR" in
            apt) pkg_install "${PYTHON_BIN}-venv" 2>/dev/null || pkg_install python3-venv ;;
            dnf|yum) pkg_install python3-venv 2>/dev/null || true ;;
            apk) pkg_install py3-virtualenv ;;
        esac
    fi
}

# ---------------------------------------------------------------------------
# Service user
# ---------------------------------------------------------------------------
ensure_service_user() {
    if id "$SERVICE_USER" >/dev/null 2>&1; then
        log_ok "system user '${SERVICE_USER}' already exists"
        return
    fi
    log_info "creating system user '${SERVICE_USER}'..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER" \
        || useradd --system --shell /sbin/nologin "$SERVICE_USER"  # some distros lack /usr/sbin/nologin
}

# ---------------------------------------------------------------------------
# Clone/update source, build venv, install package
# ---------------------------------------------------------------------------
clone_or_update_repo() {
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        log_info "existing checkout found at ${INSTALL_DIR}, updating..."
        git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF"
        git -C "$INSTALL_DIR" checkout -q FETCH_HEAD
    else
        log_info "cloning ${REPO_URL} (${REPO_REF}) into ${INSTALL_DIR}..."
        mkdir -p "$(dirname "$INSTALL_DIR")"
        git clone --depth 1 --branch "$REPO_REF" "$REPO_URL" "$INSTALL_DIR" 2>/dev/null \
            || git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"  # ref might be a commit, not a branch/tag
    fi
    log_ok "source ready at ${INSTALL_DIR}"
}

setup_venv_and_install() {
    local venv_dir="${INSTALL_DIR}/venv"
    if [[ ! -d "$venv_dir" ]]; then
        log_info "creating virtualenv at ${venv_dir}..."
        "$PYTHON_BIN" -m venv "$venv_dir"
    fi

    log_info "installing 3xui-watchdog (+ grpc extras) into the virtualenv..."
    "${venv_dir}/bin/pip" install --quiet --upgrade pip
    "${venv_dir}/bin/pip" install --quiet "${INSTALL_DIR}[grpc]"
    log_ok "package installed into ${venv_dir}"

    # Symlink the entrypoint somewhere on PATH for convenience (systemd unit
    # below uses the absolute venv path directly, so this is just for
    # anyone who wants to run `xui-watchdog --once` by hand).
    ln -sf "${venv_dir}/bin/xui-watchdog" /usr/local/bin/xui-watchdog
}

# ---------------------------------------------------------------------------
# Config + systemd
# ---------------------------------------------------------------------------
setup_config() {
    mkdir -p "$CONFIG_DIR"
    local target="${CONFIG_DIR}/config.yaml"
    if [[ -f "$target" && "$FORCE" != true ]]; then
        log_warn "config.yaml already exists at ${target} — leaving it untouched (use --force to overwrite)"
        return
    fi
    cp "${INSTALL_DIR}/config.example.yaml" "$target"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$target"
    chmod 640 "$target"
    log_ok "wrote example config to ${target} (edit this before starting the service)"
}

install_systemd_unit() {
    if [[ "$SKIP_SERVICE" == true ]]; then
        log_info "--no-service given, skipping systemd unit install"
        return
    fi
    if ! command -v systemctl >/dev/null 2>&1; then
        log_warn "systemctl not found — skipping systemd unit install. Use --once with cron instead."
        return
    fi

    local unit_path="/etc/systemd/system/${SERVICE_NAME}.service"
    local venv_bin="${INSTALL_DIR}/venv/bin/xui-watchdog"

    sed \
        -e "s#/usr/local/bin/xui-watchdog#${venv_bin}#" \
        -e "s#/etc/3xui-watchdog#${CONFIG_DIR}#g" \
        -e "s#User=xui-watchdog#User=${SERVICE_USER}#" \
        -e "s#Group=xui-watchdog#Group=${SERVICE_USER}#" \
        "${INSTALL_DIR}/systemd/3xui-watchdog.service" > "$unit_path"

    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
    log_ok "installed and enabled systemd service '${SERVICE_NAME}' (not started yet)"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------
do_uninstall() {
    log_info "uninstalling 3xui-watchdog..."

    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
        systemctl daemon-reload
        log_ok "removed systemd service"
    fi

    rm -f /usr/local/bin/xui-watchdog
    rm -rf "$INSTALL_DIR"
    log_ok "removed ${INSTALL_DIR}"

    if [[ "$FORCE" == true ]]; then
        rm -rf "$CONFIG_DIR"
        log_ok "removed ${CONFIG_DIR} (--force given)"
    else
        log_info "kept ${CONFIG_DIR} (config.yaml + any logs) — pass --force to remove it too"
    fi

    log_ok "uninstall complete. The '${SERVICE_USER}' system user was left in place; remove it yourself with:"
    echo "    userdel ${SERVICE_USER}"
    exit 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
require_root
detect_os
detect_pkg_manager

if [[ "$DO_UNINSTALL" == true ]]; then
    do_uninstall
fi

ensure_git
ensure_python
ensure_venv_module
ensure_service_user
clone_or_update_repo
setup_venv_and_install
setup_config
install_systemd_unit

echo
log_ok "${C_BOLD}3xui-watchdog installed.${C_RESET}"
echo
echo "  Next steps:"
echo "    1. Edit your config:   nano ${CONFIG_DIR}/config.yaml"
echo "       (set panel.base_url, panel.username/password or api_token,"
echo "        and xray_grpc.host/port for your server)"
if [[ "$SKIP_SERVICE" != true ]] && command -v systemctl >/dev/null 2>&1; then
echo "    2. Start it:            systemctl start ${SERVICE_NAME}"
echo "    3. Check the logs:      journalctl -u ${SERVICE_NAME} -f"
echo "    4. Dry-run first if you want to see what it *would* do with no risk:"
echo "       ${INSTALL_DIR}/venv/bin/xui-watchdog --config ${CONFIG_DIR}/config.yaml --dry-run --once"
else
echo "    2. Run it directly, or wire it into cron with --once:"
echo "       ${INSTALL_DIR}/venv/bin/xui-watchdog --config ${CONFIG_DIR}/config.yaml --once"
fi
echo
echo "  To uninstall later:"
echo "    bash <(curl -Ls https://raw.githubusercontent.com/power0matin/3xui-watchdog/main/install.sh) --uninstall"
echo
