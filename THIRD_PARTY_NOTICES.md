# Third-party notices

ShowAndTell's own code is MIT-licensed (see [LICENSE](LICENSE)). The benchmark
runs third-party open-source applications as fixtures and derives some seed
data from third-party datasets. Those works are not relicensed by this
project: they retain their upstream terms, listed here. Copies of the GPL-3.0
and AGPL-3.0 texts are checked in under [licenses/](src/showAndTell/licenses/).

## Fixture applications

The applications below are pulled from upstream registries, built locally, or
side-loaded from the WebArena release archives, and run as task fixtures. Container images are never redistributed by this project.

| Application | License | Source |
|---|---|---|
| ERPNext | GPL-3.0 | https://github.com/frappe/erpnext |
| Frappe Framework | MIT | https://github.com/frappe/frappe |
| Frappe HR (HRMS) | GPL-3.0 | https://github.com/frappe/hrms |
| ONLYOFFICE Docs (Community Edition) | AGPL-3.0 | https://github.com/ONLYOFFICE/DocumentServer |
| Roundcube | GPL-3.0-or-later (skin/plugin exception) | https://github.com/roundcube/roundcubemail |
| Dovecot | LGPL-2.1 / MIT | https://github.com/dovecot/core |
| Mailpit | MIT | https://github.com/axllent/mailpit |
| Fleetbase / Fleet-Ops | AGPL-3.0 | https://github.com/fleetbase/fleetbase |
| Twenty CRM | AGPL-3.0 | https://github.com/twentyhq/twenty |
| GitLab CE | MIT | https://gitlab.com/rluna-gitlab/gitlab-ce |
| Magento Open Source | OSL-3.0 / AFL-3.0 | https://github.com/magento/magento2 |
| Postmill | Zlib | https://gitlab.com/postmill/Postmill |
| kiwix-serve | GPL-3.0 | https://github.com/kiwix/kiwix-tools |
| WebArena (environment images) | Apache-2.0 | https://github.com/web-arena-x/webarena |
| WebArena-Verified (optimized map environment and setup metadata) | Apache-2.0 | https://github.com/ServiceNow/webarena-verified |
| OpenStreetMap website (WebArena map environment) | GPL-2.0 | https://github.com/openstreetmap/openstreetmap-website |
| Nominatim (WebArena map geocoding) | GPL-3.0 | https://github.com/osm-search/Nominatim |
| OSRM Backend (WebArena map routing) | BSD-2-Clause | https://github.com/Project-OSRM/osrm-backend |
| mod_tile / renderd (WebArena map tiles) | GPL-2.0 | https://github.com/openstreetmap/mod_tile |
| OpenStreetMap Carto | CC0-1.0 | https://github.com/gravitystorm/openstreetmap-carto |
| OpenStreetMap data | ODbL-1.0 | https://www.openstreetmap.org/copyright |
| SocketCluster (Fleetbase realtime) | MIT | https://github.com/SocketCluster/socketcluster |
| Python slim base image (ONLYOFFICE connector build) | PSF-2.0 | https://github.com/python/cpython |
| MariaDB | GPL-2.0 | https://mariadb.com/kb/en/mariadb-licenses/ |
| MySQL | GPL-2.0 | https://github.com/mysql/mysql-server |
| Redis (6.2, ERPNext fixture) | BSD-3-Clause | https://github.com/redis/redis/blob/6.2/COPYING |
| Redis (7.4, Twenty fixture) | RSALv2 / SSPLv1 | https://github.com/redis/redis/blob/7.4/LICENSE.txt |
| Valkey | BSD-3-Clause | https://github.com/valkey-io/valkey |
| PostgreSQL | PostgreSQL License | https://www.postgresql.org/about/licence/ |
| nginx | BSD-2-Clause | https://nginx.org/LICENSE |

## Application state snapshots

Task bundles may include state snapshots (`demo/*-state.tar`): database dumps
of a seeded fixture, captured so a task's world can be restored exactly.
Alongside data authored for this benchmark, these dumps necessarily contain
application-derived content — the schema the application generates and
metadata rows (for example ERPNext DocType/DocField definitions, print
formats, and reports, or Fleet-Ops fixture records) copied from the
application into its database at install or seed time. Those portions remain
under the application's license:

- `erpnext-state.tar` — contains ERPNext / Frappe HR database content,
  GPL-3.0 (https://github.com/frappe/erpnext).
- `fleetbase-state.tar` — contains Fleetbase Fleet-Ops database content,
  AGPL-3.0 (https://github.com/fleetbase/fleetbase).

Demonstration recordings and screenshots show the user interfaces of the
fixture applications listed above.

## Seed datasets

| Dataset | License | Used for | Notice |
|---|---|---|---|
| Northwind (Microsoft sql-server-samples) | MIT | ERPNext demo catalog | `src/showAndTell/applications/erpnext/THIRD_PARTY_DATA.md` |
| HR Synthetic Database (Doug Trajano) | Apache-2.0 | Frappe HR workforce | `src/showAndTell/applications/erpnext/hrms/THIRD_PARTY_DATA.md` |
