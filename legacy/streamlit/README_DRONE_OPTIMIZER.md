# Drone Optimizer v0.1

This is the first persistent local application layer for the PyThrust project.

## Install

Extract this ZIP DIRECTLY into:

    C:\Users\Campbell\Documents\PyThrust

Do not create an extra wrapper folder.

After extraction, your existing PyThrust folder should contain:

    Launch Drone Optimizer.bat
    Setup Drone Optimizer.bat
    requirements_drone_optimizer.txt
    drone_optimizer\
    tools\
    component_data\
    examples\
    pythrust\
    .venv\

Your existing data and PyThrust installation are not deleted.

## Normal use

Double-click:

    Launch Drone Optimizer.bat

You no longer need to activate the venv or type the Streamlit command in
PowerShell for ordinary use.

On the first launch, the BAT file checks for Streamlit. If it is missing, it
installs the small GUI dependency set into your EXISTING PyThrust `.venv`.

The app opens at:

    http://localhost:8501

## What v0.1 does now

### Database dashboard
- reads component_data\component_warehouse.sqlite
- shows canonical component counts
- shows optimizer-ready counts
- shows source-by-source row counts
- refreshes the component database from a button
- can force fresh source downloads from a button
- preserves the command output in the Logs tab

### Confidence scoring
Every component gets a transparent heuristic based on:
- data-quality tier
- required engineering-field completeness
- checked-date freshness
- license clarity
- the warehouse's existing optimizer_eligible flag

The result is:
- Ready
- Screening
- Discovery

This score is a prioritization tool, NOT a replacement for manufacturer
verification.

### Fast Screen
The app cheaply narrows the huge discovery warehouse before any expensive
PyThrust calculation.

It currently screens:
- motors
- APC props with aerodynamic data
- real battery cells
- automatically generated real-cell S/P pack topologies
- commercial battery packs
- ESCs

The screen deliberately does not claim thrust or endurance. Its purpose is to
reduce the candidate pool that reaches the physics solver.

### Detailed Run
The app can launch the existing:

    examples\quadrotor_fast_adaptive_optimization.py

from a button instead of a PowerShell command.

## New architecture

The GUI is separated from data and physics logic:

    drone_optimizer\
        app.py                  GUI
        db.py                   SQLite access
        quality.py              confidence/readiness scoring
        screening.py            cheap candidate reduction
        jobs.py                 database/optimizer job execution
        source_adapters\
            base.py             permanent adapter contract
            registry.py         source registry

The goal is to migrate the monolithic source parsers behind SourceAdapter over
time. A broken TUM/TyTo/etc. source should eventually be retried independently
rather than requiring another complete application rewrite.

## Next milestone: v0.2

The next valuable step is NOT adding more UI.

It is integrating the output of Fast Screen into the detailed physics stage:

1. discrete real cell/custom packs
2. discrete commercial packs
3. ESC voltage/current compatibility
4. real component cost
5. PyThrust motor/prop calculation
6. Pareto ranking:
   - maximum endurance
   - best under mass target
   - best under budget
   - lightest design above an endurance threshold
   - best value

That replaces the current parametric-battery detailed optimizer with the
actual component warehouse.
