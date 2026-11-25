# lanterN's own job machine

Since 14 Nov 2026 the project rents one server for the jobs GitHub's machines run today: a virtual server with 16 cores, 64 GB of memory and 1,024 GB of disk in Nuremberg, on a one-month contract. Why a server at all: the repository is to become private, and a private repository gets 2,000 free job minutes a month where the pipeline uses about 6,800 a day (measured over 7.3 days in November 2026).

The server was installed on 14 Nov 2026 and its six runners answer. No pipeline job has moved yet. Jobs move one at a time, each one run next to its GitHub copy first; GitHub's machines stay the fallback until the server has had several good nights.

## What is on the server
- Ubuntu 24.04, automatic security updates, a firewall that lets only SSH in, fail2ban against password guessing.
- A user `runner` the jobs run as.
- Six GitHub Actions runners (`lantern-1` to `lantern-6`), each a service that starts with the machine, all with the label `lantern`. A job goes to the server when its workflow says `runs-on: [self-hosted, lantern]`.
- No secret of the jobs is stored on the server: they receive their secrets from GitHub at run time, as on GitHub's machines. Since 16 Nov 2026 the server keeps two secrets of its own, made there (see "The service").

## Setting it up (once, about 10 minutes)
1. In the provider's panel, install Ubuntu 24.04 if the server came with something else.
2. In GitHub: Settings, Actions, General, "Approval for running fork pull request workflows from contributors": choose **Require approval for all external contributors**. While this repository is public, this is what stops a stranger's pull request from running on the server. Do not register a runner before this is set.
3. In GitHub: Settings, Actions, Runners, New self-hosted runner, Linux. Leave the page open: the token is the value after `--token` in the "Configure" box. It works for one hour.
4. Log in to the server as root and run:
   ```
   curl -fsSL https://raw.githubusercontent.com/led2192/wba-link-monitor/main/server/setup.sh -o setup.sh && bash setup.sh
   ```
   Paste the token when it asks. It ends with the list of runner services and the size of the machine.
5. In GitHub, Settings, Actions, Runners: six runners named `lantern-1` to `lantern-6`, all "Idle".

**Without typing on the server at all** (how it was done on 14 Nov 2026): in the provider's panel, Media, Images, choose Ubuntu 24.04 and paste `server/first_boot.sh` into "Custom Script", with the token in place of `PASTE_THE_TOKEN_HERE`. The installed system then runs `setup.sh` by itself on its first start; the token is deleted from the server when it has finished and the record is in `/root/lantern-setup.log`.

Running the script again is safe: it updates the system and registers only the runners that are missing. `RUNNERS=8 bash setup.sh` asks for another number; `bash setup.sh --no-runners` does the system part only.

## Moving a job to the server, and back
A workflow that can run on the server chooses its machine from the repository variable `LANTERN_JOBS` (GitHub: Settings, Secrets and variables, Actions, Variables): a list of job names separated by spaces. A job named there runs on the server; a job not named runs on GitHub's machines. Taking the name out is the way back; it needs no pull request and takes effect at the job's next start.

| Name in `LANTERN_JOBS` | Workflow | Able to move since | Moved on |
| --- | --- | --- | --- |
| `scan_now` | Scan now (the watcher) | 14 Nov 2026 | 14 Nov 2026, 14:26 UTC |
| `library_mirror` | Library mirror (nightly, 04:43 UTC) | 14 Nov 2026 | 15 Nov 2026, 06:27 UTC (38 minutes there, 27 on GitHub's machine the day before) |

First result of Network compare (14 Nov 2026, 400 pages): the same answer on both machines for 94% of the pages; GitHub's machine got an answer from 331 and the server from 335; 9 pages answered GitHub only and 13 the server only. One run, so the difference of 4 may be chance; it shows the server's address is not treated worse.

Before a job that reads company sites moves, run **Network compare** (Actions, manual): it reads the same sample of monitored pages from both machines and shows how many answer one and not the other.

What differs on the server and has to be watched for each job moved: the pages are read from another network address (a company site may answer it differently from GitHub's), the Python packages of one job stay installed for the next, and files a job leaves outside its checkout stay on the disk.

## The clock
The server starts the scheduled jobs at their time (`clock.py`, workflow **Clock**, always running like the Scan now watch). What starts when is the table `server/clock.json`: the workflow file, its times of day in UTC, and the inputs. To move a scheduled workflow to the clock: remove its `schedule:`, add the input `as_scheduled` and make it do what the scheduled run did, add its line to the table. A test fails if a workflow in the table still has a schedule of its own. An entry may name `days` (`["mon"]`, in UTC) for a job that runs weekly; with none it runs every day.

## The database (16 Nov 2026)
The server also holds a database of its own, for the search by meaning: PostgreSQL 17 with pgvector 0.8, the same major versions as Supabase, so the same SQL runs on both. Why here: the passages of every year and their index (about 21 million passages, an index of about 21 GB) need more memory than the Supabase machine has, and this machine has it already. Supabase keeps everything else.

`server/db_install.sh` installs it and the workflow **Server database** (Actions, manual) runs it: `check` reads, `install` is safe to run again, `stop` is the way back and deletes nothing.

- It listens on the machine itself only. The firewall is not changed: the database's port is shut to the world.
- Every connection over the network interface needs a password. The one way in without a password is the system user `postgres` on the machine's own socket, for administration.
- A ceiling of 40 GB of memory (28 GB until 25 Nov 2026; the owner agreed to raise it so that the index of every main report fits; the machine has 64 GB and 47 GB were free that morning) and six cores for its processes, so the other jobs are not starved. An index build raises it for the time of the build, with the runners paused.
- The packages come from the PostgreSQL project's own source (Ubuntu 24.04 carries pgvector 0.6, which has no half-precision vectors), set up by the script Ubuntu ships for that. Their security updates arrive with the system's automatic updates.
- Step 2 creates an empty database `lantern` with the vector extension: no tables, no roles, no data.
- The runners on this machine can act as root, so a job run here can reach the database whatever its passwords. The guard is who may run a job here: step 2 of "Setting it up" (approval for every outside contributor) while the repository is public.
- Never on this database: company contacts.

Before real data goes in, **Server rehearsal** (Actions, manual) measures the move on a sample: it copies a million passages from Supabase, builds their index, measures and drops them. Its numbers decide the settings of the full build.

**Server meaning** (Actions, manual) then holds the real copy: the schema `meaning` with `chunks` (a passage's document, page, number and half-precision embedding; never its text) and `chunk_filters` (a document's company, year and type), the index on the embeddings, and the comparison with Supabase search by search. Until the switch `meaning_remote` exists and is on, nothing reads it: the search people use is Supabase's own.

## The service (16 Nov 2026)
One small door in front of the database of passages, for Supabase's search to ask "the passages nearest in meaning to this question, with these filters". `server/meaning_service.py` is the service and `server/service_install.sh` installs it; the workflow **Server service** (Actions, manual) runs `check`, `install`, `open`, `close` and `stop`.

- Two routes and nothing else: `POST /near` (needs the secret; answers keys and distances, never text) and `GET /health` (on the machine itself only, for the checker).
- The service listens on the machine itself. A web server (Caddy) in front holds the certificate and passes on `POST /near` only. `install` opens no port; `open NAME` opens 80 and 443 once the name points to the server, and the certificate is fetched and renewed on its own. `close` shuts them again.
- The secret is checked before any body is read, in constant time; two secrets can be valid at once so one can be replaced without a gap. Every input is checked exactly. A request with no stated length, another method, or a body over 64 KB is refused unread and the connection closed.
- The database role can only read the two tables (the service keeps four connections; the role allows six), two seconds a statement. Requests a minute are limited for each caller and in all.
- What the server now keeps, readable by root and the service's user only: `/etc/lantern/meaning.env` (the read-only role's password, made on the server) and `/etc/lantern/meaning.secrets` (the secret Supabase sends). Neither is printed or leaves the machine through a log. This ends "no secret is stored on the server" for these two.
- What it does not protect against: a job run on this machine. The runners can act as root here, so they can read those files and the database. The guards for that are who may run a job here (approval for every outside contributor; no workflow started by a pull request may run on the server, which a test checks) and, later, the repository going private.
- Reviewed by an outside session before any port was opened (16 Nov): twelve findings, all taken (the health route not passed on, the secret before the body, connections closed on refusal, no unlimited `limit`, a retry on a connection kept across a database restart, system packages instead of pip, a stricter unit, the name checked before a certificate is asked for).

### The search page under the owner's domain (19 Nov 2026)
The same web server serves the search page. The workflow **Search page** builds the files twice: for GitHub Pages as before, and in a job on the server that copies them to `/srv/lantern/www` (the folder is swapped whole, the one before kept as `www.old`). **Server service**, mode `site`, with the domain in `host`, adds the page's block to the web server's configuration: `/` is the search page, only GET and HEAD are answered, and `www.` forwards to the bare name when it points here too. The name is kept in `/etc/lantern/site_host`, so `open` writes the block again. `site` with host `off` takes the page away. The page holds no data and no secret: it carries the project address and the publishable key, as on GitHub Pages. A key link (`#k=`) works on both addresses; a browser remembers the key for each address apart, so a person who opens the new address the first time uses their link. The old address stays as it is.

### How Supabase uses the service
Only with the option `meaning_remote` on. It is on since 17 Nov 2026 (the owner's decision, step 6 of the move, after the first nightly refresh found the two copies equal: run 44002613107). `find_meaning` then asks the server first: 300 ms to connect, 1.5 seconds in all. The server's keys are joined to Supabase's own passages (for the text) and documents (company, year and type are checked again there, since the two stores can drift). The answer is used only when it fills the list asked for, or ten pages; with nothing, too little or an error, Supabase searches its own index as before, so a server that is down costs nothing. It is not true that the option can only add: a list from the server that is full but old, or of lower recall, takes the place of Supabase's own, which is why the copy is refreshed every night and compared (below). Three failures in a row and the server is not asked for 60 seconds, so that people do not wait on a machine that is down. The address and the secret live in Supabase's vault, put there from the server by **Server service**, mode `link`; mode `probe` measures the way between the two. The option changes which passages a search by meaning sees (the server filters while it searches and looks at 400 candidates), so by the house rule it is compared on the search exam before it becomes the default.

### Keeping the copy current, and seeing what happens (16 Nov 2026, outside review 3)
- Every night at 02:23 UTC the clock starts **Server meaning** as scheduled: `server_meaning.py --refresh --commit`. Documents whose passages Supabase holds and the server does not, or holds in a different number, are copied again; documents Supabase no longer holds are marked not live, so the service leaves them out; every document's company, year and type are brought up to date. Then the count a document is compared again. If any still differs, the option `meaning_remote` is switched off in Supabase (the one row this script ever writes there), the run fails and the quality log gets a failure.
- `meaning_remote_days` in Supabase counts, a day, what became of the searches that could ask the server: asked, used, too little, failed, skipped. Written only with the option on.
- An evaluation that rolls its searches back leaves no count there. Whether the server answered during one is read on the server: **Server service**, mode `check`, prints the searches answered and failed in the last hour.
- `server_meaning.py --switch N` compares Supabase's own search with the option off and on for searches with no company (no filter, two years, a type), each judged against the exact search on the server with the same filter.

### The documentation, for the owner's panel (21 Nov 2026)
The door has a second path, `/docs`, behind the same shared secret and rate limit: an empty request lists the documents of `/srv/lantern/docs` (name, first heading, size, time changed), a request with a name gives that document. Only a plain file name ending in `.md` is opened; a link in the folder is never followed; the database is not touched. The folder is filled by the workflow **Server docs** and is not under the web server's folder. In Supabase, `admin_docs(key, name)` calls it for the owner alone (`admin_ok`), with a timeout of six seconds. After a change to `server/meaning_service.py`, run **Server service** with mode `install`: it copies the new file and restarts the service (the search falls back to Supabase's own index for the seconds in between).

### Documents kept on the server only (21 Nov 2026)
Policies, codes of conduct and human rights policies of every year (42,673 documents, 568,792 pages on 20 Nov 2026) have their passages on the server and nowhere else: Supabase's disk has no room for them. **Server own index** (Actions, manual) runs `server_own_index.py`: pages read from Supabase, cut and embedded on the server with the same model, written to `meaning.chunks`, and the document to `meaning.chunk_filters` with `own = true`. A run stops after its minutes and the next continues; two runs can share the work with `partition` 0/2 and 1/2.
- They are not live until switched on (`live: on` with `commit`), and can be switched off again. The setting is the row `own_live` of `meaning.settings`.
- The nightly refresh does not compare them with Supabase (it holds none of their passages) and does not retire them; it brings their company and year up to date and takes out of the search a document Supabase no longer holds.
- The search shows the page's own text for them (the first 600 characters), since the passage's text is not kept. `--load --reload` of Server meaning empties them with everything else: they are then computed again.
- Before the switch the search exam is run, and again after it; the switch is kept only if no kind of question gets worse by more than 3 points.

## Checking it
The workflow **Server check** (Actions, manual, and every Monday) runs on the server and fails if the firewall, fail2ban or the automatic updates are off, or if the first-boot token is still on disk. It also shows the cores, memory and free disk, and installs the repository's packages the way a job does.

## Rules
- A workflow that runs on a pull request never uses the server. Only `tests.yml` runs on pull requests, and it stays on GitHub's machines.
- The runners are attached to this repository only.
- The server keeps one thing: the working copy of the documents' files under `/srv/lantern/files` (since 14 Nov 2026; the other copy is in the project's storage). Anything else a job leaves on its disk may be deleted.
