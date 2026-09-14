#!/bin/bash -ex
#
# Pull the third-party Docker images harness uses for tooling.
#
# minlag/mermaid-cli: validates the Mermaid diagrams in docs/ (see
# scripts/validate-mermaid.sh). Bundles the mermaid.js parser GitHub renders
# with, so it catches diagram syntax errors before they ship.
# Pinned to the SAME tag scripts/validate-mermaid.sh validates with -- the
# merge gate and this pull must never track two different mermaid.js parser
# versions; bump the two together.

docker pull minlag/mermaid-cli:10.9.1
