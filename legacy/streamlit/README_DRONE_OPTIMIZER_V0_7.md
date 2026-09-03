# Drone Optimizer v0.7 — Fixed Parts + Mixed Optimization

V0.7 adds an eCalc-style component configuration workflow while preserving the
optimizer.

Each major propulsion category can now be either:

    Optimize
or
    Lock a specific real component

This means the same application can act as:

1. a full optimizer,
2. a fixed-configuration performance calculator,
3. or a mixed optimizer around hardware you already own or want to use.

## Component configuration

### Motor

Modes:

    Optimize
    Lock specific motor

The selector is loaded directly from the local PyThrust motor physics database,
not just the broad warehouse.

That is intentional: if the UI lets you lock a motor, it should be a motor for
which the physics engine has Kv, resistance, no-load current, current limit,
mass, etc.

When a motor is locked, the guide stage bypasses the ordinary motor
down-selection and forces that exact motor into the calculation.

### Propeller

Modes:

    Optimize
    Lock specific propeller

The selector is loaded from the local APC aerodynamic database and matched to
the APC physical/price catalog.

When a prop is explicitly locked, it bypasses the ordinary optimizer's
diameter/pitch candidate envelope. It still must have usable aerodynamic and
physical catalog data.

This makes fixed-part analysis behave more like a calculator rather than
silently dropping the selected prop because it would not normally have been
chosen by the broad optimizer.

### Battery

Modes:

    Optimize battery
    Lock commercial pack
    Lock cell, optimize pack topology

#### Optimize battery

Normal behavior: compare eligible custom cell-based packs and commercial packs.

#### Lock commercial pack

The exact pack's:

- series count
- nominal voltage
- capacity
- mass
- current capability
- price when known

are used.

The motor/prop guide search is also changed to that pack's actual nominal
voltage rather than forcing the ordinary 4S/6S/8S guide cases.

This lets a locked 10S, 12S, etc. pack be analyzed correctly.

#### Lock cell, optimize pack topology

The selected real cell chemistry/model is fixed, but the optimizer still chooses
the S/P topology.

This is useful when you have already decided, for example, that a specific
Molicel cell is the cell platform you want to build around but still want the
optimizer to decide the best pack geometry.

### ESC

Modes:

    Optimize
    Lock specific ESC

A locked ESC must still pass:

- battery-voltage compatibility
- actual modeled maximum motor-current requirement
- configured ESC current margin

The program will reject the aircraft rather than silently substitute a different
ESC.

## Three useful workflows

### Full optimization

Motor: Optimize
Prop: Optimize
Battery: Optimize
ESC: Optimize

This behaves like the v0.6 optimizer.

### Fixed configuration analysis

Motor: Locked
Prop: Locked
Battery: Locked commercial pack
ESC: Locked

The program evaluates the specified propulsion system, sizes the conceptual
frame, solves hover/endurance/current/TW, and gives the aircraft a decision
score.

For a true one-aircraft calculator workflow, also set the best known fixed
camera/avionics mass rather than using the fixed-mass sensitivity cases.

### Mixed optimization

Examples:

    Motor: Locked
    Prop: Optimize
    Battery: Optimize
    ESC: Optimize

or:

    Motor: Locked
    Prop: Locked
    Cell: Locked, optimize S/P
    ESC: Optimize

This is the core advantage over a traditional calculator: the program can hold
what you know constant and optimize only what remains undecided.

## Cache correctness

Component selection is included in the physical-design cache key.

Therefore a validated design pool generated with:

    Motor A locked

will not be incorrectly reused for:

    Motor B locked

or:

    Motor optimized.

The guide-search cache likewise records locked motor, locked propeller, and
locked commercial-pack voltage.

## Result traceability

The Results page now records the component configuration used for the run:

    Motor       Optimize / Locked
    Propeller   Optimize / Locked
    Battery     Optimize / Locked pack / Locked cell + optimized topology
    ESC         Optimize / Locked

The exact IDs are preserved in run_settings.json.

## Portfolio / deployable-site direction

The current Streamlit/local application is an excellent engineering prototype,
but the project is now large enough that moving the software portion into a
normal source-controlled application repository is worthwhile.

A portfolio-quality production architecture should separate:

    browser UI
        ↓
    API
        ↓
    optimization jobs / worker
        ↓
    component + propulsion caches

The expensive OpenMDAO/PyThrust work should remain server-side.

A future public demo could provide:

- fixed/optimize selectors,
- mass/TW/frame inputs,
- weighted decision controls,
- component details,
- result plots,
- top-25 comparisons,
- saved design links,
- a limited live optimization queue,
- and/or precomputed example studies for visitors.

The local engineering version can remain the unrestricted development and
validation environment.

## Suggested migration architecture

Frontend:
    React / Next.js or another polished web frontend

Backend:
    Python + FastAPI

Optimization engine:
    existing PyThrust/OpenMDAO code moved into a clean Python package

Data:
    normalized component DB + persistent propulsion cache

Long-running runs:
    background job worker / queue

Portfolio demo:
    deploy frontend + API + worker in containers

This separation also makes automated testing much easier than continuing to
grow all logic inside one Streamlit file.

## Upgrade

1. Stop Drone Optimizer.
2. Extract this ZIP directly into:

       C:\Users\Campbell\Documents\PyThrust

3. Merge/replace files.
4. Launch:

       Launch Drone Optimizer.bat

No warehouse rebuild is required solely for the v0.7 software update.

The component selectors read the databases already on disk.
