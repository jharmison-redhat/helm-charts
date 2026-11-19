"""Shared, non-test material for the openshift-ai chart test modules:
the chart fixture with hook-values helpers and the shared fake-oc harness
(``FAKE_OC`` plus ``HookScriptTests``) for the secret-reading
bootstrap/credential scripts (maas-db, EvalHub credential). Not named
``test_*`` so unittest discovery skips it; the sibling test_oa_*.py
modules import it top-level. The single-consumer fixtures (replicas,
operand) and their module-specific fake oc programs live with the test
modules that are their only consumers; this module keeps only the
harness both secret-reading smoke sets drive."""

import os
import shutil
import subprocess
import tempfile

import support
from support import docs

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "openshift-ai"))


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "oa-test-"
    values_prefix = "oa-values-"

    def render(self, values=None, expect_failure=False,
               namespace="test-release-ns"):
        """Renders the chart with the stub dependency; returns the docs, or
        the failed result when the render is expected to fail."""
        self.stub_dependency()
        result = self.helm_template(values, namespace)
        if expect_failure:
            self.assertNotEqual(result.returncode, 0)
            return result
        self.assertEqual(result.returncode, 0, result.stderr)
        return list(docs(result.stdout))

    @staticmethod
    def docs_named(by_kind, kind, fragment):
        """The docs of kind whose metadata.name carries the fragment — the
        chart renders several independent hook families, so kind-wide
        assertions must be scoped per family."""
        return [d for d in by_kind.get(kind, []) if fragment in d["metadata"]["name"]]

    def hook_values(self, **overrides):
        values = self.managed_maas()
        cfg = {"enabled": True, "namespace": "models-as-a-service",
               "waitTimeoutSeconds": 240, "pollIntervalSeconds": 5,
               "spec": {"telemetry": {"metrics": {"captureUser": False}}}}
        cfg.update(overrides)
        values["maasTenantConfig"] = cfg
        return values

    @staticmethod
    def hook_cm(by_kind):
        """The hook's script ConfigMap, identified by its patch.json key."""
        return next(d for d in by_kind["ConfigMap"]
                    if (d.get("data") or {}).get("patch.json") is not None)

    @staticmethod
    def managed_maas(mgmt="Managed"):
        return {"dataScienceCluster": {"create": True, "components": {
            "aigateway": {"managementState": "Managed",
                          "modelsAsAService": {"managementState": mgmt}}}}}

    def evalhub_values(self, evalhub_create=True, tenant_create=True,
                       **tenant_overrides):
        tenant = {"create": tenant_create, "namespace": "evalhub-tenant",
                  "waitTimeoutSeconds": 240}
        tenant.update(tenant_overrides)
        return {"evalhub": {"create": evalhub_create, "tenant": tenant}}


FAKE_OC = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" && "$2" == "secret" ]]; then
  if [[ "$*" == *metadata.name* ]]; then
    echo "$OC_SECRET"
  else
    key=""
    for k in $OC_KEYS; do
      if [[ "$*" == *data.${k}* ]]; then key="$k"; fi
    done
    if [ -n "$key" ]; then
      payload="$(cat "$OC_READ/$key")"
      printf '%s' "$(printf '%s' "$payload" | base64)"
    fi
  fi
  exit 0
fi
if [[ "$1" == "set" && "$2" == "data" ]]; then
  for a in "$@"; do
    case "$a" in
      --from-file=*)
        arg="${a#--from-file=}"
        key="${arg%%=*}"
        src="${arg#*=}"
        cat "$src" > "$OC_TARGET/$key" ;;
    esac
  done
  exit 0
fi
if [[ "$1" == "create" ]]; then
  src=""
  for a in "$@"; do
    case "$a" in --from-file=*) arg="${a#--from-file=}"; src="${arg#*=}" ;; esac
  done
  # The dry-run manifest goes to stdout (feeding the pipe); echo the decoded
  # --from-file payload as stringData
  echo 'apiVersion: v1'
  echo 'kind: Secret'
  echo 'stringData:'
  echo "  PAYLOAD: $(cat "$src")"
  exit 0
fi
if [[ "$1" == "apply" ]]; then
  cat > "$OC_TARGET/apply.yaml"
  exit 0
fi
exit 0
"""


class HookScriptTests(ChartFixture):
    """Shared fake-oc harness for the secret-reading bootstrap/credential
    scripts (maas-db, EvalHub credential): temp workdir, a happy-path fake
    oc (per-key payloads via OC_READ files, mutation capture), argv log,
    and a private TMPDIR. The smoke set runs only the success path per
    script."""

    secret_name = ""        # source secret name the fake oc reports
    secret_keys = ()        # jsonpath keys the scripts read from the secret

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="oa-hookscript-")
        self.addCleanup(shutil.rmtree, self.work)
        self.target = os.path.join(self.work, "target")
        os.makedirs(self.target)
        self.read = os.path.join(self.work, "read")
        os.makedirs(self.read)
        self.tmpdir = os.path.join(self.work, "pod-tmp")
        os.makedirs(self.tmpdir)

    def set_payload(self, key, content):
        """Sets the fake-oc payload for a secret key."""
        with open(os.path.join(self.read, key), "w") as f:
            f.write(content)

    def execute(self, argv, wait_timeout, env_extra):
        with open(os.path.join(self.work, "oc"), "w") as f:
            f.write(FAKE_OC)
        os.chmod(os.path.join(self.work, "oc"), 0o755)
        env = dict(
            os.environ,
            OC_SECRET=self.secret_name,
            OC_KEYS=" ".join(self.secret_keys),
            OC_LOG=os.path.join(self.work, "commands.log"),
            OC_READ=self.read,
            OC_TARGET=self.target,
            PATH=f"{self.work}:{os.environ['PATH']}",
            TMPDIR=self.tmpdir,
        )
        env.update(env_extra or {})
        return subprocess.run(argv, capture_output=True, text=True,
                              env=env, timeout=60)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def mutation_lines(self):
        return [l for l in self.log().splitlines()
                if l.startswith(("set data", "create ", "apply "))]

    def assert_absent(self, result, *secrets):
        combined = result.stdout + result.stderr + self.log()
        for secret in secrets:
            self.assertNotIn(secret, combined)

    def stage_rendered_script(self, values, configmap, script_name):
        """Stages the named script from its rendered script ConfigMap,
        executable, and returns its path."""
        data = self.pick(self.by_kind(self.render(values)), "ConfigMap",
                         configmap)["data"]
        script = os.path.join(self.work, script_name)
        with open(script, "w") as f:
            f.write(data[script_name])
        os.chmod(script, 0o755)
        return script
