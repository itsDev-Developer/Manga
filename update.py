"""
Startup "updater" — DISABLED BY DEFAULT.

⚠️ What this used to do, silently, on every single restart:
    1. Deleted the local .git folder
    2. Re-initialized git and committed the current code
    3. Added a remote pointing at a third-party GitHub repo
       (https://github.com/itsDev-Developer/manwa - not this bot owner's repo)
    4. Ran `git reset --hard origin/master`

That last step force-overwrites every file in this project with whatever is
currently on that external repo, with zero review and zero way to opt out.
In practice that means:
  - Any fix or customization made to this bot (including everything in this
    update) gets silently wiped the next time the process restarts.
  - The bot's behavior is ultimately controlled by whoever can push to that
    external repo, not by you - a real supply-chain risk if you didn't
    write and don't control it yourself.

This is almost certainly why bugs kept coming back after being "fixed" -
start.sh runs this script before every launch of main.py.

If you understand the risk above and specifically want to auto-sync from
your OWN fork on every restart, set both of these in your environment and
this will do a *non-destructive* fetch/fast-forward (it will simply skip
instead of overwriting if your local copy has diverged, so you never lose
uncommitted changes by surprise):
    ENABLE_AUTO_UPDATE = true
    UPSTREAM_REPO       = https://github.com/<you>/<your-fork>
    UPSTREAM_BRANCH     = master   (optional, defaults to "master")
"""
import os
from subprocess import run as srun

ENABLE_AUTO_UPDATE = str(os.environ.get("ENABLE_AUTO_UPDATE", "")).strip().lower() in ("1", "true", "yes", "on")
UPSTREAM_REPO = os.environ.get("UPSTREAM_REPO", "").strip()
UPSTREAM_BRANCH = os.environ.get("UPSTREAM_BRANCH", "master").strip()

if not ENABLE_AUTO_UPDATE or not UPSTREAM_REPO:
    print("Auto-update is disabled (this is the safe default). "
          "Set ENABLE_AUTO_UPDATE=true and UPSTREAM_REPO to opt in - see the "
          "comment at the top of update.py before you do.")
else:
    try:
        if not os.path.exists(".git"):
            srun(["git", "init", "-q"])
        srun(["git", "remote", "remove", "origin"])  # ignore failure if it doesn't exist
        srun(["git", "remote", "add", "origin", UPSTREAM_REPO])
        fetch = srun(["git", "fetch", "origin", UPSTREAM_BRANCH, "-q"])
        if fetch.returncode != 0:
            print(f"Could not fetch {UPSTREAM_REPO}@{UPSTREAM_BRANCH}; leaving local files untouched.")
        else:
            # Fast-forward only: applies new upstream commits but refuses to
            # run (rather than silently discarding them) if local files
            # were changed - never a surprise `reset --hard`.
            merge = srun(["git", "merge", "--ff-only", f"origin/{UPSTREAM_BRANCH}"])
            if merge.returncode == 0:
                print(f"Fast-forwarded to origin/{UPSTREAM_BRANCH}.")
            else:
                print("Local files have diverged from upstream; skipped updating to avoid overwriting them.")
    except Exception as err:
        print(f"Auto-update skipped due to an error: {err}")
