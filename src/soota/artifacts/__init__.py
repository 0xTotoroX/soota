"""Content documents, verified artifacts and backward-compatible content bundles."""

from soota.artifacts.bundle import (
    artifact_path,
    content_from_markdown,
    load_bundle,
    load_transcript,
    save_bundle,
    verify_artifacts,
    write_artifact,
    write_bundle,
)
from soota.artifacts.models import Artifact, ArtifactV3, Bundle, BundleV2, BundleV3, ContentBlock, ContentDocument

__all__ = [
    "Artifact",
    "ArtifactV3",
    "Bundle",
    "BundleV2",
    "BundleV3",
    "artifact_path",
    "ContentBlock",
    "ContentDocument",
    "content_from_markdown",
    "load_bundle",
    "load_transcript",
    "save_bundle",
    "verify_artifacts",
    "write_artifact",
    "write_bundle",
]
