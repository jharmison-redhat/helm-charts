helm-charts
===

This repository houses multiple Helm Charts that I have found useful for managing OpenShift clusters, in particular to
support OpenShift AI. They are published as OCI artifacts under `ghcr.io/jharmison-redhat/helm-charts`, in versioned
packages named for each chart. Each Chart is documented in its own README. They can be installed with a command like the
following example for the install-operators Chart:

```
helm upgrade --install \
    install-operators \
    oci://ghcr.io/jharmison-redhat/helm-charts/install-operators \
    --set-json=operators='{
        "cloudnative-pg": {
            "channel": "stable-v1",
            "catalog": "certified-operators"
        }
    }'
```
