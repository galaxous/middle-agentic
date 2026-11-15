#!/usr/bin/env bash
#
# bootstrap-unix.sh - Prepares a Linux (Ubuntu/Debian/Raspberry Pi OS/Fedora/RHEL) or macOS host
# as an SSH-reachable middle-agent node.
#
# Idempotent. Safe to run multiple times. Performs:
#   1. Installs openssh-server, git, sqlite3, python3 (+venv/pip) via apt, dnf, yum or brew.
#   2. Enables the SSH service (systemd on Linux, Remote Login on macOS) and opens the firewall
#      when ufw/firewalld is active.
#   3. Creates ~/.ssh (700) and ~/.ssh/authorized_keys (600), appending --pubkey if given.
#   4. Creates ./venv in the project directory and installs requirements.txt quietly.
#
# Usage:
#   ./bootstrap-unix.sh --pubkey "ssh-ed25519 AAAA... me@macbook"
#   curl -fsSL https://raw.githubusercontent.com/galaxous/middle-agentic/main/bootstrap-unix.sh | bash -s -- --pubkey "ssh-ed25519 AAAA..."
#
# Compatible with bash 3.2 (default macOS bash).

set -euo pipefail

PUBKEY=""
PROJECT_DIR=""
SKIP_PACKAGES=0
SKIP_VENV=0

# ------------------------------------------------------------------ output helpers
if [[ -t 1 ]]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_RESET=$'\033[0m'
else
    C_STEP=""; C_OK=""; C_WARN=""; C_ERR=""; C_RESET=""
fi
step() { printf '%s==> %s%s\n' "$C_STEP" "$*" "$C_RESET"; }
ok()   { printf '%s    [OK] %s%s\n' "$C_OK" "$*" "$C_RESET"; }
warn() { printf '%s    [WARN] %s%s\n' "$C_WARN" "$*" "$C_RESET" >&2; }
die()  { printf '%s    [ERROR] %s%s\n' "$C_ERR" "$*" "$C_RESET" >&2; exit 1; }

usage() {
    cat <<'EOF'
Usage: bootstrap-unix.sh [options]

Options:
  --pubkey "<key>"       OpenSSH public key to append to ~/.ssh/authorized_keys
  --project-dir <path>   Directory containing requirements.txt (default: script directory)
  --skip-packages        Do not install system packages
  --skip-venv            Do not create the Python virtual environment
  -h, --help             Show this help
EOF
}

# ------------------------------------------------------------------ argument parsing
while [[ $# -gt 0 ]]; do
    case "$1" in
        --pubkey)        [[ $# -ge 2 ]] || die "--pubkey requires a value"; PUBKEY="$2"; shift 2 ;;
        --pubkey=*)      PUBKEY="${1#*=}"; shift ;;
        --project-dir)   [[ $# -ge 2 ]] || die "--project-dir requires a value"; PROJECT_DIR="$2"; shift 2 ;;
        --project-dir=*) PROJECT_DIR="${1#*=}"; shift ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        --skip-venv)     SKIP_VENV=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               usage; die "Unknown option: $1" ;;
    esac
done

# When piped through `curl | bash`, BASH_SOURCE is empty: fall back to the current directory.
if [[ -z "$PROJECT_DIR" ]]; then
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    else
        PROJECT_DIR="$PWD"
    fi
fi

OS_NAME="$(uname -s)"
case "$OS_NAME" in
    Linux|Darwin) ;;
    *) die "Unsupported OS: $OS_NAME (use bootstrap-windows.ps1 on Windows)" ;;
esac

# Run privileged commands through sudo only when we are not root already.
run_root() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        die "Root privileges required for: $* (install sudo or run as root)"
    fi
}

# Keys and venv belong to the human user, even when the script is invoked via sudo.
TARGET_USER="${SUDO_USER:-$(id -un)}"
if [[ "$OS_NAME" == "Darwin" ]]; then
    TARGET_HOME="$(dscl . -read "/Users/$TARGET_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
else
    TARGET_HOME="$(getent passwd "$TARGET_USER" 2>/dev/null | cut -d: -f6)"
fi
TARGET_HOME="${TARGET_HOME:-$HOME}"
TARGET_GROUP="$(id -gn "$TARGET_USER")"

# ------------------------------------------------------------------ package management
PKG_MANAGER=""

load_brew_env() {
    local candidate
    for candidate in /opt/homebrew/bin/brew /usr/local/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; do
        if [[ -x "$candidate" ]]; then
            eval "$("$candidate" shellenv)"
            return 0
        fi
    done
    return 1
}

# Homebrew refuses to run as root, so drop back to the invoking user when needed.
run_brew() {
    if [[ "$(id -u)" -eq 0 ]]; then
        [[ -n "${SUDO_USER:-}" ]] || die "Homebrew cannot run as root; re-run as a regular user."
        sudo -u "$SUDO_USER" -H brew "$@"
    else
        brew "$@"
    fi
}

detect_package_manager() {
    if [[ "$OS_NAME" == "Darwin" ]]; then
        if ! command -v brew >/dev/null 2>&1 && ! load_brew_env; then
            step "Homebrew not found, installing it (non-interactive)"
            [[ "$(id -u)" -ne 0 ]] || die "Install Homebrew as a regular user first: https://brew.sh"
            NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
            load_brew_env || die "Homebrew installation finished but brew is not on PATH"
        fi
        PKG_MANAGER="brew"
    elif command -v apt-get >/dev/null 2>&1; then
        PKG_MANAGER="apt"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MANAGER="dnf"
    elif command -v yum >/dev/null 2>&1; then
        PKG_MANAGER="yum"
    else
        die "No supported package manager found (apt-get, dnf, yum, brew)"
    fi
}

required_packages() {
    case "$PKG_MANAGER" in
        apt)     echo "openssh-server git sqlite3 python3 python3-venv python3-pip" ;;
        dnf|yum) echo "openssh-server git sqlite python3 python3-pip" ;;
        # macOS ships its own sshd; brew only provides the tooling.
        brew)    echo "git sqlite python" ;;
    esac
}

is_package_installed() {
    local pkg="$1"
    case "$PKG_MANAGER" in
        apt)     dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "install ok installed" ;;
        dnf|yum) rpm -q "$pkg" >/dev/null 2>&1 ;;
        brew)    run_brew list --versions "$pkg" >/dev/null 2>&1 ;;
    esac
}

install_packages() {
    step "Checking system packages via $PKG_MANAGER"
    local missing=() pkg
    for pkg in $(required_packages); do
        if is_package_installed "$pkg"; then
            ok "$pkg already installed"
        else
            missing+=("$pkg")
        fi
    done

    if [[ ${#missing[@]} -eq 0 ]]; then
        return 0
    fi

    echo "    Installing: ${missing[*]}"
    case "$PKG_MANAGER" in
        apt)
            run_root env DEBIAN_FRONTEND=noninteractive apt-get update -qq
            run_root env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${missing[@]}" >/dev/null
            ;;
        dnf) run_root dnf install -y -q "${missing[@]}" ;;
        yum) run_root yum install -y -q "${missing[@]}" ;;
        brew) run_brew install --quiet "${missing[@]}" ;;
    esac
    ok "Installed: ${missing[*]}"
}

# ------------------------------------------------------------------ SSH service
ssh_port_open() {
    (exec 3<>/dev/tcp/127.0.0.1/22) >/dev/null 2>&1
}

enable_ssh_linux() {
    step "Enabling SSH service"
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        local svc enabled=""
        for svc in ssh sshd; do
            if systemctl list-unit-files "${svc}.service" 2>/dev/null | grep -q "^${svc}\.service"; then
                run_root systemctl enable --now "${svc}.service" >/dev/null 2>&1
                enabled="$svc"
                break
            fi
        done
        [[ -n "$enabled" ]] || die "No ssh/sshd systemd unit found even after package installation"
        ok "systemd unit ${enabled}.service enabled and started"
    elif command -v service >/dev/null 2>&1; then
        run_root service ssh start >/dev/null 2>&1 || run_root service sshd start >/dev/null 2>&1 \
            || die "Could not start SSH via the service command"
        ok "SSH started via service command (no systemd detected)"
    else
        warn "No init system detected; start sshd manually"
    fi

    # Open port 22 only when a host firewall is actually active.
    if command -v ufw >/dev/null 2>&1 && run_root ufw status 2>/dev/null | grep -q "Status: active"; then
        run_root ufw allow OpenSSH >/dev/null 2>&1 || run_root ufw allow 22/tcp >/dev/null
        ok "ufw: SSH allowed"
    fi
    if command -v firewall-cmd >/dev/null 2>&1 && run_root firewall-cmd --state >/dev/null 2>&1; then
        run_root firewall-cmd --permanent --add-service=ssh >/dev/null
        run_root firewall-cmd --reload >/dev/null
        ok "firewalld: ssh service allowed"
    fi
}

enable_ssh_macos() {
    step "Enabling macOS Remote Login (sshd)"
    if ssh_port_open; then
        ok "Remote Login already enabled (port 22 is listening)"
        return 0
    fi

    # systemsetup needs Full Disk Access for the terminal on recent macOS; launchctl is the fallback.
    run_root systemsetup -f -setremotelogin on >/dev/null 2>&1 || true
    if ! ssh_port_open; then
        run_root launchctl enable system/com.openssh.sshd >/dev/null 2>&1 || true
        run_root launchctl bootstrap system /System/Library/LaunchDaemons/ssh.plist >/dev/null 2>&1 || true
    fi

    if ssh_port_open; then
        ok "Remote Login enabled"
    else
        warn "Could not enable Remote Login automatically."
        warn "Enable it in System Settings > General > Sharing > Remote Login,"
        warn "or grant Full Disk Access to your terminal and re-run this script."
    fi
}

# ------------------------------------------------------------------ authorized_keys
is_valid_pubkey() {
    [[ "$1" =~ ^(ssh-(rsa|ed25519|dss)|ecdsa-sha2-nistp(256|384|521)|sk-(ssh-ed25519|ecdsa-sha2-nistp256)@openssh\.com)[[:space:]]+[A-Za-z0-9+/]+={0,3}([[:space:]].*)?$ ]]
}

setup_ssh_dir() {
    step "Securing $TARGET_HOME/.ssh"
    local ssh_dir="$TARGET_HOME/.ssh"
    local key_file="$ssh_dir/authorized_keys"

    mkdir -p "$ssh_dir"
    touch "$key_file"

    if [[ -n "$PUBKEY" ]]; then
        is_valid_pubkey "$PUBKEY" || die "--pubkey does not look like a valid OpenSSH public key"
        if grep -qxF -- "$PUBKEY" "$key_file"; then
            ok "Public key already present"
        else
            # Make sure we do not glue the new key onto a last line without a trailing newline.
            if [[ -s "$key_file" && -n "$(tail -c 1 "$key_file")" ]]; then
                printf '\n' >>"$key_file"
            fi
            printf '%s\n' "$PUBKEY" >>"$key_file"
            ok "Public key appended to $key_file"
        fi
    else
        warn "No --pubkey given; authorized_keys left unchanged"
    fi

    chmod 700 "$ssh_dir"
    chmod 600 "$key_file"
    if [[ "$(id -u)" -eq 0 && "$TARGET_USER" != "root" ]]; then
        chown -R "$TARGET_USER:$TARGET_GROUP" "$ssh_dir"
    fi
    ok "Permissions: $ssh_dir=700, authorized_keys=600"
}

# ------------------------------------------------------------------ Python venv
setup_venv() {
    step "Preparing Python virtual environment in $PROJECT_DIR/venv"
    local requirements="$PROJECT_DIR/requirements.txt"
    if [[ ! -f "$requirements" ]]; then
        warn "requirements.txt not found in $PROJECT_DIR; skipping venv (use --project-dir)"
        return 0
    fi

    local python_bin
    python_bin="$(command -v python3 || true)"
    [[ -n "$python_bin" ]] || die "python3 not found on PATH"
    "$python_bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
        || die "Python 3.9+ is required, found $("$python_bin" --version 2>&1)"

    if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
        "$python_bin" -m venv "$PROJECT_DIR/venv"
        ok "venv created with $("$python_bin" --version 2>&1)"
    else
        ok "venv already exists"
    fi

    "$PROJECT_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    "$PROJECT_DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check -r "$requirements"
    ok "requirements.txt installed"

    if [[ "$(id -u)" -eq 0 && "$TARGET_USER" != "root" ]]; then
        chown -R "$TARGET_USER:$TARGET_GROUP" "$PROJECT_DIR/venv"
    fi
}

# ------------------------------------------------------------------ main
step "Bootstrapping $OS_NAME host for user '$TARGET_USER'"

if [[ "$SKIP_PACKAGES" -eq 0 ]]; then
    detect_package_manager
    install_packages
else
    [[ "$OS_NAME" == "Darwin" ]] && load_brew_env || true
    warn "Skipping system packages (--skip-packages)"
fi

if [[ "$OS_NAME" == "Darwin" ]]; then
    enable_ssh_macos
else
    enable_ssh_linux
fi

setup_ssh_dir

if [[ "$SKIP_VENV" -eq 0 ]]; then
    setup_venv
else
    warn "Skipping venv (--skip-venv)"
fi

echo
step "Bootstrap summary"
echo "    OS            : $OS_NAME ($(uname -m))"
echo "    User          : $TARGET_USER"
echo "    SSH listening : $(ssh_port_open && echo yes || echo no)"
echo "    Project dir   : $PROJECT_DIR"
if [[ -x "$PROJECT_DIR/venv/bin/python" ]]; then
    echo "    Start gateway : cd \"$PROJECT_DIR\" && ./venv/bin/uvicorn gateway:app --host 0.0.0.0 --port 8000"
fi
