"""Focused tests for the cert-manager chart. The module covers the
render-time issuer validation surface (managed certificates must name a
rendered ClusterIssuer, BYO CA keypairs must be complete and only set while
the CA family is enabled, and every ACME entry must carry a server, an
email, and only well-formed solvers), the ACME ClusterIssuer render shapes
for the Let's Encrypt and ZeroSSL examples, the trust gate matrix over
`ca.enabled` × `ca.trust.enabled` × self-provisioned/BYO, the stable-CA
render shape (explicit duration and rotationPolicy), the resourceName-scoped
trust RBAC, the serving-certificate hook surface (the platform
IngressController/default and APIServer/cluster objects are never owned —
hook Jobs write only this chart's reference with a resourceVersion-
conditional read-modify-write and stop on foreign references), and script
execution for every hook against a fake ``oc`` on both JSON tool paths.

Render tests run the chart through ``helm template`` (cert-manager has no
chart dependencies, so no stubs or registry access are needed). Script
tests run the rendered hook scripts against a fake ``oc`` on a restricted
PATH built entirely from symlinks, so neither a real ``oc`` nor the test
host's ambient tools can leak into the run.
"""

import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

CHART = os.path.abspath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "charts", "cert-manager"))

# The corrected Let's Encrypt example from values.yaml: the template
# generates the account key Secret (acme-letsencrypt-privatekey) itself, so
# the example carries neither disableAccountKeyGeneration nor a
# privateKeySecretRef.
LE_ACME = {
    "server": "https://acme-v02.api.letsencrypt.org/directory",
    "email": "ops@example.com",
    "solvers": [{
        "type": "dns",
        "dnsConfig": {"route53": {
            "accessKeyID": "AKIAEXAMPLE",
            "hostedZoneID": "Z123EXAMPLE",
            "region": "us-east-2",
            "secretAccessKeySecretRef": {
                "name": "acme-letsencrypt-route53-creds",
                "key": "secret-access-key",
            },
        }},
        "dnsZones": ["example.com"],
    }],
    "secrets": [{
        "name": "route53-creds",
        "stringData": {"secret-access-key": "route53-secret"},
    }],
}

ZEROSSL_ACME = {
    "server": "https://acme.zerossl.com/v2/DV90",
    "email": "ops@example.com",
    "externalAccountBinding": {
        "keyID": "eab-key-id",
        "keySecretRef": {"name": "acme-zerossl-eabsecret", "key": "secret"},
    },
    "solvers": [{
        "type": "dns",
        "dnsConfig": {"route53": {
            "accessKeyID": "AKIAEXAMPLE",
            "hostedZoneID": "Z123EXAMPLE",
            "region": "us-east-2",
            "secretAccessKeySecretRef": {
                "name": "acme-zerossl-route53-creds",
                "key": "secret-access-key",
            },
        }},
        "dnsZones": ["example.com"],
    }],
    "secrets": [
        {"name": "route53-creds",
         "stringData": {"secret-access-key": "route53-secret"}},
        {"name": "eabsecret", "stringData": {"secret": "eab-hmac-key"}},
    ],
}

ROUTER_DEFAULT_CERT = "router-cert"
API_SECRET_NAME = "openshift-api"

# Fake oc for the hook scripts: serves a canned object for get and records
# every --patch= payload, with per-scenario API behavior. Each get bumps the
# served metadata.resourceVersion (first get serves the canned value, each
# later get serves it incremented), so a retry can only patch successfully
# with a version from a fresh read. Every invocation is logged so tests can
# assert the exact oc call surface.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
env | LC_ALL=C sort > "$OC_ENV"
if [[ "$1" == "get" ]]; then
  case "$OC_SCENARIO" in
    absent-object)
      echo 'Error from server (NotFound): "cluster" not found' >&2
      exit 1 ;;
    get-error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
    *)
      rv_file="$OC_GET_FILE.rv"
      if [[ -f "$rv_file" ]]; then
        next_rv=$(($(cat "$rv_file") + 1))
      else
        next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | grep -oE '[0-9]+')
      fi
      echo "$next_rv" > "$rv_file"
      sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$OC_GET_FILE" ;;
  esac
elif [[ "$1" == "patch" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  attempts=$(cat "$OC_ATTEMPTS" 2>/dev/null || true)
  attempts=$(( ${attempts:-0} + 1 ))
  echo "$attempts" > "$OC_ATTEMPTS"
  case "$OC_SCENARIO" in
    conflict-then-success)
      if [ "$attempts" -ge 2 ]; then
        exit 0
      fi
      echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
      exit 1 ;;
    conflict-exhausted)
      echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
      exit 1 ;;
    patch-error)
      echo 'Error from server (Forbidden): is forbidden' >&2
      exit 1 ;;
    *)
      exit 0 ;;
  esac
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""

# Fake oc for the trust hook scripts: serves a canned object per resource
# (Secret, Proxy, bundle ConfigMap), logs patch payloads tagged with the
# patched resource, and logs create behavior with per-scenario outcomes.
# GETs bump the served metadata.resourceVersion per resource (first GET
# serves the canned value, each later GET serves it incremented), so a
# retry can only patch successfully with a version from a fresh read.
TRUST_FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
env | LC_ALL=C sort > "$OC_ENV"
case "$1" in
  get)
    case "$*" in
      *"cert-manager-ca"*)
        f="$OC_SECRET_FILE"; s="${OC_SECRET_SCENARIO:-ok}" ;;
      *"proxies.config.openshift.io/cluster"*)
        f="$OC_PROXY_FILE"; s="${OC_PROXY_SCENARIO:-ok}" ;;
      *"user-ca-bundle"*)
        f="$OC_BUNDLE_FILE"; s="${OC_BUNDLE_SCENARIO:-ok}"
        if [[ "$s" == "not-found" && -f "$OC_CREATE_LOG.done" ]]; then
          s="ok"
        fi
        if [[ "$s" == "disappears" ]] &&
           [[ -s "$OC_ATTEMPTS.configmap-user-ca-bundle" ]]; then
          # The bundle vanishes after this run has successfully patched it
          # once: the initial read and the content write see it, the
          # ownership-recording write does not
          s="not-found"
        fi ;;
      *)
        echo "unexpected oc get: $*" >&2
        exit 64 ;;
    esac
    case "$s" in
      ok) ;;
      disappears) ;;
      not-found)
        echo 'Error from server (NotFound): "object" not found' >&2
        exit 1 ;;
      error)
        echo 'Error from server (Forbidden): is forbidden' >&2
        exit 1 ;;
      *)
        echo "unexpected get scenario: $s" >&2
        exit 64 ;;
    esac
    rv_file="$f.rv"
    if [[ -f "$rv_file" ]]; then
      next_rv=$(($(cat "$rv_file") + 1))
    else
      next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$f" | head -1 | grep -oE '[0-9]+')
    fi
    echo "$next_rv" > "$rv_file"
    sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$f" ;;
  patch)
    # The patched resource is the first non-flag argument that is not the
    # value of -n (conditional_write passes the namespace flag first for
    # namespaced resources)
    res=""
    prev=""
    for arg in "${@:2}"; do
      if [[ "$arg" == -* ]]; then
        prev="$arg"
        continue
      fi
      if [[ "$prev" == "-n" ]]; then
        prev="$arg"
        continue
      fi
      res="$arg"
      break
    done
    for arg in "$@"; do
      if [[ "$arg" == --patch=* ]]; then
        printf '%s\\t%s\\n' "$res" "${arg#--patch=}" >> "$OC_PATCH_LOG"
      fi
    done
    attempts_file="$OC_ATTEMPTS.$(echo "$res" | tr '/.' '--')"
    attempts=$(cat "$attempts_file" 2>/dev/null || true)
    attempts=$(( ${attempts:-0} + 1 ))
    echo "$attempts" > "$attempts_file"
    # A per-resource override (named after the slug of the patched
    # resource) wins over the global scenario, so a test can fail exactly
    # one object's writes; the slug translates /, ., and - so any
    # resource name yields a legal variable name
    scenario="${OC_PATCH_SCENARIO:-ok}"
    override_var="OC_PATCH_SCENARIO_$(echo "$res" | tr '/.-' '___')"
    if [[ "$override_var" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      override="${!override_var:-}"
      if [ -n "$override" ]; then
        scenario="$override"
      fi
    fi
    case "$scenario" in
      conflict-then-success)
        if [ "$attempts" -ge 2 ]; then
          exit 0
        fi
        echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
        exit 1 ;;
      conflict-exhausted)
        echo 'Error from server (Conflict): Operation cannot be fulfilled' >&2
        exit 1 ;;
      error)
        echo 'Error from server (Forbidden): is forbidden' >&2
        exit 1 ;;
      error-on-second)
        # The first patch of a resource succeeds; every later patch of that
        # same resource fails persistently with a non-NotFound error
        if [ "$attempts" -ge 2 ]; then
          echo 'Error from server (Forbidden): is forbidden' >&2
          exit 1
        fi
        exit 0 ;;
      *)
        exit 0 ;;
    esac ;;
  create)
    cat > "$OC_CREATE_LOG"
    case "${OC_CREATE_SCENARIO:-ok}" in
      ok)
        # The created object now exists: later gets of the bundle must not
        # keep serving a not-found scenario (no touch — the restricted
        # PATH carries no coreutils beyond the symlinked tools)
        : > "$OC_CREATE_LOG.done"
        exit 0 ;;
      already-exists)
        echo 'Error from server (AlreadyExists): configmaps "user-ca-bundle" already exists' >&2
        exit 1 ;;
      error)
        echo 'Error from server (Forbidden): is forbidden' >&2
        exit 1 ;;
    esac ;;
  delete)
    case "${OC_DELETE_SCENARIO:-ok}" in
      ok) exit 0 ;;
      not-found)
        echo 'Error from server (NotFound): configmaps "user-ca-bundle" not found' >&2
        exit 1 ;;
      error)
        echo 'Error from server (Forbidden): is forbidden' >&2
        exit 1 ;;
    esac ;;
  *)
    echo "unexpected oc invocation: $*" >&2
    exit 64 ;;
esac
"""

# Canonical PEM blocks used as stand-in certificates; the hooks classify by
# byte-equality on the block text, so the inner content only needs to be
# distinct between blocks.
CA_CERT = "-----BEGIN CERTIFICATE-----\nQ1VSUkVOVA==\n-----END CERTIFICATE-----\n"
OLD_CA_CERT = "-----BEGIN CERTIFICATE-----\nT0xE\n-----END CERTIFICATE-----\n"
LEGACY_CA_CERT = "-----BEGIN CERTIFICATE-----\nTEVHQUNZ\n-----END CERTIFICATE-----\n"
FOREIGN_CA_CERT = "-----BEGIN CERTIFICATE-----\nRk9SRUlHTg==\n-----END CERTIFICATE-----\n"
# A CRLF-encoded foreign block: recognized by the hooks' tolerant PEM scan
# and preserved verbatim (its line endings are never rewritten)
FOREIGN_CA_CRLF = FOREIGN_CA_CERT.replace("\n", "\r\n")
BUNDLE_NAME = "user-ca-bundle"


def b64(text):
    return base64.b64encode(text.encode()).decode()


def history_annotation(*payloads):
    """The trust-ca-history annotation value for the given PEM blocks."""
    return json.dumps([b64(p) for p in payloads])


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d is not None]


class ChartFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="cm-test-")
        shutil.copytree(CHART, self.dir, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("charts", "Chart.lock"))
        self.addCleanup(shutil.rmtree, self.dir)

    def values_file(self, values):
        fd, path = tempfile.mkstemp(prefix="cm-values-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(values, f)
        self.addCleanup(os.unlink, path)
        return path

    def render_raw(self, values=None):
        cmd = ["helm", "template", "test-release", self.dir,
               "--namespace", "test-release-ns"]
        if values is not None:
            cmd += ["--values", self.values_file(values)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result

    def render(self, values=None):
        result = self.render_raw(values)
        self.assertEqual(result.returncode, 0, result.stderr)
        return [d for d in docs(result.stdout) if d is not None]

    def render_failure(self, values):
        """Renders values expected to trip validation and returns stderr."""
        result = self.render_raw(values)
        self.assertNotEqual(result.returncode, 0,
                            "expected a validation failure, but rendering succeeded")
        return result.stderr

    def acme_values(self, **certificates):
        values = {
            "acme": {"letsencrypt": copy.deepcopy(LE_ACME)},
            "certificates": copy.deepcopy(certificates),
            "global": {"cluster": {"name": "c", "baseDomain": "example.com"}},
        }
        values["certificates"]["clusterIssuer"] = "letsencrypt"
        return values

    def managed_render(self, modes=(), acme="default", **extra_certificates):
        values = {
            "certificates": {"clusterIssuer": "letsencrypt"},
            "global": {"cluster": {"name": "c", "baseDomain": "example.com"}},
        }
        if acme == "default":
            values["acme"] = {"letsencrypt": copy.deepcopy(LE_ACME)}
        elif acme is not None:
            values["acme"] = acme
        for mode in modes:
            certificates = values["certificates"]
            certificates[mode] = {"managed": True}
        values["certificates"].update(extra_certificates)
        return self.by_kind(self.render(values))

    @staticmethod
    def by_kind(docs):
        by_kind = {}
        for d in docs:
            by_kind.setdefault(d["kind"], []).append(d)
        return by_kind

    @staticmethod
    def pick(by_kind, kind, name):
        matches = [d for d in by_kind[kind] if d["metadata"]["name"] == name]
        assert len(matches) == 1, (kind, name, by_kind[kind])
        return matches[0]


class CAFamilyTests(ChartFixture):
    """The default render's CA family: a stable manually-managed root
    (explicit duration and rotationPolicy), self-signed issuer, CA
    Certificate, ca-issuer, and the full trust surface, with no
    serving-certificate surface at all."""

    def default_render(self):
        return self.by_kind(self.render())

    def test_ca_chain_shape(self):
        by_kind = self.default_render()
        self.assertEqual(self.pick(by_kind, "ClusterIssuer", "selfsigned-issuer"), {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": "selfsigned-issuer"},
            "spec": {"selfSigned": {}},
        })
        self.assertEqual(self.pick(by_kind, "Certificate", "selfsigned-ca"), {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "selfsigned-ca", "namespace": "cert-manager"},
            "spec": {
                "isCA": True,
                "commonName": "selfsigned-ca",
                # Explicit 10-year lifetime and a pinned key-reuse policy:
                # the upstream 90-day/Always defaults would expire the anchor
                # and rotate the key out from under the trust bundle
                "duration": "87600h",
                "secretName": "cert-manager-ca",
                "privateKey": {"algorithm": "ECDSA", "size": 256,
                               "rotationPolicy": "Never"},
                "issuerRef": {
                    "name": "selfsigned-issuer",
                    "kind": "ClusterIssuer",
                    "group": "cert-manager.io",
                },
            },
        })
        self.assertEqual(self.pick(by_kind, "ClusterIssuer", "ca-issuer"), {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {"name": "ca-issuer"},
            "spec": {"ca": {"secretName": "cert-manager-ca"}},
        })

    def test_ca_duration_and_rotation_policy_follow_values(self):
        values = {"ca": {"duration": "43800h"}}
        cert = self.pick(self.by_kind(self.render(values)),
                         "Certificate", "selfsigned-ca")
        self.assertEqual(cert["spec"]["duration"], "43800h")
        self.assertEqual(cert["spec"]["privateKey"]["rotationPolicy"], "Never")

    def test_trust_surface_shape(self):
        by_kind = self.default_render()
        sa = self.pick(by_kind, "ServiceAccount", "trust-ca")
        self.assertEqual(sa, {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": "trust-ca", "namespace": "cert-manager"},
        })
        binding = self.pick(by_kind, "ClusterRoleBinding", "trust-ca")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "trust-ca",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "trust-ca",
            "namespace": "cert-manager",
        }])
        cm = self.pick(by_kind, "ConfigMap", "trust-ca-config")
        self.assertEqual(sorted(cm["data"]),
                         ["hook-preamble.sh", "trust-bundle.sh",
                          "trust-ca-remove.sh", "trust-ca-set.sh"])
        set_job = self.pick(by_kind, "Job", "trust-ca-set")
        self.assertEqual(
            set_job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            set_job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(set_job["spec"]["backoffLimit"], 3)
        self.assertEqual(set_job["spec"]["activeDeadlineSeconds"], 150)
        pod = set_job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "trust-ca")
        self.assertEqual(pod["restartPolicy"], "Never")
        self.assertEqual(pod["securityContext"],
                         {"runAsNonRoot": True,
                          "seccompProfile": {"type": "RuntimeDefault"}})
        container = pod["containers"][0]
        self.assertEqual(container["image"],
                         "image-registry.openshift-image-registry.svc:5000"
                         "/openshift/tools:latest")
        self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
        self.assertEqual(container["env"],
                         [{"name": "TRUST_CA_TIMEOUT", "value": "120"}])
        self.assertEqual(container["command"], ["/mnt/trust-ca-set.sh"])
        self.assertEqual(container["volumeMounts"],
                         [{"name": "trust-ca-config", "mountPath": "/mnt"}])
        self.assertEqual(pod["volumes"], [{
            "name": "trust-ca-config",
            "configMap": {"name": "trust-ca-config", "defaultMode": 493},
        }])
        remove_job = self.pick(by_kind, "Job", "trust-ca-remove")
        self.assertEqual(
            remove_job["metadata"]["annotations"]["helm.sh/hook"], "pre-delete")
        self.assertEqual(
            remove_job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertNotIn("activeDeadlineSeconds", remove_job["spec"])
        self.assertEqual(
            remove_job["spec"]["template"]["spec"]["containers"][0]["command"],
            ["/mnt/trust-ca-remove.sh"])

    def test_default_renders_no_serving_cert_surface(self):
        by_kind = self.default_render()
        # The trust-ca Role/RoleBinding are the trust surface's own grants;
        # no serving-certs grants render
        self.assertEqual(
            [r["metadata"]["name"] for r in by_kind.get("Role", [])],
            ["trust-ca"])
        self.assertEqual(
            [b["metadata"]["name"] for b in by_kind.get("RoleBinding", [])],
            ["trust-ca"])
        self.assertEqual(
            [d["metadata"]["name"] for d in by_kind.get("ClusterRole", [])],
            ["trust-ca"])
        self.assertEqual(
            [b["metadata"]["name"] for b in by_kind.get("ClusterRoleBinding", [])],
            ["trust-ca"])
        self.assertEqual(
            [s["metadata"]["name"] for s in by_kind.get("ServiceAccount", [])],
            ["trust-ca"])
        self.assertEqual(
            [j["metadata"]["name"] for j in by_kind.get("Job", [])],
            ["trust-ca-remove", "trust-ca-set"])
        self.assertEqual(
            [c["metadata"]["name"] for c in by_kind.get("ConfigMap", [])],
            ["trust-ca-config"])

    def test_platform_objects_never_render(self):
        # Managed or not, the chart never manifests the platform-owned
        # IngressController or APIServer objects as release resources.
        for by_kind in (self.default_render(),
                        self.managed_render(modes=("router",)),
                        self.managed_render(modes=("api",)),
                        self.managed_render(modes=("api", "router"))):
            self.assertNotIn("IngressController", by_kind)
            self.assertNotIn("APIServer", by_kind)

    def test_controller_override_args(self):
        cr = self.pick(self.default_render(), "CertManager", "cluster")
        self.assertEqual(cr["spec"]["controllerConfig"]["overrideArgs"], [
            "--dns01-recursive-nameservers=9.9.9.9:53,1.1.1.1:53",
            "--dns01-recursive-nameservers-only",
            "--enable-certificate-owner-ref=false",
        ])

    def test_managed_certificates_use_selected_issuer(self):
        by_kind = self.managed_render(modes=("api", "router"))
        api = self.pick(by_kind, "Certificate", "openshift-api")
        self.assertEqual(api["spec"]["secretName"], API_SECRET_NAME)
        self.assertEqual(api["spec"]["issuerRef"]["name"], "letsencrypt")
        self.assertEqual(api["spec"]["issuerRef"]["kind"], "ClusterIssuer")
        router = self.pick(by_kind, "Certificate", "router-default")
        self.assertEqual(router["spec"]["secretName"], ROUTER_DEFAULT_CERT)
        self.assertEqual(router["spec"]["issuerRef"]["name"], "letsencrypt")
        self.assertEqual(router["metadata"]["namespace"], "openshift-ingress")
        self.assertEqual(
            router["spec"]["dnsNames"],
            ["apps.c.example.com", "*.apps.c.example.com"])

    def test_managed_ca_issuer_path(self):
        by_kind = self.managed_render(
            modes=("router",), acme=None, clusterIssuer="ca-issuer")
        router = self.pick(by_kind, "Certificate", "router-default")
        self.assertEqual(router["spec"]["issuerRef"]["name"], "ca-issuer")


class ValidationTests(ChartFixture):
    """Render-time validation traps: each broken configuration fails with
    an actionable message naming the value path and the rendered options."""

    def trap(self, values):
        return self.render_failure(values)

    def test_managed_without_any_issuer_fails(self):
        stderr = self.trap({
            "certificates": {"clusterIssuer": "zerossl", "router": {"managed": True}},
        })
        self.assertIn("does not name a rendered ClusterIssuer", stderr)
        self.assertIn("set it to one of: ca-issuer", stderr)

    def test_managed_with_ca_disabled_and_no_acme_fails(self):
        stderr = self.trap({
            "ca": {"enabled": False},
            "certificates": {"clusterIssuer": "zerossl", "router": {"managed": True}},
        })
        self.assertIn("does not name a rendered ClusterIssuer", stderr)
        self.assertIn("no issuer is rendered at all", stderr)

    def test_managed_with_unconfigured_issuer_name_fails(self):
        stderr = self.trap({
            "acme": {"zerossl": copy.deepcopy(ZEROSSL_ACME)},
            "certificates": {"clusterIssuer": "letsencrypt",
                             "router": {"managed": True}},
        })
        self.assertIn("does not name a rendered ClusterIssuer", stderr)
        self.assertIn("set it to one of: ca-issuer, zerossl", stderr)

    def test_default_values_are_valid(self):
        # The shipped defaults name no issuer, but nothing is managed, so
        # no issuer reference is exercised.
        self.render()

    def test_half_bring_your_own_ca_fails(self):
        stderr = self.trap({"ca": {"tlsCrt": "PEM-CERT"}})
        self.assertIn("ca.tlsCrt and ca.tlsKey must be set together", stderr)

    def test_other_half_bring_your_own_ca_fails(self):
        stderr = self.trap({"ca": {"tlsKey": "PEM-KEY"}})
        self.assertIn("ca.tlsCrt and ca.tlsKey must be set together", stderr)

    def test_byo_ca_with_ca_family_disabled_fails(self):
        # The BYO keypair is silently ignored while the CA family is off;
        # the trap fires regardless of the trust gate's position
        for trust in (True, False):
            with self.subTest(trust_enabled=trust):
                stderr = self.trap({
                    "ca": {"enabled": False, "trust": {"enabled": trust},
                           "tlsCrt": "PEM-CERT", "tlsKey": "PEM-KEY"},
                })
                self.assertIn("ca.tlsCrt/ca.tlsKey are set while ca.enabled "
                              "is false", stderr)

    def test_acme_entry_without_server_fails(self):
        stderr = self.trap({"acme": {"le": {"email": "ops@example.com"}}})
        self.assertIn("acme.le.server is required", stderr)

    def test_acme_entry_without_email_fails(self):
        stderr = self.trap({"acme": {"le": {
            "server": "https://acme.example.com/directory"}}})
        self.assertIn("acme.le.email is required", stderr)

    def test_unknown_solver_type_fails(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["solvers"] = [{
            "type": "route53",
            "dnsConfig": {"route53": {"region": "us-east-2"}},
        }]
        stderr = self.trap(values)
        self.assertIn("solvers[0].type must be `dns` or `http`", stderr)
        self.assertIn("(got route53)", stderr)

    def test_dns_solver_without_dns_config_fails(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["solvers"] = [{"type": "dns"}]
        stderr = self.trap(values)
        self.assertIn("solvers[0].dnsConfig is required for a dns solver", stderr)

    def test_http_solver_without_ingress_class_fails(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["solvers"] = [{"type": "http"}]
        stderr = self.trap(values)
        self.assertIn("solvers[0].ingressClassName is required", stderr)
        self.assertIn("openshift-default", stderr)


class AcmeRenderTests(ChartFixture):
    """The ACME ClusterIssuer shape matches the documented cert-manager
    v1.20 spec surface, and the credential Secrets render into the
    cluster-resource namespace."""

    def test_letsencrypt_example_shape(self):
        by_kind = self.managed_render(modes=("router",))
        issuer = self.pick(by_kind, "ClusterIssuer", "letsencrypt")
        acme = issuer["spec"]["acme"]
        self.assertEqual(acme["server"], LE_ACME["server"])
        self.assertEqual(acme["email"], LE_ACME["email"])
        self.assertEqual(acme["privateKeySecretRef"],
                         {"name": "acme-letsencrypt-privatekey"})
        self.assertNotIn("disableAccountKeyGeneration", acme)
        self.assertNotIn("externalAccountBinding", acme)
        self.assertEqual(acme["solvers"], [{
            "dns01": {"route53": {
                "accessKeyID": "AKIAEXAMPLE",
                "hostedZoneID": "Z123EXAMPLE",
                "region": "us-east-2",
                "secretAccessKeySecretRef": {
                    "name": "acme-letsencrypt-route53-creds",
                    "key": "secret-access-key",
                },
            }},
            "selector": {"dnsZones": ["example.com"]},
        }])
        secret = self.pick(by_kind, "Secret", "acme-letsencrypt-route53-creds")
        self.assertEqual(secret["metadata"]["namespace"], "cert-manager")
        self.assertEqual(secret["data"]["secret-access-key"],
                         "cm91dGU1My1zZWNyZXQ=")

    def test_zerossl_example_shape(self):
        values = self.acme_values()
        values["acme"] = {"zerossl": copy.deepcopy(ZEROSSL_ACME)}
        values["certificates"]["clusterIssuer"] = "zerossl"
        by_kind = self.by_kind(self.render(values))
        issuer = self.pick(by_kind, "ClusterIssuer", "zerossl")
        eab = issuer["spec"]["acme"]["externalAccountBinding"]
        self.assertEqual(eab["keyID"], "eab-key-id")
        self.assertEqual(eab["keySecretRef"],
                         {"name": "acme-zerossl-eabsecret", "key": "secret"})
        # keyAlgorithm is deprecated upstream and must not render
        self.assertNotIn("keyAlgorithm", eab)

    def test_disable_account_key_generation_passthrough(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["disableAccountKeyGeneration"] = True
        values["acme"]["letsencrypt"]["secrets"] = [{
            "name": "privatekey",
            "data": {"tls.key": "PREPROVISIONED"},
        }]
        by_kind = self.by_kind(self.render(values))
        issuer = self.pick(by_kind, "ClusterIssuer", "letsencrypt")
        self.assertIs(issuer["spec"]["acme"]["disableAccountKeyGeneration"], True)
        secret = self.pick(by_kind, "Secret", "acme-letsencrypt-privatekey")
        self.assertEqual(secret["data"]["tls.key"], "PREPROVISIONED")

    def test_preferred_chain_renders_when_set(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["preferredChain"] = "ISRG Root X1"
        by_kind = self.by_kind(self.render(values))
        issuer = self.pick(by_kind, "ClusterIssuer", "letsencrypt")
        self.assertEqual(issuer["spec"]["acme"]["preferredChain"], "ISRG Root X1")

    def test_http_solver_shape(self):
        values = self.acme_values()
        values["acme"]["letsencrypt"]["solvers"] = [{
            "type": "http", "ingressClassName": "openshift-default"}]
        by_kind = self.by_kind(self.render(values))
        issuer = self.pick(by_kind, "ClusterIssuer", "letsencrypt")
        self.assertEqual(issuer["spec"]["acme"]["solvers"], [{
            "http01": {"ingress": {"ingressClassName": "openshift-default"}},
        }])

    def test_null_acme_renders_no_issuers(self):
        # A values override of `acme: null` must render like the empty
        # default, not trip a raw type error in the validation helpers.
        result = self.render_raw({"acme": None, "ca": {"enabled": False}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [d for d in docs(result.stdout)
             if d.get("kind") == "ClusterIssuer"],
            [])


class ServingCertHookTests(ChartFixture):
    """With a mode managed, its hook Job, script ConfigMap, ServiceAccount,
    and resourceName-scoped grants render; with neither mode managed,
    nothing of the surface renders."""

    def test_nothing_renders_when_neither_mode_is_managed(self):
        by_kind = self.managed_render()
        names = {d["metadata"]["name"]
                 for docs_ in by_kind.values() for d in docs_}
        for name in ("serving-certs", "serving-certs-config",
                     "configure-router-certificate", "configure-api-certificate",
                     "serving-certs-router", "serving-certs-api"):
            self.assertNotIn(name, names)

    def test_router_mode_renders_router_surface_only(self):
        by_kind = self.managed_render(modes=("router",))
        jobs = [j["metadata"]["name"] for j in by_kind.get("Job", [])]
        self.assertIn("configure-router-certificate", jobs)
        self.assertNotIn("configure-api-certificate", jobs)
        # The trust-ca ClusterRoleBinding renders alongside (the CA family is
        # enabled by default); the API-mode grants stay off
        self.assertEqual(
            [b["metadata"]["name"] for b in by_kind.get("ClusterRoleBinding", [])],
            ["trust-ca"])
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        self.assertIn("configure-router-cert.sh", cm["data"])
        self.assertIn("router-reference.json", cm["data"])
        self.assertNotIn("configure-api-cert.sh", cm["data"])
        self.assertNotIn("api-reference.json", cm["data"])
        # The trust surface renders alongside any managed mode (ca.enabled
        # defaults true), including its ClusterRole
        self.assertEqual(
            [r["metadata"]["name"] for r in by_kind.get("ClusterRole", [])],
            ["trust-ca"])

    def test_api_mode_renders_api_surface_only(self):
        by_kind = self.managed_render(modes=("api",))
        jobs = [j["metadata"]["name"] for j in by_kind.get("Job", [])]
        self.assertIn("configure-api-certificate", jobs)
        self.assertNotIn("configure-router-certificate", jobs)
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        self.assertIn("configure-api-cert.sh", cm["data"])
        self.assertIn("api-reference.json", cm["data"])
        self.assertNotIn("configure-router-cert.sh", cm["data"])
        self.assertNotIn("router-reference.json", cm["data"])

    def test_job_identity_and_hooks(self):
        for mode, job_name in (("router", "configure-router-certificate"),
                               ("api", "configure-api-certificate")):
            with self.subTest(mode=mode):
                job = self.pick(
                    self.managed_render(modes=(mode,)), "Job", job_name)
                self.assertEqual(
                    job["metadata"]["annotations"]["helm.sh/hook"],
                    "post-install,post-upgrade")
                self.assertEqual(
                    job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                    "before-hook-creation")
                self.assertEqual(job["spec"]["backoffLimit"], 4)
                self.assertEqual(job["spec"]["activeDeadlineSeconds"], 338)
                pod = job["spec"]["template"]["spec"]
                self.assertEqual(pod["serviceAccountName"], "serving-certs")
                self.assertEqual(pod["restartPolicy"], "Never")
                self.assertEqual(pod["securityContext"],
                                 {"runAsNonRoot": True,
                                  "seccompProfile": {"type": "RuntimeDefault"}})
                container = pod["containers"][0]
                self.assertEqual(
                    container["image"],
                    "image-registry.openshift-image-registry.svc:5000"
                    "/openshift/tools:latest")
                self.assertEqual(container["imagePullPolicy"], "IfNotPresent")
                self.assertEqual(container["command"],
                                 [f"/mnt/configure-{mode}-cert.sh"])
                self.assertEqual(container["securityContext"],
                                 {"capabilities": {"drop": ["ALL"]}})
                self.assertEqual(container["volumeMounts"],
                                 [{"name": "serving-certs-config",
                                   "mountPath": "/mnt"}])
                self.assertEqual(pod["volumes"], [{
                    "name": "serving-certs-config",
                    "configMap": {"name": "serving-certs-config",
                                  "defaultMode": 493},
                }])

    def test_service_account_renders_once_for_both_modes(self):
        by_kind = self.managed_render(modes=("api", "router"))
        sas = [s["metadata"]["name"] for s in by_kind["ServiceAccount"]]
        self.assertEqual(sas.count("serving-certs"), 1)
        for job_name in ("configure-router-certificate",
                         "configure-api-certificate"):
            job = self.pick(by_kind, "Job", job_name)
            self.assertNotIn("namespace", job["metadata"])
        sa = self.pick(by_kind, "ServiceAccount", "serving-certs")
        self.assertNotIn("namespace", sa["metadata"])

    def test_router_grants_are_resource_scoped_in_ingress_operator_ns(self):
        by_kind = self.managed_render(modes=("router",))
        role = self.pick(by_kind, "Role", "serving-certs-router")
        self.assertEqual(role["metadata"]["namespace"],
                         "openshift-ingress-operator")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["operator.openshift.io"],
            "resources": ["ingresscontrollers"],
            "resourceNames": ["default"],
            "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "RoleBinding", "serving-certs-router")
        self.assertEqual(binding["metadata"]["namespace"],
                         "openshift-ingress-operator")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "serving-certs-router",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "serving-certs",
            "namespace": "test-release-ns",
        }])

    def test_api_grants_are_cluster_scoped_and_resource_named(self):
        by_kind = self.managed_render(modes=("api",))
        role = self.pick(by_kind, "ClusterRole", "serving-certs-api")
        self.assertEqual(role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["apiservers"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])
        binding = self.pick(by_kind, "ClusterRoleBinding", "serving-certs-api")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "serving-certs-api",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "serving-certs",
            "namespace": "test-release-ns",
        }])

    def test_reference_files_follow_values(self):
        by_kind = self.managed_render(modes=("api", "router"))
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        self.assertEqual(json.loads(cm["data"]["router-reference.json"]),
                         {"name": ROUTER_DEFAULT_CERT})
        self.assertEqual(
            json.loads(cm["data"]["api-reference.json"]),
            {"names": ["api.c.example.com"],
             "servingCertificate": {"name": API_SECRET_NAME}})

        values = self.acme_values(
            router={"managed": True},
            api={"managed": True,
                 "dnsNames": ["api.other.example.com", "alt.example.com"]})
        cm = self.pick(self.by_kind(self.render(values)),
                       "ConfigMap", "serving-certs-config")
        self.assertEqual(
            json.loads(cm["data"]["api-reference.json"]),
            {"names": ["api.other.example.com", "alt.example.com"],
             "servingCertificate": {"name": API_SECRET_NAME}})

    def test_scripts_are_fail_closed_dual_tool_scripts(self):
        by_kind = self.managed_render(modes=("api", "router"))
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        for script in ("configure-router-cert.sh", "configure-api-cert.sh"):
            text = cm["data"][script]
            self.assertIn("set -euo pipefail", text)
            self.assertIn('source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"',
                          text)
            self.assertIn("foreign reference", text)
        # The fail-closed mechanics live in the shared preamble lib
        preamble = cm["data"]["hook-preamble.sh"]
        self.assertIn("neither jq nor python3", preamble)
        self.assertIn("python3", preamble)
        self.assertIn("(Conflict)", preamble)
        self.assertIn("MAX_ATTEMPTS=5", preamble)
        self.assertIn("conditional_write", preamble)
        self.assertIn("defaultCertificate", cm["data"]["configure-router-cert.sh"])
        self.assertIn("namedCertificates",
                      cm["data"]["configure-api-cert.sh"])


class FakeOcScriptFixture(ChartFixture):
    """Shared plumbing for executing rendered hook scripts against a fake
    oc binary, mirroring the keycloak-auth fixture of the same name.

    Each run gets a restricted PATH built solely from symlinks in an
    isolated bin directory (the JSON tool chosen per test path, core
    utilities, and the fake oc), so no ambient host tool or real oc can be
    reached. The child environment is an explicit minimal set — no parent
    variables are inherited. Subclasses supply the fake oc program, the
    core tool list, the rendered scripts dict, and their canned docs."""
    MAX_ATTEMPTS = 5
    work_prefix = "cm-script-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort"]
    fake_oc_script = None

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix=self.work_prefix)
        self.addCleanup(shutil.rmtree, self.work)

    def build_bin_dir(self, include_jq=None, include_python=None):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        if include_jq is None:
            include_jq = shutil.which("jq") is not None
        if include_python is None:
            include_python = shutil.which("python3") is not None
        tools = list(self.bin_tools)
        if include_jq:
            self.assertTrue(shutil.which("jq"),
                            "jq requested but not available on the host")
            tools.append("jq")
        if include_python:
            self.assertTrue(shutil.which("python3"),
                            "python3 requested but not available on the host")
            tools.append("python3")
        for tool in tools:
            os.symlink(shutil.which(tool), os.path.join(bin_dir, tool))
        fake_oc = os.path.join(bin_dir, "oc")
        with open(fake_oc, "w") as f:
            f.write(self.fake_oc_script)
        os.chmod(fake_oc, 0o755)
        return bin_dir

    def tool_path_variants(self):
        """Both JSON tool paths the hook scripts can take; the jq path is
        included only when the host provides jq."""
        variants = [{"include_jq": False, "include_python": True}]
        if shutil.which("jq"):
            variants.append({"include_jq": True, "include_python": False})
        return variants

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def child_env(self):
        with open(os.path.join(self.work, "env.log")) as f:
            return dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line)

    def patch_lines(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [line for line in f.read().splitlines() if line]

    def stage_scripts(self):
        # Every staged file lands in the same directory as the entry script
        # because the scripts source their shared libs from their own
        # directory, mirroring the single ConfigMap mount.
        for name, content in self.scripts.items():
            with open(os.path.join(self.work, name), "w") as f:
                f.write(content)

    def clear_run_state(self):
        # Each run is self-contained: drop logs and the fake oc's
        # resourceVersion counters left by any earlier run.
        for stale in ("commands.log", "env.log", "patches.log", "create.log",
                      "create.log.done"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        for stale in os.listdir(self.work):
            if stale.startswith("attempts") or stale.endswith(".rv"):
                os.unlink(os.path.join(self.work, stale))

    def run_child(self, script_path, env):
        with mock.patch.dict(os.environ, {"CM_PARENT_SENTINEL": "must-not-leak"}):
            return subprocess.run(
                ["/bin/bash", script_path], capture_output=True, text=True,
                env=env, timeout=120)


class HookScriptTests(FakeOcScriptFixture):
    """Executes the rendered serving-certificate hook scripts against a
    fake oc binary; one canned platform object per run."""
    fake_oc_script = FAKE_OC_SCRIPT

    def setUp(self):
        super().setUp()
        cm = self.pick(self.managed_render(modes=("api", "router")),
                       "ConfigMap", "serving-certs-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "configure-router-cert.sh",
                                     "configure-api-cert.sh")}
        self.router_ref = json.loads(cm["data"]["router-reference.json"])
        self.api_ref = json.loads(cm["data"]["api-reference.json"])
        self.foreign_entry = {
            "names": ["legacy.example.com"],
            "servingCertificate": {"name": "legacy-cert"},
        }

    def object_doc(self, spec, resource_version="1597"):
        return {
            "apiVersion": "v1",
            "kind": "Object",
            "metadata": {"name": "cluster", "resourceVersion": resource_version},
            "spec": spec,
        }

    def write_reference(self, name, ref):
        path = os.path.join(self.work, name)
        with open(path, "w") as f:
            json.dump(ref, f)
        return path

    def run_script(self, script_name, scenario, object_doc,
                   include_jq=None, include_python=None, reference_file=None):
        bin_dir = self.build_bin_dir(include_jq=include_jq,
                                     include_python=include_python)
        self.stage_scripts()
        script_path = os.path.join(self.work, script_name)
        if reference_file is None:
            reference_name = ("router-reference.json"
                              if script_name == "configure-router-cert.sh"
                              else "api-reference.json")
            reference_file = self.write_reference(reference_name, self.current_ref)
        get_file = os.path.join(self.work, "object.json")
        with open(get_file, "w") as f:
            json.dump(object_doc, f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "REFERENCE_FILE": reference_file,
            "OC_SCENARIO": scenario,
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_ENV": os.path.join(self.work, "env.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_ATTEMPTS": os.path.join(self.work, "attempts"),
        }
        return self.run_child(script_path, env)

    def patches(self):
        return [json.loads(line) for line in self.patch_lines()]


class RouterScriptTests(HookScriptTests):
    """Script-execution coverage for configure-router-cert.sh. The router
    reference is a single object: unset is a valid target, an exact match
    is a no-op, and any set reference is foreign."""

    def setUp(self):
        super().setUp()
        self.current_ref = self.router_ref

    def get_argv(self):
        return self.log().splitlines()[0]

    def test_child_environment_is_minimal_and_isolated(self):
        result = self.run_script("configure-router-cert.sh", "fresh",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(os.path.join(self.work, "env.log")) as f:
            child = dict(
                line.split("=", 1) for line in f.read().splitlines() if "=" in line)
        self.assertNotIn("CM_PARENT_SENTINEL", child)
        self.assertEqual(child["PATH"], os.path.join(self.work, "bin"))
        self.assertEqual(
            {k for k in child if k.startswith("OC_")},
            {"OC_SCENARIO", "OC_LOG", "OC_ENV", "OC_GET_FILE",
             "OC_PATCH_LOG", "OC_ATTEMPTS"})
        self.assertEqual(
            self.get_argv(),
            "get -n openshift-ingress-operator "
            "ingresscontrollers.operator.openshift.io/default "
            "-o json --request-timeout=30s")

    def test_unset_default_certificate_is_written(self):
        result = self.run_script("configure-router-cert.sh", "fresh",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"defaultCertificate": {"name": ROUTER_DEFAULT_CERT}},
        })

    def test_equal_reference_is_no_op(self):
        result = self.run_script(
            "configure-router-cert.sh", "fresh",
            self.object_doc({"defaultCertificate":
                             {"name": ROUTER_DEFAULT_CERT}}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_foreign_reference_stops_without_writing(self):
        result = self.run_script(
            "configure-router-cert.sh", "fresh",
            self.object_doc({"defaultCertificate": {"name": "legacy-cert"}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign reference", result.stderr)
        self.assertIn("legacy-cert", result.stderr)
        self.assertEqual(self.patches(), [])
        # The STOP decision needs exactly one read; no patch was attempted.
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_foreign_reference_without_name_stops(self):
        result = self.run_script(
            "configure-router-cert.sh", "fresh",
            self.object_doc({"defaultCertificate": {}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign reference", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_write_and_stop_across_tool_paths(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "configure-router-cert.sh", "fresh",
                    self.object_doc({}), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                self.assertEqual(
                    patches[0]["spec"]["defaultCertificate"],
                    {"name": ROUTER_DEFAULT_CERT})

                result = self.run_script(
                    "configure-router-cert.sh", "fresh",
                    self.object_doc({"defaultCertificate":
                                     {"name": "legacy-cert"}}), **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("foreign reference", result.stderr)

    def test_retries_conflict_then_succeeds(self):
        result = self.run_script("configure-router-cert.sh", "conflict-then-success",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        gets = [line for line in self.log().splitlines()
                if line.startswith("get ")]
        self.assertEqual(len(gets), 2)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        # The fake oc bumps the served resourceVersion on every GET, so the
        # failed attempt carried the stale version and the successful retry
        # carried the fresh one from its own re-read.
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1597")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1598")
        self.assertIn("retrying", result.stderr)

    def test_conflict_exhaustion_fails(self):
        result = self.run_script("configure-router-cert.sh", "conflict-exhausted",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)
        self.assertIn("Conflict", result.stderr)

    def test_non_conflict_patch_error_propagates(self):
        result = self.run_script("configure-router-cert.sh", "patch-error",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(len(self.patches()), 1)

    def test_absent_ingress_controller_fails_with_prerequisite(self):
        result = self.run_script("configure-router-cert.sh", "absent-object",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prerequisite missing", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_get_error_propagates(self):
        result = self.run_script("configure-router-cert.sh", "get-error",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("configure-router-cert.sh", "fresh",
                                 self.object_doc({}),
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class ApiScriptTests(HookScriptTests):
    """Script-execution coverage for configure-api-cert.sh. The
    namedCertificates list is atomic: empty is a valid target, an exact
    single-entry match is a no-op, and any other list is foreign."""

    def setUp(self):
        super().setUp()
        self.current_ref = self.api_ref

    def get_argv(self):
        return self.log().splitlines()[0]

    def test_absent_list_is_written(self):
        result = self.run_script("configure-api-cert.sh", "fresh",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.get_argv(),
            "get apiservers.config.openshift.io/cluster -o json "
            "--request-timeout=30s")
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"servingCerts": {"namedCertificates": [self.api_ref]}},
        })

    def test_empty_list_is_written(self):
        result = self.run_script(
            "configure-api-cert.sh", "fresh",
            self.object_doc({"servingCerts": {"namedCertificates": []}}))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["spec"]["servingCerts"]["namedCertificates"],
                         [self.api_ref])

    def test_exact_list_is_no_op(self):
        result = self.run_script(
            "configure-api-cert.sh", "fresh",
            self.object_doc({"servingCerts": {"namedCertificates":
                                              [self.api_ref]}}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_foreign_entry_stops_without_writing(self):
        result = self.run_script(
            "configure-api-cert.sh", "fresh",
            self.object_doc({"servingCerts":
                             {"namedCertificates": [self.foreign_entry]}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign configuration", result.stderr)
        self.assertIn("legacy.example.com", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_ours_plus_foreign_stops_without_writing(self):
        result = self.run_script(
            "configure-api-cert.sh", "fresh",
            self.object_doc({"servingCerts": {"namedCertificates":
                                              [self.api_ref,
                                               self.foreign_entry]}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign configuration", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_foreign_entry_without_names_stops(self):
        entry = {"servingCertificate": {"name": "legacy-cert"}}
        result = self.run_script(
            "configure-api-cert.sh", "fresh",
            self.object_doc({"servingCerts": {"namedCertificates": [entry]}}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("foreign configuration", result.stderr)
        self.assertIn("<none>", result.stderr)

    def test_write_and_stop_across_tool_paths(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "configure-api-cert.sh", "fresh",
                    self.object_doc({}), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                patches = self.patches()
                self.assertEqual(len(patches), 1)
                self.assertEqual(
                    patches[0]["spec"]["servingCerts"]["namedCertificates"],
                    [self.api_ref])

                result = self.run_script(
                    "configure-api-cert.sh", "fresh",
                    self.object_doc({"servingCerts": {"namedCertificates":
                                                      [self.foreign_entry]}}),
                    **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("foreign configuration", result.stderr)

    def test_retries_conflict_then_succeeds(self):
        result = self.run_script("configure-api-cert.sh", "conflict-then-success",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        gets = [line for line in self.log().splitlines()
                if line.startswith("get ")]
        self.assertEqual(len(gets), 2)
        patches = self.patches()
        self.assertEqual(len(patches), 2)
        self.assertEqual(patches[0]["metadata"]["resourceVersion"], "1597")
        self.assertEqual(patches[1]["metadata"]["resourceVersion"], "1598")
        self.assertIn("retrying", result.stderr)

    def test_conflict_exhaustion_fails(self):
        result = self.run_script("configure-api-cert.sh", "conflict-exhausted",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches()), self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)

    def test_non_conflict_patch_error_propagates(self):
        result = self.run_script("configure-api-cert.sh", "patch-error",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Forbidden", result.stderr)
        self.assertEqual(len(self.patches()), 1)

    def test_absent_apiserver_fails_with_prerequisite(self):
        result = self.run_script("configure-api-cert.sh", "absent-object",
                                 self.object_doc({}))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("prerequisite missing", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("configure-api-cert.sh", "fresh",
                                 self.object_doc({}),
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class TrustGateMatrixTests(ChartFixture):
    """The full gate matrix over `ca.enabled` × `ca.trust.enabled` ×
    self-provisioned/BYO (BYO = both `ca.tlsCrt` and `ca.tlsKey` set):
    issuance renders on `ca.enabled` alone, the trust surface only when the
    trust gate joins it, and a BYO keypair without the CA family fails
    loudly at render time."""

    BYO = {"tlsCrt": "PEM-CERT", "tlsKey": "PEM-KEY"}

    TRUST_KINDS = ("ServiceAccount", "Role", "RoleBinding", "ClusterRole",
                   "ClusterRoleBinding", "ConfigMap", "Job")

    def trust_names(self, by_kind):
        names = set()
        for kind in self.TRUST_KINDS:
            for d in by_kind.get(kind, []):
                if d["metadata"]["name"].startswith("trust-ca"):
                    names.add(d["metadata"]["name"])
        return names

    def render_ca(self, enabled=True, trust=True, byo=False):
        ca = {"enabled": enabled, "trust": {"enabled": trust}}
        if byo:
            ca.update(self.BYO)
        return self.by_kind(self.render({"ca": ca}))

    def test_self_provisioned_with_trust(self):
        by_kind = self.render_ca()
        self.assertEqual(self.trust_names(by_kind),
                         {"trust-ca", "trust-ca-config", "trust-ca-set",
                          "trust-ca-remove"})
        self.assertEqual(
            [i["metadata"]["name"] for i in by_kind.get("ClusterIssuer", [])],
            ["selfsigned-issuer", "ca-issuer"])
        self.assertIn("Certificate", by_kind)
        self.assertNotIn("Secret", by_kind)

    def test_byo_with_trust(self):
        by_kind = self.render_ca(byo=True)
        self.assertEqual(self.trust_names(by_kind),
                         {"trust-ca", "trust-ca-config", "trust-ca-set",
                          "trust-ca-remove"})
        secret = self.pick(by_kind, "Secret", "cert-manager-ca")
        self.assertEqual(secret["stringData"],
                         {"tls.crt": "PEM-CERT", "tls.key": "PEM-KEY"})
        self.assertEqual(
            [i["metadata"]["name"] for i in by_kind.get("ClusterIssuer", [])],
            ["ca-issuer"])
        self.assertNotIn("Certificate", by_kind)

    def test_self_provisioned_without_trust(self):
        by_kind = self.render_ca(trust=False)
        self.assertEqual(self.trust_names(by_kind), set())
        self.assertIn("Certificate", by_kind)
        self.assertEqual(
            [i["metadata"]["name"] for i in by_kind.get("ClusterIssuer", [])],
            ["selfsigned-issuer", "ca-issuer"])

    def test_byo_without_trust(self):
        by_kind = self.render_ca(trust=False, byo=True)
        self.assertEqual(self.trust_names(by_kind), set())
        self.assertIn("Secret", by_kind)
        self.assertEqual(
            [i["metadata"]["name"] for i in by_kind.get("ClusterIssuer", [])],
            ["ca-issuer"])

    def test_ca_family_disabled_renders_nothing(self):
        for trust in (True, False):
            with self.subTest(trust_enabled=trust):
                by_kind = self.render_ca(enabled=False, trust=trust)
                self.assertEqual(self.trust_names(by_kind), set())
                for kind in ("ClusterIssuer", "Certificate", "Secret"):
                    rendered = [d["metadata"]["name"]
                                for d in by_kind.get(kind, [])
                                if d["metadata"]["name"] in
                                ("cert-manager-ca", "selfsigned-issuer",
                                 "selfsigned-ca", "ca-issuer")]
                    self.assertEqual(rendered, [])

    def test_byo_with_ca_family_disabled_fails_loudly(self):
        for trust in (True, False):
            with self.subTest(trust_enabled=trust):
                stderr = self.render_failure(
                    {"ca": {"enabled": False, "trust": {"enabled": trust},
                            **self.BYO}})
                self.assertIn("ca.tlsCrt/ca.tlsKey are set while ca.enabled "
                              "is false", stderr)


class TrustRenderTests(ChartFixture):
    """The trust surface's RBAC scoping and the ConfigMap delivery of the
    hook scripts and the shared preamble lib."""

    def test_no_cluster_admin_binding_renders_anywhere(self):
        renders = [
            self.render(),
            self.render({"certificates": {"clusterIssuer": "ca-issuer",
                                          "router": {"managed": True},
                                          "api": {"managed": True}}}),
            self.render({"ca": {"enabled": True, "trust": {"enabled": False}}}),
            self.render({"ca": {"enabled": False}}),
        ]
        for docs_ in renders:
            role_refs = [d.get("roleRef", {}).get("name")
                         for d in docs_
                         if d["kind"] in ("RoleBinding", "ClusterRoleBinding")]
            self.assertNotIn("cluster-admin", role_refs)

    def test_trust_rbac_is_resource_name_scoped(self):
        by_kind = self.by_kind(self.render())
        role = self.pick(by_kind, "Role", "trust-ca")
        self.assertEqual(role["metadata"]["namespace"], "cert-manager")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["cert-manager-ca"],
            "verbs": ["get"],
        }])
        binding = self.pick(by_kind, "RoleBinding", "trust-ca")
        self.assertEqual(binding["metadata"]["namespace"], "cert-manager")
        self.assertEqual(binding["roleRef"], {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "Role",
            "name": "trust-ca",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "trust-ca",
            "namespace": "cert-manager",
        }])
        cluster_role = self.pick(by_kind, "ClusterRole", "trust-ca")
        self.assertEqual(cluster_role["rules"], [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "resourceNames": ["user-ca-bundle"],
                "verbs": ["get", "patch", "delete"],
            },
            {
                # create cannot be resourceName-scoped in RBAC; the hook
                # must create the bundle when absent (README: unscoped-create
                # honesty)
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["create"],
            },
            {
                "apiGroups": ["config.openshift.io"],
                "resources": ["proxies"],
                "resourceNames": ["cluster"],
                "verbs": ["get", "patch"],
            },
        ])
        cluster_binding = self.pick(by_kind, "ClusterRoleBinding", "trust-ca")
        self.assertEqual(cluster_binding["roleRef"]["name"], "trust-ca")
        self.assertEqual(cluster_binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "trust-ca",
            "namespace": "cert-manager",
        }])

    def test_trust_configmap_delivers_scripts_and_libs(self):
        cm = self.pick(self.by_kind(self.render()),
                       "ConfigMap", "trust-ca-config")
        self.assertEqual(cm["metadata"]["namespace"], "cert-manager")
        self.assertEqual(
            sorted(cm["data"]),
            ["hook-preamble.sh", "trust-bundle.sh", "trust-ca-remove.sh",
             "trust-ca-set.sh"])
        preamble = cm["data"]["hook-preamble.sh"]
        self.assertIn("conditional_write", preamble)
        self.assertIn("create_object", preamble)
        self.assertIn("delete_object", preamble)
        # The PEM-trust program (canonicalizer, decomposition guard, and
        # the Secret/Proxy readers) is delivered once and sourced by both
        # hook scripts
        trust_lib = cm["data"]["trust-bundle.sh"]
        self.assertIn("TRUST_PEM_JQ_DEFS", trust_lib)
        self.assertIn("export TRUST_PEM_PY", trust_lib)
        self.assertIn("pem_guard", trust_lib)
        self.assertIn("secret_cert_block", trust_lib)
        for script in ("trust-ca-set.sh", "trust-ca-remove.sh"):
            self.assertIn('source "${BASH_SOURCE[0]%/*}/hook-preamble.sh"',
                          cm["data"][script])
            self.assertIn('source "${BASH_SOURCE[0]%/*}/trust-bundle.sh"',
                          cm["data"][script])
        self.assertIn("trust-ca-history", cm["data"]["trust-ca-set.sh"])
        self.assertIn("trust-ca-owns-proxy-reference",
                      cm["data"]["trust-ca-set.sh"])
        # The Secret/Proxy readers live in the lib; the scripts only call
        # them
        for script in ("trust-ca-set.sh", "trust-ca-remove.sh"):
            self.assertIn("secret_cert_block", cm["data"][script])
            self.assertNotIn("secret_cert_block() {", cm["data"][script])


class TrustFakeOcFixture(FakeOcScriptFixture):
    """Executes the rendered trust hook scripts against a fake oc binary;
    one canned object per cluster resource (Secret, Proxy, bundle)."""
    work_prefix = "cm-trust-"
    fake_oc_script = TRUST_FAKE_OC_SCRIPT
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort",
                 "date", "tr", "head"]

    def setUp(self):
        super().setUp()
        cm = self.pick(self.by_kind(self.render()),
                       "ConfigMap", "trust-ca-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh", "trust-bundle.sh",
                                     "trust-ca-set.sh", "trust-ca-remove.sh")}

    def secret_doc(self, cert_pem=CA_CERT, include_crt=True,
                   resource_version="1001"):
        data = {"tls.key": b64("key")}
        if include_crt:
            data["tls.crt"] = b64(cert_pem)
        return {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "kubernetes.io/tls",
            "metadata": {"name": "cert-manager-ca",
                         "namespace": "cert-manager",
                         "resourceVersion": resource_version},
            "data": data,
        }

    def proxy_doc(self, reference=None, resource_version="2001"):
        spec = {}
        if reference is not None:
            spec["trustedCA"] = {"name": reference}
        return {
            "apiVersion": "config.openshift.io/v1",
            "kind": "Proxy",
            "metadata": {"name": "cluster",
                         "resourceVersion": resource_version},
            "spec": spec,
        }

    def bundle_doc(self, content="", annotations=None,
                   resource_version="3001"):
        doc = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": BUNDLE_NAME,
                         "namespace": "openshift-config",
                         "resourceVersion": resource_version},
            "data": {"ca-bundle.crt": content},
        }
        if annotations:
            doc["metadata"]["annotations"] = annotations
        return doc

    def patches(self):
        """(resource, patch payload) pairs in call order."""
        return [tuple(line.split("\t", 1)) for line in self.patch_lines()]

    def patches_for(self, resource):
        return [payload for res, payload in self.patches()
                if res == resource]

    def create_payload(self):
        path = os.path.join(self.work, "create.log")
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def deleted(self):
        return any(line.startswith("delete ") for line in
                   self.log().splitlines())

    def run_script(self, script_name, secret_doc=None, proxy_doc=None,
                   bundle_doc=None, secret_scenario="ok", proxy_scenario="ok",
                   bundle_scenario="ok", patch_scenario="ok",
                   create_scenario="ok", delete_scenario="ok",
                   proxy_patch_scenario=None, bundle_patch_scenario=None,
                   include_jq=None, include_python=None):
        bin_dir = self.build_bin_dir(include_jq=include_jq,
                                     include_python=include_python)
        self.stage_scripts()
        docs_ = {"secret": secret_doc or self.secret_doc(),
                 "proxy": proxy_doc or self.proxy_doc(),
                 "bundle": bundle_doc or self.bundle_doc()}
        files = {}
        for key, doc in docs_.items():
            files[key] = os.path.join(self.work, f"{key}.json")
            with open(files[key], "w") as f:
                json.dump(doc, f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "TRUST_CA_TIMEOUT": "2",
            "TRUST_CA_POLL_INTERVAL": "0",
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_ENV": os.path.join(self.work, "env.log"),
            "OC_SECRET_FILE": files["secret"],
            "OC_PROXY_FILE": files["proxy"],
            "OC_BUNDLE_FILE": files["bundle"],
            "OC_SECRET_SCENARIO": secret_scenario,
            "OC_PROXY_SCENARIO": proxy_scenario,
            "OC_BUNDLE_SCENARIO": bundle_scenario,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_PATCH_SCENARIO": patch_scenario,
            "OC_ATTEMPTS": os.path.join(self.work, "attempts"),
            "OC_CREATE_LOG": os.path.join(self.work, "create.log"),
            "OC_CREATE_SCENARIO": create_scenario,
            "OC_DELETE_SCENARIO": delete_scenario,
        }
        if proxy_patch_scenario is not None:
            # The fake oc names per-resource overrides after the resource
            # slug it computes for the patch log
            env["OC_PATCH_SCENARIO_" +
                "proxies.config.openshift.io/cluster".replace("/", "_")
                .replace(".", "_")] = proxy_patch_scenario
        if bundle_patch_scenario is not None:
            env["OC_PATCH_SCENARIO_" +
                "configmap/user-ca-bundle".replace("/", "_")
                .replace(".", "_").replace("-", "_")] = bundle_patch_scenario
        return self.run_child(os.path.join(self.work, script_name), env)


class TrustSetScriptTests(TrustFakeOcFixture):
    """Script-execution coverage for trust-ca-set.sh: the D3 state machine
    (create-on-absent, merge-into-existing, equal no-op, ours-recorded
    pruning, adoption, foreign-reference STOP, zero-PEM STOP, missing-
    tls.crt FAIL, and the bounded 409 retry) on both JSON tool paths."""

    def test_child_environment_is_minimal_and_isolated(self):
        result = self.run_script("trust-ca-set.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        child = self.child_env()
        self.assertNotIn("CM_PARENT_SENTINEL", child)
        self.assertEqual(child["TRUST_CA_TIMEOUT"], "2")
        self.assertEqual(child["PATH"], os.path.join(self.work, "bin"))
        self.assertEqual(
            {k for k in child if k.startswith("OC_")},
            {"OC_LOG", "OC_ENV", "OC_SECRET_FILE", "OC_PROXY_FILE",
             "OC_BUNDLE_FILE", "OC_PATCH_LOG", "OC_ATTEMPTS",
             "OC_CREATE_LOG", "OC_SECRET_SCENARIO", "OC_PROXY_SCENARIO",
             "OC_BUNDLE_SCENARIO", "OC_PATCH_SCENARIO",
             "OC_CREATE_SCENARIO", "OC_DELETE_SCENARIO"})

    def test_create_on_absent_bundle(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh", bundle_scenario="not-found", **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(self.create_payload())
                self.assertEqual(manifest["metadata"]["name"], BUNDLE_NAME)
                self.assertEqual(manifest["metadata"]["namespace"],
                                 "openshift-config")
                self.assertEqual(manifest["data"]["ca-bundle.crt"], CA_CERT)
                # The created manifest carries only the history annotation:
                # ownership is claimed by a separate write AFTER the
                # reference write succeeds
                self.assertEqual(
                    manifest["metadata"]["annotations"],
                    {"trust-ca-history": history_annotation(CA_CERT)})
                # The reference was unset, so the hook takes ownership by
                # setting it through a conditional patch
                proxy_patches = self.patches_for(
                    "proxies.config.openshift.io/cluster")
                self.assertEqual(len(proxy_patches), 1)
                self.assertEqual(json.loads(proxy_patches[0]), {
                    "metadata": {"resourceVersion": "2002"},
                    "spec": {"trustedCA": {"name": BUNDLE_NAME}},
                })
                # ...and only then records the ownership annotation on the
                # bundle through its own conditional write (the fake serves
                # the created object at its canned resourceVersion)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                self.assertEqual(json.loads(bundle_patches[0]), {
                    "metadata": {
                        "resourceVersion": "3001",
                        "annotations":
                            {"trust-ca-owns-proxy-reference": "true"}},
                })

    def test_create_on_absent_bundle_without_claiming_foreign_reference(self):
        # The reference already points at the (missing) bundle — a third
        # party set it; creating the bundle does not confer ownership
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_scenario="not-found")
        self.assertEqual(result.returncode, 0, result.stderr)
        manifest = json.loads(self.create_payload())
        self.assertEqual(
            manifest["metadata"]["annotations"],
            {"trust-ca-history": history_annotation(CA_CERT)})
        self.assertEqual(self.patches(), [])

    def test_merges_into_existing_bundle(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    bundle_doc=self.bundle_doc(FOREIGN_CA_CERT), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 2)
                patch = json.loads(bundle_patches[0])
                self.assertEqual(patch["data"]["ca-bundle.crt"],
                                 FOREIGN_CA_CERT + CA_CERT)
                # The content write carries no ownership annotation: the
                # reference write below has not succeeded yet
                self.assertEqual(
                    patch["metadata"]["annotations"],
                    {"trust-ca-history": history_annotation(CA_CERT)})
                proxy_patches = self.patches_for(
                    "proxies.config.openshift.io/cluster")
                self.assertEqual(len(proxy_patches), 1)
                self.assertEqual(
                    json.loads(proxy_patches[0])["spec"]["trustedCA"],
                    {"name": BUNDLE_NAME})
                # Only after the reference write succeeded does the hook
                # record reference ownership on the bundle
                adopt = json.loads(bundle_patches[1])
                self.assertEqual(adopt, {
                    "metadata": {
                        "resourceVersion": "3003",
                        "annotations":
                            {"trust-ca-owns-proxy-reference": "true"}},
                })

    def test_equal_content_is_no_op(self):
        # Prior output: reference already points at the bundle, the bundle
        # holds a foreign block plus the current one, history is recorded —
        # nothing to write anywhere
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc(
                FOREIGN_CA_CERT + CA_CERT,
                {"trust-ca-history": history_annotation(CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertIsNone(self.create_payload())
        self.assertNotIn("create ", self.log())

    def test_current_block_in_place_is_a_no_op(self):
        # The current block is already present, ahead of a foreign block:
        # the hook must not reposition it to the tail or rewrite the
        # content at all — only the annotation and reference writes are
        # state changes, and here none applies
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc(
                CA_CERT + FOREIGN_CA_CERT,
                {"trust-ca-history": history_annotation(CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertIsNone(self.create_payload())
        self.assertIn("no changes needed", result.stdout)

    def test_prunes_recorded_blocks_that_are_no_longer_current(self):
        # A recorded predecessor block is pruned while foreign blocks and
        # their order are preserved, and the current block is appended
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc(
                OLD_CA_CERT + FOREIGN_CA_CERT,
                {"trust-ca-history": history_annotation(OLD_CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        self.assertEqual(len(bundle_patches), 1)
        patch = json.loads(bundle_patches[0])
        self.assertEqual(patch["data"]["ca-bundle.crt"],
                         FOREIGN_CA_CERT + CA_CERT)
        self.assertEqual(
            patch["metadata"]["annotations"],
            {"trust-ca-history": history_annotation(CA_CERT)})
        self.assertNotIn("trust-ca-owns-proxy-reference",
                         patch["metadata"]["annotations"])

    def test_prior_output_adoption(self):
        # Reference already points at the bundle and the bundle holds
        # exactly the current CA: claim reference ownership, change nothing
        # else
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
                    bundle_doc=self.bundle_doc(CA_CERT), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                patch = json.loads(bundle_patches[0])
                self.assertEqual(
                    patch,
                    {"metadata": {
                        "resourceVersion": "3002",
                        "annotations": {
                            "trust-ca-owns-proxy-reference": "true"}}})
                self.assertNotIn("data", patch)
                self.assertEqual(self.patches_for(
                    "proxies.config.openshift.io/cluster"), [])
                self.assertIsNone(self.create_payload())

    def test_foreign_reference_stops_without_writing(self):
        result = self.run_script(
            "trust-ca-set.sh",
            proxy_doc=self.proxy_doc(reference="corporate-ca"),
            bundle_doc=self.bundle_doc(FOREIGN_CA_CERT))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite a foreign reference", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertIsNone(self.create_payload())
        # The STOP decision needs exactly the Secret and Proxy reads
        self.assertEqual(len(self.log().splitlines()), 2)

    def test_zero_pem_block_content_stops(self):
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc("   \n"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("holds no PEM certificates", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_unparseable_content_stops(self):
        result = self.run_script(
            "trust-ca-set.sh", proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc("-----BEGIN CERTIFICATE-----\ngarbage\n"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatched PEM block markers", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_mixed_format_bundle_with_unparsed_region_stops(self):
        # A bundle mixing LF and CRLF blocks with an unparsed region: the
        # rewrite would silently drop everything the scan failed to
        # recognize, so the complete-decomposition guard stops the hook
        # instead of writing
        mixed = FOREIGN_CA_CERT + "not a certificate\n" + FOREIGN_CA_CRLF
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
                    bundle_doc=self.bundle_doc(mixed), **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unrecognized non-PEM regions", result.stderr)
                self.assertEqual(self.patches(), [])
                self.assertIsNone(self.create_payload())

    def test_crlf_byo_tls_crt_is_canonicalized(self):
        # A CRLF-encoded bring-your-own tls.crt parses (previously it
        # failed with a misleading no-certificate diagnostic) and is
        # written canonically as LF
        crlf_cert = CA_CERT.replace("\n", "\r\n")
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh", bundle_scenario="not-found",
                    secret_doc=self.secret_doc(cert_pem=crlf_cert), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                manifest = json.loads(self.create_payload())
                self.assertEqual(manifest["data"]["ca-bundle.crt"], CA_CERT)

    def test_foreign_crlf_block_is_preserved_verbatim(self):
        # A CRLF-encoded foreign block is recognized, kept byte-for-byte
        # (its line endings are never rewritten), and never repositioned
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
                    bundle_doc=self.bundle_doc(FOREIGN_CA_CRLF), **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                patch = json.loads(bundle_patches[0])
                self.assertEqual(patch["data"]["ca-bundle.crt"],
                                 FOREIGN_CA_CRLF + CA_CERT)
                self.assertNotIn("\r", patch["data"]["ca-bundle.crt"]
                                 [len(FOREIGN_CA_CRLF):])

    def test_failed_reference_write_leaves_no_ownership_annotation(self):
        # The ownership annotation rides its own write AFTER the reference
        # write succeeds: when that write fails, the bundle carries no
        # ownership record, so a reference a third party later sets is
        # never claimed (and never unset) by teardown
        for variant in self.tool_path_variants():
            with self.subTest(merge=variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    bundle_doc=self.bundle_doc(FOREIGN_CA_CERT),
                    proxy_patch_scenario="error", **variant)
                self.assertNotEqual(result.returncode, 0)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                self.assertNotIn("trust-ca-owns-proxy-reference",
                                 bundle_patches[0])
            with self.subTest(create=variant):
                result = self.run_script(
                    "trust-ca-set.sh", bundle_scenario="not-found",
                    proxy_patch_scenario="error", **variant)
                self.assertNotEqual(result.returncode, 0)
                manifest = json.loads(self.create_payload())
                self.assertNotIn("trust-ca-owns-proxy-reference",
                                 manifest["metadata"]["annotations"])
                self.assertEqual(
                    self.patches_for("configmap/user-ca-bundle"), [])

    def test_failed_ownership_write_reports_the_degraded_missing_bundle(self):
        # When the bundle vanishes after the reference write succeeded, the
        # ownership write finds it gone (NotFound): the reference points at
        # a missing bundle, which degrades cluster trust, but a re-run
        # recreates the bundle (a further run then records ownership), so
        # the diagnostic must promise that recovery and state the dangling
        # reference an uninstall before then leaves behind
        for variant in self.tool_path_variants():
            with self.subTest(merge=variant):
                result = self.run_script(
                    "trust-ca-set.sh",
                    bundle_doc=self.bundle_doc(FOREIGN_CA_CERT),
                    bundle_scenario="disappears", **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("disappeared mid-run", result.stderr)
                self.assertIn("degrades cluster trust", result.stderr)
                self.assertIn("re-running this hook recreates the bundle",
                              result.stderr)
                self.assertIn("dangling trustedCA reference", result.stderr)
                self.assertNotIn("surviving bundle contents", result.stderr)
                # The reference was set and the bundle content was merged
                # before the bundle vanished; only the ownership record is
                # missing
                self.assertEqual(len(self.patches_for(
                    "proxies.config.openshift.io/cluster")), 1)
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                self.assertNotIn("trust-ca-owns-proxy-reference",
                                 bundle_patches[0])

    def test_failed_ownership_write_reports_the_unowned_reference(self):
        # When the ownership-annotation write fails with a non-NotFound
        # error after the reference write succeeded, the bundle is intact
        # and the reference keeps functioning; a re-run records ownership
        # only when the bundle holds exactly the current CA, so uninstall
        # otherwise leaves the reference in place pointing at the surviving
        # bundle contents
        result = self.run_script(
            "trust-ca-set.sh",
            bundle_doc=self.bundle_doc(FOREIGN_CA_CERT),
            bundle_patch_scenario="error-on-second")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ownership record could not be written", result.stderr)
        self.assertIn("the bundle contents are intact", result.stderr)
        self.assertIn("holds exactly the current CA certificate", result.stderr)
        self.assertIn("pointing at the surviving bundle contents",
                      result.stderr)
        # The raw oc error from the failed write precedes the diagnostic
        # and is not repeated
        self.assertEqual(result.stderr.count("Forbidden"), 1)
        # The reference was set and the content merge succeeded; the second
        # bundle patch is the ownership attempt that failed
        self.assertEqual(len(self.patches_for(
            "proxies.config.openshift.io/cluster")), 1)
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        self.assertEqual(len(bundle_patches), 2)
        self.assertIn("trust-ca-owns-proxy-reference", bundle_patches[1])

    def test_missing_tls_crt_fails_before_any_write(self):
        result = self.run_script("trust-ca-set.sh",
                                 secret_doc=self.secret_doc(include_crt=False))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to install an empty", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertIsNone(self.create_payload())
        # The FAIL decision needs exactly the Secret read
        self.assertEqual(len(self.log().splitlines()), 1)

    def test_secret_wait_times_out_and_fails(self):
        # The bounded wait (poll interval 0, timeout 2) fails with its own
        # diagnostic when the CA Secret never appears
        result = self.run_script("trust-ca-set.sh",
                                 secret_scenario="not-found")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out", result.stderr)
        self.assertIn("cert-manager-ca", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_conflict_retry_is_bounded_and_rewrites(self):
        result = self.run_script(
            "trust-ca-set.sh", bundle_doc=self.bundle_doc(FOREIGN_CA_CERT),
            patch_scenario="conflict-then-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        # Two content attempts (the first rejected with 409) plus the
        # ownership-annotation write that follows the successful reference
        # write
        self.assertEqual(len(bundle_patches), 3)
        # The fake oc bumps the served resourceVersion on every GET, so the
        # failed attempt carried the stale version and the successful retry
        # carried the fresh one from its own re-read
        self.assertEqual(json.loads(bundle_patches[0])["metadata"]
                         ["resourceVersion"], "3002")
        self.assertEqual(json.loads(bundle_patches[1])["metadata"]
                         ["resourceVersion"], "3003")
        self.assertEqual(json.loads(bundle_patches[2])["metadata"]
                         ["resourceVersion"], "3004")
        self.assertEqual(
            json.loads(bundle_patches[2])["metadata"]["annotations"],
            {"trust-ca-owns-proxy-reference": "true"})
        self.assertIn("retrying", result.stderr)

    def test_conflict_exhaustion_fails(self):
        result = self.run_script(
            "trust-ca-set.sh", bundle_doc=self.bundle_doc(FOREIGN_CA_CERT),
            patch_scenario="conflict-exhausted")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.patches_for("configmap/user-ca-bundle")),
                         self.MAX_ATTEMPTS)
        self.assertIn("concurrently", result.stderr)

    def test_create_race_fails_for_the_job_retry_to_merge(self):
        result = self.run_script("trust-ca-set.sh",
                                 bundle_scenario="not-found",
                                 create_scenario="already-exists")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("created concurrently", result.stderr)
        self.assertEqual(self.patches(), [])

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("trust-ca-set.sh",
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class TrustRemoveScriptTests(TrustFakeOcFixture):
    """Script-execution coverage for trust-ca-remove.sh: the D4 state
    machine (unset-when-annotated, leave-when-foreign, current+history
    removal preserving foreign blocks, delete-on-empty, unreadable-Secret
    history-only path, and the bounded 409 retry)."""

    def annotated(self, content):
        return self.bundle_doc(content, {
            "trust-ca-owns-proxy-reference": "true",
            "trust-ca-history": history_annotation(CA_CERT),
        })

    def test_unsets_annotated_reference_and_removes_current_block(self):
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-remove.sh",
                    proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
                    bundle_doc=self.annotated(FOREIGN_CA_CERT + CA_CERT),
                    **variant)
                self.assertEqual(result.returncode, 0, result.stderr)
                proxy_patches = self.patches_for(
                    "proxies.config.openshift.io/cluster")
                self.assertEqual(len(proxy_patches), 1)
                self.assertEqual(json.loads(proxy_patches[0]), {
                    "metadata": {"resourceVersion": "2002"},
                    "spec": {"trustedCA": None},
                })
                bundle_patches = self.patches_for("configmap/user-ca-bundle")
                self.assertEqual(len(bundle_patches), 1)
                patch = json.loads(bundle_patches[0])
                self.assertEqual(patch["data"]["ca-bundle.crt"],
                                 FOREIGN_CA_CERT)
                self.assertEqual(
                    patch["metadata"]["annotations"],
                    {"trust-ca-history": None,
                     "trust-ca-owns-proxy-reference": None})
                self.assertFalse(self.deleted())

    def test_leaves_foreign_reference_in_place(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            proxy_doc=self.proxy_doc(reference="corporate-ca"),
            bundle_doc=self.bundle_doc(
                CA_CERT, {"trust-ca-history": history_annotation(CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nothing to unset", result.stdout)
        self.assertEqual(
            self.patches_for("proxies.config.openshift.io/cluster"), [])
        # The bundle cleanup still runs: our block goes, the emptied
        # ConfigMap is deleted
        self.assertTrue(self.deleted())
        self.assertEqual(self.patches(), [])

    def test_leaves_unowned_reference_set(self):
        # The reference points at the bundle but no ownership annotation
        # records it as this chart's — a third party set it
        result = self.run_script(
            "trust-ca-remove.sh",
            proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.bundle_doc(CA_CERT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("set outside this chart", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertTrue(self.deleted())

    def test_removes_current_and_history_blocks_preserving_foreign(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.annotated(LEGACY_CA_CERT + CA_CERT
                                      + FOREIGN_CA_CERT))
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        self.assertEqual(len(bundle_patches), 1)
        patch = json.loads(bundle_patches[0])
        # The never-recorded legacy block is foreign by classification and
        # stays, in its original position, next to the untouched foreign
        # block
        self.assertEqual(patch["data"]["ca-bundle.crt"],
                         LEGACY_CA_CERT + FOREIGN_CA_CERT)

    def test_deletes_emptied_bundle(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            bundle_doc=self.bundle_doc(
                CA_CERT, {"trust-ca-history": history_annotation(CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.deleted())
        self.assertIn("delete configmap/user-ca-bundle", self.log())
        self.assertEqual(self.patches(), [])

    def test_history_alone_identifies_when_secret_is_unreadable(self):
        result = self.run_script(
            "trust-ca-remove.sh", secret_scenario="error",
            bundle_doc=self.bundle_doc(
                FOREIGN_CA_CERT + OLD_CA_CERT,
                {"trust-ca-history": history_annotation(CA_CERT, OLD_CA_CERT)}))
        self.assertEqual(result.returncode, 0, result.stderr)
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        self.assertEqual(len(bundle_patches), 1)
        patch = json.loads(bundle_patches[0])
        self.assertEqual(patch["data"]["ca-bundle.crt"], FOREIGN_CA_CERT)

    def test_fails_safe_when_nothing_identifies_our_blocks(self):
        # The Secret is unreadable and no history annotation exists — the
        # hook cannot tell which blocks belong to this chart and must not
        # delete anything
        result = self.run_script("trust-ca-remove.sh", secret_scenario="error",
                                 bundle_doc=self.bundle_doc(FOREIGN_CA_CERT))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot identify", result.stderr)
        self.assertIn("leaving the bundle untouched", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_nothing_of_ours_is_a_successful_no_op(self):
        result = self.run_script(
            "trust-ca-remove.sh", bundle_doc=self.bundle_doc(FOREIGN_CA_CERT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No entries belonging to this chart", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_bundle_absent_is_a_no_op(self):
        result = self.run_script("trust-ca-remove.sh",
                                 proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
                                 bundle_scenario="not-found")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("absent; nothing to remove", result.stdout)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_proxy_absent_still_cleans_bundle(self):
        result = self.run_script("trust-ca-remove.sh",
                                 proxy_scenario="not-found",
                                 bundle_doc=self.bundle_doc(CA_CERT))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.deleted())

    def test_zero_pem_content_is_left_untouched(self):
        result = self.run_script("trust-ca-remove.sh",
                                 bundle_doc=self.bundle_doc("   \n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_unparseable_content_fails_safe(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            bundle_doc=self.bundle_doc("-----BEGIN CERTIFICATE-----\nx\n"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mismatched PEM block markers", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(self.deleted())

    def test_mixed_format_bundle_is_left_untouched(self):
        # A mixed LF/CRLF bundle with an unparsed region: the classifier
        # would have returned delete for such content (removing the
        # recognized current block and dropping the rest with the emptied
        # ConfigMap), so the complete-decomposition guard fails safe —
        # nothing is patched or deleted
        mixed = CA_CERT + "not a certificate\n" + FOREIGN_CA_CRLF
        for variant in self.tool_path_variants():
            with self.subTest(**variant):
                result = self.run_script(
                    "trust-ca-remove.sh", bundle_doc=self.bundle_doc(mixed),
                    **variant)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unrecognized non-PEM regions", result.stderr)
                self.assertEqual(self.patches(), [])
                self.assertFalse(self.deleted())

    def test_conflict_retry_is_bounded(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.annotated(FOREIGN_CA_CERT + CA_CERT),
            patch_scenario="conflict-then-success")
        self.assertEqual(result.returncode, 0, result.stderr)
        proxy_patches = self.patches_for(
            "proxies.config.openshift.io/cluster")
        self.assertEqual(len(proxy_patches), 2)
        self.assertEqual(json.loads(proxy_patches[0])["metadata"]
                         ["resourceVersion"], "2002")
        self.assertEqual(json.loads(proxy_patches[1])["metadata"]
                         ["resourceVersion"], "2003")
        bundle_patches = self.patches_for("configmap/user-ca-bundle")
        self.assertEqual(len(bundle_patches), 2)
        self.assertIn("retrying", result.stderr)

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("trust-ca-remove.sh",
                                 include_jq=False, include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class ValuesContractTests(unittest.TestCase):
    """Static checks on the shipped values: the default stays a deliberate
    validation trap, and the documented examples match what renders."""

    @staticmethod
    def load(path):
        with open(path) as f:
            return yaml.safe_load(f)

    def test_default_cluster_issuer_is_zerossl(self):
        values = self.load(os.path.join(CHART, "values.yaml"))
        self.assertEqual(values["certificates"]["clusterIssuer"], "zerossl")
        self.assertEqual(values["acme"], {})
        for mode in ("api", "router", "nlbRouter"):
            self.assertIs(values["certificates"][mode]["managed"], False)

    def test_managed_defaults_are_false(self):
        values = self.load(os.path.join(CHART, "values.yaml"))
        for mode in ("api", "router", "nlbRouter"):
            self.assertIs(values["certificates"][mode]["managed"], False)

    def test_ca_defaults(self):
        values = self.load(os.path.join(CHART, "values.yaml"))
        self.assertIs(values["ca"]["enabled"], True)
        self.assertEqual(values["ca"]["duration"], "87600h")
        self.assertIs(values["ca"]["trust"]["enabled"], True)
        self.assertEqual(values["ca"]["trustTimeoutSeconds"], 120)


if __name__ == "__main__":
    unittest.main()
