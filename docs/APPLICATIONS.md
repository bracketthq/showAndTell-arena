# Applications

Everything specific to one application lives in
`src/showAndTell/applications/<name>/`. The
registry is the directory listing: adding an application is adding a folder,
and nothing outside the folder names it.

```
src/showAndTell/applications/roundcube/
├── app.toml            identity, ports, credentials, capabilities
├── compose.yaml        containers
├── config/             files compose mounts into them
├── driver.py           lifecycle — start, reset, snapshot
├── state.py            task data — seed, export, capture
└── browser.py          Playwright — login, semantic operations
```

## Three planes

The split is not cosmetic. Each plane runs somewhere different and needs
different access, which is why bundling them scattered an application's
knowledge across ten files.

| Plane | Runs where | Needs | Owns |
|---|---|---|---|
| `driver.py` | beside the containers — your laptop, or the VM | the Docker socket | the application's existence. Never sees task data. |
| `state.py` | beside the harness | a published port | task data. Speaks the app's own API — HTTP, IMAP, REST. |
| `browser.py` | in the Playwright process | a page | interaction. Serves a demonstration and a replay alike. |

```python
# driver.py — the fixture host agent serves this over HTTP
class Driver:
    def __init__(self, manifest, *, root=None, runner=None): ...
    def start(self, *, wait=True) -> None
    def stop(self) -> None
    def status(self) -> dict          # {"state": ..., "ports": {...}}
    def reset(self) -> None           # coarse: back to an empty application
    def snapshot(self, name) -> None  # or raise Unsupported
    def restore(self, name) -> None   # or raise Unsupported
    def secrets(self) -> dict         # what the harness cannot guess

# state.py — held in process by AppSession
class State:
    application = "roundcube"
    def prepare(self, ctx) -> None            # make it usable for a capture
    def seed(self, ctx, block) -> None        # apply a task's declared state
    def export(self, ctx) -> dict             # what is there now
    def capture(self, ctx, directory) -> dict # freeze hand-made state to files
    def reset(self, ctx) -> None              # remove task-owned records
```

`prepare` and `capture` are optional. Most applications need no preparation and
hold no state a person can build by hand; the ones that do — a spreadsheet, a
mailbox — implement them so setup work reaches the draft instead of being lost.

### Seed forms, for state the application cannot create

Usually the application *is* the authoring UI: the operator sets it up in
managed Chrome during the setup stage. Some starting state has no such route —
a mail client composes outgoing mail, not mail arriving from a supplier; a
helpdesk shows tickets other people filed. An application declares a form for
exactly that, and the viewer opens it as a tab beside the applications:

```python
# src/showAndTell/applications/roundcube/state.py
class State:
    SEED_FORM = SeedForm(
        title="Inbox", row_noun="message",
        fields=(Field("sender", "From", required=True),
                Field("subject", "Subject", required=True),
                Field("body", "Message", kind="textarea"),
                Field("unread", "Leave unread", kind="checkbox", default=True)),
    )

    def form_block(self, ctx, rows):
        """Rows -> this application's seed block."""
```

The viewer renders the fields and never interprets a row: only Roundcube knows
a row becomes an RFC822 message addressed to the mailbox in `ctx.credentials`.
Declaring `SEED_FORM` without `form_block` is a loud error rather than a
half-working form.

Applying a form seeds the live application, so the author switches to its tab
and the data is there — and it is captured with everything else, through the
same `capture()` path. There is no second seeding mechanism. Row ids are
positional, so editing and re-applying replaces rather than accumulates.

The authoring tab is opened by managed Chrome only when some selected
application declares a form, and everything served from the viewer's origin is
excluded from the recording: filling in starting data is setup for the setup,
and replaying it would seed twice.

### Why `secrets()` exists

The driver runs beside the containers; the state plane runs on the harness and
reaches them over the network. Only the driver can answer "which host port did
Dovecot get, and does it want STARTTLS?" That answer crosses the boundary
through `secrets()` and arrives in `AppContext.secrets`. It is never inferred,
and it never travels as an environment variable.

## app.toml

The only file both the agent and the harness read. Neither imports the
application's Python to learn what it can run or what to open.

```toml
[app]
name  = "roundcube"
label = "Roundcube"

[compose]
file    = "compose.yaml"
service = "roundcube"
# external = true    for something already running that this repo does not deploy;
#                    the manifest then waives the compose-file requirement.

[ports]
app  = { container = 80,    host = 8082 }
imap = { container = 31143, host = 1143, service = "dovecot" }

[health]
http = "/"        # < 400 on the app port
tcp  = ["imap"]   # and a banner on this one

[credentials]
username = "agent@showAndTell.test"
password = "showAndTell-mail"

[surface]
entry = "/"       # where a capture opens the browser

[capabilities]
snapshots = false # the driver answers 501 for the snapshot family
capture   = true  # state.capture() writes assets
```

One manifest is what stops a port or a password disagreeing between the
container and the surface that logs into it.

## AppSession

Capture and a task run are the same lifecycle at different phases, so they
share one object built from a list of application names.

```python
session = AppSession(["roundcube", "erpnext"], client=agent, holder="capture-x")
session.start()               # one lease, publish origins, then start each driver
session.prepare()             # state.prepare() for each      — capture only
session.capture(demo_dir)     # state.capture() + driver.snapshot()
session.seed(seed_doc)        # route application-keyed blocks to state.seed()
session.export()              # application-keyed export back out
session.urls, session.credentials
session.browser_credentials() # primary login plus supporting-app context
session.close()               # release the lease
```

Routing is a dictionary lookup because task seeds and `AppSession` both use
application-keyed documents. The task runtime rejects missing, unknown, or
non-object application blocks before acquiring a host lease.

Multi-application demonstrations receive a reserved application-context block
inside their credentials argument. Browser adapters bind it to the Playwright
page and resolve supporting URLs and credentials through
`showAndTell.applications.browser.context`; they do not publish task-family environment
variables.

## Local and VM

The fixture host agent is one process that scans `showAndTell.applications` and serves
each folder's driver. Where it runs is the only difference.

| | Local | Shared VM |
|---|---|---|
| driver | agent auto-spawned on loopback | agent on the VM, `:8091` |
| state | harness-side | harness-side |
| browser | harness-side | harness-side |
| selected by | nothing set | `SHOWANDTELL_FIXTURE_HOST_URL` |
| bind address | loopback | `SHOWANDTELL_BIND_HOST=0.0.0.0` + firewall |

Keeping the state plane harness-side means a task's seed never ships to the VM:
the runner holds the task, the VM holds only containers.

Nothing about the VM enumerates applications. The agent serves whatever
`showAndTell.applications` contains, the unit sets one `SHOWANDTELL_BIND_HOST` that every
driver honours, and the fixture-host deployment reads the ports to
open out of the manifests. A per-application list in either place is a trap:
the new application starts, reports healthy against the VM's own loopback, and
is unreachable from anywhere else — which reads as a broken application rather
than a missing deployment line.

After adding a folder, redeploy the fixture host so the new application's
ports are served and reachable.

## Archived WebArena application prerequisites

The capture picker also exposes application adapters retained for archived
WebArena coverage. They are not downloaded by `./showAndTell setup`, because the
upstream fixtures are very large and no current benchmark task uses them. When
one is selected for a new task, the viewer shows the exact download and
temporary disk requirements, asks for confirmation, and then displays live
progress. Downloads can be canceled and resume from the retained partial file.
The sources match WebArena's
[official environment guide](https://github.com/web-arena-x/webarena/blob/main/environment_docker/README.md).
The consolidated map runtime and volume layout follow
[WebArena-Verified's map environment](https://servicenow.github.io/webarena-verified/dev/environments/map/):

| Application | Required local asset |
|---|---|
| GitLab | Docker image `gitlab-populated-final-port8023` |
| Magento storefront | Docker image `shopping_final_0712` |
| Magento Admin | Docker image `shopping_admin_final_0719` |
| Postmill | Docker image `postmill-populated-exposed-withimg` |
| Kiwix / Wikipedia | `wikipedia_en_all_maxi_2022-05.zim` in `~/.cache/webarena-images/` (or configure `WEBARENA_ZIM_DIR` and `SHOWANDTELL_WIKIPEDIA_ZIM`) |
| OpenStreetMap | WebArena-Verified's pinned single-container map runtime plus the official tile, Nominatim, and OSRM data archives (or configure `SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL` to use an existing deployment) |

Docker archives are retained under `~/.cache/showAndTell/webarena-assets/` after
import so a later Docker prune can reload without another network transfer.
The Wikipedia archive is SHA-256 verified. WebArena does not publish checksums
for its legacy Docker archives, so the viewer tries the upstream HTTPS Google
Drive links first, discloses and uses WebArena's HTTP CMU mirror only when Drive
is quota-limited, and checks the exact byte sizes plus required image names
after import. OpenStreetMap downloads three HTTPS WebArena S3 archives, retains
them for resumable/recoverable setup, extracts nine Docker volumes, starts the
pinned WebArena-Verified map image on loopback port 3000, and verifies the
website, world tile, Nominatim search, and OSRM routing endpoints before capture
continues. WebArena-Verified does not publish archive hashes, so exact S3 sizes
and the end-to-end service probes are the available integrity controls. The
current archives total 187,334,164,480 bytes, the pinned compressed image is
1,187,513,735 bytes, and fresh setup needs 384,445,777,287 free bytes for
downloads, extracted data, the runtime
image, and working overhead. The published map image is amd64-only, so Docker
emulation is used on Apple Silicon. An explicitly configured but unreachable
`SHOWANDTELL_OPENSTREETMAP_PUBLIC_URL` is never silently replaced by a local stack.

## Adding an application

1. `mkdir src/showAndTell/applications/<name>/`
2. Write `app.toml`.
3. Drop in `compose.yaml`, or set `external = true`.
4. Write `driver.py`. `reset()` is the only method needing real thought — it is
   the guarantee that nothing survives from a previous run.

   **Verify `reset()` against the real containers before trusting it.** It is
   the one method where being wrong is silent: the application still starts,
   still looks healthy, and simply serves the previous capture's data. Two
   traps found this way in Roundcube's driver:

   - The pinned `dovecot:2.4.3` image ships **no shell utilities at all** — no
     `rm`, `ls` or `find`. `sh -c "rm -rf /path/* || true"` therefore fails,
     the `|| true` swallows it, and the reset does nothing. Prefer the
     application's own admin tool (`doveadm expunge`) over filesystem surgery,
     and never write `|| true` around the step that does the actual work.
   - `doveadm expunge -A` needs a listable userdb, which the testing image has
     no way to provide. `-u <account>` is the form that works.
5. Write `state.py`. `capture` only if a person can build state by hand.
6. Write `browser.py` — `login()`, then the operations your tasks need.

There is no step seven. It appears in the capture picker because the folder
exists.

Every benchmark task now declares `applications` and a
`primary_application`. There is no fixture-family fallback: task execution and
capture both compose those application folders through `AppSession`.
