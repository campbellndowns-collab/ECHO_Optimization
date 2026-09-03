# Portfolio Deployment Migration Plan

## Recommendation

Keep the current local Streamlit application as the engineering reference
implementation while migrating the public-facing version into a proper web
application repository.

Do not rewrite the numerical core from scratch.

Refactor it into a Python package first, then put an API around it.

## Target repository structure

    drone-optimizer/
      frontend/
      backend/
        app/
          api/
          services/
          models/
      optimizer/
        components/
        propulsion/
        batteries/
        frame/
        ranking/
        cache/
      tests/
      data/
      docker/
      docs/

## Migration sequence

1. Freeze v0.7 as the reference behavior.
2. Add automated regression cases for known designs.
3. Extract numerical functions out of the Streamlit layer.
4. Create typed request/response models for one aircraft study.
5. Add a FastAPI endpoint for fixed-configuration evaluation.
6. Add job-based endpoints for mixed/full optimization.
7. Build the polished frontend around those APIs.
8. Add persistent job/result storage.
9. Containerize the API and worker.
10. Deploy a public demo.
11. Keep expensive/deep runs rate-limited or authenticated.
12. Link the deployed demo and source repository from the engineering portfolio.

## Best first public feature

Build the fixed/mixed component calculator first.

It is easier to explain in a portfolio:

    "Select components, lock what you already know, optimize the remaining
    propulsion choices, and compare valid quadrotor designs using real
    propulsion data and multidisciplinary constraints."

Then expose Deep optimization after the server/job infrastructure is stable.

## Portfolio presentation

The project page should show:

- the engineering problem,
- architecture diagram,
- data pipeline,
- PyThrust validation,
- physical frame model,
- cached search architecture,
- fixed/mixed optimization workflow,
- weighted decision matrix,
- screenshots,
- one example trade study,
- source repository,
- live demo.

That communicates significantly more engineering depth than simply linking to
a generic calculator.
