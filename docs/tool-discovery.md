# Client-executed tool discovery

The Responses gateway translates `tool_search` with `execution: client` into a
collision-safe Chat function and restores native `tool_search_call` items on
JSON and streaming responses. Hosted search (including the default when
`execution` is omitted) is rejected before contacting the upstream.

Deferred function schemas stay out of the active Chat catalog until they are
loaded through `tool_search_output` or `additional_tools`. Repeated definitions
are deduplicated by namespace and name. Search history retains call/result IDs,
arguments and reasoning; loaded schemas are put in the catalog, not serialized
again as conversation text. Namespace restoration works with or without search.

The client controls the search argument schema: `query`, `goal` and other object
fields are preserved. Malformed JSON, duplicate keys, nonfinite numbers,
oversized arguments and truncated search turns are never executable. All search
arguments in a buffered batch are checked before any tool in that batch is
emitted. JSON-schema validation and search execution remain the client's job.

Streaming HTTP requests wait for the upstream's separate trailing usage chunk
before their terminal Responses event. Later content after a finish reason is
ignored. Input and cached-token usage therefore remain available for client
context accounting. A third-party client still needs to enable its own discovery
capability; this gateway does not change client configuration.

Run regressions:

```sh
python -m unittest server.tests.test_tool_discovery \
  server.tests.test_tool_discovery_http server.tests.test_response_message_order \
  server.tests.test_responses_api server.tests.test_reasoning_credential_roundtrip
```

See [the comparison and integration notes](codex-tools-comparison.md).
