# The office-network runner (the robot on the owner's computer)

About 3,000 documents have no file because the company's site refuses requests from cloud servers, where GitHub runs our robot. On 23 October 2026 the free routes from the cloud (a browser-like request, Internet Archive copies) recovered none of 61 recent sustainability and annual reports. From an ordinary office or home connection most of these sites open normally. So the robot also runs on the owner's Windows computer, every 2 hours, for those files and pages only.

## How it works
- A GitHub **self-hosted runner** is installed on the computer. It is attached **only to the private repository `wba-text-store`**, never to this public repository (on a public repository, anyone opening a pull request could run code on the computer).
- The workflow **Office-network fetch** in `wba-text-store` runs one long job a day that repeats the fetch every 2 hours for 22 hours (since 24 Oct 2026: GitHub skipped two of the four 4-hourly scheduled runs that day, so the job no longer depends on each schedule firing); the schedule every 2 hours stays as the fallback and starts a new job when none is running. Before that it ran every 2 hours (at 17 minutes past every even hour, UTC; every 4 hours until 24 Oct 2026). If the computer is off, at most one run waits for it and starts when it is back; nothing piles up, nothing breaks.
- It runs this repository's `pc_fetch.py`, at most 90 minutes a run, one request at a time per site with a pause. The files get the first 65 minutes and the pages the last 25 (since 24 Oct 2026: in the first scheduled run the 300 files used all 90 minutes and no page was read):
  - **refused files**: up to 300 a run, recent sustainability and annual reports first, each tried at most once a day. A PDF of 5 MB or less is attached to its row; a bigger one is read on the computer (first six pages) so it can be typed and dated; every row tried gets `retried_on` and a `retry_note`.
  - **refused report pages**: up to 150 a run, read longest ago first (`pc_checked`). The document links found go to `seen_links` and `new_links` as the nightly monitor writes them; the nightly harvest turns them into documents, and any file the cloud cannot then download comes back to the runner as a refused file. This is how reports on about 340 sites that refuse us entirely start to arrive.
  - **second pass in a browser** (owner's decision, 23 Oct 2026): whatever a plain request could not reach (up to 100 files a run, and that run's refused pages) is tried again in **Microsoft Edge, already on every Windows, without a window**. To the site it is an ordinary browser on an ordinary connection, so automatic "checking your browser" pages pass by themselves. Safeguards: a path the site's robots.txt disallows is skipped; nothing clicks, types or solves, so a captcha or a login stays refused; one page at a time with a pause. The workflow installs the small Python library that drives Edge; nothing else to install. To use Google Chrome instead, set the repository variable `PC_BROWSER` to `chrome`.
- The computer only downloads public reports and writes to Airtable. It never gets around a login, a captcha or a paywall.

## Setting it up (about 15 minutes, once)

**1. An Airtable token for the computer.**
Airtable, Builder hub, Personal access tokens, Create token. Name: `office-network runner`. Scopes: `data.records:read` and `data.records:write`. Access: the bases **Disclosure Library** and **Pipeline QA**. Create, and copy the token (it is shown once).

**2. Give the token to the private repository.**
GitHub, `led2192/wba-text-store`, Settings, Secrets and variables, Actions, New repository secret. Name: `AIRTABLE_TOKEN`. Value: the token. Add secret.

**3. Merge the workflow.**
In `wba-text-store`, merge the pull request "Office-network fetch".

**4. Install the runner on the computer.**
GitHub, `wba-text-store`, Settings, Actions, Runners, **New self-hosted runner**, choose **Windows** and **x64**. GitHub shows a short list of PowerShell commands with your own token in them. Open **PowerShell** (not as administrator) and paste them one block at a time:
- the "Download" block (creates `C:\actions-runner` and downloads the runner);
- the "Configure" block (`./config.cmd --url ... --token ...`). Press Enter to accept every default. When it asks **"Would you like to run the runner as service?"**, answer **N**.

**5. Make it start with Windows.**
Press Windows + R, type `shell:startup`, Enter. In the folder that opens, right-click, New, Shortcut, location `C:\actions-runner\run.cmd`, name it `GitHub runner`. From now on it starts when you log in (a small console window: minimise it, do not close it). Start it now by double-clicking the shortcut; it says "Listening for Jobs".

Why not as a service: a Windows service runs under a system account that does not see the Python installed for your user, and the job would fail.

**6. Check Python.**
In PowerShell: `python --version` should say 3.12 or newer. If it says the command is not found, reinstall Python from python.org and tick **"Add python.exe to PATH"**.

**7. First run, as a test.**
GitHub, `wba-text-store`, Actions, **Office-network fetch**, Run workflow, limit `30`, mode `dry_run`. When it finishes (a few minutes), open the run: the summary says how many refused files your connection reaches. Nothing is written in a dry run. If the numbers are good, the weekday runs write by themselves.

## Day to day
Nothing. Keep the runner window open (it starts with Windows). The Monday notification says how many refused files and pages were reached.

## A second computer
Repeat steps 4 to 6 on it (step 4 gives it its own runner name). Both serve the same workflow: one run at a time, picked up by whichever computer is on, and a file or page tried today is not tried again the same day. If a computer is replaced: Settings, Actions, Runners, remove the old runner, and repeat steps 4 to 6 on the new one.

## Turning it off
GitHub, `wba-text-store`, Actions, Office-network fetch, the "..." menu, Disable workflow. To remove it completely: Settings, Actions, Runners, remove the runner, and delete `C:\actions-runner`.
