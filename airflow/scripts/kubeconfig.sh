#!/usr/bin/env bash
# Write a kubeconfig for the Airflow containers that targets Docker Desktop Kubernetes.
# kubernetes.docker.internal resolves to the cluster both on the host and inside containers.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .kube
kubectl config view --raw --minify --flatten --context docker-desktop > .kube/config
chmod 644 .kube/config  # containers run as uid 50000; the directory is gitignored
echo "wrote $(pwd)/.kube/config for context docker-desktop"
