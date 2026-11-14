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
| `scan_now` | Scan now (the watcher) | 14 Nov 2026 | |

What differs on the server and has to be watched for each job moved: the pages are read from another network address (a company site may answer it differently from GitHub's), the Python packages of one job stay installed for the next, and files a job leaves outside its checkout stay on the disk.

## Checking it
The workflow **Server check** (Actions, manual, and every Monday) runs on the server and fails if the firewall, fail2ban or the automatic updates are off, or if the first-boot token is still on disk. It also shows the cores, memory and free disk, and installs the repository's packages the way a job does.

## Rules
- A workflow that runs on a pull request never uses the server. Only `tests.yml` runs on pull requests, and it stays on GitHub's machines.
- The runners are attached to this repository only.
- The server is not a place to keep data: the documents live in Airtable and Supabase. Anything a job leaves on its disk may be deleted.
