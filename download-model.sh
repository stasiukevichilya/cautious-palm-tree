#!/usr/bin/env bash
# Download model files from Hugging Face into ./models
#
# Usage:
#   download-model.sh                      -> default model (DEFAULT_REPO / DEFAULT_FILE below)
#   download-model.sh <repo> <file>        -> e.g. unsloth/Qwen3.8-27B-GGUF Qwen3.8-27B-UD-Q4_K_M.gguf
#   download-model.sh <huggingface URL>    -> any huggingface.co link to the repo/file
#
# Behaviour:
#   - The resolved Hugging Face revision is cached per repo in models/.revisions/,
#     so an interrupted download can be resumed even if the repo is updated.
#   - A pinned revision in the URL (/resolve/<rev>/<file>) is used as-is.
#   - A complete local copy is detected and the transfer is skipped.
#   - Local SHA256 is recorded in models/SHA256SUMS (one line per file).
set -euo pipefail
cd -- "$(dirname -- "$0")"

HF_BASE="https://huggingface.co"
DEFAULT_REPO="unsloth/Qwen3.8-27B-GGUF"
DEFAULT_FILE="Qwen3.8-27B-UD-Q4_K_M.gguf"

usage() {
  cat <<'EOF'
Download a model file from Hugging Face into ./models

Usage:
  download-model.sh
  download-model.sh <repo> <file>
  download-model.sh <huggingface URL>

Arguments:
  <repo>   Hugging Face repository, e.g. unsloth/Qwen3.8-27B-GGUF
  <file>   file inside the repository, e.g. Qwen3.8-27B-UD-Q4_K_M.gguf
  <URL>    any huggingface.co link to the repo/file, e.g.
             https://huggingface.co/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF?show_file_info=Qwen3.8-27B-Uncensored-Q4_K_M.gguf
             https://huggingface.co/<repo>/resolve/<rev>/<file>
             https://huggingface.co/<repo>/tree/main/<file>

Examples:
  download-model.sh
  download-model.sh JonathanColetti/Qwen3.8-27B-Uncensored-GGUF Qwen3.8-27B-Uncensored-Q4_K_M.gguf
  download-model.sh "https://huggingface.co/JonathanColetti/Qwen3.8-27B-Uncensored-GGUF?show_file_info=Qwen3.8-27B-Uncensored-Q4_K_M.gguf"

Notes:
  - Downloads are resumable (curl -C -): interrupt and re-run to continue.
  - The Hugging Face revision is cached per repo in models/.revisions/ so a
    resume keeps downloading the same file even if the repo is updated.
  - Local SHA256 is recorded in models/SHA256SUMS (one line per file).
  - Set HF_TOKEN to download from gated/private repositories.
EOF
}

repo=""
file=""
pinned_revision=""

if [[ $# -eq 1 && ("$1" == "-h" || "$1" == "--help") ]]; then
  usage
  exit 0
elif [[ $# -eq 0 ]]; then
  repo="$DEFAULT_REPO"
  file="$DEFAULT_FILE"
elif [[ $# -ge 2 && "$1" != *"://"* ]]; then
  if [[ $# -gt 2 ]]; then
    echo "error: expected <repo> <file> or <URL>, got $# arguments" >&2
    exit 2
  fi
  repo="$1"
  file="$2"
elif [[ "$1" == *"://"* ]]; then
  if [[ $# -gt 1 ]]; then
    echo "error: pass the URL alone or use <repo> <file>" >&2
    exit 2
  fi
  url="$1"
  parsed="$(python3 - "$url" <<'PY'
import sys
import urllib.parse

raw = sys.argv[1]
u = urllib.parse.urlsplit(raw)
if u.netloc not in ("huggingface.co", "hf.co"):
    sys.stderr.write("only huggingface.co URLs are supported, got: %r\n" % u.netloc)
    sys.exit(2)

parts = [p for p in u.path.split("/") if p]
if len(parts) < 2:
    sys.stderr.write("cannot determine <owner>/<repo> from URL\n")
    sys.exit(2)
repo = parts[0] + "/" + parts[1]

file = ""
revision = ""
if len(parts) >= 4 and parts[2] == "resolve":
    revision = parts[3]
    file = "/".join(parts[4:])
elif len(parts) >= 5 and parts[2] == "tree":
    file = "/".join(parts[4:])
elif len(parts) == 3:
    file = parts[2]

query = urllib.parse.parse_qs(u.query)
if not file:
    file = (query.get("show_file_info") or query.get("file") or [""])[0]

if not file:
    sys.stderr.write("cannot determine file name from URL; use <repo> <file> instead\n")
    sys.exit(2)

sys.stdout.write(repo + "\t" + file + "\t" + revision)
PY
)" || exit 1
  IFS=$'\t' read -r repo file pinned_revision <<<"$parsed"
else
  usage >&2
  exit 2
fi

if [[ -z "$repo" || -z "$file" ]]; then
  echo "error: empty repository or file name" >&2
  exit 2
fi

auth_args=()
if [[ -n "${HF_TOKEN:-}" ]]; then
  auth_args=(-H "Authorization: Bearer $HF_TOKEN")
fi

# Resolve the revision: pinned in the URL, cached per repo, or current from the API.
if [[ -n "$pinned_revision" && "$pinned_revision" != "main" ]]; then
  revision="$pinned_revision"
else
  revision_file="models/.revisions/$(tr '/' '_' <<<"$repo").txt"
  if [[ -f "$revision_file" ]]; then
    revision="$(cat "$revision_file")"
  else
    mkdir -p "$(dirname "$revision_file")"
    revision="$(curl --fail --silent --show-error --location "${auth_args[@]}" \
      "$HF_BASE/api/models/$repo" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha"])')"
    printf '%s\n' "$revision" > "$revision_file"
  fi
fi

if ! [[ "$revision" =~ ^([0-9a-f]{40}|[A-Za-z0-9][A-Za-z0-9._/-]{0,63})$ ]]; then
  echo "error: invalid revision '$revision'" >&2
  exit 1
fi

destination="models/$file"
mkdir -p "$(dirname "$destination")"

# Skip the transfer if a complete copy already exists.
if [[ -f "$destination" ]]; then
  remote_size="$(curl --fail --silent --show-error --location --head "${auth_args[@]}" \
    "$HF_BASE/$repo/resolve/$revision/$file" \
    | tr -d '\r' | awk 'tolower($1)=="content-length:"{v=$2} END{print v}')"
  if [[ "$remote_size" =~ ^[0-9]+$ ]] && [[ "$remote_size" -eq "$(stat -c %s "$destination")" ]]; then
    echo "models/$file is already complete ($remote_size bytes); nothing to do."
    exit 0
  fi
fi

curl --fail --location --retry 5 --retry-delay 2 --retry-all-errors --continue-at - \
  "${auth_args[@]}" \
  "$HF_BASE/$repo/resolve/$revision/$file" -o "$destination"

# Record local SHA256: replace the line for this file, append if absent.
(
  cd models
  { grep -vF "  $file" SHA256SUMS 2>/dev/null || true; sha256sum "$file"; } > SHA256SUMS.tmp
  mv SHA256SUMS.tmp SHA256SUMS
)

echo "Downloaded models/$file from $repo (revision $revision)."
echo "Local SHA256 recorded in models/SHA256SUMS."
