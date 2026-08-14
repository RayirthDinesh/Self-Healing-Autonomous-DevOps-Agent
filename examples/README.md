# examples/

## `sample-payload.json`

A real, unedited CI failure: the `bug/easy-1-nameerror` branch of
[sre-demo-app](https://github.com/RayirthDinesh/sre-demo-app), where
`total_value` sums `transactins` instead of `transactions`. Four of twenty-three
tests fail with `NameError`.

This is exactly what GitHub Actions POSTs to `/webhook`, so posting it drives
the full production path - clone, map, patch, validate in Docker, PR - with no
CI run of your own and no repo to break first.

```bash
scripts/try-it.sh
```

The repo is public and the commit is pinned, so the run is reproducible. It
spends model credits (typically four to twelve calls) and, if `GITHUB_TOKEN` is
set, opens a PR against `RayirthDinesh/sre-demo-app` - a repo you almost
certainly cannot push to, so the run ends at "publish failed" unless you point
it at your own fork. Leave `GITHUB_TOKEN` unset to stop cleanly after
validation instead.

To build a payload from a different branch, or from your own repo, see
`scripts/capture-payload.sh`.
