---
name: Production hosting
description: User-confirmed production hosting and source delivery.
---

The project runs on an external Railway server connected to a GitHub repository; the user uploads the project to GitHub for execution.

**Why:** the user explicitly clarified the production setup; older project documentation mentions Render and must not override that clarification.

**How to apply:** assess cleanup against Railway production as well as local development. Do not remove build or startup configuration merely because the local workflow does not use it; confirm the actual production builder and start command first.
