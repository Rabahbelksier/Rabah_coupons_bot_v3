---
name: Production hosting
description: User-confirmed production hosting and source delivery.
---

The project runs on an external Railway server connected to a GitHub repository; the user uploads the project to GitHub for execution.

**Why:** the user explicitly clarified the production setup; older project documentation mentions Render and must not override that clarification.

**How to apply:** assess cleanup against Railway production as well as local development. Do not remove build or startup configuration merely because the local workflow does not use it; confirm the actual production builder and start command first.

## Cleanup evidence

Do not judge whether a file is unused by its name: the user cautioned that names may be old while their contents are still used.

**Why:** the user explicitly corrected name-based cleanup reasoning.

**How to apply:** inspect contents, direct and indirect references, framework registration, scripts, tests, and deployment configuration. An absent in-repository reference does not prove that an external deployment or manual maintenance process does not use a file.
