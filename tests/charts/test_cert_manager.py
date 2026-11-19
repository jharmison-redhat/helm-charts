"""Focused tests for the cert-manager chart: the default render's core
object inventory (CA family, trust surface with its resourceName-scoped
RBAC, and the CertManager converge surface), the platform-objects-never-
render contract, one representative render-failure trap, the managed
serving-certificate surface, and the hook-script smoke set (one happy-path
run per script plus the chart's fail-closed no-tool check).

Render tests run the chart through ``helm template`` (cert-manager has no
chart dependencies, so no stubs or registry access are needed). The smoke
runs execute the rendered hook scripts against a fake ``oc`` on a
python3-only restricted PATH built entirely from symlinks, so neither a
real ``oc`` nor the test host's ambient tools can leak into the run.
State-machine behavior (409 retries, foreign-value STOP, walk-back) is
proven by live-cluster validation, not fixtures.
"""

import base64
import copy
import json
import os
import unittest

import support

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

ROUTER_DEFAULT_CERT = "router-cert"
API_SECRET_NAME = "openshift-api"

OVERRIDE_ARGS = [
    "--dns01-recursive-nameservers=9.9.9.9:53,1.1.1.1:53",
    "--dns01-recursive-nameservers-only",
    "--enable-certificate-owner-ref=false",
]

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


class ChartFixture(support.ChartFixture):
    chart = CHART
    tmp_prefix = "cm-test-"
    values_prefix = "cm-values-"

    def managed_render(self, modes=()):
        values = {
            "acme": {"letsencrypt": copy.deepcopy(LE_ACME)},
            "certificates": {"clusterIssuer": "letsencrypt"},
            "global": {"cluster": {"name": "c", "baseDomain": "example.com"}},
        }
        for mode in modes:
            values["certificates"][mode] = {"managed": True}
        return self.by_kind(self.render(values))


class DefaultRenderTests(ChartFixture):
    """The default render's core object inventory: the CA family, the full
    trust surface, and the CertManager converge surface, with no
    serving-certificate surface at all."""

    def test_default_render_inventory(self):
        by_kind = self.by_kind(self.render())
        # CA chain: a stable manually-managed root (explicit duration and
        # rotationPolicy), self-signed issuer, CA Certificate, ca-issuer.
        self.assertEqual(
            [i["metadata"]["name"] for i in by_kind["ClusterIssuer"]],
            ["selfsigned-issuer", "ca-issuer"])
        cert = self.pick(by_kind, "Certificate", "selfsigned-ca")
        self.assertEqual(cert["spec"]["duration"], "87600h")
        self.assertEqual(cert["spec"]["privateKey"]["rotationPolicy"], "Never")
        self.assertNotIn("Secret", by_kind)
        # Object inventory by kind (the serving surface is absent by
        # default).
        self.assertEqual(
            [j["metadata"]["name"] for j in by_kind.get("Job", [])],
            ["converge-cert-manager", "trust-ca-remove", "trust-ca-set"])
        self.assertEqual(
            [c["metadata"]["name"] for c in by_kind.get("ConfigMap", [])],
            ["converge-cert-manager-config", "trust-ca-config"])
        # The trust RBAC is resourceName-scoped; the hook Jobs are
        # post-install,post-upgrade (set) and pre-delete (remove) hooks.
        role = self.pick(by_kind, "Role", "trust-ca")
        self.assertEqual(role["metadata"]["namespace"], "cert-manager")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""],
            "resources": ["secrets"],
            "resourceNames": ["cert-manager-ca"],
            "verbs": ["get"],
        }])
        set_job = self.pick(by_kind, "Job", "trust-ca-set")
        self.assertEqual(
            set_job["metadata"]["annotations"]["helm.sh/hook"],
            "post-install,post-upgrade")
        remove_job = self.pick(by_kind, "Job", "trust-ca-remove")
        self.assertEqual(
            remove_job["metadata"]["annotations"]["helm.sh/hook"], "pre-delete")

    def test_platform_objects_never_render(self):
        # Managed or not, the chart never manifests the platform-owned
        # IngressController or APIServer objects — nor the operator-created
        # CertManager singleton — as release resources.
        for by_kind in (self.by_kind(self.render()),
                        self.managed_render(modes=("router",)),
                        self.managed_render(modes=("api",)),
                        self.managed_render(modes=("api", "router"))):
            self.assertNotIn("IngressController", by_kind)
            self.assertNotIn("APIServer", by_kind)
            self.assertNotIn("CertManager", by_kind)


class ManagedSurfaceTests(ChartFixture):
    """With a mode managed, its hook Job, script ConfigMap, ServiceAccount,
    and resourceName-scoped grants render; the hook surface pins
    cert-manager (the release namespace is free)."""

    def test_managed_modes_render_their_surfaces(self):
        by_kind = self.managed_render(modes=("api", "router"))
        for mode, job_name in (("router", "configure-router-certificate"),
                               ("api", "configure-api-certificate")):
            job = self.pick(by_kind, "Job", job_name)
            self.assertEqual(job["metadata"]["namespace"], "cert-manager")
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook"],
                "post-install,post-upgrade")
            self.assertEqual(
                job["metadata"]["annotations"]["helm.sh/hook-delete-policy"],
                "before-hook-creation")
        cm = self.pick(by_kind, "ConfigMap", "serving-certs-config")
        for key in ("configure-router-cert.sh", "configure-api-cert.sh",
                    "router-reference.json", "api-reference.json"):
            self.assertIn(key, cm["data"])
        # The router grant is resourceName-scoped in the ingress operator
        # namespace; the api grant is cluster-scoped and resource-named.
        router_role = self.pick(by_kind, "Role", "serving-certs-router")
        self.assertEqual(router_role["rules"], [{
            "apiGroups": ["operator.openshift.io"],
            "resources": ["ingresscontrollers"],
            "resourceNames": ["default"],
            "verbs": ["get", "patch"],
        }])
        api_role = self.pick(by_kind, "ClusterRole", "serving-certs-api")
        self.assertEqual(api_role["rules"], [{
            "apiGroups": ["config.openshift.io"],
            "resources": ["apiservers"],
            "resourceNames": ["cluster"],
            "verbs": ["get", "patch"],
        }])
        # The managed certificates use the selected issuer and pin their
        # platform namespaces.
        router = self.pick(by_kind, "Certificate", "router-default")
        self.assertEqual(router["spec"]["secretName"], ROUTER_DEFAULT_CERT)
        self.assertEqual(router["metadata"]["namespace"], "openshift-ingress")
        api = self.pick(by_kind, "Certificate", "openshift-api")
        self.assertEqual(api["spec"]["secretName"], API_SECRET_NAME)
        self.assertEqual(api["spec"]["issuerRef"]["name"], "letsencrypt")

    def test_managed_without_any_issuer_fails(self):
        values = {
            "certificates": {"clusterIssuer": "zerossl",
                             "router": {"managed": True}},
        }
        self.render_failure(values)


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


class FakeOcScriptFixture(ChartFixture, support.FakeOcScriptFixture):
    """Restricted-PATH harness for the cert-manager hook scripts; the
    canned oc program is set per subclass (the serving-cert fake and the
    multi-resource trust fake differ)."""

    work_prefix = "cm-script-"


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
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1597"},
            "spec": {"servingCerts": {"namedCertificates": [self.api_ref]}},
        })


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
        self.clear_run_state("create.log", "create.log.done")
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
        # The chart's fail-closed no-tool check.
        result = self.run_script("trust-ca-set.sh",
                                 include_python=False)
        self.assertNotEqual(result.returncode, 0)
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


# Fake oc for the converge hook smoke run: answers the CRD-establishment
# jsonpath check, serves a canned CertManager object for get (bumping the
# served metadata.resourceVersion on every get), and records every --patch=
# payload. Every invocation is logged.
CONVERGE_FAKE_OC_SCRIPT = """\
#!/bin/bash
echo "$@" >> "$OC_LOG"
case "$1" in
  get)
    case "$*" in
      *"crd/"*)
        echo "True"
        exit 0 ;;
      *"certmanagers.operator.openshift.io/cluster"*)
        rv_file="$OC_OBJECT_FILE.rv"
        if [[ -f "$rv_file" ]]; then
          next_rv=$(($(cat "$rv_file") + 1))
        else
          next_rv=$(grep -oE '"resourceVersion": *"[0-9]+"' "$OC_OBJECT_FILE" \\
            | grep -oE '[0-9]+')
        fi
        echo "$next_rv" > "$rv_file"
        sed "s/\\"resourceVersion\\": *\\"[0-9]*\\"/\\"resourceVersion\\": \\"$next_rv\\"/" \\
          "$OC_OBJECT_FILE" ;;
      *)
        echo "unexpected oc get: $*" >&2
        exit 64 ;;
    esac ;;
  patch)
    for arg in "$@"; do
      if [[ "$arg" == --patch=* ]]; then
        printf '%s\\n' "${arg#--patch=}" >> "$OC_PATCH_LOG"
      fi
    done
    exit 0 ;;
  *)
    echo "unexpected oc invocation: $*" >&2
    exit 64 ;;
esac
"""


class ConvergeScriptTests(ChartFixture, support.FakeOcScriptFixture):
    """Happy-path smoke for the rendered converge-cert-manager script: the
    operator-created singleton present with a drifted overrideArgs converges
    to one merge patch carrying the live resourceVersion, and nothing is
    created or deleted."""
    work_prefix = "cm-converge-"
    bin_tools = ["mktemp", "grep", "cat", "sed", "sleep", "rm", "env", "sort",
                 "date"]
    fake_oc_script = CONVERGE_FAKE_OC_SCRIPT

    def setUp(self):
        super().setUp()
        cm = self.pick(self.by_kind(self.render()), "ConfigMap",
                       "converge-cert-manager-config")
        self.scripts = {name: cm["data"][name]
                        for name in ("hook-preamble.sh",
                                     "converge-cert-manager.sh")}

    def object_doc(self, override_args, resource_version="1597"):
        return {
            "apiVersion": "operator.openshift.io/v1alpha1",
            "kind": "CertManager",
            "metadata": {"name": "cluster",
                         "resourceVersion": resource_version},
            "spec": {"controllerConfig": {"overrideArgs": override_args},
                     "managementState": "Managed",
                     "logLevel": "Normal"},
        }

    def run_script(self, object_doc, include_python=True):
        bin_dir = self.build_bin_dir(include_python=include_python)
        self.stage_scripts()
        object_file = os.path.join(self.work, "object.json")
        with open(object_file, "w") as f:
            json.dump(object_doc, f)
        self.clear_run_state()
        env = {
            "PATH": bin_dir,
            "HOME": self.work,
            "WAIT_TIMEOUT": "2",
            "POLL_INTERVAL": "0",
            "CERT_MANAGER_ARGS": os.path.join(self.work, "args.json"),
            "OC_LOG": os.path.join(self.work, "commands.log"),
            "OC_OBJECT_FILE": object_file,
            "OC_PATCH_LOG": os.path.join(self.work, "patches.log"),
        }
        with open(env["CERT_MANAGER_ARGS"], "w") as f:
            json.dump({"spec": {"controllerConfig": {"overrideArgs":
                                                     OVERRIDE_ARGS}}}, f)
        return self.run_child(os.path.join(self.work, "converge-cert-manager.sh"),
                              env)

    def test_drifted_override_args_converge(self):
        result = self.run_script(
            self.object_doc(["--enable-certificate-owner-ref=true"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        patches = self.patches()
        self.assertEqual(len(patches), 1)
        # The fake bumps the served resourceVersion on every get: the
        # bounded wait's get served 1597, the conditional write's fresh
        # read served 1598
        self.assertEqual(patches[0], {
            "metadata": {"resourceVersion": "1598"},
            "spec": {"controllerConfig": {"overrideArgs": OVERRIDE_ARGS}},
        })
        # The converge neither creates nor deletes the operator-owned
        # object
        self.assertNotIn("create", self.log())
        self.assertNotIn("delete", self.log())


class HookGuardTests(ChartFixture):
    """Every hook Job container carries the guard-without-pin
    securityContext: the four restricted-pod guards and no
    runAsUser/runAsGroup anywhere in the pod — restricted SCCs assign the
    namespace range UID to UID-less pods, and a pin would fight that
    platform assignment."""

    def test_every_hook_job_is_guarded_without_pin(self):
        by_kind = self.managed_render(modes=("router", "api"))
        jobs = support.hook_jobs(
            [d for docs_ in by_kind.values() for d in docs_])
        self.assertEqual(
            sorted(j["metadata"]["name"] for j in jobs),
            ["configure-api-certificate", "configure-router-certificate",
             "converge-cert-manager", "trust-ca-remove", "trust-ca-set"])
        for job in jobs:
            support.assert_guard_without_pin(
                self, job["spec"]["template"]["spec"])


if __name__ == "__main__":
    unittest.main()
