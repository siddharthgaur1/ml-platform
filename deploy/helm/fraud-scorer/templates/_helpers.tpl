{{- define "fraud-scorer.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fraud-scorer.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "fraud-scorer.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "fraud-scorer.labels" -}}
helm.sh/chart: {{ include "fraud-scorer.chart" . }}
{{ include "fraud-scorer.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "fraud-scorer.selectorLabels" -}}
app.kubernetes.io/name: {{ include "fraud-scorer.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fraud-scorer.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "fraud-scorer.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret holding the API keys: an externally managed one if given,
otherwise the one this chart renders.
*/}}
{{- define "fraud-scorer.secretName" -}}
{{- if .Values.auth.existingSecret -}}
{{- .Values.auth.existingSecret -}}
{{- else -}}
{{- include "fraud-scorer.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Refuse to render a config the app will reject at startup anyway.

api/security.py raises unless API_KEYS is set or ALLOW_UNAUTHENTICATED=true. A
CrashLoopBackOff is a much worse way to learn that than a failed `helm install`,
and an unauthenticated scoring endpoint that silently works is how a demo ends up
on the internet with no auth on it.
*/}}
{{- define "fraud-scorer.validateAuth" -}}
{{- if and (not .Values.auth.apiKeys) (not .Values.auth.existingSecret) (not .Values.auth.allowUnauthenticated) -}}
{{- fail "auth.apiKeys or auth.existingSecret must be set, or auth.allowUnauthenticated must be true (local clusters only)" -}}
{{- end -}}
{{- end -}}
