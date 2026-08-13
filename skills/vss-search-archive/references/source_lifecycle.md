# Search source lifecycle

Use the search agent's source endpoints so VST, VIOS, and Elasticsearch remain
consistent. Never replace these operations with direct backend mutations.

These Agent-backed mutations are the full-stack path. For a headless
`vss-build-vision-agent` deployment with no Agent tier, provision the source
through `vss-manage-video-io-storage`'s
[direct register-and-fan-out workflow](../../vss-manage-video-io-storage/references/provision-vios-source.md),
then return here for search. Do not apply the Agent endpoint recipes below to
a deployment that has no Agent.

## Contents

- [Deployment and runtime state](#deployment-and-runtime-state)
- [Pre-ingestion cleanup](#pre-ingestion-cleanup)
- [File source](#file-source)
- [RTSP source](#rtsp-source)
- [Delete source](#delete-source)

## Deployment and runtime state

Use the operator-provided Compose or Ingress origin. Never inspect Compose or
Kubernetes internals to rediscover it. On Brev, run the public-origin selection
block below first and set `VSS_ORIGIN` to its result. Do not configure a
provisional origin and change it afterward. Record that final origin, then read
the backends' own service, model, and index inventory:

```bash
: "${VSS_ORIGIN:?set the deployment origin}"
: "${VSS_REPO_ROOT:?set the validated checkout}"
VSS_ORIGIN="${VSS_ORIGIN%/}"
AGENT_URL="${VSS_ORIGIN}"
VST_URL="${VSS_ORIGIN}"

VSS=(uv run --project "${VSS_REPO_ROOT}/services/agent" --no-dev --extra cli vss)
"${VSS[@]}" search run --help >/dev/null || exit 1
"${VSS[@]}" configure --base-url "${VSS_ORIGIN}" || exit 1
CONFIG_JSON=$("${VSS[@]}" configure show) || exit 1

ES_URL=$(printf '%s' "${CONFIG_JSON}" | jq -er '.services.elasticsearch.url') || exit 1
RTVI_EMBED_URL=$(printf '%s' "${CONFIG_JSON}" | jq -er '.services.rt_embed.url') || exit 1
RTVI_EMBED_MODEL=$(printf '%s' "${CONFIG_JSON}" | jq -er \
  '.services.rt_embed.models[0] | select(type == "string" and length > 0)') || exit 1
RTVI_CV_URL=$(printf '%s' "${CONFIG_JSON}" | jq -er '.services.rtvi_cv.url') || exit 1
RTVI_VLM_URL=$(printf '%s' "${CONFIG_JSON}" | jq -er \
  '.services.rt_vlm.url // empty') || RTVI_VLM_URL=
resolve_search_indexes() {
  CONFIG_JSON=$("${VSS[@]}" configure show) || return 1
  printf '%s' "${CONFIG_JSON}" |
    jq -e '.services.elasticsearch.indices | type == "array"' >/dev/null || return 1
  EMBED_INDEX=$(printf '%s' "${CONFIG_JSON}" | jq -er \
    '[.services.elasticsearch.indices[] | select(startswith("mdx-embed-"))] | sort | first') || return 1
  BEHAVIOR_INDEX=$(printf '%s' "${CONFIG_JSON}" | jq -er \
    '[.services.elasticsearch.indices[] | select(startswith("mdx-behavior-"))] | sort | first') || return 1
  RAW_INDEX=$(printf '%s' "${CONFIG_JSON}" | jq -er \
    '[.services.elasticsearch.indices[] | select(startswith("mdx-raw-"))] | sort | first') || return 1
  [ "${EMBED_INDEX}" != "${BEHAVIOR_INDEX}" ] &&
    [ "${EMBED_INDEX}" != "${RAW_INDEX}" ] &&
    [ "${BEHAVIOR_INDEX}" != "${RAW_INDEX}" ]
}
```

Do not call `resolve_search_indexes` on a fresh stack. Indexes are created
lazily and may not all appear at the same instant after ingestion. After every
intended upload completes, boundedly refresh
`vss configure --base-url "${VSS_ORIGIN}"` under the shared source-setup budget
until `resolve_search_indexes` observes all three distinct indexes. Only then
begin document readiness checks.
Never use `ELASTIC_SEARCH_INDEX`, an index template, or a guessed date in place
of `vss configure show`.

Before downloading or ingesting media, require bounded Agent and VST health
through the deployment's host-reachable origin, a nonempty model from the
recorded RT-Embed `/v1/models` route, and RTVI-CV readiness. Missing RT-Embed
is a deployment-readiness failure: stop before cleanup, download, or upload
instead of waiting for an embedding index that cannot be produced. If
RT-VLM is recorded, probe its `/v1/models` endpoint; if it is absent, continue
and let search hits remain `unverified`. A particular eval or deployment
request may explicitly require RT-VLM and should then stop when that stronger
prerequisite is unmet. RTVI-CV may build its TensorRT engine for several
minutes, so poll its readiness with backoff rather than probing once.

Cleanup, upload, RTVI-CV readiness, and post-ingest index readiness draw on ONE
shared 40-minute source-setup budget, not 40 minutes each. Deployment and
public-origin selection are prerequisite work outside this ingestion budget.
Carry the remaining source-setup budget forward instead of restarting the
clock, and never redeploy, restart, or re-ingest to recover time already spent.

On Brev, two different origins produce media URLs, and they are easy to
conflate:

1. **The host CLI stamps the origin you gave `vss configure`.** `vss search
   run` builds every `screenshot_url` from the recorded deployment origin —
   `vst_external_url` is set to that base URL, not to `VST_EXTERNAL_URL`. So
   the only way to make CLI hits carry browser-usable media links is to run
   `vss configure --base-url` against the public HTTPS secure-link origin.
   Editing `VST_EXTERNAL_URL` in `generated.env` cannot change them, and
   recreating containers to chase that value is wasted work.
2. **`VST_EXTERNAL_URL` governs the Agent-served path.** The profile's
   `config.yml` feeds it to the agent, so it is what the UI and
   `/api/v1/search` responses emit. Give the deployment workflow the Brev
   values before it writes `generated.env` so that path is right too, but do
   not expect it to affect the CLI.

Prefer the public secure-link origin for `vss configure` whenever a bounded
probe shows it answers `/vst/api/v1/sensor/version` from this host. If it does
not answer, configure against the host-reachable origin so retrieval still
works, and report that CLI media URLs will be host-local until the secure link
is fixed — that is a routing failure to report, not to repair in a loop, and it
must not block fixture download, Agent-backed ingestion, or index readiness.

A probe succeeds only on a non-redirecting HTTP 200 with the VST version
schema. A Cloudflare/Pomerium redirect or HTML login page is a failed public
probe even though plain `curl -f` would return zero for a 3xx response. The
bundled selector owns the sole public request. Execute it exactly once and
consume its decision, even when it selects the fallback. Do not issue a
public-origin `curl` before or after it, reconstruct its command, rerun it to
confirm the result, or troubleshoot a `000`/redirect/schema failure during
this workflow:

```bash
: "${VSS_PUBLIC_CANDIDATE:?deployment-minted public HTTPS origin}"
: "${VSS_HOST_ORIGIN:?host-reachable HAProxy origin}"
ORIGIN_SELECTOR="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/select_brev_origin.sh"
test -x "${ORIGIN_SELECTOR}" || exit 1
ORIGIN_SELECTION=$("${ORIGIN_SELECTOR}" \
  "${VSS_PUBLIC_CANDIDATE}" "${VSS_HOST_ORIGIN}") || exit 1
VSS_ORIGIN=$(printf '%s' "${ORIGIN_SELECTION}" |
  jq -er '.origin | select(type == "string" and length > 0)') || exit 1
VSS_MEDIA_SCOPE=$(printf '%s' "${ORIGIN_SELECTION}" |
  jq -er '.media_scope | select(. == "public" or . == "host-local")') || exit 1
if [ "${VSS_MEDIA_SCOPE}" = host-local ]; then
  echo "Public VST probe failed semantic validation; CLI media URLs will be host-local" >&2
fi
```

Never assemble a Brev hostname from guesswork: the documented
`7777-<BREV_ENV_ID>.<BREV_LINK_DOMAIN>` form, built only from values read out
of `/etc/environment`, is the one sanctioned construction, and letting the
deployment workflow write it is preferred. Never rewrite a returned media URL.

On Kubernetes, use only routed Ingress services. Do not port-forward
Elasticsearch for readiness or cleanup. When Elasticsearch is not routed,
report only the Agent and VST state you can actually validate.

```bash
index_count() {
  INDEX=$1 FIELD=$2 VALUE=$3
  QUERY=$(jq -cn --arg field "${FIELD}" --arg value "${VALUE}" \
    '{query:{term:{($field):$value}}}') || return 1
  SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
  COUNT_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15) || return 1
  curl -fsS --max-time "${COUNT_TIMEOUT}" -H 'Content-Type: application/json' \
    "${ES_URL}/${INDEX}/_count" -d "${QUERY}" | jq -er '.count | numbers'
}
```

## Pre-ingestion cleanup

At the start of one source-setup operation, after deployment, public-origin
selection, and `vss configure` are complete, initialize the persisted ingestion
budget exactly once. The helper stores its absolute deadline under the VSS
configuration directory, so independent Bash tool calls consume the same
clock. Later calls must use `remaining`; never call `start` again during the
operation, create a phase timer, or recompute an epoch-plus-duration deadline:

```bash
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
test -x "${SOURCE_SETUP_BUDGET}" || exit 1
"${SOURCE_SETUP_BUDGET}" start 2400 || exit 1
```

For every subsequent blocking source-mutation or readiness request, obtain its
`--max-time` from `${SOURCE_SETUP_BUDGET} remaining <per-request-cap>`
immediately before the request. Re-declare only the helper path when a new Bash
call begins. A literal `--max-time`, another `start`, a new
epoch-plus-duration expression, or a phase-local deadline after initialization
violates the source-setup contract.

Before fixture cleanup, prove that the deployment recorded a working embedding
model and that RT-CV finished initializing its DeepStream pipeline. These
read-only probes consume the same source-setup budget. An HTTP 200 alone is not
RT-CV readiness: require `ds-ready` to be exactly `YES`, accepting the response
field either at the top level or below `ready-info` for compatibility across
the supported RT-CV API shapes:

```bash
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
EMBED_MODELS_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 30) || exit 1
EMBED_MODELS=$(curl -fsS --connect-timeout 5 --max-time "${EMBED_MODELS_TIMEOUT}" \
  "${RTVI_EMBED_URL%/}/v1/models") || exit 1
printf '%s' "${EMBED_MODELS}" | jq -e \
  '.data | type == "array" and length > 0 and
   all(.[]; .id | type == "string" and length > 0)' >/dev/null || exit 1

while :; do
  RTVI_CV_READY_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15) || exit 1
  RTVI_CV_READY=$(curl -fsS --connect-timeout 5 \
    --max-time "${RTVI_CV_READY_TIMEOUT}" \
    "${RTVI_CV_URL%/}/api/v1/ready" 2>/dev/null || true)
  if printf '%s' "${RTVI_CV_READY}" | jq -e \
    '(."ds-ready" // ."ready-info"."ds-ready" // "") == "YES"' \
    >/dev/null 2>&1; then
    break
  fi
  "${SOURCE_SETUP_BUDGET}" remaining 10 >/dev/null || exit 1
  sleep 10
done
```

Do not begin cleanup, fixture download, or an Agent upload while this poll is
still pending. In particular, a listening REST endpoint with `ds-ready: NO`
can still be building TensorRT engines; adding streams during that phase can
turn an initialization problem into a persistent CUDNN processing failure.

Cleanup is an Agent operation. Resolve every exact or duplicate fixture entry
from the VST source list, then delete its UUID only through the Agent:

```bash
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
VST_LIST_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15) || exit 1
VST_SENSOR_LIST=$(curl -fsS --connect-timeout 5 --max-time "${VST_LIST_TIMEOUT}" \
  "${VST_URL%/}/vst/api/v1/sensor/list") || exit 1
mapfile -t SENSORS_TO_DELETE < <(
  printf '%s' "${VST_SENSOR_LIST}" |
    jq -er '.[] | select(.name == "airport" or
                        .name == "warehouse_sample" or
                        .name == "warehouse-ladder" or
                        .name == "sample-warehouse-ladder") |
            .sensorId | select(type == "string" and length > 0)'
)
for SENSOR_TO_DELETE in "${SENSORS_TO_DELETE[@]}"; do
  test -n "${SENSOR_TO_DELETE}" || exit 1
  DELETE_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 300) || exit 1
  curl -fsS --connect-timeout 5 --max-time "${DELETE_TIMEOUT}" -X DELETE \
    "${AGENT_URL%/}/api/v1/videos/${SENSOR_TO_DELETE}" |
    jq -e '.status == "success"' >/dev/null || exit 1
done

while :; do
  VST_LIST_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15) || exit 1
  VST_SENSOR_LIST=$(curl -fsS --connect-timeout 5 --max-time "${VST_LIST_TIMEOUT}" \
    "${VST_URL%/}/vst/api/v1/sensor/list") || exit 1
  if ! printf '%s' "${VST_SENSOR_LIST}" | jq -e \
    'any(.[]; .name == "airport" or
              .name == "warehouse_sample" or
              .name == "warehouse-ladder" or
              .name == "sample-warehouse-ladder")' >/dev/null; then
    break
  fi
  sleep 10
done
```

Never send a mutating request directly to VST, RTVI-CV, RTVI-Embed,
storage-ms, or Elasticsearch. In particular, do not use `DELETE` on ports
30888, 9000, 8010, 8017, or 9200. If Agent cleanup fails, stop; do not repair
partial state through a backend.

## File source

List current sources through `vss-manage-video-io-storage`; do not upload an
exact existing source. Confirm an interactive upload, then use the mandatory
three-step Agent HTTP flow. This flow is a sequence of HTTP requests sent with
Bash/curl; it does not imply or require a dedicated Workflow or Agent harness
tool call. The three mutations are mandatory for every file ingestion:

For the release fixtures, download the exact pinned bundle into a fresh
directory; never use `find` to substitute a pre-existing warehouse-looking
file. Ingest only the files the request names:

```bash
FIXTURE_ROOT=$(mktemp -d /tmp/vss-search-fixtures.XXXXXX)
cd "${FIXTURE_ROOT}" || exit 1
ngc registry resource download-version \
  nvidia/vss-developer/dev-profile-sample-data:3.2.0 \
  --org nvidia --team vss-developer || exit 1
tar -xzf dev-profile-sample-data_v3.2.0/dev-profile-sample-data.tar.gz || exit 1
SAMPLE_DIR="${FIXTURE_ROOT}/dev-profile-sample-data"
test -s "${SAMPLE_DIR}/warehouse_sample.mp4" || exit 1
test -s "${SAMPLE_DIR}/sample-warehouse-ladder.mp4" || exit 1
```

```bash
: "${AGENT_URL:?resolve the selected search agent}"
RTVI_CV_LOG_SINCE="${RTVI_CV_LOG_SINCE:-$(date -u +%Y-%m-%dT%H:%M:%SZ)}"
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"

stage_agent_upload() {
  local file_path=$1 upload_filename=$2 request request_timeout
  local url_response returned_url returned_path effective_url identifier upload_timeout
  test -r "${file_path}" || return 1
  request=$(jq -cn --arg filename "${upload_filename}" '{filename: $filename}') || return 1
  request_timeout=$("${SOURCE_SETUP_BUDGET}" remaining 30) || return 1
  url_response=$(curl -sfS --max-time "${request_timeout}" -X POST \
    "${AGENT_URL%/}/api/v1/videos" \
    -H "Content-Type: application/json" -d "${request}") || return 1
  returned_url=$(printf '%s' "${url_response}" |
    jq -er '.url | select(type == "string" and length > 0)') || return 1

  case "${returned_url}" in
    "${VSS_ORIGIN%/}"/*) effective_url=${returned_url} ;;
    *)
      # A host-fallback configuration can coexist with the deployment-owned
      # public URL in the Agent response. Preserve only its validated VST path;
      # the selector already chose VSS_ORIGIN as the host-reachable authority.
      returned_path=$(python3 - "${returned_url}" "${VSS_ORIGIN}" <<'PY'
import ipaddress
import sys
from urllib.parse import urlsplit

parsed = urlsplit(sys.argv[1])
configured = urlsplit(sys.argv[2])
try:
    configured_ip = ipaddress.ip_address(configured.hostname or "")
except ValueError:
    raise SystemExit(1)
if (
    parsed.scheme not in {"http", "https"}
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.query
    or parsed.fragment
    or parsed.path != "/vst/api/v1/storage/file"
    or configured.scheme not in {"http", "https"}
    or configured_ip.is_global
):
    raise SystemExit(1)
print(parsed.path)
PY
      ) || return 1
      effective_url="${VSS_ORIGIN%/}${returned_path}"
      ;;
  esac

  identifier=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid)
  upload_timeout=$("${SOURCE_SETUP_BUDGET}" remaining 300) || return 1
  curl -sfS --connect-timeout 10 --max-time "${upload_timeout}" -X POST \
    "${effective_url}" \
    -H "nvstreamer-chunk-number: 1" \
    -H "nvstreamer-total-chunks: 1" \
    -H "nvstreamer-is-last-chunk: true" \
    -H "nvstreamer-identifier: ${identifier}" \
    -H "nvstreamer-file-name: ${upload_filename}" \
    -F "mediaFile=@${file_path};filename=${upload_filename}" \
    -F "filename=${upload_filename}" \
    -F 'metadata={"timestamp":"2025-01-01T00:00:00"}'
}

WAREHOUSE_SAMPLE_UPLOAD=$(stage_agent_upload \
  "${SAMPLE_DIR}/warehouse_sample.mp4" warehouse_sample.mp4) || exit 1
WAREHOUSE_LADDER_UPLOAD=$(stage_agent_upload \
  "${SAMPLE_DIR}/sample-warehouse-ladder.mp4" warehouse-ladder.mp4) || exit 1
```

Use the returned URL unchanged when it has the configured origin. The only
permitted authority translation is the branch above: the deployment returned a
different public authority, `VSS_ORIGIN` has a literal non-global IP host that
proves it is the selected host-reachable fallback, and the returned path
validated as the exact VST upload route. A differing authority with a global or
hostname-based configured origin fails closed. This is not permission to probe
the rejected public origin again, guess another port, or edit routing.

The final chunk registers the file sensor before Agent post-processing starts.
For one upload, boundedly prove that VST lists the exact name and returned UUID,
then call `/complete`. For a batch, **stage every handshake and file transfer
before completing any item**. File-source playback and downstream processing
can emit lifecycle events while `/complete` is blocked, so completing the first
fixture before transferring the second creates a race in which no single VST
listing ever contains the whole batch.

For the two search fixtures, save the two complete upload-response objects as
`WAREHOUSE_SAMPLE_UPLOAD` and `WAREHOUSE_LADDER_UPLOAD`, extract their UUIDs,
and require one bounded VST response to contain both exact name/UUID pairs
simultaneously. Do not infer this registration from upload responses, index
documents, logs, or two listings taken at different times:

```bash
WAREHOUSE_SAMPLE_SENSOR=$(printf '%s' "${WAREHOUSE_SAMPLE_UPLOAD}" |
  jq -er '.sensorId | select(type == "string" and length > 0)') || exit 1
WAREHOUSE_LADDER_SENSOR=$(printf '%s' "${WAREHOUSE_LADDER_UPLOAD}" |
  jq -er '.sensorId | select(type == "string" and length > 0)') || exit 1

while :; do
  VST_LIST_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15) || exit 1
  VST_SENSOR_LIST=$(curl -fsS --connect-timeout 5 --max-time "${VST_LIST_TIMEOUT}" \
    "${VST_URL%/}/vst/api/v1/sensor/list") || exit 1
  if printf '%s' "${VST_SENSOR_LIST}" | jq -e \
    --arg sample "${WAREHOUSE_SAMPLE_SENSOR}" \
    --arg ladder "${WAREHOUSE_LADDER_SENSOR}" '
      any(.[]; .name == "warehouse_sample" and .sensorId == $sample) and
      any(.[]; .name == "warehouse-ladder" and .sensorId == $ladder)' \
      >/dev/null; then
    break
  fi
  "${SOURCE_SETUP_BUDGET}" remaining 2 >/dev/null || exit 1
  sleep 2
done

complete_upload() {
  local sensor=$1 filename=$2 upload_response=$3 response_file=$4 timeout
  timeout=$("${SOURCE_SETUP_BUDGET}" remaining 900) || return 1
  printf '%s' "${upload_response}" |
    jq --arg filename "${filename}" '. + {filename: $filename}' |
    curl -sfS --connect-timeout 10 --max-time "${timeout}" -X POST \
      "${AGENT_URL}/api/v1/videos/${sensor}/complete" \
      -H "Content-Type: application/json" -d @- >"${response_file}"
}
COMPLETE_DIR=$(mktemp -d /tmp/vss-search-complete.XXXXXX) || exit 1
complete_upload "${WAREHOUSE_SAMPLE_SENSOR}" warehouse_sample.mp4 \
  "${WAREHOUSE_SAMPLE_UPLOAD}" "${COMPLETE_DIR}/sample.json" &
SAMPLE_COMPLETE_PID=$!
complete_upload "${WAREHOUSE_LADDER_SENSOR}" warehouse-ladder.mp4 \
  "${WAREHOUSE_LADDER_UPLOAD}" "${COMPLETE_DIR}/ladder.json" &
LADDER_COMPLETE_PID=$!
SAMPLE_COMPLETE_STATUS=0
LADDER_COMPLETE_STATUS=0
wait "${SAMPLE_COMPLETE_PID}" || SAMPLE_COMPLETE_STATUS=$?
wait "${LADDER_COMPLETE_PID}" || LADDER_COMPLETE_STATUS=$?
(( SAMPLE_COMPLETE_STATUS == 0 && LADDER_COMPLETE_STATUS == 0 )) || exit 1
jq -e --arg sensor "${WAREHOUSE_SAMPLE_SENSOR}" '
  .sensor_id == $sensor and
  (.chunks_processed | type == "number" and . > 0)' \
  "${COMPLETE_DIR}/sample.json" >/dev/null || exit 1
jq -e --arg sensor "${WAREHOUSE_LADDER_SENSOR}" '
  .sensor_id == $sensor and
  (.chunks_processed | type == "number" and . > 0)' \
  "${COMPLETE_DIR}/ladder.json" >/dev/null || exit 1
```

Start both completion calls before waiting for either one. Each request can
block on media processing, so sequential completion can leave the second staged
sensor idle for many minutes. Validate the two response files independently;
one successful response never substitutes for the other.

For a single file, apply the same order with one name/UUID predicate: transfer,
observe it in VST, then complete it. Do not call `/complete` before the required
VST registration evidence has been captured.

Never call the deprecated single-step
`PUT /api/v1/videos-for-search/{filename}`. Use
`UPLOAD_FILENAME` consistently in every request and multipart field; use that
same value for the upload request, VST metadata, and completion body.
Completion alone is not readiness. After completing all intended uploads, run
one bounded readiness wait (at most 20 minutes) until the search indexes contain
the required documents. Capture the simultaneous VST listing before
`/complete`, as described above, while the returned UUIDs remain the keys for
post-completion document checks:

- `EMBED_INDEX`, `sensor.id.keyword`, resolved VST sensor UUID;
- `BEHAVIOR_INDEX`, `sensor.id.keyword`, canonical source name;
- `RAW_INDEX`, `sensorId.keyword`, canonical source name.

Embed search requires the first tuple. Fusion requires all three. Agent and
RTVI-CV logs are bounded diagnostics only: captured live VST registration plus
the required embedding, behavior, and raw documents are the readiness contract.
Never keep an otherwise-ready setup waiting for an exact log message.

For the two search fixtures, preserve the upload UUIDs as
`WAREHOUSE_SAMPLE_SENSOR` and `WAREHOUSE_LADDER_SENSOR`, then use this single
bounded wait:

```bash
: "${WAREHOUSE_SAMPLE_SENSOR:?preserve warehouse_sample upload sensorId}"
: "${WAREHOUSE_LADDER_SENSOR:?preserve warehouse-ladder upload sensorId}"
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
while :; do
  CONFIGURE_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 30) || break
  if timeout "${CONFIGURE_TIMEOUT}" "${VSS[@]}" configure \
       --base-url "${VSS_ORIGIN}" >/dev/null && resolve_search_indexes; then
    break
  fi
  sleep 15
done
: "${EMBED_INDEX:?embedding index was not discovered before the deadline}"
: "${BEHAVIOR_INDEX:?behavior index was not discovered before the deadline}"
: "${RAW_INDEX:?raw index was not discovered before the deadline}"
while :; do
  SAMPLE_EMBED_COUNT=$(index_count "${EMBED_INDEX}" sensor.id.keyword \
    "${WAREHOUSE_SAMPLE_SENSOR}" 2>/dev/null || echo 0)
  LADDER_EMBED_COUNT=$(index_count "${EMBED_INDEX}" sensor.id.keyword \
    "${WAREHOUSE_LADDER_SENSOR}" 2>/dev/null || echo 0)
  LADDER_BEHAVIOR_COUNT=$(index_count "${BEHAVIOR_INDEX}" sensor.id.keyword \
    warehouse-ladder 2>/dev/null || echo 0)
  LADDER_RAW_COUNT=$(index_count "${RAW_INDEX}" sensorId.keyword \
    warehouse-ladder 2>/dev/null || echo 0)
  if (( SAMPLE_EMBED_COUNT > 0 && LADDER_EMBED_COUNT > 0 &&
        LADDER_BEHAVIOR_COUNT > 0 && LADDER_RAW_COUNT > 0 )); then
    break
  fi
  "${SOURCE_SETUP_BUDGET}" remaining 15 >/dev/null || break
  sleep 15
done
printf 'indexes=%s,%s,%s sensors=%s,%s counts=%s,%s,%s,%s\n' \
  "${EMBED_INDEX}" "${BEHAVIOR_INDEX}" "${RAW_INDEX}" \
  "${WAREHOUSE_SAMPLE_SENSOR}" "${WAREHOUSE_LADDER_SENSOR}" \
  "${SAMPLE_EMBED_COUNT}" "${LADDER_EMBED_COUNT}" \
  "${LADDER_BEHAVIOR_COUNT}" "${LADDER_RAW_COUNT}"
(( SAMPLE_EMBED_COUNT > 0 && LADDER_EMBED_COUNT > 0 &&
   LADDER_BEHAVIOR_COUNT > 0 && LADDER_RAW_COUNT > 0 )) || exit 1
```

A timeout or partial registration is an error, not
permission to query another source. Do not automatically delete, repair, or
reingest after `/complete`: that turns a bounded setup into an unbounded
recovery loop and destroys evidence of the original failure. Print the
resolved endpoints, index names, UUIDs, and counts, then collect only bounded
read-only diagnostics:

```bash
SOURCE_SETUP_BUDGET="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/source_setup_budget.sh"
if DIAGNOSTIC_TIMEOUT=$("${SOURCE_SETUP_BUDGET}" remaining 15); then
  curl -fsS --connect-timeout 5 --max-time "${DIAGNOSTIC_TIMEOUT}" \
    "${ES_URL%/}/_cat/indices/mdx-*?format=json" | jq . || true
  for CONTAINER in vss-rtvi-embed vss-rtvi-cv vss-behavior-analytics vss-video-analytics-api; do
    docker logs --since "${RTVI_CV_LOG_SINCE}" --tail 200 "${CONTAINER}" 2>&1 || true
  done
fi
```

Then stop with an error. Never post directly to RTVI-CV or Elasticsearch to
patch partial state. Use `index_count` with each exact tuple and accept
readiness only when each required count is greater than zero. A count from
another index or field does not satisfy readiness.

For Kubernetes, do not query Elasticsearch directly. After `/complete`
succeeds, poll `${VSS_VIOS_URL}/api/v1/sensor/list` for the canonical source,
then retry the requested Agent search only while ingestion is incomplete. A
valid Agent result proves the public workflow is operational; do not claim
direct index-level validation or create a port-forward.

## RTSP source

Register the exact RTSP URL through the selected search agent:

```bash
curl -sfS -X POST "${AGENT_URL}/api/v1/rtsp-streams/add" \
  -H "Content-Type: application/json" \
  -d '{
    "sensorUrl": "rtsp://<host>:<port>/<path>",
    "name": "<source-name>",
    "username": "",
    "password": "",
    "location": "",
    "tags": ""
  }' | jq .
```

The response is `{status, message, error}` and does not contain a sensor UUID;
the agent keys the stream by `name`. Do not log credentials. Poll boundedly
until the source is registered, then resolve its exact VST sensor identity
before search. A successful add only starts embedding generation; it does not
prove that searchable documents exist. Poll the selected embedding index for
the exact registered stream identity and require a count greater than zero
within five minutes.

## Delete source

Resolve exactly one source and save its UUID and canonical name before deletion.
Confirm the target unless deletion was already explicit:

```bash
: "${SAVED_SENSOR_ID:?save the exact file-source UUID before deletion}"
: "${SAVED_SOURCE_NAME:?save the canonical source name before deletion}"
: "${EMBED_INDEX:?resolve from vss configure show}"
: "${BEHAVIOR_INDEX:?resolve from vss configure show}"
: "${RAW_INDEX:?resolve from vss configure show}"

DELETE_READINESS_DEADLINE=$(($(date +%s) + 600))
delete_timeout() {
  local request_cap=$1 remaining
  remaining=$((DELETE_READINESS_DEADLINE - $(date +%s)))
  (( remaining > 0 )) || return 1
  (( request_cap < remaining )) && printf '%s\n' "${request_cap}" || printf '%s\n' "${remaining}"
}
DELETE_TIMEOUT=$(delete_timeout 60) || exit 1
DELETE_RESPONSE=$(curl -sfS --max-time "${DELETE_TIMEOUT}" -X DELETE \
  "${AGENT_URL%/}/api/v1/videos/${SAVED_SENSOR_ID}") || exit 1
DELETE_STATUS=$(printf '%s' "${DELETE_RESPONSE}" | \
  jq -er '.status | select(. == "success" or . == "partial" or . == "failure")') || exit 1

CLEANUP_VERIFIER="${VSS_REPO_ROOT}/skills/vss-search-archive/scripts/verify_source_cleanup.sh"
test -x "${CLEANUP_VERIFIER}" || exit 1
CLEANUP_TIMEOUT=$(delete_timeout 600) || exit 1
if CLEANUP_RESULT=$("${CLEANUP_VERIFIER}" \
    "${VSS_ORIGIN}" "${ES_URL}" \
    "${EMBED_INDEX}" "${BEHAVIOR_INDEX}" "${RAW_INDEX}" \
    "${SAVED_SENSOR_ID}" "${SAVED_SOURCE_NAME}" "${CLEANUP_TIMEOUT}" 2>&1); then
  CLEANUP_VERIFIED=true
else
  CLEANUP_VERIFIED=false
fi
printf '%s\n' "${CLEANUP_RESULT}" | jq .
[[ ${DELETE_STATUS} == success && ${CLEANUP_VERIFIED} == true ]] || exit 1
```

Always run the read-only bundled verifier after the one Agent DELETE so a
non-success response cannot suppress cleanup evidence. Still require response
`status` to be `success`; `partial` is not success. Reuse the same runtime
values and poll until VST no longer lists the source, the embedding
tuple for the saved UUID is zero, and behavior/raw tuples for the canonical name
are zero. The bundled verifier emits each exact index, field, value, and count;
report those values rather than reconstructing alternate queries. Pass
`VSS_ORIGIN` as the verifier's VST argument; do not append `/vst` or reuse a
sensor-list route. Invoke the verifier exactly once. If it exits nonzero, report
its error and stop without rerunning it or issuing substitute VST or
Elasticsearch queries. Never delete an ambiguous source or issue
independent backend cleanup. `AGENT_URL` is exactly the selected deployment
origin (`VSS_ORIGIN`), without an `/api` suffix; the code above appends the one
canonical route. Issue that DELETE exactly once. Do not probe alternate route
spellings and do not retry a `partial`, `failure`, or `success` response—a
second call loses the source-name context needed for exact behavior/raw cleanup
and cannot repair the first result. RTSP deletion uses the advertised
`DELETE /api/v1/rtsp-streams/delete/<name>` Agent route and the same bounded
absence checks; never substitute a direct backend mutation.

For storage API version details use `vss-manage-video-io-storage`; use schemas
advertised by the exact running deployment rather than guessing.
