"""Focused tests for the cert-manager chart. The module covers the
render-time issuer validation surface (managed certificates must name a
rendered ClusterIssuer, BYO CA keypairs must be complete and only set while
the CA family is enabled, and every ACME entry must carry a server, an
email, and only well-formed solvers), the ACME ClusterIssuer render shapes
for the Let's Encrypt and ZeroSSL examples, the trust gate matrix over
`ca.enabled` × `ca.trust.enabled` × self-provisioned/BYO, the stable-CA
render shape (explicit duration and rotationPolicy), the resourceName-scoped
trust RBAC, and the serving-certificate hook surface (the platform
IngressController/default and APIServer/cluster objects are never owned —
hook Jobs write only this chart's reference with a resourceVersion-
conditional read-modify-write).

Render tests run the chart through ``helm template`` (cert-manager has no
chart dependencies, so no stubs or registry access are needed). Per the
repo's unit-test policy the script-execution layer is a minimal smoke set:
ONE happy-path run per hook script (on a python3-only restricted PATH built
entirely from symlinks, so neither a real ``oc`` nor the test host's ambient
tools can leak into the run) and ONE fail-closed no-tool check for the
chart. State-machine behavior (409 retries, foreign-value STOP, walk-back)
is proven by live-cluster validation, not fixtures.
"""

import base64
import copy
import json
import os
import shutil
import subprocess
import tempfile
import unittest

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

# Fake oc for the serving-cert hook smoke runs: serves a canned object for
# get (bumping the served metadata.resourceVersion on every get, so a retry
# could only patch successfully with a version from a fresh read) and
# records every --patch= payload. Every invocation is logged.
FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
if [[ "$1" == "get" ]]; then
  rv_file="$OC_GET_FILE.rv"
  if [[ -f "$rv_file" ]]; then
    next_rv=$(($(cat "$rv_file") + 1))
  else
    next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_GET_FILE" | grep -oE '[0-9]+')
  fi
  echo "$next_rv" > "$rv_file"
  sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" "$OC_GET_FILE"
elif [[ "$1" == "patch" ]]; then
  for arg in "$@"; do
    if [[ "$arg" == --patch=* ]]; then
      printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
    fi
  done
  exit 0
else
  echo "unexpected oc invocation: $*" >&2
  exit 64
fi
"""

# Fake oc for the trust hook smoke runs: serves a canned object per
# resource (Secret, Proxy, bundle ConfigMap), logs patch payloads tagged
# with the patched resource, and logs created objects. GETs bump the served
# metadata.resourceVersion per resource (first GET serves the canned value,
# each later GET serves it incremented). The bundle serves NotFound until a
# create has completed, mirroring the hook's create-then-own sequence.
TRUST_FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
case "$1" in
  get)
    case "$*" in
      *"cert-manager-ca"*)
        f="$OC_SECRET_FILE" ;;
      *"proxies.config.openshift.io/cluster"*)
        f="$OC_PROXY_FILE" ;;
      *"user-ca-bundle"*)
        f="$OC_BUNDLE_FILE"
        if [[ "${OC_BUNDLE_SCENARIO:-ok}" == "not-found" &&
              ! -f "$OC_CREATE_LOG.done" ]]; then
          echo 'Error from server (NotFound): "object" not found' >&2
          exit 1
        fi ;;
      *)
        echo "unexpected oc get: $*" >&2
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
    exit 0 ;;
  create)
    cat > "$OC_CREATE_LOG"
    : > "$OC_CREATE_LOG.done"
    exit 0 ;;
  *)
    echo "unexpected oc invocation: $*" >&2
    exit 64 ;;
esac
"""

# Canonical PEM blocks used as stand-in certificates; the hooks classify by
# byte-equality on the block text, so the inner content only needs to be
# distinct between blocks.
CA_CERT = "-----BEGIN CERTIFICATE-----\nQ1VSUkVOVA==\n-----END CERTIFICATE-----\n"
FOREIGN_CA_CERT = "-----BEGIN CERTIFICATE-----\nRk9SRUlHTg==\n-----END CERTIFICATE-----\n"
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
    isolated bin directory (the requested JSON tool, core utilities, and
    the fake oc), so no ambient host tool or real oc can be reached. The
    child environment is an explicit minimal set — no parent variables are
    inherited. Subclasses supply the fake oc program, the rendered scripts
    dict, and their canned docs."""
    work_prefix = "cm-script-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort"]
    fake_oc_script = None

    def setUp(self):
        super().setUp()
        self.work = tempfile.mkdtemp(prefix=self.work_prefix)
        self.addCleanup(shutil.rmtree, self.work)

    def build_bin_dir(self, include_python=True):
        bin_dir = os.path.join(self.work, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        for name in os.listdir(bin_dir):
            os.unlink(os.path.join(bin_dir, name))
        tools = list(self.bin_tools)
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

    def log(self):
        with open(os.path.join(self.work, "commands.log")) as f:
            return f.read()

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
        for stale in ("commands.log", "patches.log", "create.log",
                      "create.log.done"):
            stale_path = os.path.join(self.work, stale)
            if os.path.exists(stale_path):
                os.unlink(stale_path)
        for stale in os.listdir(self.work):
            if stale.endswith(".rv"):
                os.unlink(os.path.join(self.work, stale))

    def run_child(self, script_path, env):
        return subprocess.run(
            ["/bin/bash", script_path], capture_output=True, text=True,
            env=env, timeout=120)


class HookScriptTests(FakeOcScriptFixture):
    """Smoke plumbing for the rendered serving-certificate hook scripts
    against a fake oc binary; one canned platform object per run."""
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

    def run_script(self, script_name, object_doc, reference_file=None):
        # Single tool path for the smoke set: python3-only restricted PATH.
        bin_dir = self.build_bin_dir()
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
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_GET_FILE": get_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
        }
        return self.run_child(script_path, env)

    def patches(self):
        return [json.loads(line) for line in self.patch_lines()]


class RouterScriptTests(HookScriptTests):
    """Happy-path smoke for configure-router-cert.sh: an unset platform
    defaultCertificate is written through one conditional patch."""

    def setUp(self):
        super().setUp()
        self.current_ref = self.router_ref

    def test_unset_default_certificate_is_written(self):
        result = self.run_script("configure-router-cert.sh",
                                 self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log().splitlines()[0],
            "get -n openshift-ingress-operator "
            "ingresscontrollers.operator.openshift.io/default "
            "-o json --request-timeout=30s")
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"defaultCertificate": {"name": ROUTER_DEFAULT_CERT}},
        })


class ApiScriptTests(HookScriptTests):
    """Happy-path smoke for configure-api-cert.sh: an absent platform
    namedCertificates list is written through one conditional patch."""

    def setUp(self):
        super().setUp()
        self.current_ref = self.api_ref

    def test_absent_list_is_written(self):
        result = self.run_script("configure-api-cert.sh", self.object_doc({}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.log().splitlines()[0],
            "get apiservers.config.openshift.io/cluster -o json "
            "--request-timeout=30s")
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"servingCerts": {"namedCertificates": [self.api_ref]}},
        })


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

    def run_script(self, script_name, secret_doc=None, proxy_doc=None,
                   bundle_doc=None, bundle_scenario="ok",
                   include_python=True):
        # Single tool path for the smoke set: python3-only restricted PATH.
        bin_dir = self.build_bin_dir(include_python=include_python)
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
            "OC_SECRET_FILE": files["secret"],
            "OC_PROXY_FILE": files["proxy"],
            "OC_BUNDLE_FILE": files["bundle"],
            "OC_BUNDLE_SCENARIO": bundle_scenario,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
            "OC_CREATE_LOG": os.path.join(self.work, "create.log"),
        }
        return self.run_child(os.path.join(self.work, script_name), env)


class TrustSetScriptTests(TrustFakeOcFixture):
    """Happy-path smoke for trust-ca-set.sh (the hook creates the bundle
    when absent, sets the Proxy reference, and only then records reference
    ownership) plus the chart's fail-closed no-tool check."""

    def test_create_on_absent_bundle(self):
        result = self.run_script("trust-ca-set.sh", bundle_scenario="not-found")
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

    def test_missing_json_tool_fails_before_any_cluster_call(self):
        result = self.run_script("trust-ca-set.sh",
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("neither jq nor python3", result.stderr)
        self.assertEqual(self.patches(), [])
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "commands.log")),
            "oc must not be invoked when no JSON tool is available")


class TrustRemoveScriptTests(TrustFakeOcFixture):
    """Happy-path smoke for trust-ca-remove.sh: an annotated-owned
    reference is unset and the current block removed while foreign blocks
    are preserved."""

    def annotated(self, content):
        return self.bundle_doc(content, {
            "trust-ca-owns-proxy-reference": "true",
            "trust-ca-history": history_annotation(CA_CERT),
        })

    def test_unsets_annotated_reference_and_removes_current_block(self):
        result = self.run_script(
            "trust-ca-remove.sh",
            proxy_doc=self.proxy_doc(reference=BUNDLE_NAME),
            bundle_doc=self.annotated(FOREIGN_CA_CERT + CA_CERT))
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
        # The bundle still carries foreign content, so the remove hook
        # must not delete the ConfigMap — the walk-back only applies to
        # hooks that emptied (or created) it.
        self.assertNotIn("delete ", self.log(),
                         "remove hook must not delete a non-emptied bundle")


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
