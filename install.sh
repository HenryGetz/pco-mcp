#!/usr/bin/env bash
# One-line installer for the Planning Center MCP server.
#
#   curl -fsSL https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.sh | bash
#
# Installs into ${PCO_MCP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/pco-mcp},
# creates a virtual environment, installs dependencies, optionally saves a
# Planning Center Personal Access Token, and prints an MCP client snippet.
set -euo pipefail

REPO_TARBALL="https://github.com/HenryGetz/pco-mcp/archive/refs/heads/main.tar.gz"
README_URL="https://github.com/HenryGetz/pco-mcp#readme"
DEFAULT_INSTALL_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/pco-mcp"
INSTALL_DIR="${PCO_MCP_HOME:-$DEFAULT_INSTALL_DIR}"
SOURCE_DIR="${PCO_MCP_SOURCE:-}"
NONINTERACTIVE="${PCO_NONINTERACTIVE:-0}"
CREDS_FILE="$HOME/.mpco_credentials.json"

TMP_DIR=""
VENV_PY=""
CREDS_KIND="missing"
CREDS_NAMES=""

info() { printf '%s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<EOF
Planning Center MCP installer

Installs the Planning Center MCP server and prepares it for any MCP-capable
AI app. You can run this script again at any time to update; your credentials
are never removed.

Usage:
  curl -fsSL https://raw.githubusercontent.com/HenryGetz/pco-mcp/main/install.sh | bash
  ./install.sh --help

Install location:
  \${PCO_MCP_HOME:-\${XDG_DATA_HOME:-\$HOME/.local/share}/pco-mcp}

Environment variables:
  PCO_MCP_HOME          Where to install (default shown above).
  PCO_MCP_SOURCE        Install from a local checkout instead of downloading.
  PCO_NONINTERACTIVE=1  Never prompt; skip credential setup if none is supplied.
  PCO_SETUP_ACCOUNT     Account nickname (default: main).
  PCO_SETUP_ORG_NAME    Organization display name (default: the nickname).
  PCO_SETUP_APP_ID      Planning Center application ID.
  PCO_SETUP_APP_SECRET  Planning Center application secret.
  PCO_SETUP_OVERWRITE=1 Replace an existing account with the same nickname.

Credentials live in ~/.mpco_credentials.json with mode 0600.
Create a Personal Access Token at:
  https://api.planningcenteronline.com/oauth/applications
EOF
}

cleanup() {
  if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"
  fi
}

on_interrupt() {
  printf '\nInstall cancelled. You can rerun the same command safely.\n' >&2
  exit 130
}

trap cleanup EXIT
trap on_interrupt INT TERM

for arg in "$@"; do
  case "$arg" in
    -h|--help) usage; exit 0 ;;
    *) die "Unknown option: $arg. Run with --help for usage." ;;
  esac
done

if [ -z "${HOME:-}" ]; then
  die "HOME is not set. Set HOME to your user folder and rerun."
fi

command -v tar >/dev/null 2>&1 || die "The 'tar' command is required but was not found."
command -v mktemp >/dev/null 2>&1 || die "The 'mktemp' command is required but was not found."
if [ -z "$SOURCE_DIR" ]; then
  command -v curl >/dev/null 2>&1 || die "The 'curl' command is required but was not found. Install curl and rerun."
fi

PYTHON="${PCO_MCP_PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || die "Python 3 was not found. Install Python 3.10 or newer from https://www.python.org/downloads/ and rerun."
PY_VERSION=$("$PYTHON" -c 'import sys; print(".".join(str(part) for part in sys.version_info[:3]))')
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  die "Python 3.10 or newer is required, but $PY_VERSION was found. Install a newer Python and rerun."
fi
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
  info "Note: Python $PY_VERSION found. Python 3.12 or newer is recommended, but $PY_VERSION works."
fi

TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pco-mcp-install.XXXXXX") || die "Could not create a temporary folder."

if [ -n "$SOURCE_DIR" ]; then
  [ -d "$SOURCE_DIR" ] || die "PCO_MCP_SOURCE points to '$SOURCE_DIR', which is not a folder."
  SRC_DIR=$(cd "$SOURCE_DIR" && pwd -P)
else
  TARBALL="$TMP_DIR/pco-mcp.tar.gz"
  info "Downloading the latest version..."
  curl --fail --location --silent --show-error --output "$TARBALL" "$REPO_TARBALL" \
    || die "Could not download the installer files. Check your internet connection and rerun."
  mkdir -p "$TMP_DIR/src"
  tar -xzf "$TARBALL" -C "$TMP_DIR/src" || die "Could not unpack the downloaded files."
  SRC_DIR=$(find "$TMP_DIR/src" -mindepth 1 -maxdepth 1 -type d -print -quit)
  [ -n "$SRC_DIR" ] || die "The download did not contain the expected files."
fi

[ -f "$SRC_DIR/server.py" ] || die "The server files are incomplete (server.py is missing)."
[ -f "$SRC_DIR/requirements.txt" ] || die "The server files are incomplete (requirements.txt is missing)."

mkdir -p "$INSTALL_DIR" || die "Could not create the install folder at $INSTALL_DIR."
INSTALL_DIR=$(cd "$INSTALL_DIR" && pwd -P)

if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
  info "Installing files to $INSTALL_DIR..."
  (
    cd "$SRC_DIR"
    tar -cf - \
      --exclude='./.git' \
      --exclude='./venv' \
      --exclude='./.env' \
      --exclude='__pycache__' \
      --exclude='*.pyc' \
      --exclude='*recon*.md' \
      --exclude='*recon*.json' \
      --exclude='*_history.json' \
      --exclude='*_history.md' \
      --exclude='server.py.bak-*' \
      --exclude='server.py.pre-repo-*' \
      --exclude='.DS_Store' \
      .
  ) | (
    cd "$INSTALL_DIR"
    tar -xf -
  ) || die "Could not copy the server files to $INSTALL_DIR."
else
  info "Using the files already in $INSTALL_DIR."
fi

ensure_venv() {
  local candidate="$INSTALL_DIR/venv/bin/python"
  if [ -x "$candidate" ] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    VENV_PY="$candidate"
    info "Using the existing Python environment."
    return 0
  fi

  if [ -e "$INSTALL_DIR/venv" ]; then
    warn "The existing Python environment is unusable; recreating it."
    rm -rf "$INSTALL_DIR/venv"
  fi

  info "Creating a Python environment..."
  if ! "$PYTHON" -m venv "$INSTALL_DIR/venv"; then
    die "Could not create a Python environment. On Debian/Ubuntu, install python3-venv (for example: sudo apt install python3-venv) and rerun."
  fi
  VENV_PY="$INSTALL_DIR/venv/bin/python"
  [ -x "$VENV_PY" ] || die "Python created an environment without a usable interpreter at $VENV_PY."
}

ensure_venv

if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
  "$VENV_PY" -m ensurepip --upgrade >/dev/null 2>&1 \
    || die "The Python environment is missing pip. Reinstall Python 3 and rerun."
fi

info "Installing dependencies (this can take a minute)..."
if ! "$VENV_PY" -m pip install --disable-pip-version-check --no-input --quiet \
  -r "$INSTALL_DIR/requirements.txt"; then
  die "Could not install the Python dependencies. Check your internet connection and rerun."
fi

info "Checking the server..."
if ! SMOKE_NAME=$(cd "$INSTALL_DIR" && "$VENV_PY" -c "import server; print(server.mcp.name)"); then
  die "The server was installed but did not start. Rerun the installer; if it keeps failing, see $README_URL."
fi
info "Server check passed: ${SMOKE_NAME:-planning-center}."

inspect_credentials() {
  local result
  CREDS_KIND="invalid"
  CREDS_NAMES=""
  if result=$("$VENV_PY" - "$CREDS_FILE" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
except FileNotFoundError:
    print("missing")
    raise SystemExit(0)
except (OSError, ValueError):
    print("invalid")
    raise SystemExit(0)

if not isinstance(data, dict):
    print("invalid")
    raise SystemExit(0)

names = [
    str(name)
    for name, entry in data.items()
    if isinstance(entry, dict) and (entry.get("appId") or entry.get("appSecret"))
]
if names:
    print("ok:" + ",".join(names))
else:
    print("empty")
PY
  ); then
    case "$result" in
      ok:*) CREDS_KIND="ok"; CREDS_NAMES=${result#ok:} ;;
      empty) CREDS_KIND="empty" ;;
      missing) CREDS_KIND="missing" ;;
      *) CREDS_KIND="invalid" ;;
    esac
  fi
}

has_tty() {
  if { : < /dev/tty; } 2>/dev/null; then
    return 0
  fi
  return 1
}

ask() {
  # $1 = prompt, $2 = default (optional). Sets ANSWER.
  local prompt="$1" default="${2-}" value
  while :; do
    printf '%s' "$prompt" > /dev/tty
    if [ -n "$default" ]; then
      printf ' [%s]' "$default" > /dev/tty
    fi
    printf ': ' > /dev/tty
    IFS= read -r value < /dev/tty || die "Could not read from the terminal."
    value=${value#"${value%%[![:space:]]*}"}
    value=${value%"${value##*[![:space:]]}"}
    if [ -z "$value" ] && [ -n "$default" ]; then
      value="$default"
    fi
    if [ -n "$value" ]; then
      ANSWER="$value"
      return 0
    fi
    printf 'Please enter a value.\n' > /dev/tty
  done
}

ask_secret() {
  local value
  while :; do
    printf '%s' 'Application Secret (typing is hidden): ' > /dev/tty
    IFS= read -r -s value < /dev/tty || die "Could not read from the terminal."
    printf '\n' > /dev/tty
    if [ -n "$value" ]; then
      ANSWER="$value"
      return 0
    fi
    printf 'Please enter the application secret.\n' > /dev/tty
  done
}

write_credentials() {
  PCO_WRITE_PATH="$CREDS_FILE" \
  PCO_WRITE_ACCOUNT="$1" \
  PCO_WRITE_ORG="$2" \
  PCO_WRITE_APP_ID="$3" \
  PCO_WRITE_SECRET="$4" \
  PCO_WRITE_OVERWRITE="${PCO_SETUP_OVERWRITE:-0}" \
  "$VENV_PY" - <<'PY'
import json
import os
import sys
import tempfile

path = os.path.expanduser(os.environ["PCO_WRITE_PATH"])
account = os.environ["PCO_WRITE_ACCOUNT"].strip()
org = os.environ.get("PCO_WRITE_ORG", "").strip() or account
app_id = os.environ["PCO_WRITE_APP_ID"].strip()
secret = os.environ["PCO_WRITE_SECRET"]
overwrite = os.environ.get("PCO_WRITE_OVERWRITE") == "1"

if not account or not app_id or not secret:
    print("Error: account nickname, application ID, and secret are all required.", file=sys.stderr)
    raise SystemExit(1)

data = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"Error: {path} exists but is not readable JSON ({exc}).", file=sys.stderr)
        print("Fix or move that file, then rerun the installer.", file=sys.stderr)
        raise SystemExit(1)
    if not isinstance(data, dict):
        print(f"Error: {path} must contain a JSON object of accounts.", file=sys.stderr)
        raise SystemExit(1)

if account in data and not overwrite:
    print(f"An account named '{account}' already exists in {path}; leaving it unchanged.")
    print("Set PCO_SETUP_OVERWRITE=1 to replace it.")
    raise SystemExit(0)

data[account] = {"appId": app_id, "appSecret": secret, "orgName": org}

directory = os.path.dirname(os.path.abspath(path)) or "."
os.makedirs(directory, exist_ok=True)
descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".mpco_credentials.", suffix=".tmp")
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
except BaseException:
    try:
        os.unlink(temporary)
    except OSError:
        pass
    raise

with open(path, encoding="utf-8") as handle:
    check = json.load(handle)
entry = check.get(account) if isinstance(check, dict) else None
if not isinstance(entry, dict) or entry.get("appId") != app_id or entry.get("appSecret") != secret:
    print(f"Error: could not validate the credentials written to {path}.", file=sys.stderr)
    raise SystemExit(1)
os.chmod(path, 0o600)
print(f"Saved credentials for '{account}' to {path}.")
PY
}

inspect_credentials

ACCOUNT="${PCO_SETUP_ACCOUNT:-main}"
ORG_NAME="${PCO_SETUP_ORG_NAME:-}"
APP_ID="${PCO_SETUP_APP_ID:-}"
APP_SECRET="${PCO_SETUP_APP_SECRET:-}"

if [ -n "$APP_ID" ] || [ -n "$APP_SECRET" ]; then
  [ -n "$APP_ID" ] || die "PCO_SETUP_APP_SECRET is set, but PCO_SETUP_APP_ID is missing."
  [ -n "$APP_SECRET" ] || die "PCO_SETUP_APP_ID is set, but PCO_SETUP_APP_SECRET is missing."
  write_credentials "$ACCOUNT" "$ORG_NAME" "$APP_ID" "$APP_SECRET"
  inspect_credentials
elif [ "$CREDS_KIND" = "ok" ]; then
  info "Planning Center credentials are already configured for: $CREDS_NAMES. Leaving them unchanged."
elif [ "$CREDS_KIND" = "invalid" ]; then
  warn "$CREDS_FILE exists but could not be read as JSON. It was left untouched."
  warn "Fix or move that file, then rerun the installer to add credentials."
elif [ "$NONINTERACTIVE" = "1" ]; then
  info "Skipping credential setup because PCO_NONINTERACTIVE=1 and no PCO_SETUP_APP_ID/PCO_SETUP_APP_SECRET were supplied."
elif ! has_tty; then
  info "No terminal is available, so credential prompts were skipped."
  info "Rerun the installer in a terminal, or set PCO_SETUP_APP_ID and PCO_SETUP_APP_SECRET."
else
  printf '\n' > /dev/tty
  printf 'Planning Center access needs a Personal Access Token.\n' > /dev/tty
  printf 'Create one here (or open an existing application):\n' > /dev/tty
  printf '  https://api.planningcenteronline.com/oauth/applications\n' > /dev/tty
  printf 'Copy the Application ID and Secret from that page.\n\n' > /dev/tty

  ask "Account nickname" "main"
  ACCOUNT="$ANSWER"
  ask "Organization display name (optional)" "$ACCOUNT"
  ORG_NAME="$ANSWER"
  ask "Application ID" ""
  APP_ID="$ANSWER"
  ask_secret
  APP_SECRET="$ANSWER"

  write_credentials "$ACCOUNT" "$ORG_NAME" "$APP_ID" "$APP_SECRET"
  inspect_credentials
fi

printf '\n'
info "Planning Center MCP is installed."
printf '\n'
printf '  Install folder: %s\n' "$INSTALL_DIR"
printf '  Python:         %s\n' "$VENV_PY"
printf '  Server:         %s\n' "$INSTALL_DIR/server.py"
case "$CREDS_KIND" in
  ok) printf '  Planning Center: connected (%s)\n' "$CREDS_NAMES" ;;
  invalid) printf '  Planning Center: credentials file needs attention\n' ;;
  *) printf '  Planning Center: not connected yet\n' ;;
esac

printf '\n'
info "Add this to your AI app's MCP configuration:"
printf '\n'
cat <<EOF
{
  "mcpServers": {
    "pco": {
      "command": "$VENV_PY",
      "args": ["$INSTALL_DIR/server.py"]
    }
  }
}
EOF

printf '\n'
info 'Then restart your AI app and ask:'
info '  "List my Planning Center service types."'
printf '\n'
if [ "$CREDS_KIND" != "ok" ]; then
  info "Credentials are not set up yet. Rerun this installer in a terminal, or see the README to add ~/.mpco_credentials.json."
  printf '\n'
fi
info "Client-specific setup examples are in the README: $README_URL"
