{{- define "mlflow-azure-sso.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "mlflow-azure-sso.fullname" -}}
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

{{- define "mlflow-azure-sso.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "mlflow-azure-sso.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "mlflow-azure-sso.selectorLabels" -}}
app.kubernetes.io/name: {{ include "mlflow-azure-sso.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "mlflow-azure-sso.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "mlflow-azure-sso.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "mlflow-azure-sso.secretName" -}}
{{ include "mlflow-azure-sso.fullname" . }}-secret
{{- end -}}

{{- define "mlflow-azure-sso.artifactRoot" -}}
{{- if .Values.artifactRoot.s3.enabled -}}
{{- $path := .Values.artifactRoot.s3.path -}}
s3://{{ .Values.artifactRoot.s3.bucket }}{{ if $path }}/{{ $path }}{{ end }}
{{- end -}}
{{- end -}}
