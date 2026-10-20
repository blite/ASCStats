"""Airflow only orchestrates: every unit of work is a Django management command running in the
ascstats image as a Kubernetes pod. Deployment-specific values come from environment
variables read at parse time (no Variable lookups at the top level), so the same DAG files
run on local Docker Compose and on Cloud Composer.
"""

from __future__ import annotations

import os

import pendulum
from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.sdk import Asset
from kubernetes.client import models as k8s

NAMESPACE = os.environ.get("ASCSTATS_K8S_NAMESPACE", "ascstats")
IMAGE = os.environ.get("ASCSTATS_IMAGE", "ascstats:dev")
K8S_CONN_ID = os.environ.get("ASCSTATS_K8S_CONN_ID", "kubernetes_default")
# First data interval for catchup. Local Compose sets it near today to keep catchup small.
START_DATE = pendulum.parse(os.environ.get("ASCSTATS_START_DATE", "2026-09-01"), tz="UTC")
ASC_CONN_ID = "asc_default"
ASC_POOL = "asc_api"  # bounds concurrent App Store Connect calls across all DAGs

SALES_ASSET = Asset("ascstats://postgres/stats_salesrow", group="ascstats")
ANALYTICS_ASSET = Asset("ascstats://postgres/stats_analyticsrow", group="ascstats")
CONVERSION_ASSET = Asset("ascstats://postgres/stats_conversionmetric", group="ascstats")

DEFAULT_ARGS = {
    "owner": "ascstats",
    "retries": 2,
    "retry_exponential_backoff": True,
}

_ENV_FROM = [
    k8s.V1EnvFromSource(config_map_ref=k8s.V1ConfigMapEnvSource(name="ascstats-config")),
    # Real-data settings (ASC_SOURCE=asc, vendor number) live only in the cluster; later
    # sources win, so this overrides the ConfigMap's synthetic defaults.
    k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="asc-settings", optional=True)),
    k8s.V1EnvFromSource(secret_ref=k8s.V1SecretEnvSource(name="ascstats-app")),
]
_ASC_ENV = [
    k8s.V1EnvVar(
        name=name,
        value_from=k8s.V1EnvVarSource(
            secret_key_ref=k8s.V1SecretKeySelector(name="asc-api-key", key=name, optional=True)
        ),
    )
    for name in ("ASC_KEY_ID", "ASC_ISSUER_ID")
]
_ASC_KEY_VOLUME = k8s.V1Volume(
    name="asc-key",
    secret=k8s.V1SecretVolumeSource(
        secret_name="asc-api-key",
        optional=True,
        items=[k8s.V1KeyToPath(key="AuthKey.p8", path="AuthKey.p8")],
        default_mode=0o440,
    ),
)
_ASC_KEY_MOUNT = k8s.V1VolumeMount(
    name="asc-key", mount_path="/var/run/secrets/asc", read_only=True
)


def _pod_kwargs(task_id: str, talks_to_asc: bool, overrides: dict) -> dict:
    kwargs = {
        "task_id": task_id,
        "name": f"ascstats-{task_id.replace('_', '-')}",
        "namespace": NAMESPACE,
        "kubernetes_conn_id": K8S_CONN_ID,
        "image": IMAGE,
        "image_pull_policy": "IfNotPresent",
        "cmds": ["python", "manage.py"],
        "env_from": _ENV_FROM,
        "env_vars": _ASC_ENV,
        "volumes": [_ASC_KEY_VOLUME],
        "volume_mounts": [_ASC_KEY_MOUNT],
        "security_context": k8s.V1PodSecurityContext(
            run_as_non_root=True, run_as_user=10001, fs_group=10001
        ),
        "container_resources": k8s.V1ResourceRequirements(
            requests={"cpu": "50m", "memory": "192Mi"}, limits={"memory": "512Mi"}
        ),
        "get_logs": True,
        "on_finish_action": "delete_succeeded_pod",
    }
    if talks_to_asc:
        kwargs["pool"] = ASC_POOL
    return kwargs | overrides


def manage_py(task_id: str, arguments, *, talks_to_asc: bool = False, **overrides):
    """A KubernetesPodOperator running ``python manage.py <arguments>`` in the ascstats image.
    Tasks that call App Store Connect run in the ``asc_api`` pool."""
    return KubernetesPodOperator(
        arguments=arguments, **_pod_kwargs(task_id, talks_to_asc, overrides)
    )


def partial_manage_py(task_id: str, *, talks_to_asc: bool = False, **overrides):
    """``manage_py`` for dynamic task mapping: ``.expand(arguments=...)`` the result."""
    return KubernetesPodOperator.partial(**_pod_kwargs(task_id, talks_to_asc, overrides))
