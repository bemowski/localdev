{{- define "usage-client.name" -}}{{ .Release.Name }}{{- end }}
{{- define "usage-client.clusterName" -}}{{ .Release.Namespace }}-{{ .Release.Name }}{{- end }}
{{- define "usage-client.labels" -}}
app.kubernetes.io/name: usage-client
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ .Chart.Name }}-{{ .Chart.Version }}
{{- end }}
{{- define "usage-client.selector" -}}
app.kubernetes.io/name: usage-client
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
