# Task application runtime

Every benchmark task owns its scenario data in `tasks/<name>/demo/seed.json`.
Each task declares the real applications it needs in `task.toml`.

## Lifecycle

All recording and snapshot entry points use `showAndTell.task_runtime`, which
constructs one `AppSession` from those application names:

1. load and validate the task and its seed;
2. create a unique run identity;
3. acquire one host-agent lease covering every declared application;
4. publish each browser-visible origin and idempotently start every lifecycle driver;
5. reset each application through its driver and state plane;
6. validate that the seed contains exactly the declared application blocks and
   route each block to its state plane;
7. run the demonstration and write `task-runtime.json` with seed provenance;
8. release the application lease.

The lifecycle driver owns containers and process health. The state plane owns
task records through supported application APIs. Browser operations come from
the application's `browser.py`; there is no registry mapping a task family to
an implementation.

## Contributor commands

Validate a task and its owned seed:

```bash
uv run showAndTell task-validate --task customer-returns-inbox-triage
```

Open the exact isolated and seeded application stack used by recordings:

```bash
uv run showAndTell task-serve --task customer-returns-inbox-triage
```

`Ctrl+C` releases the session lease. Downloaded images remain.

## Adding a task

If the applications already exist, add only the task artifacts: `task.toml`,
`task_logic.py`, `demonstrate.py`, `demo/seed.json`, narration, and quiz data.
Do not add a new application merely for a different scenario or seed. Add one
only for a genuinely new product surface and lifecycle contract.
