#!/usr/bin/env bash
set -euo pipefail
PATH="/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
export PATH

if [[ "$(id -u)" -ne 0 ]]; then
  echo "install_production_monitor_v2.sh must run as root." >&2
  exit 1
fi
installer_real_path="$(readlink -f "${BASH_SOURCE[0]}")"
if [[ "$(id -u)" -eq 0 ]]; then
  installer_mode="$(stat -c %a "$installer_real_path")"
  if [[ ! -f "$installer_real_path" || -L "$installer_real_path" || \
        "$(stat -c %u "$installer_real_path")" != "0" || \
        "$(stat -c %h "$installer_real_path")" != "1" ]] || \
     (( (8#$installer_mode & 8#022) != 0 )); then
    echo "The running installer must be root-owned and non-writable." >&2
    exit 1
  fi
fi
if [[ "$#" -ne 2 || "$1" != "--expected-head" || \
      ! "$2" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Usage: $0 --expected-head <approved-40-character-sha>" >&2
  exit 2
fi
approved_head="$2"

PRODUCTION_ROOT="/opt/telegram-kol-analyzer"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
SYSTEMD_SOURCE="$PRODUCTION_ROOT/deploy/systemd"
SYSTEMD_DEST="/etc/systemd/system"
SNAPSHOT_USER="telegram-kol-monitor-snapshot"
SENTINEL_USER="telegram-kol-monitor-sentinel"
SNAPSHOT_CREDENTIAL_FILE="/etc/telegram-kol-monitor-snapshot.credentials"
MAIN_TRADING_CREDENTIAL_FILE="$PRODUCTION_ROOT/.env"
SENTINEL_ENV_FILE="/etc/telegram-kol-monitor-sentinel.env"
RUNTIME_POLICY_FILE="$PRODUCTION_ROOT/config/runtime_incident_agent.env"
STATE_ROOT="/var/lib/telegram-kol-monitor-v2"
CACHE_ROOT="/var/cache/telegram-kol-monitor-v2"
RELEASE_ROOT="/opt/telegram-kol-monitor-v2/releases"
CURRENT_RELEASE_LINK="/opt/telegram-kol-monitor-v2/current"
UV_CACHE_TRUST_ANCHOR="/var/cache"
UV_CACHE_PARENT="/var/cache/telegram-kol-monitor-v2-build"
UV_CACHE_ROOT="/var/cache/telegram-kol-monitor-v2-build/uv"
UV_CACHE_VALIDATOR_SOURCE="$PRODUCTION_ROOT/scripts/validate_production_monitor_uv_cache.py"
DB_STAGE_HELPER_SOURCE="$PRODUCTION_ROOT/src/telegram_kol_research/production_monitor_db_stage.py"
DB_STAGE_HELPER_DEST="/usr/local/libexec/telegram-kol-monitor-db-stage"

services=(
  telegram-kol-monitor-snapshot.service
  telegram-kol-sentinel.service
  telegram-kol-monitor-audit.service
)
timers=(
  telegram-kol-monitor-snapshot.timer
  telegram-kol-sentinel.timer
  telegram-kol-monitor-audit.timer
)
units=("${services[@]}" "${timers[@]}")
staging_instances=(
  telegram-kol-monitor-db-stage@sentinel.service
  telegram-kol-monitor-db-stage@audit.service
)
source_units=("${units[@]}" telegram-kol-monitor-db-stage@.service)
reviewed_relative_paths=(
  scripts/install_production_monitor_v2.sh
  scripts/validate_production_monitor_uv_cache.py
  deploy/systemd/telegram-kol-monitor-snapshot.service
  deploy/systemd/telegram-kol-monitor-snapshot.timer
  deploy/systemd/telegram-kol-sentinel.service
  deploy/systemd/telegram-kol-sentinel.timer
  deploy/systemd/telegram-kol-monitor-audit.service
  deploy/systemd/telegram-kol-monitor-audit.timer
  deploy/systemd/telegram-kol-monitor-db-stage@.service
  src/telegram_kol_research/production_monitor_db_stage.py
  src/telegram_kol_research/cli.py
  config/production-monitor-build-constraints.txt
)

if [[ "$PROJECT_ROOT" != "$PRODUCTION_ROOT" ]]; then
  echo "Run this installer only from the fixed production checkout $PRODUCTION_ROOT." >&2
  exit 1
fi
git_root="$(git -C "$PRODUCTION_ROOT" rev-parse --show-toplevel)"
if [[ "$(cd "$git_root" && pwd -P)" != "$PRODUCTION_ROOT" ]]; then
  echo "The production path is not the validated Git checkout root." >&2
  exit 1
fi
expected_head="$(git -C "$PRODUCTION_ROOT" rev-parse --verify HEAD)"
if [[ ! "$expected_head" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Unable to capture a valid reviewed Git HEAD." >&2
  exit 1
fi
if [[ "$expected_head" != "$approved_head" ]]; then
  echo "Production HEAD does not match the explicitly approved SHA." >&2
  exit 1
fi
if ! git -C "$PRODUCTION_ROOT" diff --quiet --ignore-submodules=none \
  "$expected_head" -- . || \
   ! git -C "$PRODUCTION_ROOT" diff --cached --quiet --ignore-submodules=none \
  "$expected_head" -- .; then
  echo "The complete production checkout differs from the approved SHA." >&2
  exit 1
fi
if [[ -n "$(git -C "$PRODUCTION_ROOT" ls-files --others --exclude-standard -- .)" ]]; then
  echo "The production checkout contains an untracked path." >&2
  exit 1
fi
index_tags="$(git -C "$PRODUCTION_ROOT" ls-files -t -v)"
if grep -Eq '^[S]|^[a-z]' <<<"$index_tags"; then
  echo "The production checkout contains a hidden index override." >&2
  exit 1
fi
runtime_shadow=""
while IFS= read -r -d '' candidate_path; do
  relative_path="${candidate_path#"$PRODUCTION_ROOT/"}"
  if ! git -C "$PRODUCTION_ROOT" --literal-pathspecs \
    ls-files --error-unmatch -- "$relative_path" \
    >/dev/null 2>&1; then
    runtime_shadow="$relative_path"
    break
  fi
done < <(
  find "$PRODUCTION_ROOT/src" "$PRODUCTION_ROOT/scripts" \
    ! -type d -print0
  find "$PRODUCTION_ROOT" -maxdepth 1 \
    ! -type d \( \
      -name '*.py' -o -name '*.pyc' -o -name '*.pyo' -o \
      -name '*.so' -o -name '*.dylib' -o -name '*.pth' \
    \) -print0
)
if [[ -n "$runtime_shadow" ]]; then
  echo "The production checkout contains an untracked or ignored import shadow." >&2
  exit 1
fi
checkout_mode="$(stat -c %a "$PRODUCTION_ROOT")"
if [[ "$(stat -c %u "$PRODUCTION_ROOT")" != "0" ]] || \
   (( (8#$checkout_mode & 8#022) != 0 )); then
  echo "The production checkout must be root-owned and non-writable by other identities." >&2
  exit 1
fi
untrusted_metadata="$({
  find "$PRODUCTION_ROOT/.git" "$PRODUCTION_ROOT/src" \
    "$PRODUCTION_ROOT/scripts" "$PRODUCTION_ROOT/deploy" -xdev \
    \( ! -user root -o -perm /022 \) -print -quit
} 2>/dev/null)"
if [[ -n "$untrusted_metadata" ]]; then
  echo "The reviewed checkout closure must be root-owned and non-writable." >&2
  exit 1
fi
if ! command -v uv >/dev/null 2>&1; then
  echo "A trusted uv binary is required to build the locked sealed runtime." >&2
  exit 1
fi
uv_path="$(readlink -f "$(command -v uv)")"
uv_mode="$(stat -c %a "$uv_path")"
if [[ ! -f "$uv_path" || -L "$uv_path" || \
      "$(stat -c %u "$uv_path")" != "0" || \
      "$(stat -c %h "$uv_path")" != "1" ]] || \
   (( (8#$uv_mode & 8#022) != 0 )); then
  echo "The uv runtime builder must be root-owned and non-writable." >&2
  exit 1
fi
if [[ ! -d "$UV_CACHE_PARENT" || -L "$UV_CACHE_PARENT" || \
      ! -d "$UV_CACHE_ROOT" || -L "$UV_CACHE_ROOT" ]]; then
  echo "The locked dependency cache is unavailable." >&2
  exit 1
fi
if ! /usr/bin/python3 "$UV_CACHE_VALIDATOR_SOURCE" \
  --cache-root "$UV_CACHE_ROOT" \
  --trust-anchor "$UV_CACHE_TRUST_ANCHOR" \
  --expected-owner-uid 0; then
  echo "Locked dependency cache must be root-owned and non-writable." >&2
  exit 1
fi
if ! command -v python3.12 >/dev/null 2>&1; then
  echo "Python 3.12 is required to build the sealed runtime." >&2
  exit 1
fi
python_path="$(readlink -f "$(command -v python3.12)")"
python_mode="$(stat -c %a "$python_path")"
if [[ ! -x "$python_path" || "$(stat -c %u "$python_path")" != "0" ]] || \
   (( (8#$python_mode & 8#022) != 0 )); then
  echo "The sealed runtime Python must be root-owned and non-writable." >&2
  exit 1
fi
if ! "$python_path" -I -S -c \
  'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "The sealed runtime builder requires exactly Python 3.12." >&2
  exit 1
fi
for relative_path in "${reviewed_relative_paths[@]}"; do
  if ! git -C "$PRODUCTION_ROOT" cat-file -e \
    "${expected_head}:${relative_path}"; then
    echo "Approved HEAD does not own $relative_path." >&2
    exit 1
  fi
done
if ! git -C "$PRODUCTION_ROOT" diff --quiet "$expected_head" -- \
  "${reviewed_relative_paths[@]}"; then
  echo "Monitor v2 unit bytes differ from the explicitly approved HEAD." >&2
  exit 1
fi

for target_unit in "${units[@]}"; do
  active_status=0
  systemctl is-active --quiet "$target_unit" || active_status=$?
  case "$active_status" in
    3|4) ;;
    0)
      echo "Stop $target_unit before installing or upgrading." >&2
      exit 1
      ;;
    *)
      echo "Unable to prove $target_unit is inactive." >&2
      exit 1
      ;;
  esac
  enabled_status=0
  systemctl is-enabled --quiet "$target_unit" || enabled_status=$?
  case "$enabled_status" in
    1|3|4) ;;
    0)
      echo "Disable $target_unit before this install-only phase." >&2
      exit 1
      ;;
    *)
      echo "Unable to prove $target_unit is disabled." >&2
      exit 1
      ;;
  esac
done
for staging_instance in "${staging_instances[@]}"; do
  active_status=0
  systemctl is-active --quiet "$staging_instance" || active_status=$?
  case "$active_status" in
    3|4) ;;
    0)
      echo "Stop $staging_instance before installing or upgrading." >&2
      exit 1
      ;;
    *)
      echo "Unable to prove $staging_instance is inactive." >&2
      exit 1
      ;;
  esac
done
for target_timer in "${timers[@]}"; do
  enabled_status=0
  systemctl is-enabled --quiet "$target_timer" || enabled_status=$?
  case "$enabled_status" in
    1|3|4) ;;
    0)
      echo "Disable $target_timer before this install-only phase." >&2
      exit 1
      ;;
    *)
      echo "Unable to prove $target_timer is disabled." >&2
      exit 1
      ;;
  esac
done

for source_unit in "${source_units[@]}"; do
  source_path="$SYSTEMD_SOURCE/$source_unit"
  if [[ ! -f "$source_path" || -L "$source_path" ]]; then
    echo "Missing regular reviewed unit $source_path." >&2
    exit 1
  fi
done
for source_service in "${services[@]}"; do
  source_path="$SYSTEMD_SOURCE/$source_service"
  for directive in \
    "CapabilityBoundingSet=" \
    "NoNewPrivileges=true" \
    "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private"
  do
    if ! grep -Fxq "$directive" "$source_path"; then
      echo "$source_service is missing required hardening: $directive" >&2
      exit 1
    fi
  done
  if grep -Eq "^ReadWritePaths=$PRODUCTION_ROOT(/|$)" "$source_path"; then
    echo "$source_service must not mount the production checkout writable." >&2
    exit 1
  fi
  if ! grep -Eq '^ReadWritePaths=/var/(lib|cache)/telegram-kol-monitor-v2(/|$)' "$source_path"; then
    echo "$source_service must write only below the v2 state/cache roots." >&2
    exit 1
  fi
  unexpected_write="$(
    grep '^ReadWritePaths=' "$source_path" | \
      grep -Ev '^ReadWritePaths=/var/(lib|cache)/telegram-kol-monitor-v2(/[^[:space:]]*)?$' \
      || true
  )"
  if [[ -n "$unexpected_write" ]]; then
    echo "$source_service contains an unexpected writable path." >&2
    exit 1
  fi
  if ! grep -Fq "/opt/telegram-kol-monitor-v2/current/.venv/bin/telegram-kol-research" \
       "$source_path" || \
     grep -Fq "/opt/telegram-kol-analyzer/.venv" "$source_path" || \
     grep -Fq "BindReadOnlyPaths=/opt/telegram-kol-analyzer/src" "$source_path"; then
    echo "$source_service must execute only the sealed approved runtime." >&2
    exit 1
  fi
done
stage_service="$SYSTEMD_SOURCE/telegram-kol-monitor-db-stage@.service"
for isolated_reader in \
  telegram-kol-monitor-snapshot.service \
  telegram-kol-sentinel.service \
  telegram-kol-monitor-audit.service
do
  if grep -Fq "BindReadOnlyPaths=$PRODUCTION_ROOT/data" \
    "$SYSTEMD_SOURCE/$isolated_reader"; then
    echo "$isolated_reader must not see the production data tree." >&2
    exit 1
  fi
done
if ! grep -Fxq "BindReadOnlyPaths=/opt/telegram-kol-analyzer/data" "$stage_service" || \
   ! grep -Fxq "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_OVERRIDE" \
     "$stage_service"; then
  echo "The database staging broker has an invalid read/capability boundary." >&2
  exit 1
fi
for directive in \
  "NoNewPrivileges=true" \
  "InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private" \
  "PrivateNetwork=true"
do
  if ! grep -Fxq "$directive" "$stage_service"; then
    echo "The database staging broker is missing hardening: $directive" >&2
    exit 1
  fi
done
if ! grep -Fxq \
  "ExecStart=/usr/bin/python3 /usr/local/libexec/telegram-kol-monitor-db-stage --consumer %i" \
  "$stage_service"; then
  echo "The database staging broker must execute only its closed helper." >&2
  exit 1
fi
if ! grep -Fxq \
  "ReadOnlyPaths=/var/cache/telegram-kol-monitor-v2/sentinel/research-snapshot.db" \
  "$SYSTEMD_SOURCE/telegram-kol-sentinel.service" || \
   ! grep -Fxq \
  "ReadOnlyPaths=/var/cache/telegram-kol-monitor-v2/audit/research-snapshot.db" \
  "$SYSTEMD_SOURCE/telegram-kol-monitor-audit.service"; then
  echo "Monitor database readers must use only their coherent staged snapshot." >&2
  exit 1
fi
if grep -R -F "$PRODUCTION_ROOT/.env" \
  "$SYSTEMD_SOURCE/telegram-kol-monitor-snapshot.service" \
  "$SYSTEMD_SOURCE/telegram-kol-sentinel.service" \
  "$SYSTEMD_SOURCE/telegram-kol-monitor-audit.service" \
  "$stage_service" >/dev/null; then
  echo "V2 monitor units must not mount or load the checkout .env." >&2
  exit 1
fi

if [[ ! -f "$SNAPSHOT_CREDENTIAL_FILE" || -L "$SNAPSHOT_CREDENTIAL_FILE" ]]; then
  echo "Missing independent regular snapshot credential file." >&2
  exit 1
fi
if [[ -e "$MAIN_TRADING_CREDENTIAL_FILE" ]] && \
   [[ "$SNAPSHOT_CREDENTIAL_FILE" -ef "$MAIN_TRADING_CREDENTIAL_FILE" ]]; then
  echo "The read-only snapshot credential must not be the main trading credential." >&2
  exit 1
fi
if [[ "$(stat -c %u "$SNAPSHOT_CREDENTIAL_FILE")" != "0" || \
      "$(stat -c %a "$SNAPSHOT_CREDENTIAL_FILE")" != "600" || \
      "$(stat -c %h "$SNAPSHOT_CREDENTIAL_FILE")" != "1" ]]; then
  echo "Snapshot credential must be one root-owned file with mode 0600." >&2
  exit 1
fi
if grep -Ev '^(DEEPCOIN_API_KEY=[A-Za-z0-9._-]{8,256}|DEEPCOIN_API_SECRET=[A-Za-z0-9+/=_-]{8,256}|DEEPCOIN_API_PASSPHRASE=[A-Za-z0-9._-]{8,256}|DEEPCOIN_BASE_URL=https://api\.deepcoin\.com|DEEPCOIN_READ_ONLY_PERMISSION_PROOF=verified-read-only-v1)$' "$SNAPSHOT_CREDENTIAL_FILE" >/dev/null; then
  echo "Snapshot credential contains a non-allowlisted field or value." >&2
  exit 1
fi
for required_key in \
  DEEPCOIN_API_KEY \
  DEEPCOIN_API_SECRET \
  DEEPCOIN_API_PASSPHRASE \
  DEEPCOIN_BASE_URL \
  DEEPCOIN_READ_ONLY_PERMISSION_PROOF
do
  if [[ "$(grep -c "^${required_key}=" "$SNAPSHOT_CREDENTIAL_FILE")" -ne 1 ]]; then
    echo "Snapshot credential must contain exactly one $required_key." >&2
    exit 1
  fi
done
if ! grep -Fxq 'DEEPCOIN_READ_ONLY_PERMISSION_PROOF=verified-read-only-v1' \
  "$SNAPSHOT_CREDENTIAL_FILE"; then
  echo "Snapshot credential has no closed read-only permission proof marker." >&2
  exit 1
fi

if [[ ! -f "$RUNTIME_POLICY_FILE" || -L "$RUNTIME_POLICY_FILE" ]]; then
  echo "Missing regular Runtime Incident policy file." >&2
  exit 1
fi
if ! command -v runuser >/dev/null 2>&1; then
  echo "The trusted runuser identity probe is unavailable." >&2
  exit 1
fi
if [[ -e "$DB_STAGE_HELPER_DEST" || -L "$DB_STAGE_HELPER_DEST" ]]; then
  if [[ ! -f "$DB_STAGE_HELPER_DEST" || -L "$DB_STAGE_HELPER_DEST" || \
        "$(stat -c %u "$DB_STAGE_HELPER_DEST")" != "0" || \
        "$(stat -c %a "$DB_STAGE_HELPER_DEST")" != "755" || \
        "$(stat -c %h "$DB_STAGE_HELPER_DEST")" != "1" ]]; then
    echo "Existing database staging helper metadata is invalid." >&2
    exit 1
  fi
fi
if [[ "$(stat -c %u "$RUNTIME_POLICY_FILE")" != "0" || \
      "$(stat -c %a "$RUNTIME_POLICY_FILE")" != "600" ]]; then
  echo "Runtime Incident policy must be root-owned with mode 0600." >&2
  exit 1
fi
if [[ "$(grep -c '^TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN=' "$RUNTIME_POLICY_FILE")" -ne 1 ]]; then
  echo "Runtime Incident policy must contain exactly one monitor token." >&2
  exit 1
fi
monitor_token="$(grep '^TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN=' "$RUNTIME_POLICY_FILE")"
if [[ ! "$monitor_token" =~ ^TELEGRAM_KOL_RUNTIME_MONITOR_CAPTURE_TOKEN=[A-Za-z0-9_-]{32,128}$ ]]; then
  echo "Runtime Incident monitor token is invalid." >&2
  exit 1
fi

for existing_path in "$STATE_ROOT" "$CACHE_ROOT"; do
  if [[ -e "$existing_path" || -L "$existing_path" ]]; then
    if [[ ! -d "$existing_path" || -L "$existing_path" ]]; then
      echo "Existing state/cache path is not a regular directory: $existing_path" >&2
      exit 1
    fi
    existing_mode="$(stat -c %a "$existing_path")"
    if [[ "$existing_mode" != "711" || \
          "$(stat -c %u "$existing_path")" != "0" || \
          "$(stat -c %g "$existing_path")" != "0" ]]; then
      echo "Existing state/cache root must be root-owned mode 0711: $existing_path" >&2
      exit 1
    fi
  fi
done
if [[ -e "$STATE_ROOT/snapshot" || -L "$STATE_ROOT/snapshot" ]]; then
  if [[ ! -d "$STATE_ROOT/snapshot" || -L "$STATE_ROOT/snapshot" || \
        "$(stat -c %a "$STATE_ROOT/snapshot")" != "700" || \
        "$(stat -c %U "$STATE_ROOT/snapshot")" != "$SNAPSHOT_USER" || \
        "$(stat -c %G "$STATE_ROOT/snapshot")" != "$SNAPSHOT_USER" ]]; then
    echo "Existing sealed snapshot directory metadata is invalid." >&2
    exit 1
  fi
fi
for existing_path in \
  "$STATE_ROOT/sentinel" "$CACHE_ROOT/sentinel" "$CACHE_ROOT/audit"
do
  if [[ -e "$existing_path" || -L "$existing_path" ]]; then
    if [[ ! -d "$existing_path" || -L "$existing_path" ]]; then
      echo "Existing private state/cache path is invalid: $existing_path" >&2
      exit 1
    fi
    if [[ "$(stat -c %a "$existing_path")" != "700" || \
          "$(stat -c %U "$existing_path")" != "$SENTINEL_USER" || \
          "$(stat -c %G "$existing_path")" != "$SENTINEL_USER" ]]; then
      echo "Existing private state/cache directory metadata is invalid: $existing_path" >&2
      exit 1
    fi
  fi
done
state_file_specs=(
  "$STATE_ROOT/snapshot/manifest.json:$SNAPSHOT_USER"
  "$STATE_ROOT/sentinel/sentinel-v2.json:$SENTINEL_USER"
  "$CACHE_ROOT/sentinel/snapshot.json:$SENTINEL_USER"
  "$CACHE_ROOT/sentinel/coverage.json:$SENTINEL_USER"
  "$CACHE_ROOT/sentinel/journal.json:$SENTINEL_USER"
  "$CACHE_ROOT/sentinel/research-snapshot.db:$SENTINEL_USER"
  "$CACHE_ROOT/audit/research-snapshot.db:$SENTINEL_USER"
)
for state_file_spec in "${state_file_specs[@]}"; do
  existing_file="${state_file_spec%:*}"
  expected_owner="${state_file_spec##*:}"
  if [[ -e "$existing_file" || -L "$existing_file" ]]; then
    if [[ ! -f "$existing_file" || -L "$existing_file" || \
          "$(stat -c %a "$existing_file")" != "600" || \
          "$(stat -c %h "$existing_file")" != "1" || \
          "$(stat -c %U "$existing_file")" != "$expected_owner" || \
          "$(stat -c %G "$existing_file")" != "$expected_owner" ]]; then
      echo "Existing monitor v2 state file metadata is invalid: $existing_file" >&2
      exit 1
    fi
  fi
done

# Preflight complete; mutations may begin.
if ! getent group "$SNAPSHOT_USER" >/dev/null; then
  groupadd --system "$SNAPSHOT_USER"
fi
if ! id "$SNAPSHOT_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SNAPSHOT_USER" --home-dir "$STATE_ROOT/snapshot" \
    --shell /usr/sbin/nologin "$SNAPSHOT_USER"
fi
if ! getent group "$SENTINEL_USER" >/dev/null; then
  groupadd --system "$SENTINEL_USER"
fi
if ! id "$SENTINEL_USER" >/dev/null 2>&1; then
  useradd --system --gid "$SENTINEL_USER" --home-dir "$STATE_ROOT/sentinel" \
    --shell /usr/sbin/nologin "$SENTINEL_USER"
fi
if [[ "$(id -u "$SNAPSHOT_USER")" -eq 0 || \
      "$(id -u "$SENTINEL_USER")" -eq 0 || \
      "$(id -gn "$SNAPSHOT_USER")" != "$SNAPSHOT_USER" || \
      "$(id -gn "$SENTINEL_USER")" != "$SENTINEL_USER" ]]; then
  echo "Monitor v2 identities must remain unprivileged and isolated." >&2
  exit 1
fi
if runuser -u "$SENTINEL_USER" -- test -x "$PRODUCTION_ROOT/data" || \
   runuser -u "$SENTINEL_USER" -- test -r "$PRODUCTION_ROOT/data/research.db"; then
  echo "Production data and database source must not be readable by the sentinel identity." >&2
  exit 1
fi

release_staging=""
current_link_staging=""
sentinel_env=""
cleanup_installer_temporaries() {
  if [[ -n "$sentinel_env" ]]; then
    rm -f -- "$sentinel_env"
  fi
  if [[ -n "$current_link_staging" ]]; then
    rm -f -- "$current_link_staging"
  fi
  if [[ -n "$release_staging" && -L "$CURRENT_RELEASE_LINK" && \
        "$(readlink "$CURRENT_RELEASE_LINK")" == "$release_staging" ]]; then
    release_staging=""
  fi
  if [[ -n "$release_staging" && \
        "$release_staging" == "$RELEASE_ROOT/$expected_head" ]]; then
    rm -rf -- "$release_staging"
  fi
}
trap cleanup_installer_temporaries EXIT

install -d -o root -g root -m 0755 "$(dirname "$RELEASE_ROOT")" "$RELEASE_ROOT"
sealed_release="$RELEASE_ROOT/$expected_head"
if [[ -e "$sealed_release" || -L "$sealed_release" ]]; then
  echo "The sealed release target already exists; refusing to reuse mutable runtime bytes." >&2
  exit 1
fi
mkdir -m 0700 "$sealed_release"
release_staging="$sealed_release"
git -c core.hooksPath=/dev/null -C "$release_staging" init -q
git -c core.hooksPath=/dev/null -C "$release_staging" fetch \
  --no-tags "$PRODUCTION_ROOT" "$expected_head"
git -c core.hooksPath=/dev/null -C "$release_staging" checkout \
  "--detach" "$expected_head"
if [[ "$(git -C "$release_staging" rev-parse --verify HEAD)" != \
      "$expected_head" ]]; then
  echo "The sealed release checkout does not match the approved SHA." >&2
  exit 1
fi
(
  cd "$release_staging"
  HOME=/nonexistent UV_BUILD_CONSTRAINT="$release_staging/config/production-monitor-build-constraints.txt" \
    UV_CACHE_DIR="$UV_CACHE_ROOT" UV_LINK_MODE=copy UV_NO_CONFIG=1 \
    UV_NO_MANAGED_PYTHON=1 UV_PYTHON="$python_path" UV_OFFLINE=1 \
    uv sync --locked --offline --no-dev
)
if [[ ! -x "$release_staging/.venv/bin/telegram-kol-research" ]]; then
  echo "The locked sealed runtime entrypoint was not created." >&2
  exit 1
fi
printf '%s\n' "$expected_head" > \
  "$release_staging/.production-monitor-release-sha"
chown -R root:root "$release_staging"
chmod -R u=rwX,go=rX "$release_staging"
chmod -R a-w "$release_staging"
sealed_symlink_problem=""
while IFS= read -r -d '' sealed_link; do
  case "$sealed_link" in
    "$release_staging/.venv/bin/python"|\
    "$release_staging/.venv/bin/python3"|\
    "$release_staging/.venv/bin/python3.12")
      if [[ "$(readlink -f "$sealed_link")" != "$python_path" ]]; then
        sealed_symlink_problem="$sealed_link"
      fi
      ;;
    *)
      sealed_symlink_problem="$sealed_link"
      ;;
  esac
  if [[ -n "$sealed_symlink_problem" ]]; then
    break
  fi
done < <(find "$sealed_release" -xdev -type l -print0)
if [[ -n "$sealed_symlink_problem" ]]; then
  echo "The sealed release contains an unexpected or untrusted symlink." >&2
  exit 1
fi
release_entrypoint="$release_staging/.venv/bin/telegram-kol-research"
if [[ "$(head -n 1 "$release_entrypoint")" != \
      "#!$release_staging/.venv/bin/python" ]]; then
  echo "The sealed runtime entrypoint shebang is not release-bound." >&2
  exit 1
fi
if ! runuser -u "$SNAPSHOT_USER" -- env -i HOME=/nonexistent \
      PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
      /usr/bin/timeout 15 "$release_entrypoint" \
      "refresh-production-monitor-snapshot" --help >/dev/null || \
   ! runuser -u "$SENTINEL_USER" -- env -i HOME=/nonexistent \
      PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
      /usr/bin/timeout 15 "$release_entrypoint" \
      "run-production-monitor-sentinel" --help >/dev/null || \
   ! runuser -u "$SENTINEL_USER" -- env -i HOME=/nonexistent \
      PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
      /usr/bin/timeout 15 "$release_entrypoint" \
      "run-production-monitor-audit" --help >/dev/null; then
  echo "The sealed runtime command self-check failed." >&2
  exit 1
fi
sealed_metadata_problem="$({
  find "$sealed_release" -xdev \
    \( ! -user root -o \( ! -type l -perm /022 \) \) -print -quit
} 2>/dev/null)"
if [[ -n "$sealed_metadata_problem" || \
      "$(git -C "$sealed_release" rev-parse --verify HEAD)" != \
      "$expected_head" || \
      "$(cat "$sealed_release/.production-monitor-release-sha")" != \
      "$expected_head" ]]; then
  echo "The sealed release must remain root-owned, non-writable, and SHA-bound." >&2
  exit 1
fi

install -d -o root -g root -m 0711 "$STATE_ROOT" "$CACHE_ROOT"
install -d -o "$SNAPSHOT_USER" -g "$SNAPSHOT_USER" -m 0700 "$STATE_ROOT/snapshot"
install -d -o "$SENTINEL_USER" -g "$SENTINEL_USER" -m 0700 \
  "$STATE_ROOT/sentinel" "$CACHE_ROOT/sentinel" "$CACHE_ROOT/audit"

sentinel_env="$(mktemp)"
chmod 0600 "$sentinel_env"
printf 'TELEGRAM_KOL_MONITOR_EXPECTED_HEAD=%s\n' "$expected_head" > "$sentinel_env"
printf '%s\n' "$monitor_token" >> "$sentinel_env"
install -o root -g root -m 0600 "$sentinel_env" "$SENTINEL_ENV_FILE"
install -d -o root -g root -m 0755 "$(dirname "$DB_STAGE_HELPER_DEST")"
install -o root -g root -m 0755 \
  "$sealed_release/src/telegram_kol_research/production_monitor_db_stage.py" \
  "$DB_STAGE_HELPER_DEST"

for source_unit in "${source_units[@]}"; do
  install -o root -g root -m 0644 \
    "$sealed_release/deploy/systemd/$source_unit" "$SYSTEMD_DEST/$source_unit"
done
systemctl daemon-reload
current_link_staging="$(dirname "$CURRENT_RELEASE_LINK")/.current.${expected_head}.$$"
ln -s "$sealed_release" "$current_link_staging"
mv -T "$current_link_staging" "$CURRENT_RELEASE_LINK"
release_staging=""
current_link_staging=""

echo "Installed production monitor v2 units for reviewed HEAD $expected_head."
echo "All v2 services and timers remain disabled and inactive."
