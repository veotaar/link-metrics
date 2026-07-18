# Discover contenders from local manifests

Each `backends/<id>/` directory contains a schema-validated `contender.yaml` declaring its stable identity, display name, language, runtime, framework, container build context, exposed port, worker configuration, and resource profile. The harness discovers these manifests automatically, allowing new contenders without a central registry or contender-specific orchestration branches.
