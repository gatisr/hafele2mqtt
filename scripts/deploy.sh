#!/usr/bin/env bash
#
# deploy.sh - build this checkout into a container image, start it on the
# Docker target from the configuration, confirm the running container really is
# that version, and publish the deployed version to Home Assistant as a sensor.
#
# Settings come from .env (then .env.deploy, if present) in the repo root. Any
# key can be overridden by an environment variable of the same name. See
# .env.deploy.example. Nothing installation-specific is baked into this file.
#
# Written for bash 3.2, which is still what macOS ships.

set -euo pipefail

PROJECT="hafele2mqtt"
SENSOR="sensor.hafele2mqtt_deployed_version"
FRIENDLY_NAME="hafele2mqtt deployed version"
ICON="mdi:bluetooth-transfer"

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_DIR"

# ---------------------------------------------------------------- options --

DRY_RUN=0
NO_RELOAD=0
VERSION_ONLY=0
FORCE=0

usage() {
  cat <<'EOF'
Usage: scripts/deploy.sh [options]

Builds the image from this checkout, tags it with the version and with latest,
starts just this service on the configured Docker target, asks the running
container which version it is, then publishes the deployed version to Home
Assistant as sensor.hafele2mqtt_deployed_version and reads it back.

The target is a Docker context or endpoint (DOCKER_CONTEXT, DOCKER_HOST), or a
Docker Engine API over HTTP (DOCKER_API_URL), in which case the image is built
here, exported and loaded onto the target instead. DEPLOY_CMD replaces
`docker compose up -d` when something other than this checkout owns the stack.

Options:
  --dry-run       print what would be done and change nothing. Makes no
                  network calls and builds nothing, so it also works offline.
  --no-reload     build and tag the image but leave the running container
                  alone.
  --version-only  publish the version sensor to Home Assistant and nothing
                  else. Deploys no code.
  --force         deploy even though the working tree has uncommitted changes.
  -h, --help      show this text.

Settings are read from .env and then .env.deploy in the repo root; an
environment variable of the same name wins over both. The deploy settings live
in .env.deploy because .env is handed to the container as its runtime
environment, and the deploy token has no business being in there. Copy
.env.deploy.example to .env.deploy to get started.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --no-reload) NO_RELOAD=1 ;;
    --version-only) VERSION_ONLY=1 ;;
    --force) FORCE=1 ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      printf 'error: unknown option "%s". Run scripts/deploy.sh --help.\n' "$1" >&2
      exit 2
      ;;
  esac
  shift
done

# ---------------------------------------------------------------- output ---

# A step is "open" between step() and the ok/fail that closes it, so an abort
# from deep inside still closes the line with "fail" instead of leaving a
# dangling "... " on screen.
STEP_OPEN=0

step() { STEP_OPEN=1; printf '  %-40s' "$1 ..."; }

ok() {
  STEP_OPEN=0
  if [ -n "${1:-}" ]; then printf 'ok    %s\n' "$1"; else printf 'ok\n'; fi
}

# Used inside a step: closes the line with "fail", then explains on stderr.
fail() {
  STEP_OPEN=0
  printf 'fail\n'
  printf '\nerror: %s\n' "$1" >&2
  exit 1
}

# Explains and exits; closes an open step first, if there is one.
abort() {
  if [ "$STEP_OPEN" = 1 ]; then STEP_OPEN=0; printf 'fail\n'; fi
  printf '\nerror: %s\n' "$1" >&2
  exit 1
}

note() { printf '        %s\n' "$1"; }

WORK_DIR=$(mktemp -d)
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT

# -------------------------------------------------------------- settings ---

# Reads KEY=VALUE lines. A value is taken literally except for one optional
# layer of surrounding quotes, so there is no inline-comment syntax: put
# comments on their own line. A key already present in the environment is left
# alone, which is what makes every setting overridable.
load_env_file() {
  local file="$1" line key value
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      '' | '#'*) continue ;;
    esac
    if [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]]; then
      key="${BASH_REMATCH[2]}"
      value="${BASH_REMATCH[3]}"
    else
      continue
    fi
    case "$value" in
      '"'*'"') value="${value#\"}" && value="${value%\"}" ;;
      "'"*"'") value="${value#\'}" && value="${value%\'}" ;;
    esac
    if [ -z "${!key:-}" ]; then
      export "$key=$value"
    fi
  done <"$file"
}

load_env_file "$REPO_DIR/.env"
load_env_file "$REPO_DIR/.env.deploy"

require_settings() {
  local key missing=""
  for key in "$@"; do
    if [ -z "${!key:-}" ]; then missing="$missing $key"; fi
  done
  if [ -n "$missing" ]; then
    abort "missing required settings:$missing. Copy .env.deploy.example to .env.deploy, fill them in, or pass them as environment variables."
  fi
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    abort "$1 is not installed, and this script needs it. $2"
  fi
}

# Every setting this script understands, with its default. Listing them here
# means an unset one is an empty string rather than a crash under set -u.
HA_URL="${HA_URL:-}"
HA_TOKEN="${HA_TOKEN:-}"
HA_TIMEOUT="${HA_TIMEOUT:-15}"
IMAGE="${IMAGE:-hafele2mqtt}"
COMPOSE_FILE="${COMPOSE_FILE:-compose.yaml}"
SERVICE="${SERVICE:-hafele2mqtt}"
PLATFORM="${PLATFORM:-}"

# Deploying over the Docker Engine API instead of the Docker CLI. This is for a
# daemon that is only reachable as plain HTTP behind a proxy of its own, which
# the CLI cannot be pointed at because it cannot be told to send the proxy's
# header. With it the image is built here, exported, and loaded over the API.
DOCKER_API_URL="${DOCKER_API_URL:-}"
DOCKER_API_URL="${DOCKER_API_URL%/}"
DOCKER_API_HEADER="${DOCKER_API_HEADER:-}"
DOCKER_API_TIMEOUT="${DOCKER_API_TIMEOUT:-600}"

# Replaces `docker compose up -d`, for a target whose stack is owned by
# something other than this checkout. It is run with IMAGE, TAG and SERVICE in
# its environment.
DEPLOY_CMD="${DEPLOY_CMD:-}"

# A credential with a line break in it cannot go through a curl config file,
# and it is a copy-paste accident every time. Checked here rather than inside
# curl_with_header, which only ever runs inside a command substitution: an
# abort in there would end the subshell and let the deploy carry on with an
# empty header.
for cred_key in HA_TOKEN DOCKER_API_HEADER; do
  case "${!cred_key}" in
    *$'\n'* | *$'\r'*)
      abort "$cred_key contains a line break. Each credential has to be a single line - check .env.deploy."
      ;;
  esac
done
unset cred_key

# An exported but empty DOCKER_CONTEXT or DOCKER_HOST is worse than an absent
# one, because the Docker CLI would take it as a choice. Leaving one of the two
# blank in the file is normal, so clear those out instead of passing them on.
[ -n "${DOCKER_CONTEXT:-}" ] || unset DOCKER_CONTEXT
[ -n "${DOCKER_HOST:-}" ] || unset DOCKER_HOST

# ----------------------------------------------------------------- curl ---

# curl_with_header HEADER ARGS... - runs curl with one header that is a secret.
#
# The header goes in through a config file on stdin rather than on the command
# line, because a command line is not private: `ps auxww` shows every running
# process's full argv to any local user on the machine, so an argv-borne token
# is readable by anyone for as long as the request takes. Everything else stays
# where it was; there is nothing to learn from -o, --max-time or a file name.
#
# An empty HEADER means "no header", which is how docker_api runs against a
# target that needs no credential.
curl_with_header() {
  local header="$1" escaped
  shift
  if [ -z "$header" ]; then
    curl "$@"
    return
  fi
  # Inside the double quotes of a curl config file, curl reads \\ and \" as
  # escapes, so both have to be doubled on the way in. Without this a token
  # containing either would produce a config curl reads as something else:
  # a truncated header, or a stray option.
  escaped="${header//\\/\\\\}"
  escaped="${escaped//\"/\\\"}"
  # `-K -` points curl at this pipe, which is the only thing on its stdin, so
  # it cannot consume input a caller was reading.
  printf 'header = "%s"\n' "$escaped" | curl -K - "$@"
}

# ------------------------------------------------------------------- HA ----

# ha_api METHOD PATH [BODY_FILE] - prints the response body, fails loudly.
ha_api() {
  local method="$1" path="$2" body_file="${3:-}" code out
  out="$WORK_DIR/response.json"
  local args
  # The token is the one argument that does not go on the command line; see
  # curl_with_header. A content type is not a secret.
  args=(-sS -o "$out" -w '%{http_code}' -X "$method" --max-time "$HA_TIMEOUT"
    -H "Content-Type: application/json")
  if [ -n "$body_file" ]; then
    args=("${args[@]}" --data-binary "@$body_file")
  fi
  if ! code=$(curl_with_header "Authorization: Bearer $HA_TOKEN" \
    "${args[@]}" "${HA_URL%/}$path"); then
    printf 'could not reach Home Assistant at %s. Check HA_URL and that the machine can see it.\n' "${HA_URL%/}" >&2
    return 1
  fi
  case "$code" in
    2*) ;;
    401 | 403)
      printf 'Home Assistant rejected the token (HTTP %s) on %s %s. Check HA_TOKEN.\n' "$code" "$method" "$path" >&2
      return 1
      ;;
    *)
      printf 'Home Assistant answered HTTP %s on %s %s: %s\n' "$code" "$method" "$path" "$(head -c 300 "$out")" >&2
      return 1
      ;;
  esac
  cat "$out"
}

publish_version() {
  local body="$WORK_DIR/state.json"
  jq -n \
    --arg state "$VERSION" \
    --arg commit "$COMMIT" \
    --arg deployed_at "$DEPLOYED_AT" \
    --arg deployed_from "$DEPLOYED_FROM" \
    --arg friendly_name "$FRIENDLY_NAME" \
    --arg icon "$ICON" \
    '{state: $state, attributes: {commit: $commit, deployed_at: $deployed_at,
      deployed_from: $deployed_from, friendly_name: $friendly_name, icon: $icon}}' \
    >"$body"
  ha_api POST "/api/states/$SENSOR" "$body" >/dev/null
}

# ----------------------------------------------------- Docker Engine API ---

# docker_api METHOD PATH [BODY_FILE [CONTENT_TYPE]] - prints the response body.
# DOCKER_API_HEADER is never echoed, not even in an error, and never put on a
# command line, because for most targets it is the credential.
docker_api() {
  local method="$1" path="$2" body_file="${3:-}" ctype="${4:-application/json}" code out
  out="$WORK_DIR/docker-api.out"
  local args
  args=(-sS -o "$out" -w '%{http_code}' -X "$method" --max-time "$DOCKER_API_TIMEOUT")
  if [ -n "$body_file" ]; then
    args=("${args[@]}" -H "Content-Type: $ctype" --data-binary "@$body_file")
  fi
  if ! code=$(curl_with_header "$DOCKER_API_HEADER" "${args[@]}" "$DOCKER_API_URL$path"); then
    printf 'could not reach the Docker API at %s. Check DOCKER_API_URL and that this machine can see it.\n' "$DOCKER_API_URL" >&2
    return 1
  fi
  case "$code" in
    2*) ;;
    401 | 403)
      printf 'the Docker API rejected the request (HTTP %s) on %s %s. Check DOCKER_API_HEADER.\n' "$code" "$method" "$path" >&2
      return 1
      ;;
    *)
      printf 'the Docker API answered HTTP %s on %s %s: %s\n' "$code" "$method" "$path" "$(head -c 300 "$out")" >&2
      return 1
      ;;
  esac
  cat "$out"
}

# The API equivalent of `docker compose ps -q SERVICE`: compose stamps the
# service name onto every container it creates. A target whose stack is not run
# by compose has no such label, so fall back to the plain container name.
remote_container_id() {
  local filters id
  filters=$(jq -rn --arg s "$SERVICE" '{label: ["com.docker.compose.service=\($s)"]} | @json | @uri')
  # Both guarded: "no such container" is the normal answer right after a start,
  # and the fallback below answers it with an HTTP 404, which docker_api
  # reports as a non-zero exit. Unguarded that is fatal under `set -e` - inside
  # a function, so it would kill the caller's retry loop rather than return to
  # it. An empty id is the signal the callers are written for.
  id=$(docker_api GET "/containers/json?filters=$filters" | jq -r '.[0].Id // empty') || id=""
  if [ -z "$id" ]; then
    id=$(docker_api GET "/containers/$SERVICE/json" | jq -r '.Id // empty') || id=""
  fi
  printf '%s' "$id"
}

# Runs one command inside a remote container and prints what it wrote. Tty asks
# for the raw stream rather than Docker's multiplexed stdout/stderr framing.
remote_exec() {
  local cid="$1" exec_id
  shift
  # The command arrives one argument per line rather than as jq's --args,
  # because jq reads a leading dash as one of its own options and this command
  # has several.
  printf '%s\n' "$@" |
    jq -Rsc '{AttachStdout: true, AttachStderr: true, Tty: true, Cmd: (rtrimstr("\n") | split("\n"))}' \
      >"$WORK_DIR/exec-create.json"
  # Guarded so the line below is reachable: a container that is still starting
  # refuses the exec, and `set -e` would end the deploy there instead of
  # returning 1 to the caller, which retries.
  exec_id=$(docker_api POST "/containers/$cid/exec" "$WORK_DIR/exec-create.json" | jq -r '.Id // empty') || exec_id=""
  [ -n "$exec_id" ] || return 1
  jq -n '{Detach: false, Tty: true}' >"$WORK_DIR/exec-start.json"
  docker_api POST "/exec/$exec_id/start" "$WORK_DIR/exec-start.json" | tr -d '\r'
}

# ----------------------------------------------------------------- facts ---

require_command jq "Install it with: brew install jq (macOS) or apt install jq (Debian/Ubuntu)."
require_command curl "Install it with: brew install curl (macOS) or apt install curl (Debian/Ubuntu)."

VERSION_FILE="app/__init__.py"
[ -f "$REPO_DIR/$VERSION_FILE" ] || abort "$VERSION_FILE is missing, so there is no version to deploy. Run this from a full checkout."
# `|| VERSION=""` so the sentence below is what the user sees. `head -1` closes
# the pipe as soon as it has its line, which can leave sed killed by SIGPIPE,
# and `pipefail` turns that into a failed assignment on a file that was read
# perfectly well.
VERSION=$(sed -n 's/^__version__[[:space:]]*=[[:space:]]*["'"'"']\([^"'"'"']*\)["'"'"'].*/\1/p' "$REPO_DIR/$VERSION_FILE" | head -1) || VERSION=""
[ -n "$VERSION" ] || abort "$VERSION_FILE has no __version__ assignment to read the version from."

if ! COMMIT=$(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null); then
  abort "this is not a git checkout, so the deployed commit cannot be recorded."
fi
DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEPLOYED_FROM=$(hostname -s 2>/dev/null || hostname)

printf '%s %s (%s)\n' "$PROJECT" "$VERSION" "$COMMIT"

# --------------------------------------------------------------- preflight -

step "check prerequisites"
if [ "$VERSION_ONLY" -eq 1 ]; then
  require_settings HA_URL HA_TOKEN
  ok "version sensor only"
else
  require_settings HA_URL HA_TOKEN
  if [ -n "$DOCKER_API_URL" ]; then
    # Two different answers to "which daemon", and no way to tell which one was
    # meant. Refuse rather than silently pick one.
    if [ -n "${DOCKER_CONTEXT:-}" ] || [ -n "${DOCKER_HOST:-}" ]; then
      abort "DOCKER_API_URL is set together with ${DOCKER_HOST:+DOCKER_HOST}${DOCKER_CONTEXT:+DOCKER_CONTEXT}, and those name two different targets. Unset the one you did not mean."
    fi
    # Over the API the image arrives as a tarball, and nothing in it knows how
    # to start a container. Without DEPLOY_CMD this run would end with a loaded
    # image and the old container still serving, which looks like success.
    if [ "$NO_RELOAD" -eq 0 ] && [ -z "$DEPLOY_CMD" ]; then
      abort "DOCKER_API_URL is set but DEPLOY_CMD is empty, so the image would be loaded onto the target and never started. Set DEPLOY_CMD, or pass --no-reload if staging the image is all you wanted."
    fi
  elif [ -z "${DOCKER_CONTEXT:-}" ] && [ -z "${DOCKER_HOST:-}" ]; then
    abort "none of DOCKER_CONTEXT, DOCKER_HOST or DOCKER_API_URL is set, and guessing which Docker daemon you meant is how the wrong machine gets a new container. Set one of them; DOCKER_CONTEXT=default means the local daemon."
  fi
  require_command docker "Install Docker: https://docs.docker.com/get-docker/"
  if [ -z "$DEPLOY_CMD" ]; then
    [ -f "$REPO_DIR/$COMPOSE_FILE" ] || abort "$COMPOSE_FILE does not exist in the repo root. Set COMPOSE_FILE if your compose file has another name, or DEPLOY_CMD if compose is not what starts this service."
  fi
  if [ "$DRY_RUN" -eq 0 ] && ! docker version >"$WORK_DIR/docker.log" 2>&1; then
    printf 'fail\n'
    tail -n 10 "$WORK_DIR/docker.log" >&2
    abort "cannot talk to the Docker daemon at ${DOCKER_HOST:-context ${DOCKER_CONTEXT:-default}}; its answer is above."
  fi
  if [ -n "$DOCKER_API_URL" ]; then
    # The build is local even in API mode, so both daemons have to answer.
    if [ "$DRY_RUN" -eq 0 ] && ! docker_api GET /version >"$WORK_DIR/api-version.json" 2>"$WORK_DIR/api-version.err"; then
      printf 'fail\n'
      cat "$WORK_DIR/api-version.err" >&2
      abort "cannot talk to the Docker API at $DOCKER_API_URL; its answer is above."
    fi
    ok "API $DOCKER_API_URL"
  else
    ok "target ${DOCKER_HOST:-context ${DOCKER_CONTEXT:-default}}"
  fi
fi

step "check working tree"
if [ -n "$(git -C "$REPO_DIR" status --porcelain)" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    ok "dirty (a real run would need --force)"
  elif [ "$FORCE" -eq 0 ]; then
    fail "the working tree has uncommitted changes, so what you deploy would not match commit $COMMIT. Commit them, stash them, or re-run with --force."
  else
    COMMIT="$COMMIT-dirty"
    ok "dirty, continuing because of --force"
  fi
else
  ok "clean"
fi

# ---------------------------------------------------------------- deploy ---

RELOADED=0
RUNNING_VERSION=""

if [ "$VERSION_ONLY" -eq 0 ]; then
  # compose.yaml reads both, so the tag that gets built is the tag that gets
  # started.
  export IMAGE
  export TAG="$VERSION"
  export SERVICE

  BUILD_ARGS=(build -t "$IMAGE:$VERSION" -t "$IMAGE:latest")
  if [ -n "$PLATFORM" ]; then
    BUILD_ARGS=("${BUILD_ARGS[@]}" --platform "$PLATFORM")
  fi

  step "build the image"
  if [ "$DRY_RUN" -eq 1 ]; then
    ok "would run docker ${BUILD_ARGS[*]} ."
  else
    if ! docker "${BUILD_ARGS[@]}" "$REPO_DIR" >"$WORK_DIR/build.log" 2>&1; then
      printf 'fail\n'
      tail -n 30 "$WORK_DIR/build.log" >&2
      abort "docker build failed; its last lines are above."
    fi
    ok "$IMAGE:$VERSION and $IMAGE:latest"
  fi

  if [ -n "$DOCKER_API_URL" ]; then
    # The tarball runs to hundreds of megabytes, so it goes to a file and is
    # handed to curl by name. Nothing here passes through a shell variable.
    IMAGE_TAR="$WORK_DIR/image.tar"
    step "export the image"
    if [ "$DRY_RUN" -eq 1 ]; then
      ok "would run docker save $IMAGE:$VERSION $IMAGE:latest"
    else
      if ! docker save --output "$IMAGE_TAR" "$IMAGE:$VERSION" "$IMAGE:latest" >"$WORK_DIR/save.log" 2>&1; then
        printf 'fail\n'
        tail -n 10 "$WORK_DIR/save.log" >&2
        abort "docker save failed; its last lines are above."
      fi
      ok "$(du -h "$IMAGE_TAR" | awk '{print $1}') tarball"
    fi

    step "load the image onto the target"
    if [ "$DRY_RUN" -eq 1 ]; then
      ok "would POST it to $DOCKER_API_URL/images/load"
    else
      docker_api POST "/images/load" "$IMAGE_TAR" "application/x-tar" >"$WORK_DIR/load.json" ||
        abort "could not load the image onto the Docker API target (see the message above)."
      # /images/load answers 200 and then reports the failure inside the
      # progress stream, so the status code alone proves nothing.
      if grep -q '"error' "$WORK_DIR/load.json"; then
        printf 'fail\n'
        head -c 500 "$WORK_DIR/load.json" >&2
        printf '\n' >&2
        abort "the Docker API accepted the upload but could not load the image; its answer is above."
      fi
      rm -f "$IMAGE_TAR"
      ok "uploaded"
    fi

    step "confirm the image on the target"
    if [ "$DRY_RUN" -eq 1 ]; then
      ok "would read back $IMAGE:$VERSION"
    else
      REMOTE_ID=$(docker_api GET "/images/$IMAGE:$VERSION/json" | jq -r '.Id // empty') ||
        abort "could not read $IMAGE:$VERSION back from the Docker API target (see the message above)."
      [ -n "$REMOTE_ID" ] || fail "the Docker API target has no $IMAGE:$VERSION after the load, so there is nothing for it to start."
      ok "${REMOTE_ID#sha256:}"
    fi
  fi

  step "start the service"
  if [ "$NO_RELOAD" -eq 1 ]; then
    ok "skipped (--no-reload)"
  elif [ -n "$DEPLOY_CMD" ]; then
    if [ "$DRY_RUN" -eq 1 ]; then
      ok "would run DEPLOY_CMD"
    else
      if ! bash -c "$DEPLOY_CMD" >"$WORK_DIR/up.log" 2>&1; then
        printf 'fail\n'
        tail -n 30 "$WORK_DIR/up.log" >&2
        abort "DEPLOY_CMD failed; its last lines are above."
      fi
      RELOADED=1
      ok "DEPLOY_CMD finished"
    fi
  elif [ "$DRY_RUN" -eq 1 ]; then
    ok "would run docker compose -f $COMPOSE_FILE up -d --no-build $SERVICE"
  else
    if ! docker compose -f "$COMPOSE_FILE" up -d --no-build "$SERVICE" >"$WORK_DIR/up.log" 2>&1; then
      printf 'fail\n'
      tail -n 30 "$WORK_DIR/up.log" >&2
      abort "docker compose up failed; its last lines are above. Only the $SERVICE service was touched."
    fi
    RELOADED=1
    ok "$SERVICE is up"
  fi

  step "verify the running version"
  if [ "$DRY_RUN" -eq 1 ] || [ "$RELOADED" -eq 0 ]; then
    ok "skipped (nothing was started)"
  else
    # The container needs a moment before exec works, and a crash loop needs to
    # be reported as a crash loop rather than as a missing version.
    if [ -n "$DOCKER_API_URL" ]; then
      CONTAINER=""
      ATTEMPT=0
      while [ "$ATTEMPT" -lt 10 ] && [ -z "$CONTAINER" ]; do
        # `|| CONTAINER=""` is what makes this a retry loop. An assignment is
        # not a tested command, so under `set -e` a failing one takes the whole
        # script down - and the first few seconds after a start are exactly
        # when it does fail, because the container is not there yet. Without
        # this the loop runs once, silently, and the message below never prints.
        CONTAINER=$(remote_container_id 2>/dev/null) || CONTAINER=""
        [ -n "$CONTAINER" ] || sleep 1
        ATTEMPT=$((ATTEMPT + 1))
      done
      [ -n "$CONTAINER" ] || fail "the deploy reported success, but the Docker API target has no container for $SERVICE."
      ATTEMPT=0
      while [ "$ATTEMPT" -lt 10 ]; do
        if RUNNING_VERSION=$(remote_exec "$CONTAINER" python -m app.main --version 2>/dev/null); then
          [ -n "$RUNNING_VERSION" ] && break
        fi
        RUNNING_VERSION=""
        ATTEMPT=$((ATTEMPT + 1))
        sleep 1
      done
    else
      # Guarded for the same reason, with `pipefail` adding a second way to
      # fail: `head -1` closes the pipe, which can leave `ps` on SIGPIPE even
      # when it did its job.
      CONTAINER=$(docker compose -f "$COMPOSE_FILE" ps -q "$SERVICE" 2>/dev/null | head -1) || CONTAINER=""
      [ -n "$CONTAINER" ] || fail "docker compose started $SERVICE but then reported no container for it."
      ATTEMPT=0
      while [ "$ATTEMPT" -lt 10 ]; do
        if RUNNING_VERSION=$(docker exec "$CONTAINER" python -m app.main --version 2>/dev/null); then
          break
        fi
        RUNNING_VERSION=""
        ATTEMPT=$((ATTEMPT + 1))
        sleep 1
      done
    fi
    if [ -z "$RUNNING_VERSION" ]; then
      fail "could not ask the container for its version. Check it is still running: docker logs $CONTAINER"
    fi
    RUNNING_VERSION=$(printf '%s' "$RUNNING_VERSION" | tr -d '\r' | tail -1)
    RUNNING_VERSION=${RUNNING_VERSION##* }
    if [ "$RUNNING_VERSION" != "$VERSION" ]; then
      fail "the running container reports version $RUNNING_VERSION, but $VERSION was just built. Something else is holding the $IMAGE tag."
    fi
    ok "container reports $RUNNING_VERSION"
  fi
fi

# ------------------------------------------------------- version to HA -----

step "publish version to Home Assistant"
if [ "$DRY_RUN" -eq 1 ]; then
  ok "would set $SENSOR = $VERSION"
else
  publish_version || abort "could not publish $SENSOR to Home Assistant (see the message above)."
  ok "$SENSOR = $VERSION"
fi

step "read the sensor back"
if [ "$DRY_RUN" -eq 1 ]; then
  ok "would read $SENSOR back"
  REPORTED="(not read, dry run)"
else
  ha_api GET "/api/states/$SENSOR" >"$WORK_DIR/readback.json" ||
    abort "could not read $SENSOR back from Home Assistant (see the message above)."
  # Guarded so a reply that is not the JSON we expected is reported as a
  # mismatch with both values named, rather than as a bare non-zero exit.
  REPORTED=$(jq -r '.state' <"$WORK_DIR/readback.json") || REPORTED="(unreadable)"
  if [ "$REPORTED" != "$VERSION" ]; then
    fail "Home Assistant reports $SENSOR = $REPORTED, but $VERSION was just published."
  fi
  ok "$SENSOR = $REPORTED"
  note "commit $(jq -r '.attributes.commit' <"$WORK_DIR/readback.json"), deployed_at $(jq -r '.attributes.deployed_at' <"$WORK_DIR/readback.json")"
fi

# --------------------------------------------------------------- verdict ---

printf '\n'
if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s %s dry run: nothing was built, started or published.\n' "$PROJECT" "$VERSION"
elif [ "$VERSION_ONLY" -eq 1 ]; then
  printf '%s %s was not deployed (--version-only); HA reports %s = %s\n' "$PROJECT" "$VERSION" "$SENSOR" "$REPORTED"
elif [ "$RELOADED" -eq 1 ]; then
  printf '%s %s deployed and running (the container itself reports %s); HA reports %s = %s\n' "$PROJECT" "$VERSION" "$RUNNING_VERSION" "$SENSOR" "$REPORTED"
else
  printf '%s %s built and tagged but not started, so the old container keeps running; HA reports %s = %s\n' "$PROJECT" "$VERSION" "$SENSOR" "$REPORTED"
fi
