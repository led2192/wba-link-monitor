# lanterN's own job machine

Since 14 Nov 2026 the project rents one server for the jobs GitHub's machines run today: a virtual server with 16 cores, 64 GB of memory and 1,024 GB of disk in Nuremberg, on a one-month contract. Why a server at all: the repository is to become private, and a private repository gets 2,000 free job minutes a month where the pipeline uses about 6,800 a day (measured over 7.3 days in November 2026).

The server was installed on 14 Nov 2026 and its six runners answer. No pipeline job has moved yet. Jobs move one at a time, each one run next to its GitHub copy first; GitHub's machines stay the fallback until the server has had several good nights.

## What is on the server
- Ubuntu 24.04, automatic security updates, a firewall that lets only SSH in, fail2ban against password guessing.
- A user `runner` the jobs run as.
- Six GitHub Actions runners (`lantern-1` to `lantern-6`), each a service that starts with the machine, all with the label `lantern`. A job goes to the server when its workflow says `runs-on: [self-hosted, lantern]`.
- No secret is stored on the server. The jobs receive their secrets from GitHub at run time, as on GitHub's machines.

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
The server starts the scheduled jobs at their time (`clock.py`, workflow **Clock**, always running like the Scan now watch). What starts when is the table `server/clock.json`: the workflow file, its times of day in UTC, and the inputs. To move a scheduled workflow to the clock: remove its `schedule:`, add the input `as_scheduled` and make it do what the scheduled run did, add its line to the table. A test fails if a workflow in the table still has a schedule of its own.

## The database (16 Nov 2026)
The server also holds a database of its own, for the search by meaning: PostgreSQL 17 with pgvector 0.8, the same major versions as Supabase, so the same SQL runs on both. Why here: the passages of every year and their index (about 21 million passages, an index of about 21 GB) need more memory than the Supabase machine has, and this machine has it already. Supabase keeps everything else.

`server/db_install.sh` installs it and the workflow **Server database** (Actions, manual) runs it: `check` reads, `install` is safe to run again, `stop` is the way back and deletes nothing.

- It listens on the machine itself only. The firewall is not changed: the database's port is shut to the world.
- Every connection over the network interface needs a password. The one way in without a password is the system user `postgres` on the machine's own socket, for administration.
- A ceiling of 28 GB of memory and six cores for its processes, so the other jobs are not starved. An index build raises it for the time of the build, with the runners paused.
- The packages come from the PostgreSQL project's own source (Ubuntu 24.04 carries pgvector 0.6, which has no half-precision vectors), set up by the script Ubuntu ships for that. Their security updates arrive with the system's automatic updates.
- Step 2 creates an empty database `lantern` with the vector extension: no tables, no roles, no data.
- The runners on this machine can act as root, so a job run here can reach the database whatever its passwords. The guard is who may run a job here: step 2 of "Setting it up" (approval for every outside contributor) while the repository is public.
- Never on this database: company contacts.

Before real data goes in, **Server rehearsal** (Actions, manual) measures the move on a sample: it copies a million passages from Supabase, builds their index, measures and drops them. Its numbers decide the settings of the full build.

**Server meaning** (Actions, manual) then holds the real copy: the schema `meaning` with `chunks` (a passage's document, page, number and half-precision embedding; never its text) and `chunk_filters` (a document's company, year and type), the index on the embeddings, and the comparison with Supabase search by search. Until the switch `meaning_remote` exists and is on, nothing reads it: the search people use is Supabase's own.

## Checking it
The workflow **Server check** (Actions, manual, and every Monday) runs on the server and fails if the firewall, fail2ban or the automatic updates are off, or if the first-boot token is still on disk. It also shows the cores, memory and free disk, and installs the repository's packages the way a job does.

## Rules
- A workflow that runs on a pull request never uses the server. Only `tests.yml` runs on pull requests, and it stays on GitHub's machines.
- The runners are attached to this repository only.
- The server keeps one thing: the working copy of the documents' files under `/srv/lantern/files` (since 14 Nov 2026; the other copy is in the project's storage). Anything else a job leaves on its disk may be deleted.
