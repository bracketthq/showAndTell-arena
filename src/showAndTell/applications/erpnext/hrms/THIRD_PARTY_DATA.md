# Third-party demo data

`demo_dataset.json` is a deterministic, bounded subset of the
[HR Synthetic Database](https://huggingface.co/datasets/dougtrajano/hr-synthetic-database)
by Doug Trajano. The source dataset contains entirely synthetic HR data and is
licensed under the Apache License 2.0.

The checked-in transformation keeps all 4 business units, all 17 departments,
all 94 jobs, and 100 adult employees from the source's 15,700 employees. It
selects one adult employee per job when the source provides one, preserves the
available department managers and business-unit directors, then fills to 100
deterministically. It also adds Frappe-safe names where the source contains
duplicate department or job names, deterministic joining dates, non-deliverable
`showAndTell.example` email addresses, attendance device IDs, and derived reporting
relationships. No other dataset is mixed into this fixture.

Source: https://huggingface.co/datasets/dougtrajano/hr-synthetic-database

Upstream revision: `54c51c47e1ce420e1ab7614ed61ddd9f3c2728c5`

License: Apache-2.0 — see `LICENSE.apache-2.0.txt`.
