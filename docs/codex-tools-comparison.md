# Codex tool compatibility: upstream and local fixes

Reviewed against upstream manager `d716210` (`main`, v1.0.90) on 2026-10-15.
The PR is a single incremental commit directly on that upstream revision.
Local `221b452` is a behavioral reference only; no deployment snapshot or local
proxy configuration is imported. Upstream tool-result grouping (#177), namespace
restoration (#172), and current gateway token clamping remain in place.

| Case | Upstream main | Previous local patch | Result in this PR |
|---|---|---|---|
| Ordinary namespace call without search | Restores namespace | Restores only when discovery is enabled | Keep upstream's unconditional restoration |
| Two tool results with images and an intervening system/developer notice | Groups all results before images/notices, avoiding 11148 | Inserts images/notices between results | Keep upstream's grouping/state machine |
| Deferred tools discovered at runtime | No search adapter; search items are not translated | Loads returned schemas and preserves search pairs | Port local discovery onto upstream |
| Text before calls on history replay | Emits two assistant turns, separating reasoning from calls | Reuses one assistant turn | Preserve text, reasoning and calls together |
| Final usage sent after `finish_reason` | Ends response before client receives trailing usage | Defers completion until end of stream | Keep deferred completion and cached-token accounting |
| Progress text plus tools | JSON places text after tools; no message phase | Orders preamble before tools, sets commentary/final phases | Keep local ordering and reset item buffers |
| HTTPX streaming error response | Uses unsupported async Response context manager; loses body | Reads/closes Response directly | Preserve upstream status and error detail |
| Search schema uses `goal` rather than `query` | Unsupported feature | Rejects valid schema-specific arguments | Preserve any JSON object; client validates its schema |
| Duplicate keys or malformed search in a multi-call batch | Unsupported feature | Can emit an earlier tool before finding malformed search | Reject ambiguous JSON and validate the complete batch first |
| Restricted same-named namespace tools | Falls back to unqualified selection | Also loses namespace in allowed-tools selection | Resolve qualified names before catalog filtering |

For example, replaying `reasoning -> assistant("先检查") -> function_call(f) ->
function_call_output(f)` previously sent two upstream assistant messages. This
PR sends one assistant containing the text, reasoning and call, followed by the
matching tool result. Conversely, tool-result images/notices continue to use
upstream's stronger grouping logic; the old deployed implementation is not
copied wholesale.

Validation uses synthetic credentials, mock HTTPX transports and local fixtures;
no production accounts or model calls are required. The same 35 new regression
cases were run against upstream, the old deployed file and the integrated file.
The integrated file passes all cases. Failures in upstream mostly reflect
missing features, so this is not a general quality score. Existing Responses and
reasoning regressions also pass (132 related tests in total). Full backend validation on the current upstream base is recorded in the PR.

The client-search contract was checked against the official guide:
https://developers.openai.com/api/docs/guides/tools-tool-search

This PR does not change account proxies, the upstream Go snapshot, or deployment
configuration. Upgrade after merge still needs to preserve any locally maintained
upstream patches.
