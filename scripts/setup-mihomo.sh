#!/usr/bin/env bash
set -euo pipefail

work_dir="${RUNNER_TEMP:-/tmp}/mihomo"
config_file="$work_dir/config.yaml"
binary_file="$work_dir/mihomo"
proxy_url="http://127.0.0.1:7890"

mkdir -p "$work_dir"

download_subscription() {
  curl --fail --silent --show-error --location \
    --retry 3 --retry-delay 1 \
    -A "ClashforWindows/0.20.39" \
    -H "Accept: text/yaml,application/yaml,text/plain,*/*" \
    "$1" -o "$config_file" 2>/dev/null
}

write_output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    printf '%s\n' "$1" >> "$GITHUB_OUTPUT"
  fi
}

check_no_proxy() {
  [[ -z "${PROXY_TARGET_URL:-}" ]] && return
  local target_host
  target_host="$(python -c 'from urllib.parse import urlparse; import os; print(urlparse(os.environ["PROXY_TARGET_URL"]).hostname or "")')"
  [[ -z "$target_host" ]] && return
  local entry
  IFS=',' read -ra entries <<< "${NO_PROXY:-}"
  for entry in "${entries[@]}"; do
    entry="${entry//[[:space:]]/}"
    entry="${entry#.}"
    entry="${entry%%:*}"
    if [[ "$entry" == "*" || "$entry" == "$target_host" || "$target_host" == *".$entry" ]]; then
      echo "NO_PROXY would bypass the configured target host."
      exit 1
    fi
  done
}

provider_is_ready() {
  local status providers_file
  providers_file="$work_dir/providers.json"
  status="$(curl --silent --show-error --noproxy '*' --connect-timeout 3 --max-time 8 \
    --output "$providers_file" --write-out '%{http_code}' \
    http://127.0.0.1:9091/providers/proxies 2>/dev/null || true)"
  [[ "$status" == "200" ]] || return 1

  python - "$providers_file" <<'PY' >/dev/null
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

providers = payload.get("providers", payload)
if not isinstance(providers, dict):
    raise SystemExit(1)

loaded = sum(
    len(provider.get("proxies", []))
    for provider in providers.values()
    if isinstance(provider, dict) and isinstance(provider.get("proxies"), list)
)
raise SystemExit(0 if loaded else 1)
PY
}

if [[ -n "${CLASH_CONFIG_YAML:-}" ]]; then
  echo "Using Mihomo configuration from GitHub Secret."
  printf '%s\n' "$CLASH_CONFIG_YAML" > "$config_file"
elif [[ -n "${CLASH_SUBSCRIPTION_URL:-}" ]]; then
  echo "Downloading Mihomo subscription."
  if ! download_subscription "$CLASH_SUBSCRIPTION_URL"; then
    echo "Primary subscription endpoint failed; trying its embedded provider URL."
    embedded_url="$(SUBSCRIPTION_URL="$CLASH_SUBSCRIPTION_URL" python -c '
from os import environ
from urllib.parse import parse_qs, urlparse

print(parse_qs(urlparse(environ["SUBSCRIPTION_URL"]).query).get("url", [""])[0])
')"
    if [[ -z "$embedded_url" ]] || ! download_subscription "$embedded_url"; then
      echo "Unable to download Mihomo subscription."
      exit 1
    fi
  fi
else
  echo "No Mihomo configuration secret is configured."
  exit 1
fi

if ! grep -Eq '^(proxies|proxy-providers|proxy-groups|mixed-port|port|socks-port):' "$config_file"; then
  echo "Mihomo configuration is not a Clash-compatible YAML file."
  exit 1
fi

uses_proxy_providers=false
if grep -q '^proxy-providers:' "$config_file"; then
  uses_proxy_providers=true
fi

tmp_config="$work_dir/config.normalized.yaml"
awk '!/^(mixed-port|allow-lan|bind-address|mode|log-level|external-controller|secret):/' "$config_file" > "$tmp_config"
cat >> "$tmp_config" <<'YAML'

mixed-port: 7890
allow-lan: false
bind-address: 127.0.0.1
mode: global
log-level: warning
external-controller: 127.0.0.1:9091
secret: ""
YAML
mv "$tmp_config" "$config_file"

echo "Downloading Mihomo core."
release_json="$work_dir/release.json"
curl --fail --silent --show-error --location --retry 3 --retry-delay 1 \
  https://api.github.com/repos/MetaCubeX/mihomo/releases/latest -o "$release_json"
asset_url="$(python - "$release_json" <<'PY'
import json
import re
import sys

assets = json.load(open(sys.argv[1], encoding="utf-8")).get("assets", [])
patterns = (
    r"^mihomo-linux-amd64-v1.*\.gz$",
    r"^mihomo-linux-amd64-compatible-.*\.gz$",
    r"^mihomo-linux-amd64.*\.gz$",
)
for pattern in patterns:
    asset = next((item for item in assets if re.search(pattern, item.get("name", ""))), None)
    if asset:
        print(asset["browser_download_url"])
        break
else:
    raise SystemExit("No compatible Mihomo Linux amd64 asset found.")
PY
)"
curl --fail --silent --show-error --location --retry 3 --retry-delay 1 "$asset_url" -o "$work_dir/mihomo.gz"
gzip -dc "$work_dir/mihomo.gz" > "$binary_file"
chmod +x "$binary_file"

check_no_proxy
echo "Starting Mihomo local proxy."
"$binary_file" -f "$config_file" -d "$work_dir" > "$work_dir/mihomo.log" 2>&1 &
pid="$!"

for delay in 1 2 4; do
  sleep "$delay"
  if kill -0 "$pid" 2>/dev/null && ss -ltn 'sport = :7890' | grep -q ':7890'; then
    if [[ "$uses_proxy_providers" == "true" ]]; then
      if provider_is_ready; then
        echo "Proxy provider load check succeeded."
      else
        echo "Proxy provider is not ready yet."
        continue
      fi
    fi

    metrics_file="$work_dir/egress.txt"
    if curl --fail --silent --show-error --noproxy '' --proxy "$proxy_url" --connect-timeout 8 --max-time 20 \
      --write-out '%{http_code} %{time_total}' --output "$metrics_file" https://ipinfo.io/country > "$work_dir/egress.metrics" 2>/dev/null; then
      country="$(tr -d '[:space:]' < "$metrics_file")"
      read -r status elapsed < "$work_dir/egress.metrics"
      if [[ "$status" == "200" && "$country" == "CN" ]]; then
        echo "Proxy egress check succeeded: HTTP $status in ${elapsed}s; region=CN."
        if [[ -n "${PROXY_TARGET_URL:-}" ]]; then
          if curl --silent --show-error --noproxy '' --proxy "$proxy_url" --connect-timeout 8 --max-time 20 \
            --write-out '%{http_code} %{time_total}' --output /dev/null "$PROXY_TARGET_URL" > "$work_dir/target.metrics" 2>/dev/null; then
            read -r target_status target_elapsed < "$work_dir/target.metrics"
            if [[ "$target_status" =~ ^[23][0-9][0-9]$ ]]; then
              echo "Target reachability check succeeded: HTTP $target_status in ${target_elapsed}s."
              write_output "proxy=$proxy_url"
              exit 0
            fi
            echo "Target reachability check returned HTTP $target_status in ${target_elapsed}s."
            exit 1
          fi
          echo "Target reachability check failed after ${delay}s backoff."
          continue
        else
          write_output "proxy=$proxy_url"
          exit 0
        fi
      fi
      echo "Proxy egress check did not confirm a China mainland exit."
    else
      echo "Proxy egress check failed after ${delay}s backoff."
    fi
  fi
done

echo "Mihomo proxy was not usable after 3 retries."
exit 1
