cert-manager
============

A chart to configure cert-manager on OpenShift and manage its issuer surface: a self-provisioned CA chain (self-signed
issuer → CA Certificate → `cert-manager-ca` Secret → `ca-issuer` ClusterIssuer), optional ACME ClusterIssuers (Let's
Encrypt, ZeroSSL, or any ACME provider), and optional management of platform certificates (API server, default
IngressController, NLB router).

Prerequisites
-------------

The cert-manager Operator for Red Hat OpenShift is NOT installed by this chart. It must already be installed before this
chart is deployed — the chart renders custom resources directly (`CertManager`, ClusterIssuers, Certificates), and a
plain `helm install` fails on the missing CRDs otherwise. Use the `install-operators` chart for the operator install:

```yaml
# install-operators values
operators:
  openshift-cert-manager-operator:
    channel: stable-v1
    namespace: cert-manager-operator
    operatorGroup:
      enabled: true # AllNamespaces scope — no targetNamespaces
```

Wait for the operator deployments (and therefore the CRDs) to be ready before the first install of this chart.

The `cert-manager` namespace must also exist before this chart is deployed (the operator install creates it) — these
objects target it:

- the `cert-manager-ca` Secret (bring-your-own CA path)
- the `selfsigned-ca` Certificate, which creates the `cert-manager-ca` Secret on the self-provisioned path
- the ACME credential Secrets from `secrets` entries under each `acme` provider (ClusterIssuer account keys and ACME
  credentials resolve in this cluster-resource namespace)
- the `trust-ca` ServiceAccount, Role/RoleBinding, ClusterRole/ClusterRoleBinding `cert-manager-trust-ca`,
  `trust-ca-config` ConfigMap, and the `trust-ca-set`/`trust-ca-remove` hook Jobs (when `ca.trust.enabled`)
- the `serving-certs` ServiceAccount and the certificate hook Jobs (when a platform certificate is managed)

The release namespace hosts the platform-certificate hook Jobs (`job-configure-api-certificate` and
`job-configure-router-certificate` render without a `namespace` and run in the release namespace), so it must admit the
root-running tools image — avoid a namespace enforcing restricted Pod Security Admission. The trust hooks
(`job-trust-ca-set`/`job-trust-ca-remove`) pin `namespace: cert-manager`, which the operator install creates fresh. The
namespace is therefore the only placement with a chart-owned object-namespace requirement, and
`helm install <name> charts/cert-manager -n cert-manager` satisfies both hook families at once.

Operator configuration
----------------------

The chart renders the `CertManager` cluster-scoped operator configuration CR (governed by the operator's own CRD, so it
applies as soon as the CRDs are established) with two controller overrides:

- `--dns01-recursive-nameservers=9.9.9.9:53,1.1.1.1:53` plus `--dns01-recursive-nameservers-only`. Red Hat documents
  that DNS01 propagation checks against the cluster's own DNS fail when the public zone (e.g. `example.com` on Route53)
  overlaps the cluster's private hosted zone — the private zone shadows the public records, so cert-manager never sees
  its own TXT records. Checking propagation through public recursive resolvers instead makes DNS01 work on such
  clusters, which is why the chart sets the override unconditionally.
- `--enable-certificate-owner-ref=false`. Certificate Secrets are created by cert-manager, not by Helm, so the only
  deletion path when a Certificate resource is removed (e.g. at `helm uninstall`) is the owner reference cascading the
  Secret. Keeping the flag explicitly off — which matches the upstream default at cert-manager 1.20 — makes the teardown
  contract below deterministic. The flag is controller-global, so it applies to the CA-family and NLB Secrets as well,
  not only to the platform serving certificates.

Issuers
-------

Two issuer families are supported, and they are not interchangeable:

- **CA (`ca.enabled`, default `true`)** — a self-signed ClusterIssuer provisions an ECDSA CA Certificate whose keypair
  lands in the `cert-manager-ca` Secret in the `cert-manager` namespace, and the `ca-issuer` ClusterIssuer signs from
  it. Set `ca.tlsCrt`/`ca.tlsKey` to bring your own CA keypair instead of self-provisioning; the two must be set
  together or not at all — rendering fails on a half-supplied keypair, which would otherwise silently self-provision a
  different CA. This is the issuer required by the `postgres-cluster` chart: its TLS configuration needs a CA-capable
  issuer because the referenced Secret doubles as `serverCASecret` and must carry a `ca.crt`, which ACME issuance does
  not produce.
- **ACME (`acme` map)** — one ClusterIssuer per key, with DNS01 or HTTP01 solvers and per-issuer credential Secrets
  (rendered from `secrets` entries under each ACME provider). ACME certificates are unusable for `postgres-cluster`'s
  server CA; use them for platform or route certificates. Each entry requires `server` and `email`, and every solver
  requires `type: dns` (with `dnsConfig`) or `type: http` (with `ingressClassName`); rendering fails on anything else,
  since an unknown type would render no solver at all and leave the ClusterIssuer unable to answer challenges.

`certificates.clusterIssuer` names the ClusterIssuer used by managed platform certificates. Rendering fails unless it
names a rendered ClusterIssuer — a key of the `acme` map, or `ca-issuer` when `ca.enabled`. The default value `zerossl`
deliberately names no issuer when `acme` is empty, so enabling a managed certificate without configuring an issuer fails
loudly at render time instead of issuing nothing.

Cluster trust
-------------

When `ca.enabled` and `ca.trust.enabled` are both true, the chart integrates the CA family with cluster trust: a
`trust-ca-set` hook Job (post-install,post-upgrade) propagates the CA certificate from the `cert-manager-ca` Secret into
the `openshift-config/user-ca-bundle` ConfigMap and points the cluster Proxy's `spec.trustedCA` at it, so in-cluster
clients trust certificates issued by `ca-issuer` (or by the bring-your-own CA). The Job waits up to
`ca.trustTimeoutSeconds` (default 120) for the `cert-manager-ca` Secret to exist before failing, and it fails — never
installs an empty bundle — when the Secret carries no certificate: an empty referenced bundle degrades the Proxy (the
Cluster Network Operator keeps the last-good merged bundle and reports an `InvalidProxyConfig` / `ProxyCAMergeFailure`
condition instead of trusting nothing).

Both objects are user-owned cluster inputs the release never owns blindly: it never modifies content it did not install,
and on uninstall it deletes the bundle ConfigMap only once removing this chart's entries has emptied it. Both are
modified under the same rules as every other platform write in this chart: read-modify-write merge patches carrying the
live `metadata.resourceVersion`, with a concurrent modification rejected with HTTP 409 and retried (5 attempts, 2s
apart).

**The bundle is merged, never replaced.** The hook classifies every PEM block in the existing bundle: a block belongs to
this chart when it byte-equals the Secret's current `tls.crt`, or when its base64 payload is listed in the
`trust-ca-history` annotation on the bundle — an annotation that is replaced, not appended to, on every hook run with
the payload that run installed (payloads rather than hashes, because `jq` has no hash function and the mechanics must
run under either `jq` or `python3`). The hook maintains exactly one of its own entries: the previously recorded block
that is no longer current is pruned, so repeated CA replacements do not accumulate. Foreign blocks are never removed or
reordered, and content the hook cannot fully parse as PEM certificates plus whitespace (mismatched PEM markers,
unrecognized non-PEM regions, or non-empty content holding zero PEM blocks such as whitespace) stops the hook instead of
being modified.

**Foreign references stop the hook.** When the Proxy's `spec.trustedCA` already names any other ConfigMap, the set hook
refuses to overwrite it — point `trustedCA` at `user-ca-bundle` or unset it manually, then re-run. When it already names
`user-ca-bundle` and the bundle holds exactly the current CA certificate, the hook recognizes its own prior output and
records the `trust-ca-owns-proxy-reference` annotation on the bundle. That annotation marks that this chart set the
reference, and it is written only on a transition into owning it — and only after the reference write itself has
succeeded, so a failed reference write never leaves an ownership record behind: a reference a third party set is never
claimed, even when the hook creates or heals the bundle underneath it — teardown leaves such a reference in place.

**The CA is a stable, manually-managed root.** The `selfsigned-ca` Certificate pins `duration: 87600h` (`ca.duration`)
and `privateKey.rotationPolicy: Never`, killing cert-manager's 90-day/key-rotating defaults: the anchor is valid ten
years, and renewal at ~2/3 of the lifetime reuses the private key, so the CA's identity never churns. The hook
propagates whatever the Secret currently holds on every run — that is propagation, not rotation. Changing `ca.duration`
(or swapping the BYO keypair) triggers an asynchronous re-issuance by cert-manager; the bundle converges on the _next_
hook run (a later upgrade or the re-created Job), not necessarily within the same upgrade, and until then the previous
anchor stays valid and trusted.

**Legacy blocks from pre-0.2.0 installs are not auto-removed.** Releases older than 0.2.0 wrote the bundle with a
key-rotating 90-day CA and kept no history annotation, so the block they installed is indistinguishable from a foreign
one. It remains in the bundle as an inert entry (verifiers skip expired root candidates; chains verify through the live
anchor) until a one-time manual prune — edit `openshift-config/user-ca-bundle` and delete the expired `selfsigned-ca`
block by hand.

**Teardown walks the footprint back.** A `trust-ca-remove` Job (pre-delete) unsets the Proxy reference — but only when
the ownership annotation records that this chart set it — then removes the blocks the Secret's current certificate and
the history annotation identify, and deletes the `user-ca-bundle` ConfigMap only when nothing remains in it (a non-empty
bundle is written back, since the referenced ConfigMap must never be emptied). Deleting the emptied bundle while a third
party still holds the reference returns the cluster to its prior (degraded) condition — the same state it was in before
this chart was installed, which is the expected outcome of the walk-back, not a defect. When the Secret is unreadable
and no history annotation exists, nothing can be identified and the hook fails safe with a diagnostic instead of
deleting anything. **Uninstall residue is expected in one case**: when the legacy (pre-0.2.0) path ran on the live
cluster, the reference is un-annotated and the legacy block is un-recorded, so uninstall leaves the Proxy reference set
and the legacy block in place until the manual prune above — that is the documented behavior, not a bug. **A second
residue arises when the write recording `trust-ca-owns-proxy-reference` fails after the set hook has already pointed
`spec.trustedCA` at the bundle, and its shape depends on why the write failed.** If the bundle ConfigMap itself vanished
mid-run, the reference is set but points at a missing ConfigMap, which degrades cluster trust (the Proxy references a
nonexistent `trustedCA` bundle) — that case is recoverable by re-running the hook, which recreates the bundle with the
current CA, with a further run then recording ownership; uninstalling before that leaves a dangling reference. For any
other ownership-write failure the bundle is intact and the reference keeps functioning — a later run re-records
ownership only when the bundle holds exactly the current CA at its start (a merged bundle holding foreign blocks never
qualifies), so uninstall otherwise leaves the reference in place, pointing at the surviving bundle contents; the manual
cleanup is unsetting `spec.trustedCA` on the Proxy.

**The trust gate is independent of issuance.** Setting `ca.trust.enabled: false` (with `ca.enabled: true`) renders the
issuance objects — the CA family, `ca-issuer`, and the `cert-manager-ca` Secret — and nothing of the trust surface: no
ServiceAccount, RBAC, ConfigMap, or hook Jobs. Because Helm runs pre-delete hooks only on uninstall, flipping
`ca.trust.enabled` off after an install stops rendering the cleanup hook too: the Proxy reference and the bundle entry
persist until removed manually, or until the chart is uninstalled with trust re-enabled (which renders the walk-back
hook again). The manual cleanup one-liner:

```sh
oc patch proxy cluster --type=merge --patch '{"spec":{"trustedCA":null}}'
oc edit configmap user-ca-bundle -n openshift-config   # remove this chart's block(s)
```

**RBAC scope, honestly stated.** The trust surface runs as the `trust-ca` ServiceAccount with resourceName-scoped
grants: get on exactly the `cert-manager-ca` Secret, get/patch/delete on exactly the `user-ca-bundle` ConfigMap, and
get/patch on exactly the Proxy `cluster` object. The grants are bound through the `cert-manager-trust-ca`
ClusterRoleBinding. One grant is broader than that surface: **create on configmaps in `openshift-config` is unscoped**,
because RBAC cannot resourceName-scope create and the hook must create the bundle when it is absent. A `user-ca-bundle`
ConfigMap created by something else (the same name-collision caveat class as the router-ca one) is merged into, never
replaced; a foreign _reference_ is never overwritten. The CA Secret itself persists across uninstall — the
owner-reference override described under "Teardown and persistence" applies chart-wide — so a CA that outlives its chart
keeps signing for anything that still trusts it.

DNS01 for private names
-----------------------

ACME issuance requires the ACME server to reach back and validate domain control:

- **HTTP01** serves a challenge route on the cluster. Red Hat documents that HTTP01 fails when the ACME server cannot
  reach that route — which is the case for any name that is not publicly resolvable, such as `*.apps.cluster.internal`
  subdomains. HTTP01 also cannot issue wildcard certificates at all.
- **DNS01** publishes a TXT record in the domain's DNS zone and is therefore the only viable solver for private cluster
  names, and the only mechanism for wildcards regardless. It requires control of the domain's public zone and DNS
  provider credentials — for an AWS cluster, Route53 access keys rendered through the per-issuer `secrets` entries.

Let's Encrypt
-------------

The production endpoint is `https://acme-v02.api.letsencrypt.org/directory`. Start against the staging endpoint
(`https://acme-staging-v02.api.letsencrypt.org/directory`) while debugging solver configuration — staging issues
untrusted certificates but its limits are effectively unbounded (~30,000 certificates/week), so iterative failures do
not burn production quota.

Production rate limits (2025+ model) matter for automation: 50 certificates per week per registered domain, but only **5
certificates per week per exact identifier set** — the set of DNS names on the certificate. Repeatedly deploying with
the same wildcard set (`apps.cluster.example.com` + `*.apps.cluster.example.com`) hits that 5-per-week ceiling on the
sixth issuance in a rolling multi-cluster or re-deploy scenario, even though the domain limit allows 50. Renewals driven
by ARI are exempt, so a stable long-lived issuance is unaffected. The examples leave `preferredChain` unset: Let's
Encrypt's cross-signed intermediates (R3/R4) expired on 2025-09-15 and the DST-rooted cross-sign is retired, so the
default ISRG chains are correct for any modern trust store.

ZeroSSL
-------

The ZeroSSL ACME endpoint (`https://acme.zerossl.com/v2/DV90`) requires external account binding (EAB): generate an EAB
credential in the ZeroSSL dashboard, render the HMAC key through a per-issuer `secrets` entry, and reference it under
`externalAccountBinding.keySecretRef`. The EAB credential is reusable across accounts since 2022, so one dashboard
credential serves every cluster. ZeroSSL issues 90-day certificates and its free tier caps subdomains at 6 labels
including the wildcard label (`*.apps.cluster.example.com` is 5 labels — fine; deeper wildcard levels are not). The EAB
HMAC key must be un-padded base64url. `keyAlgorithm` is deprecated upstream (HS256 is hardcoded) and is omitted from the
chart's examples.

Managed platform certificates
-----------------------------

`certificates.{api,router,nlbRouter}.managed` control whether the chart issues certificates for the API server, the
default IngressController, and the NLB router respectively:

- `router: true` issues the `router-default` Certificate (Secret `router-cert` in `openshift-ingress`) and a hook Job
  writes `IngressController/default.spec.defaultCertificate` to reference it. The router hot-reloads renewals with no
  rolling update.
- `api: true` issues the `openshift-api` Certificate and a hook Job writes it into
  `APIServer/cluster.spec.servingCerts.namedCertificates`. The first named certificate rolls a new API-server revision;
  subsequent Secret updates reload without disruption. Never add `api-int.<cluster>` to `dnsNames` — naming it degrades
  the cluster.
- `nlbRouter: true` issues a Certificate for the NLB router hostnames only; the external load balancer references its
  Secret, and no platform object is patched.

By default all three are `false`: stock OpenShift clusters keep their platform-issued certificates unless a service opts
in, so installing this chart does not replace platform certs.

The platform references are written by hook Jobs, not release-owned manifests: `IngressController/default` and
`APIServer/cluster` exist on every OpenShift cluster, so manifesting them from a chart makes plain `helm install` reject
the release on the ownership preflight (and any adopt-then-delete path a GitOps tool might take would delete platform
objects on uninstall). Each hook Job is gated on its `managed` flag, runs as a `post-install,post-upgrade` hook, and
holds a narrow, resourceName-scoped grant — get/patch on exactly `IngressController/default` (Role in
`openshift-ingress-operator`) for the router mode, and get/patch on exactly `APIServer/cluster` for the API mode. The
reference write is a read-modify-write merge patch carrying the live `metadata.resourceVersion`, so a concurrent
modification is rejected with HTTP 409 and retried (5 attempts, 2s apart) rather than clobbering.

The hooks fail closed on foreign references instead of overwriting another owner's configuration:

- **Router**: an unset `defaultCertificate` (the stock-cluster state) is a valid target and is written; a reference to
  `router-cert` is a no-op; a reference to any other Secret stops the hook with an error naming the foreign reference.
- **API**: `namedCertificates` is an atomic list, so entries cannot be merged per-item. An empty or absent list is
  written with this chart's entry; a list exactly equal to this chart's entry is a no-op; a list containing any other
  entry — or this chart's entry plus a foreign one — stops the hook with an error naming the foreign names.

Changing `certificates.api.dnsNames` on an existing install rewrites this chart's expected entry, so the live
`namedCertificates` list is no longer equal to the new reference and the API hook stops by design — the list must be
brought back in line manually (or emptied) before the hook can write it again.

The hooks patch the reference immediately and do not wait for issuance: the platform keeps serving the previous
certificate until the ACME certificate issues, then reconciles on its own (the router hot-reloads; the API server
reloads the Secret). This avoids failing the install on slow or failed ACME issuance. While issuance is pending the
affected operators report a degraded condition; the `Certificate` resource's Ready condition and its failure reason are
the diagnostic path (for DNS01, typically the solver credentials or zone control, and the propagation-check behavior
described above).

Teardown and persistence
------------------------

Certificates keep serving after `helm uninstall`, by design:

- The serving Secrets persist (the `--enable-certificate-owner-ref=false` override above keeps cert-manager from
  cascading them when the Certificate resources are deleted), and the platform references written by the hook Jobs stay
  in place. The platform keeps serving the kept Secret's certificate until it expires — after that the endpoint breaks
  until the chart is reinstalled or the reference is manually restored. Removal of a router reference reverts the router
  to the platform-generated wildcard certificate.
- The controller-global owner-ref flag applies chart-wide, so the `cert-manager-ca` Secret (the CA family) and the NLB
  Secret also persist across uninstall. A CA that outlives its chart keeps signing for anything that still trusts it;
  treat an uninstall as "issuance stops", not "trust is revoked".
- First-install caveat: the operator may not have reconciled the `--enable-certificate-owner-ref=false` override before
  the controller issues a first certificate, so a Secret created on the very first install can carry the owner reference
  and be cascaded away with the Certificate at uninstall. The persistence contract is deterministic for issuance after
  the operator reconcile lands; on a first install followed immediately by an uninstall, verify the Secret survived if
  it matters.

Examples
--------

A stock cluster — CA chain, `ca-issuer`, cluster trust, and no platform certificates touched (with the operator
installed via `install-operators` as above):

```yaml
global:
  cluster:
    name: mycluster
    baseDomain: example.com
```

CA-trusted platform certificates (opt in per service; explicit wildcard DNS names for the router):

```yaml
global:
  cluster:
    name: mycluster
    baseDomain: example.com

certificates:
  clusterIssuer: ca-issuer
  api:
    managed: true
  router:
    managed: true
    dnsNames:
      - apps.mycluster.example.com
      - "*.apps.mycluster.example.com"
```

Let's Encrypt with Route53 DNS01 for a private cluster — public-zone control required:

```yaml
acme:
  letsencrypt:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: <your email here>
    solvers:
      - type: dns
        dnsConfig:
          route53:
            accessKeyID: <AWS access key ID>
            hostedZoneID: <hosted zone ID>
            region: us-east-2
            secretAccessKeySecretRef:
              key: secret-access-key
              name: acme-letsencrypt-route53-creds
        dnsZones:
          - example.com
    secrets:
      - name: route53-creds
        stringData:
          secret-access-key: <AWS secret access key>

certificates:
  clusterIssuer: letsencrypt
  router:
    managed: true
```

An ACME issuer (ZeroSSL with Route53 DNS01) alongside the CA:

```yaml
acme:
  zerossl:
    server: https://acme.zerossl.com/v2/DV90
    email: <your email here>
    externalAccountBinding:
      keyID: <ZeroSSL EAB key ID>
      keySecretRef:
        name: acme-zerossl-eabsecret
        key: secret
    solvers:
      - type: dns
        dnsConfig:
          route53:
            accessKeyID: <AWS access key ID>
            hostedZoneID: <hosted zone ID>
            region: us-east-2
            secretAccessKeySecretRef:
              key: secret-access-key
              name: acme-zerossl-route53-creds
        dnsZones:
          - example.com
    secrets:
      - name: route53-creds
        stringData:
          secret-access-key: <AWS secret access key>
      - name: eabsecret
        stringData:
          secret: <ZeroSSL EAB HMAC key>

certificates:
  clusterIssuer: zerossl
  router:
    managed: true
```
