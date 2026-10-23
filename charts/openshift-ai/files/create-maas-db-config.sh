#!/bin/bash
{{- $maasDb := .Values.maasDb }}
{{- $secret := required "maasDb.secretName is required: name of the secret holding the postgres connection URI (the Cluster's <fullname>-app secret)" $maasDb.secretName }}

uri=$(oc get secret {{ $secret }} -ojsonpath='{.data.uri}' | base64 -d)
# maas-api (RHOAI 3.5.0+) reads maas-db-config from its own namespace, which is
# also where the postgres cluster lives
oc create secret generic maas-db-config -n {{ $maasDb.namespace }} --from-literal=DB_CONNECTION_URL="$uri" --dry-run=client -oyaml | oc apply -f-
