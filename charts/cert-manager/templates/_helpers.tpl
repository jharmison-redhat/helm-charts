{{/*
Render-time validation, split by concern and included from the templates
whose values each concern governs:

- validateCa and validateAcme are included from clusterissuer.yaml, which
  always renders, so every `helm template` / `helm lint` / `helm install`
  run of the chart is validated.
- validateCertificateIssuer is included from the templates that consume
  `certificates.*` (api-certificate.yaml, router-certificates.yaml) when a
  managed platform certificate is enabled.
*/}}
{{- define "cert-manager.validateCa" -}}
{{- $crt := .Values.ca.tlsCrt -}}
{{- $key := .Values.ca.tlsKey -}}
{{- if or (and $crt (not $key)) (and $key (not $crt)) -}}
{{- fail "ca.tlsCrt and ca.tlsKey must be set together — a half-supplied CA keypair would silently self-provision a different CA instead; set both to bring your own CA, or neither to self-provision" -}}
{{- end -}}
{{- end -}}

{{- define "cert-manager.validateAcme" -}}
{{- range $name := keys (.Values.acme | default dict) | sortAlpha -}}
{{- $entry := index $.Values.acme $name -}}
{{- if not $entry.server -}}
{{- fail (printf "acme.%s.server is required — the ACME directory endpoint the ClusterIssuer registers against (e.g. https://acme-v02.api.letsencrypt.org/directory)" $name) -}}
{{- end -}}
{{- if not $entry.email -}}
{{- fail (printf "acme.%s.email is required — the ACME account contact address the provider notifies about expiring certificates and abuse" $name) -}}
{{- end -}}
{{- range $index, $solver := ($entry.solvers | default (list)) -}}
{{- if not (has $solver.type (list "dns" "http")) -}}
{{- fail (printf "acme.%s.solvers[%d].type must be `dns` or `http` (got %v) — any other value renders no solver at all, leaving the ClusterIssuer unable to answer challenges" $name $index $solver.type) -}}
{{- else if eq $solver.type "dns" -}}
{{- if not $solver.dnsConfig -}}
{{- fail (printf "acme.%s.solvers[%d].dnsConfig is required for a dns solver — the dns01 challenge would render with no provider to publish the TXT record (e.g. route53: {accessKeyID, hostedZoneID, region, secretAccessKeySecretRef})" $name $index) -}}
{{- end -}}
{{- else if not $solver.ingressClassName -}}
{{- fail (printf "acme.%s.solvers[%d].ingressClassName is required for an http solver — the IngressClass the challenge route is served on (OpenShift's value is `openshift-default`)" $name $index) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "cert-manager.validateCertificateIssuer" -}}
{{- $acmeKeys := keys (.Values.acme | default dict) | sortAlpha -}}
{{- $issuer := .Values.certificates.clusterIssuer -}}
{{- $valid := $acmeKeys -}}
{{- if .Values.ca.enabled -}}
{{- $valid = concat (list "ca-issuer") $acmeKeys -}}
{{- end -}}
{{- if empty $valid -}}
{{- fail (printf "certificates.clusterIssuer %q does not name a rendered ClusterIssuer — no issuer is rendered at all; managed platform certificates issue from it, so enable `ca` (renders ca-issuer) or configure an issuer under `acme` first" $issuer) -}}
{{- end -}}
{{- if not (has $issuer $valid) -}}
{{- fail (printf "certificates.clusterIssuer %q does not name a rendered ClusterIssuer — managed platform certificates issue from it; set it to one of: %s" $issuer (join ", " $valid)) -}}
{{- end -}}
{{- end -}}
