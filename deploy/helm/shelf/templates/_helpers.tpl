{{/* Expand the name of the chart. */}}
{{- define "shelf.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name. */}}
{{- define "shelf.fullname" -}}
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

{{/* Common labels. */}}
{{- define "shelf.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{ include "shelf.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels. */}}
{{- define "shelf.selectorLabels" -}}
app.kubernetes.io/name: {{ include "shelf.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Reusable env block: envFrom (secret) + .Values.nats.url +
.Values.extraEnv. The nats.url value flows in through `env` so it's
visible without a Secret round-trip; secrets remain reserved for
credentials. */}}
{{- define "shelf.containerEnv" -}}
{{- if .Values.existingSecretName }}
envFrom:
  - secretRef:
      name: {{ .Values.existingSecretName }}
{{- end }}
{{- if or .Values.nats.url .Values.gotenberg.enabled .Values.frameAncestors .Values.extraEnv }}
env:
{{- if .Values.nats.url }}
  - name: SHELF_NATS_URL
    value: {{ .Values.nats.url | quote }}
{{- end }}
{{- if .Values.gotenberg.enabled }}
  - name: SHELF_GOTENBERG_URL
    value: "http://{{ include "shelf.fullname" . }}-gotenberg:3000"
{{- end }}
{{- if .Values.frameAncestors }}
  - name: SHELF_FRAME_ANCESTORS
    value: {{ .Values.frameAncestors | toJson | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
{{- end -}}
