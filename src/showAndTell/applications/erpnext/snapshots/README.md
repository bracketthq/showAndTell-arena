# ERPNext snapshots

`golden.tar` is the portable baseline produced by
`scripts/vm-fixtures/refresh_demo_snapshot.py`. It carries the shared ERP demo
catalog and HR workforce for the unified ERPNext + HRMS site. Runtime snapshots
live under the fixture-host data root; this checked-in copy bootstraps a new
host. Older ERP-only snapshots are upgraded by the driver during restore.
