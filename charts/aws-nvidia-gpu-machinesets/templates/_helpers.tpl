{{- define "aws-nvidia-gpu-machinesets.az" -}}
{{- if .Values.aws.az -}}
{{ .Values.aws.az }}
{{- else -}}
{{ last .Values.aws.azs }}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateAz" -}}
{{- $az := .Values.aws.az -}}
{{- if $az -}}
{{- if not (has $az .Values.aws.azs) -}}
{{- fail (printf "aws.az '%s' is not a member of aws.azs %v — only the zones in aws.azs render MachineSets, so an unlisted aws.az silently leaves every MachineSet at zero replicas; set aws.az to one of the aws.azs entries or unset it to default to the last entry (%s)" $az .Values.aws.azs (last .Values.aws.azs)) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateInfraID" -}}
{{- $infraID := .Values.cluster.infraID -}}
{{- if kindIs "invalid" $infraID -}}
{{- fail "cluster.infraID is required — it constructs the MachineSet names and the AWS resource names (kubernetes.io/cluster/<infraID>: owned tag, <infraID>-worker-profile instance profile, <infraID>-node/<infraID>-lb security groups, <infraID>-subnet-private-<az> subnets). Find it in the names of the existing worker MachineSets in openshift-machine-api" -}}
{{- end -}}
{{- if not (kindIs "string" $infraID) -}}
{{- fail (printf "cluster.infraID must be a string (got %s)" (kindOf $infraID)) -}}
{{- end -}}
{{- if eq (trim $infraID) "" -}}
{{- fail "cluster.infraID must be a non-empty string — it constructs the MachineSet names and the AWS resource names (kubernetes.io/cluster/<infraID>: owned tag, <infraID>-worker-profile instance profile, <infraID>-node/<infraID>-lb security groups, <infraID>-subnet-private-<az> subnets). Find it in the names of the existing worker MachineSets in openshift-machine-api" -}}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateRegion" -}}
{{- $region := .Values.aws.region -}}
{{- if kindIs "invalid" $region -}}
{{- fail "aws.region is required — the MachineSet placement region the cluster runs in" -}}
{{- end -}}
{{- if not (kindIs "string" $region) -}}
{{- fail (printf "aws.region must be a string (got %s)" (kindOf $region)) -}}
{{- end -}}
{{- if eq (trim $region) "" -}}
{{- fail "aws.region must be a non-empty string — the MachineSet placement region the cluster runs in" -}}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateAmi" -}}
{{- $ami := .Values.aws.ami -}}
{{- if kindIs "invalid" $ami -}}
{{- fail "aws.ami is required — the RHCOS worker AMI ID for the cluster's region; copy it from an existing worker MachineSet's providerSpec in openshift-machine-api" -}}
{{- end -}}
{{- if not (kindIs "string" $ami) -}}
{{- fail (printf "aws.ami must be a string (got %s)" (kindOf $ami)) -}}
{{- end -}}
{{- if eq (trim $ami) "" -}}
{{- fail "aws.ami must be a non-empty string — the RHCOS worker AMI ID for the cluster's region; copy it from an existing worker MachineSet's providerSpec in openshift-machine-api" -}}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateAzs" -}}
{{- $azs := .Values.aws.azs -}}
{{- if kindIs "invalid" $azs -}}
{{- fail "aws.azs is required — a non-empty list of availability zones; one MachineSet is rendered per entry" -}}
{{- end -}}
{{- if not (kindIs "slice" $azs) -}}
{{- fail (printf "aws.azs must be a list of availability zones (got %s)" (kindOf $azs)) -}}
{{- end -}}
{{- if eq (len $azs) 0 -}}
{{- fail "aws.azs must not be empty — one MachineSet is rendered per availability zone" -}}
{{- end -}}
{{- end -}}

{{- define "aws-nvidia-gpu-machinesets.validateMaxReplicas" -}}
{{- $maxReplicas := int .Values.autoscaling.maxReplicas -}}
{{- $azCount := len .Values.aws.azs -}}
{{- if lt $maxReplicas $azCount -}}
{{- fail (printf "autoscaling.maxReplicas (%d) is lower than the number of entries in aws.azs (%d) — the MachineAutoscaler CRD enforces maxReplicas >= 1 per zone, and the exact-sum distribution would render maxReplicas: 0 zones the API server rejects; set autoscaling.maxReplicas to at least %d, or reduce aws.azs" $maxReplicas $azCount $azCount) -}}
{{- end -}}
{{- end -}}
