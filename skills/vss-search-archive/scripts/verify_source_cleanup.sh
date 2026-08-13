#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

if (( $# != 8 )); then
  echo "usage: $0 VST_URL ES_URL EMBED_INDEX BEHAVIOR_INDEX RAW_INDEX SENSOR_ID SOURCE_NAME TIMEOUT_SECONDS" >&2
  exit 2
fi

VST_URL=${1%/}
# Accept either the deployment origin or a VST route base. The verifier owns
# the one canonical `/vst/api/...` suffix and must never construct `/vst/vst`.
VST_URL=${VST_URL%/vst}
ES_URL=${2%/}
EMBED_INDEX=$3
BEHAVIOR_INDEX=$4
RAW_INDEX=$5
SENSOR_ID=$6
SOURCE_NAME=$7
TIMEOUT_SECONDS=$8

[[ ${TIMEOUT_SECONDS} =~ ^[1-9][0-9]*$ ]] || {
  echo "TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
}

deadline=$(($(date +%s) + TIMEOUT_SECONDS))

request_timeout() {
  local remaining
  remaining=$((deadline - $(date +%s)))
  (( remaining > 0 )) || return 1
  (( remaining < 15 )) && printf '%s\n' "${remaining}" || printf '15\n'
}

index_count() {
  local index=$1 field=$2 value=$3 timeout query
  timeout=$(request_timeout) || return 1
  query=$(jq -cn --arg field "${field}" --arg value "${value}" \
    '{query:{term:{($field):$value}}}')
  curl -fsS --connect-timeout 5 --max-time "${timeout}" \
    -H 'Content-Type: application/json' \
    "${ES_URL}/${index}/_count" -d "${query}" | jq -er '.count | numbers'
}

vst_present=true
embed_count=-1
behavior_count=-1
raw_count=-1

while (( $(date +%s) < deadline )); do
  timeout=$(request_timeout) || break
  sensors=$(curl -fsS --connect-timeout 5 --max-time "${timeout}" \
    "${VST_URL}/vst/api/v1/sensor/list") || exit 1
  vst_present=$(printf '%s' "${sensors}" | jq -r \
    --arg id "${SENSOR_ID}" --arg name "${SOURCE_NAME}" \
    'any(.[]; .sensorId == $id or .name == $name)') || exit 1
  case ${vst_present} in true|false) ;; *) exit 1 ;; esac

  embed_count=$(index_count "${EMBED_INDEX}" sensor.id.keyword "${SENSOR_ID}") || exit 1
  behavior_count=$(index_count "${BEHAVIOR_INDEX}" sensor.id.keyword "${SOURCE_NAME}") || exit 1
  raw_count=$(index_count "${RAW_INDEX}" sensorId.keyword "${SOURCE_NAME}") || exit 1

  if [[ ${vst_present} == false ]] &&
     (( embed_count == 0 && behavior_count == 0 && raw_count == 0 )); then
    jq -cn \
      --argjson vst_present "${vst_present}" \
      --arg embed_index "${EMBED_INDEX}" --arg embed_field sensor.id.keyword \
      --arg embed_value "${SENSOR_ID}" --argjson embed_count "${embed_count}" \
      --arg behavior_index "${BEHAVIOR_INDEX}" --arg behavior_field sensor.id.keyword \
      --arg behavior_value "${SOURCE_NAME}" --argjson behavior_count "${behavior_count}" \
      --arg raw_index "${RAW_INDEX}" --arg raw_field sensorId.keyword \
      --arg raw_value "${SOURCE_NAME}" --argjson raw_count "${raw_count}" \
      '{vst_present:$vst_present,
        embedding:{index:$embed_index,field:$embed_field,value:$embed_value,count:$embed_count},
        behavior:{index:$behavior_index,field:$behavior_field,value:$behavior_value,count:$behavior_count},
        raw:{index:$raw_index,field:$raw_field,value:$raw_value,count:$raw_count}}'
    exit 0
  fi
  sleep 5
done

jq -cn \
  --argjson vst_present "${vst_present}" \
  --arg embed_index "${EMBED_INDEX}" --arg embed_field sensor.id.keyword \
  --arg embed_value "${SENSOR_ID}" --argjson embed_count "${embed_count}" \
  --arg behavior_index "${BEHAVIOR_INDEX}" --arg behavior_field sensor.id.keyword \
  --arg behavior_value "${SOURCE_NAME}" --argjson behavior_count "${behavior_count}" \
  --arg raw_index "${RAW_INDEX}" --arg raw_field sensorId.keyword \
  --arg raw_value "${SOURCE_NAME}" --argjson raw_count "${raw_count}" \
  '{error:"timed out waiting for source cleanup",vst_present:$vst_present,
    embedding:{index:$embed_index,field:$embed_field,value:$embed_value,count:$embed_count},
    behavior:{index:$behavior_index,field:$behavior_field,value:$behavior_value,count:$behavior_count},
    raw:{index:$raw_index,field:$raw_field,value:$raw_value,count:$raw_count}}' >&2
exit 1
