# Assistant progress messages

Chat Completions cannot directly supply the Responses assistant `phase`. This
adapter uses a conservative mapping: text accompanying tool calls is
`commentary`; a complete `stop` response without tools is `final_answer`.
Truncated responses remain commentary. Reasoning is emitted separately.

For JSON responses, a tool preamble precedes its tool items. Streaming messages
start as commentary and receive their definitive phase on
`response.output_item.done`, retaining the same item ID. The terminal output
array follows `output_index`, including buffered calls that complete after later
text. Reopened text/reasoning items use fresh buffers to avoid repeated content.

On history replay, a preceding assistant message, its reasoning and its tool
calls are kept in the same Chat assistant turn. Grouped tool outputs still
preserve upstream's image/notice ordering fix.

This mapping is a heuristic for Chat upstreams. It does not rewrite stored
histories, guarantee UI placement in every client, or infer a final answer from
an incomplete response. Tests exercise protocol items and HTTP, not a mobile UI.

```sh
python -m unittest server.tests.test_response_message_order \
  server.tests.test_tool_discovery server.tests.test_tool_discovery_http \
  server.tests.test_responses_api server.tests.test_reasoning_credential_roundtrip
```
