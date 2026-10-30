{{/* Where the ConfigMap is mounted in the sidecar; polaris-values.yaml mounts it at the same path. */}}
{{- define "polaris-log-sidecar.mountPath" -}}/fluent-bit/policy{{- end -}}
