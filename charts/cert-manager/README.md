cert-manager
============

A chart to configure cert-manager on OpenShift and manage its issuer surface: a self-provisioned CA chain (self-signed
issuer → CA Certificate → `cert-manager-ca` Secret → `ca-issuer` ClusterIssuer), optional ACME ClusterIssuers, and
optional management of platform certificates (API server, default IngressController, NLB router).

The operator install is gated behind `operator.create` (default `false`). Helm applies all release manifests at once and
does not wait for OLM to establish the operator CRDs, so operand CRs (`CertManager`, ClusterIssuers, Certificates)
cannot be applied in the same install as the Subscription on a fresh cluster. Per repo convention, install the operator
through the `install-operators` chart instead:

```yaml
operators:
  openshift-cert-manager-operator:
    channel: stable-v1
    namespace: cert-manager-operator
    operatorGroup:
      enabled: true
```

With `operator.create: false`, this chart renders only the operand configuration. The `cert-manager` Namespace is
operator-gated, so it must already exist (the operator install creates it) before this chart is deployed — these objects
target it:

- the `cert-manager-ca` Secret (bring-your-own CA path)
- the `selfsigned-ca` Certificate, which creates the `cert-manager-ca` Secret on the self-provisioned path
- the ACME credential Secrets from `secrets` entries under each `acme` provider
- the `trust-ca` ServiceAccount and `trust-cert-manager-ca` Job

Setting `operator.create: true` renders the `cert-manager` Namespace, an OperatorGroup, the
`openshift-cert-manager-operator` Subscription (channel `stable-v1`, redhat-operators), and the `CertManager` operator
config CR — which sets `--dns01-recursive-nameservers-only` with 9.9.9.9/1.1.1.1 so DNS01 challenges use public
resolvers rather than the cluster DNS. This is only safe on a cluster where the operator CRDs are already established
(for example, a cluster where the operator is already installed); on such a cluster it replaces a separate
install-operators entry and avoids duplicate Subscriptions.

Issuers
-------

Two issuer families are supported, and they are not interchangeable:

- **CA (`ca.enabled`, default `true`)** — a self-signed ClusterIssuer provisions an ECDSA CA Certificate whose keypair
  lands in the `cert-manager-ca` Secret in the `cert-manager` namespace, and the `ca-issuer` ClusterIssuer signs from
  it. Set `ca.tlsCrt`/`ca.tlsKey` to bring your own CA keypair instead of self-provisioning. This is the issuer required
  by the `postgres-cluster` chart: its TLS configuration needs a CA-capable issuer because the referenced Secret doubles
  as `serverCASecret` and must carry a `ca.crt`, which ACME issuance does not produce.
- **ACME (`acme` map)** — one ClusterIssuer per key, with DNS01 or HTTP01 solvers and per-issuer credential Secrets
  (rendered from `secrets` entries under each ACME provider). ACME certificates are unusable for `postgres-cluster`'s
  server CA; use them for platform or route certificates.

The `certificates.clusterIssuer` value names the ClusterIssuer used by managed platform certificates — it must be a key
of the `acme` map or `ca-issuer`.

Cluster trust
-------------

When `ca.enabled`, the chart also runs a `trust-cert-manager-ca` Job (cluster-admin `trust-ca` ServiceAccount) that
pushes the CA certificate into the `openshift-config/user-ca-bundle` ConfigMap and patches the cluster Proxy's
`trustedCA`, so in-cluster clients trust certificates issued by `ca-issuer`. The Job runs as a Helm post-install /
post-upgrade hook and waits up to `ca.trustTimeoutSeconds` (default 120) for the `cert-manager-ca` Secret to exist
before failing, so an incomplete CA chain aborts the trust setup instead of installing an empty bundle.

Managed platform certificates
-----------------------------

`certificates.{api,router,nlbRouter}.managed` control whether the chart issues certificates for the API server, the
default IngressController, and the NLB router respectively:

- `api: true` issues the API server certificate and patches the `APIServer` resource with the named certificate
- `router: true` issues the default router certificate and patches the default `IngressController` with it
- `nlbRouter: true` issues a Certificate for the NLB router hostnames only — it does not patch any IngressController

By default all three are `false`: stock OpenShift clusters keep their platform-issued certificates unless a service opts
in, so installing this chart does not replace platform certs.

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
      keyAlgorithm: HS256
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
