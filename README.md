# logan-automation

Default branch of this fork so its scheduled workflow runs. It holds automation only; the code
that runs on the comma is on `logan-staging`.

`.github/workflows/sync-logan-staging.yml` (four times a day, or run it by hand from Actions):

1. Shallow-fetches `sunnypilot/sunnypilot` `staging`. Each upstream build is a single commit with no
   shared history.
2. Finds the build `logan-staging` sits on (the github-actions[bot] "(staging)" commit) and Logan's
   commits above it. Stops if upstream's tree is unchanged.
3. Cherry-picks those commits, in order, onto the new upstream commit. A conflict fails the run and
   opens (or comments on) the issue "logan-staging: upstream sync conflict"; `logan-staging` is left alone.
4. Runs py_compile on the changed Python files and the stdlib tests (deleter, phone_relay).
5. Saves the old head as `logan-staging-prev`, then force-pushes (with lease) the result to
   `logan-staging`. The comma's updater picks it up from there.

Run it by hand with **dry_run** to replay and test onto the current upstream build without pushing.

Roll back: `git push --force origin origin/logan-staging-prev:logan-staging`.
