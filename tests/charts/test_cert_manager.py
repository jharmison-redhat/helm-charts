"""Focused tests for the cert-manager chart. The module covers the
render-time issuer validation surface (managed certificates must name a
rendered ClusterIssuer, BYO CA keypairs must be complete, and every ACME
entry must carry a server, an email, and only well-formed solvers), the
ACME ClusterIssuer render shapes for the Let's Encrypt and ZeroSSL
examples, the serving-certificate hook surface (the platform
IngressController/default and APIServer/cluster objects are never owned —
hook Jobs write only this chart's reference with a resourceVersion-
conditional read-modify-write and stop on foreign references), the
operator controller overrideArgs, and the untouched CA family.

Render tests run the chart through ``helm template`` (cert-manager has no
chart dependencies, so no stubs or registry access are needed). Script
tests run the rendered hook scripts against a fake ``oc`` on a restricted
PATH built entirely from symlinks, so neither a real ``oc`` nor the test
host's ambient tools can leak into the run.
"""

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
    """The default render keeps the CA family exactly as before this
    change: self-signed issuer, CA Certificate, ca-issuer, and the trust
    hook, with no serving-certificate surface at all."""

    def default_render(self):
        return self.by_kind(self.render())

    def test_ca_chain_shape_is_unchanged(self):
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
                "secretName": "cert-manager-ca",
                "privateKey": {"algorithm": "ECDSA", "size": 256},
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

    def test_trust_hook_is_unchanged(self):
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
            "name": "cluster-admin",
        })
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount",
            "name": "trust-ca",
            "namespace": "cert-manager",
        }])
        job = self.pick(by_kind, "Job", "trust-cert-manager-ca")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        self.assertEqual(
            job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
            "before-hook-creation")
        self.assertEqual(job["spec"]["backoffLimit"], 3)
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 150)
        pod = job["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "trust-ca")
        container = pod["containers"][0]
        self.assertEqual(container["image"],
                         "image-registry.openshift-image-registry.svc:5000"
                         "/openshift/tools:latest")
        self.assertIn("secret cert-manager-ca -n cert-manager",
                      container["args"][1])
        self.assertIn("oc patch proxy cluster", container["args"][1])

    def test_default_renders_no_serving_cert_surface(self):
        by_kind = self.default_render()
        for kind in ("Role", "RoleBinding"):
            self.assertNotIn(kind, by_kind)
        self.assertEqual(
            [d["metadata"]["name"] for d in by_kind.get("ClusterRole", [])],
            [])
        self.assertEqual(
            [b["metadata"]["name"] for b in by_kind.get("ClusterRoleBinding", [])],
            ["trust-ca"])
        self.assertEqual(
            [s["metadata"]["name"] for s in by_kind.get("ServiceAccount", [])],
            ["trust-ca"])
        self.assertEqual(
            [j["metadata"]["name"] for j in by_kind.get("Job", [])],
            ["trust-cert-manager-ca"])
        self.assertNotIn("ConfigMap", by_kind)

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
        # Only the trust-ca CA-family ClusterRole binding renders in router
        # mode — the API-mode grants stay off.
        self.assertNotIn("ClusterRole", by_kind)
        self.assertEqual(
            [b["metadata"]["name"] for b in by_kind.get("ClusterRoleBinding", [])],
            ["trust-ca"])
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        self.assertIn("configure-router-cert.sh", cm["data"])
        self.assertIn("router-reference.json", cm["data"])
        self.assertNotIn("configure-api-cert.sh", cm["data"])
        self.assertNotIn("api-reference.json", cm["data"])

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
            self.assertIn("jq", text)
            self.assertIn("python3", text)
            self.assertIn("neither jq nor python3", text)
            self.assertIn("(Conflict)", text)
            self.assertIn("foreign reference", text)
        self.assertIn("defaultCertificate", cm["data"]["configure-router-cert.sh"])
        self.assertIn("namedCertificates",
                      cm["data"]["configure-api-cert.sh"])


class HookScriptTests(ChartFixture):
    """Executes the rendered serving-certificate hook scripts against a
    fake oc binary.

    Each run gets a restricted PATH built solely from symlinks in an
    isolated bin directory (the JSON tool chosen per test path, core
    utilities, and the fake oc), so no ambient host tool or real oc can be
    reached. The child environment is an explicit minimal set — no parent
    variables are inherited."""
    MAX_ATTEMPTS = 5

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix="cm-script-")
        self.addCleanup(shutil.rmtree, self.work)
        cm = self.pick(self.managed_render(modes=("api", "router")),
                       "ConfigMap", "serving-certs-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("configure-router-cert.sh",
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

    def build_bin_dir(self, include_jq=None, include_python=None):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        if include_jq is None:
            include_jq = shutil.which("jq") is not None
        if include_python is None:
            include_python = shutil.which("python3") is not None
        tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort"]
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
            f.write(FAKE_OC_SCRIPT)
        os.chmod(fake_oc, 0o755)
        return bin_dir

    def run_script(self, script_name, scenario, object_doc,
                   include_jq=None, include_python=None, reference_file=None):
        bin_dir = self.build_bin_dir(include_jq=include_jq,
                                     include_python=include_python)
        script_path = os.path.join(self.work, script_name)
        with open(script_path, "w") as f:
            f.write(self.scripts[script_name])
        if reference_file is None:
            reference_name = ("router-reference.json"
                              if script_name == "configure-router-cert.sh"
                              else "api-reference.json")
            reference_file = self.write_reference(reference_name, self.current_ref)
        get_file = os.path.join(self.work, "object.json")
        with open(get_file, "w") as f:
            json.dump(object_doc, f)
        # Each run is self-contained: drop logs and the fake oc's
        # resourceVersion counter left by any earlier run.
        for stale in ("commands.log", "env.log", "patches.log", "attempts",
                      get_file + ".rv"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
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
        with mock.patch.dict(os.environ, {"CM_PARENT_SENTINEL": "must-not-leak"}):
            return subprocess.run(
                ["/bin/bash", script_path], capture_output=True, text=True,
                env=env, timeout=120)

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

    def patches(self):
        path = os.path.join(self.work, "patches.log")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f.read().splitlines() if line]

    def tool_path_variants(self):
        """Both JSON tool paths the hook scripts can take; the jq path is
        included only when the host provides jq."""
        variants = [{"include_jq": False, "include_python": True}]
        if shutil.which("jq"):
            variants.append({"include_jq": True, "include_python": False})
        return variants


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
            "get ingresscontrollers.operator.openshift.io/default "
            "-n openshift-ingress-operator -o json --request-timeout=30s")

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


if __name__ == "__main__":
    unittest.main()
