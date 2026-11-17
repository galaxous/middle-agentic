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
#   4. Installs Tailscale and joins the tailnet when --ts-authkey is given.
#   5. Role "llm": installs Ollama, starts it (127.0.0.1 only) and pulls --model.
#   6. Creates ./venv with Python 3.10+ and installs the middle_agentic package (pip install -e).
#   7. Writes config.yaml/.env via `magent setup`; role "gateway": registers the gateway service.
#
# Usage:
#   ./scripts/bootstrap-unix.sh --pubkey "ssh-ed25519 AAAA... me@macbook" --roles gateway,llm --ts-authkey tskey-auth-...
#   curl -fsSL https://raw.githubusercontent.com/galaxous/middle-agentic/main/scripts/bootstrap-unix.sh | bash -s -- --pubkey "ssh-ed25519 AAAA..."
#
# Compatible with bash 3.2 (default macOS bash).

set -euo pipefail

PUBKEY=""
PROJECT_DIR=""
ROLES="gateway,llm,client"
TS_AUTHKEY=""
LLM_MODEL=""
SKIP_PACKAGES=0
SKIP_VENV=0
SKIP_TAILSCALE=0
SKIP_LLM=0
SKIP_SERVICE=0
PYTHON_BIN=""

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
  --roles <list>         Node roles: gateway,llm,client (default: all three)
  --ts-authkey <key>     Tailscale auth key; joins the tailnet non-interactively
  --model <name>         Local model for the llm role (default: qwen2.5:3b)
  --project-dir <path>   Repository root containing pyproject.toml (default: auto-detected)
  --skip-packages        Do not install system packages
  --skip-tailscale       Do not install/configure Tailscale
  --skip-llm             Do not install Ollama even if the llm role is set
  --skip-venv            Do not create the Python virtual environment
  --skip-service         Do not register the gateway as a background service
  -h, --help             Show this help
EOF
}

# ------------------------------------------------------------------ argument parsing
require_value() { [[ $1 -ge 2 ]] || die "$2 requires a value"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pubkey)         require_value $# "$1"; PUBKEY="$2"; shift 2 ;;
        --pubkey=*)       PUBKEY="${1#*=}"; shift ;;
        --roles)          require_value $# "$1"; ROLES="$2"; shift 2 ;;
        --roles=*)        ROLES="${1#*=}"; shift ;;
        --ts-authkey)     require_value $# "$1"; TS_AUTHKEY="$2"; shift 2 ;;
        --ts-authkey=*)   TS_AUTHKEY="${1#*=}"; shift ;;
        --model)          require_value $# "$1"; LLM_MODEL="$2"; shift 2 ;;
        --model=*)        LLM_MODEL="${1#*=}"; shift ;;
        --project-dir)    require_value $# "$1"; PROJECT_DIR="$2"; shift 2 ;;
        --project-dir=*)  PROJECT_DIR="${1#*=}"; shift ;;
        --skip-packages)  SKIP_PACKAGES=1; shift ;;
        --skip-tailscale) SKIP_TAILSCALE=1; shift ;;
        --skip-llm)       SKIP_LLM=1; shift ;;
        --skip-venv)      SKIP_VENV=1; shift ;;
        --skip-service)   SKIP_SERVICE=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        *)                usage; die "Unknown option: $1" ;;
    esac
done

has_role() { [[ ",$ROLES," == *",$1,"* ]]; }

# When piped through `curl | bash`, BASH_SOURCE is empty: fall back to the current directory.
if [[ -z "$PROJECT_DIR" ]]; then
    if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
        PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        # The script lives in <repo>/scripts; the package root is one level up.
        [[ -f "$PROJECT_DIR/../pyproject.toml" ]] && PROJECT_DIR="$(cd "$PROJECT_DIR/.." && pwd)"
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

# venv, config and user services must be owned by / registered for the human user.
run_as_user() {
    if [[ "$(id -u)" -eq 0 && "$TARGET_USER" != "root" ]]; then
        sudo -u "$TARGET_USER" -H "$@"
    else
        "$@"
    fi
}

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
        apt)     echo "openssh-server git sqlite3 curl python3 python3-venv python3-pip" ;;
        dnf|yum) echo "openssh-server git sqlite curl python3 python3-pip" ;;
        # macOS ships its own sshd; brew only provides the tooling.
        brew)    echo "git sqlite python@3.12" ;;
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

# ------------------------------------------------------------------ Tailscale
tailscale_bin() {
    if command -v tailscale >/dev/null 2>&1; then
        command -v tailscale
    elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
        echo /Applications/Tailscale.app/Contents/MacOS/Tailscale
    fi
}

setup_tailscale() {
    step "Configuring Tailscale"
    local ts
    ts="$(tailscale_bin)"
    if [[ -z "$ts" ]]; then
        if [[ "$OS_NAME" == "Darwin" ]]; then
            run_brew install --quiet tailscale
            load_brew_env || true
            # The Homebrew build ships the daemon separately; register it as a system LaunchDaemon.
            run_root "$(command -v tailscaled)" install-system-daemon
        else
            curl -fsSL https://tailscale.com/install.sh | run_root sh
        fi
        ts="$(tailscale_bin)"
        [[ -n "$ts" ]] || die "Tailscale installation failed"
        ok "Tailscale installed"
    else
        ok "Tailscale already installed ($ts)"
    fi

    if [[ "$OS_NAME" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
        run_root systemctl enable --now tailscaled >/dev/null 2>&1 || true
    fi

    if "$ts" status >/dev/null 2>&1; then
        ok "Connected to tailnet as $("$ts" ip -4 2>/dev/null | head -n 1)"
    elif [[ -n "$TS_AUTHKEY" ]]; then
        run_root "$ts" up --authkey "$TS_AUTHKEY" --hostname "$(hostname -s | tr '[:upper:]' '[:lower:]')"
        ok "Joined tailnet as $("$ts" ip -4 2>/dev/null | head -n 1)"
    else
        warn "Tailscale is installed but not connected: run 'sudo tailscale up' or pass --ts-authkey"
    fi
}

# ------------------------------------------------------------------ Ollama (role: llm)
ollama_api_up() {
    curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1
}

setup_ollama() {
    step "Installing local LLM runtime (Ollama)"
    if ! command -v ollama >/dev/null 2>&1; then
        if [[ "$OS_NAME" == "Darwin" ]]; then
            run_brew install --quiet ollama
        else
            case "$(uname -m)" in
                armv6l|armv7l)
                    warn "Ollama needs a 64-bit OS (aarch64); skipping. Use a cloud tier or llama.cpp on this device."
                    return 0 ;;
            esac
            curl -fsSL https://ollama.com/install.sh | run_root sh
        fi
        ok "Ollama installed"
    else
        ok "Ollama already installed"
    fi

    if ! ollama_api_up; then
        if [[ "$OS_NAME" == "Darwin" ]]; then
            run_brew services start ollama >/dev/null 2>&1 || true
        elif command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
            run_root systemctl enable --now ollama >/dev/null 2>&1 || true
        else
            run_as_user nohup ollama serve >/tmp/ollama.log 2>&1 &
        fi
        local waited=0
        while ! ollama_api_up && [[ $waited -lt 30 ]]; do
            sleep 1
            waited=$((waited + 1))
        done
    fi

    if ollama_api_up; then
        ok "Ollama API listening on 127.0.0.1:11434 (not exposed; remote access goes through the gateway)"
    else
        warn "Ollama API is not reachable on 127.0.0.1:11434; start it and run: magent llm pull"
    fi
}

# ------------------------------------------------------------------ Python package
find_python() {
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 python3 \
        /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

setup_venv() {
    step "Preparing Python virtual environment in $PROJECT_DIR/venv"
    if [[ ! -f "$PROJECT_DIR/pyproject.toml" ]]; then
        warn "pyproject.toml not found in $PROJECT_DIR; clone the repository and re-run (or use --project-dir)"
        return 1
    fi

    PYTHON_BIN="$(find_python)" \
        || die "Python 3.10+ is required (Debian 12+/Ubuntu 22.04+ ship it; macOS: brew install python@3.12)"

    local venv_python="$PROJECT_DIR/venv/bin/python"
    if [[ -x "$venv_python" ]] && ! "$venv_python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        warn "Existing venv uses Python < 3.10, recreating it"
        rm -rf "$PROJECT_DIR/venv"
    fi
    if [[ ! -x "$venv_python" ]]; then
        run_as_user "$PYTHON_BIN" -m venv "$PROJECT_DIR/venv"
        ok "venv created with $("$PYTHON_BIN" --version 2>&1)"
    else
        ok "venv already exists ($("$venv_python" --version 2>&1))"
    fi

    run_as_user "$venv_python" -m pip install --quiet --disable-pip-version-check --upgrade pip
    run_as_user "$venv_python" -m pip install --quiet --disable-pip-version-check -e "$PROJECT_DIR"
    ok "middle_agentic installed (magent CLI: $PROJECT_DIR/venv/bin/magent)"
}

# Gateway port 8000 is only opened for tailnet addresses (100.64.0.0/10) when a firewall is active.
open_gateway_port() {
    [[ "$OS_NAME" == "Linux" ]] || return 0
    if command -v ufw >/dev/null 2>&1 && run_root ufw status 2>/dev/null | grep -q "Status: active"; then
        run_root ufw allow from 100.64.0.0/10 to any port 8000 proto tcp >/dev/null
        ok "ufw: TCP 8000 allowed from the tailnet"
    elif command -v firewall-cmd >/dev/null 2>&1 && run_root firewall-cmd --state >/dev/null 2>&1; then
        run_root firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="100.64.0.0/10" port port="8000" protocol="tcp" accept' >/dev/null
        run_root firewall-cmd --reload >/dev/null
        ok "firewalld: TCP 8000 allowed from the tailnet"
    fi
}

configure_node() {
    step "Writing node configuration (roles: $ROLES)"
    local magent="$PROJECT_DIR/venv/bin/magent"
    local setup_args=(setup --roles "$ROLES" --no-pull)
    [[ -n "$LLM_MODEL" ]] && setup_args+=(--model "$LLM_MODEL")
    run_as_user "$magent" "${setup_args[@]}"

    if has_role llm && [[ "$SKIP_LLM" -eq 0 ]] && ollama_api_up; then
        step "Pulling local model"
        run_as_user "$magent" llm pull || warn "Model pull failed; retry with: $magent llm pull"
    fi

    if has_role gateway; then
        open_gateway_port
    fi

    if has_role gateway && [[ "$SKIP_SERVICE" -eq 0 ]]; then
        step "Registering gateway service"
        run_as_user "$magent" service install || warn "Service registration failed; run the gateway with: $magent serve"
    fi
}

# ------------------------------------------------------------------ main
step "Bootstrapping $OS_NAME host for user '$TARGET_USER' (roles: $ROLES)"

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

if [[ "$SKIP_TAILSCALE" -eq 0 ]]; then
    setup_tailscale
else
    warn "Skipping Tailscale (--skip-tailscale)"
fi

if has_role llm && [[ "$SKIP_LLM" -eq 0 ]]; then
    setup_ollama
fi

if [[ "$SKIP_VENV" -eq 0 ]]; then
    if setup_venv; then
        configure_node
    fi
else
    warn "Skipping venv, configuration and service (--skip-venv)"
fi

TS_IP=""
TS="$(tailscale_bin)"
[[ -n "$TS" ]] && TS_IP="$("$TS" ip -4 2>/dev/null | head -n 1 || true)"

echo
step "Bootstrap summary"
echo "    OS            : $OS_NAME ($(uname -m))"
echo "    User          : $TARGET_USER"
echo "    Roles         : $ROLES"
echo "    SSH listening : $(ssh_port_open && echo yes || echo no)"
echo "    Tailscale IP  : ${TS_IP:-not connected}"
echo "    Ollama        : $(ollama_api_up && echo running || echo 'not running')"
echo "    Project dir   : $PROJECT_DIR"
if [[ -x "$PROJECT_DIR/venv/bin/magent" ]]; then
    echo "    CLI           : $PROJECT_DIR/venv/bin/magent (try: magent nodes, magent ask \"hi\")"
    if has_role gateway; then
        echo "    Gateway       : http://${TS_IP:-127.0.0.1}:8000  (status: magent service status)"
    fi
fi
