# The office-network runner (the robot on the owner's computer)

About 3,000 documents have no file because the company's site refuses requests from cloud servers, where GitHub runs our robot. On 23 October 2026 the free routes from the cloud (a browser-like request, Internet Archive copies) recovered none of 61 recent sustainability and annual reports. From an ordinary office or home connection most of these sites open normally. So the robot also runs on the owner's Windows computer, once each weekday, for those files only.

## How it works
- A GitHub **self-hosted runner** is installed on the computer. It is attached **only to the private repository `wba-text-store`**, never to this public repository (on a public repository, anyone opening a pull request could run code on the computer).
- The workflow **Office-network fetch** in `wba-text-store` runs on weekdays at 12:17 UTC (14:17 Madrid in winter, 13:17 in summer). If the computer is off, the run waits for it, up to a day.
- It runs this repository's `pc_fetch.py`: up to 300 refused files, recent sustainability and annual reports first, one request at a time per site with a pause, at most 90 minutes. A PDF of 5 MB or less is attached to its row; a bigger one is read on the computer (first six pages) so it can be typed and dated; every row tried gets `retried_on` and a `retry_note`.
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
Nothing. Keep the runner window open (it starts with Windows). The Monday notification says how many refused files were recovered. If the computer is replaced: Settings, Actions, Runners, remove the old runner, and repeat steps 4 to 6.

## Turning it off
GitHub, `wba-text-store`, Actions, Office-network fetch, the "..." menu, Disable workflow. To remove it completely: Settings, Actions, Runners, remove the runner, and delete `C:\actions-runner`.
