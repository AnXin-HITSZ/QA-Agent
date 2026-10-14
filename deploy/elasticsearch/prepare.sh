#!/usr/bin/env bash
# Prepare host limits and private TLS material. Never sets a user password.
set -euo pipefail
cd "$(dirname "$0")/../.."
DEPLOY_DIR="$(pwd)/deploy/elasticsearch"
CERT_DIR="$DEPLOY_DIR/certs"
IMAGE="docker.elastic.co/elasticsearch/elasticsearch:9.5.5"
PYTHON="${QA_AGENT_PYTHON:-/root/miniconda3/envs/qa-agent/bin/python}"

if [[ -e "$CERT_DIR" ]]; then
  echo 'Certificate directory already exists; refusing to replace keys.' >&2
  exit 1
fi
if [[ -e /etc/sysctl.d/99-qa-elasticsearch.conf ]]; then
  echo 'Host sysctl file already exists; inspect it before preparing.' >&2
  exit 1
fi

docker pull "$IMAGE"
printf 'vm.max_map_count=1048576\n' > /etc/sysctl.d/99-qa-elasticsearch.conf
sysctl -w vm.max_map_count=1048576
mkdir -p "$CERT_DIR"
chown 1000:0 "$CERT_DIR"
chmod 750 "$CERT_DIR"

docker run --rm --memory 512m --user 1000:0 \
  -v "$CERT_DIR:/certs" "$IMAGE" \
  bin/elasticsearch-certutil ca --silent --pem --out /certs/ca.zip
"$PYTHON" -m zipfile -e "$CERT_DIR/ca.zip" "$CERT_DIR"
chown -R 1000:0 "$CERT_DIR"
chmod 750 "$CERT_DIR/ca"
chmod 600 "$CERT_DIR/ca/ca.key"

docker run --rm --memory 512m --user 1000:0 \
  -v "$CERT_DIR:/certs" -v "$DEPLOY_DIR/instances.yml:/instances.yml:ro" "$IMAGE" \
  bin/elasticsearch-certutil cert --silent --pem --in /instances.yml \
  --ca-cert /certs/ca/ca.crt --ca-key /certs/ca/ca.key --out /certs/es01.zip
"$PYTHON" -m zipfile -e "$CERT_DIR/es01.zip" "$CERT_DIR"
chown -R 1000:0 "$CERT_DIR/es01"
chmod 750 "$CERT_DIR/es01"
chmod 600 "$CERT_DIR/es01/es01.key"
chmod 644 "$CERT_DIR/es01/es01.crt" "$CERT_DIR/ca/ca.crt"
# Signing key and archives stay private to the host administrator.
chown root:root "$CERT_DIR/ca/ca.key" "$CERT_DIR/ca.zip" "$CERT_DIR/es01.zip"
chmod 600 "$CERT_DIR/ca/ca.key" "$CERT_DIR/ca.zip" "$CERT_DIR/es01.zip"
chown root:root "$CERT_DIR" "$CERT_DIR/ca"
chmod 750 "$CERT_DIR" "$CERT_DIR/ca"
echo 'TLS material prepared. No Elasticsearch user password was set.'
