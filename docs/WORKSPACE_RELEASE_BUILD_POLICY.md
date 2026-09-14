# Workspace Release Build Policy (P0)

This document is the version-controlled canonical release policy for repositories in this workspace that already use GitHub Actions for automated image publication.

## User release objective

The preferred release path is **GitHub Actions clean-building and publishing the repository-defined multi-architecture images from the pushed release commit / `v*` tag**.

## Mandatory rules

1. **CI is authoritative for formal releases.** If the repository already has a GitHub Actions build/publish workflow, the formal release artifact should be produced by that workflow. Do not bypass CI merely to preserve a locally built image digest.
2. **Candidate digest is provenance, not a release invariant.** Images built on `gau-unraid` or another local builder may be used for smoke, canary, upgrade rehearsal, and pre-release acceptance. Their digest does not need to equal a later GitHub Actions clean-build digest or multi-architecture manifest digest.
3. **Do not impose digest equality by default.** Never require `candidate digest == formal GHCR release digest` unless the user explicitly requests promotion of the exact same artifact without rebuilding.
4. **Canonical release chain:**

   `source commit/tag -> GitHub Actions tests -> GitHub Actions multi-arch build -> registry publish -> release smoke/acceptance`

5. **CI failures must be fixed, not bypassed.** If tests or image publication fail in GitHub Actions, diagnose and repair the workflow/code, then rerun CI. Manual image publication is not an acceptable workaround unless the user explicitly authorizes an emergency/manual release.
6. **Preserve multi-architecture intent.** The workflow-defined platform matrix is the release target. A single-architecture local candidate must not silently reduce the formal release to one architecture.
7. **Record both identities correctly.** Keep local/canary image digests as candidate provenance, and separately record the final registry digest / multi-architecture manifest digest produced by GitHub Actions.

## Research Memory Gateway application

For `research-memory-gateway`, `.github/workflows/docker-publish.yml` is the normal formal publication path. A release should normally be produced by pushing the intended `main` commit and/or `v*` tag and allowing GitHub Actions to run its tests and multi-architecture Buildx publication flow.

Do not repeat the v0.2.7 mistake of constraining the formal GitHub Actions release digest to match a previously validated local candidate digest.
