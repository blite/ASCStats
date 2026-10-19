#!/usr/bin/env bash
# Write a kubeconfig for the Airflow containers that targets Docker Desktop Kubernetes.
# The host's kubeconfig says 127.0.0.1, which inside a container is the container itself;
# kubernetes.docker.internal resolves to the cluster from containers and is in the API cert.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .kube
kubectl config view --raw --minify --flatten --context docker-desktop \
  | sed -E 's#server: https://(127\.0\.0\.1|localhost):6443#server: https://kubernetes.docker.internal:6443#' \
  > .kube/config
chmod 644 .kube/config  # containers run as uid 50000; the directory is gitignored
echo "wrote $(pwd)/.kube/config for context docker-desktop"
