# Bundled upstream: tool-history pairing fix

The manager distributes wb2api as the `upstream-src` Release asset rather than tracking its full source. The reviewed patch in `dev/upstream-patches/0001-tool-result-pairing.patch` fixes two request-level failures in that archived source.

## Reproduction and behavior

A plugin notice can be inserted before the first tool result:

```text
assistant(tool_calls=[c1]) -> developer/system(notice) -> tool(c1)
```

The previous result-block repacker stopped before finding the first result. The orphan cleanup kept both sides because their IDs matched, leaving a broken sequence. The patch moves intervening system/developer notices after the results without crossing user or assistant boundaries or dropping their content.

If upstream still rejects a request with HTTP 400 / code 11148 (`tool calls and tool results do not match`), the gateway returns a request-level 400 with the upstream error. It does not rotate accounts or count it as an account failure. The request lease is released. Authentication, rate-limit, model and server errors retain their existing handling.

## Release integration

The new `dev/pack_fixed_upstream_src.py` copies the supplied source, applies the reviewed patches, then delegates archive generation to the existing `dev/pack_upstream_src.py`. The caller's input directory is not modified. Patches are checked before application. A fully applied patch is detected using a reverse check, so a future refreshed source asset can already contain the fix. Snapshot drift or partial application fails source packaging and requires maintainer review; the packer must not silently produce an unpatched archive.

The Release workflow, signing and draft-release steps are unchanged. After merging this change, the maintainer needs to regenerate and refresh the `upstream-src` asset once; subsequent manager releases will consume the fixed asset. This patch does not deploy or restart a running gateway.

## Manual validation / refreshing the upstream asset

The initial patch was based on `workbuddy2api-src.tar.gz` from the `upstream-src` tag, downloaded 2026-10-09:

```text
sha256: fc1b9c72891ee04eafca803086551a068b02a6d34babd7fdb0fe60da472b8d40
```

From the manager checkout, with a clean extracted source directory:

```bash
python3 dev/apply_upstream_patches.py /path/to/workbuddy2api
(cd /path/to/workbuddy2api && go test ./...)
python3 -m unittest server.tests.test_upstream_patches -v
python3 dev/pack_fixed_upstream_src.py /path/to/workbuddy2api -o /path/to/output \
  --stamp 'Fix tool-result notice ordering and request-level 11148 handling'
```

After review, maintainers must regenerate the archive with `dev/pack_fixed_upstream_src.py`, run the Go regression tests, and refresh the `upstream-src` asset before publishing a manager release. Merely merging this PR does not replace the currently uploaded asset. If a future snapshot no longer matches either the forward or reverse patch, rebase the patch against that snapshot and rerun its Go regression tests before publishing.
