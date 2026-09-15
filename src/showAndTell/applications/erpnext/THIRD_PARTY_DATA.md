# Third-party demo data

`demo_dataset.json` is derived from the
[Northwind sample dataset](https://github.com/microsoft/sql-server-samples/tree/master/samples/databases/northwind-pubs)
in Microsoft's `sql-server-samples` repository, which is licensed under the
MIT License.

The checked-in transformation anglicizes names, moves locations to
English-speaking regions, and remaps the catalog (item groups, suppliers,
customers, items) plus purchase and sales documents onto ERPNext masters for
ShowAndTell capture worlds. The HR side of the same fixture is covered
separately in `hrms/THIRD_PARTY_DATA.md`.

Source: https://github.com/microsoft/sql-server-samples

License: MIT — see `LICENSE.mit.txt`.
