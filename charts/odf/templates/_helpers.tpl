{{/*
Validates the two configuration modes against the values combination. The
StorageCluster renders unconditionally, so invoking this from its template
gates every render of the chart.

Standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) runs
NooBaa without Ceph: no device sets, no disks, no Ceph tooling, no
StorageSystem, no monitor host path. Full Ceph+MCGW mode (reconcileStrategy
unset or not standalone) reconciles Ceph capacity from storageDeviceSets or
hostPath disks (with monDataDirHostPath when disks are the only capacity).
*/}}
{{- define "odf.validateModes" -}}
{{- $mcg := .Values.multiCloudGateway | default dict -}}
{{- $strategy := dig "reconcileStrategy" "" $mcg -}}
{{- $deviceSets := .Values.storageDeviceSets | default list -}}
{{- $disks := .Values.disks | default list -}}
{{- $monPath := .Values.monDataDirHostPath | default "" -}}
{{- if eq $strategy "standalone" -}}
{{- if gt (len $deviceSets) 0 -}}
{{- fail "standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) runs NooBaa without Ceph — storageDeviceSets provision Ceph capacity and must be empty in this mode; remove them or use full Ceph+MCGW mode" -}}
{{- end -}}
{{- if gt (len $disks) 0 -}}
{{- fail "standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) runs NooBaa without Ceph — disks label nodes for Ceph storage and must be empty in this mode; remove them or use full Ceph+MCGW mode" -}}
{{- end -}}
{{- if ne (trim $monPath) "" -}}
{{- fail "standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) runs NooBaa without Ceph — monDataDirHostPath is a Ceph monitor path and must be empty in this mode; remove it or use full Ceph+MCGW mode" -}}
{{- end -}}
{{- if .Values.createStorageSystem -}}
{{- fail "standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) has no Ceph StorageCluster to manage — createStorageSystem must be false (the default) in this mode" -}}
{{- end -}}
{{- if .Values.enableCephTools -}}
{{- fail "standalone MCG mode (multiCloudGateway.reconcileStrategy: standalone) has no Ceph cluster for the tools to manage — enableCephTools must be false in this mode" -}}
{{- end -}}
{{- else if eq (len $deviceSets) 0 -}}
{{- if eq (len $disks) 0 -}}
{{- fail "full Ceph+MCGW mode (multiCloudGateway.reconcileStrategy is unset or not standalone) reconciles no Ceph capacity without storage — provide storageDeviceSets, or label hostPath nodes via disks (and set monDataDirHostPath), or set multiCloudGateway.reconcileStrategy: standalone for standalone MCG mode" -}}
{{- else if eq (trim $monPath) "" -}}
{{- fail "full Ceph+MCGW mode (multiCloudGateway.reconcileStrategy is unset or not standalone) with disks-only storage requires monDataDirHostPath — Ceph monitors need a host path on the nodes labeled via disks; set monDataDirHostPath or provide storageDeviceSets instead" -}}
{{- end -}}
{{- end -}}
{{- end -}}
