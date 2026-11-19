"""Client tool discovery must survive the complete Responses/Chat round trip."""
import json
import unittest

from server.routers import responses as R


SEARCH = {'type': 'tool_search', 'execution': 'client', 'parameters': {
    'type': 'object', 'properties': {'query': {'type': 'string'}, 'limit': {'type': 'number'}},
    'required': ['query']}}


def tool(name='fetch', deferred=False):
    return {'type': 'function', 'name': name, 'defer_loading': deferred,
            'parameters': {'type': 'object', 'properties': {'id': {'type': 'string'}}}}


def loaded(ns='fixture'):
    return {'type': 'tool_search_output', 'call_id': 'search1', 'status': 'completed',
            'tools': [{'type': 'namespace', 'name': ns, 'tools': [tool(deferred=True)]}]}


def body(**kw):
    return {'model': 'deepseek-v4.1-flash', 'tools': [tool('exec_command'), SEARCH],
            'input': [{'role': 'user', 'content': 'Find and use the fixture.'}], **kw}


def chunk(name='', args='', finish=None, ident=None):
    call = {'index': 0, 'function': {'name': name, 'arguments': args}}
    if ident: call['id'] = ident
    return {'choices': [{'delta': {'tool_calls': [call]}, 'finish_reason': finish}]}


def events(chunks):
    return [json.loads(line[5:]) for raw in chunks for line in raw.decode().splitlines()
            if line.startswith('data:')]


class DiscoveryContractTest(unittest.TestCase):
    def test_undeferred_basic_tools_and_search_only(self):
        request = body(tools=[tool('exec_command'), SEARCH, tool('unused', True)])
        out = R.to_chat_request(request)
        self.assertEqual([x['function']['name'] for x in out['tools']], ['exec_command', 'tool_search'])
        self.assertTrue(request['tools'][-1]['defer_loading'])

    def test_loaded_tools_promoted_once_despite_deferred_marker(self):
        output = loaded()
        out = R.to_chat_request(body(input=[output, output]))
        self.assertEqual([x['function']['name'] for x in out['tools']], ['exec_command', 'fetch', 'tool_search'])
        self.assertNotIn('parameters', out['messages'][0]['content'])

    def test_search_history_preserves_pair_arguments_and_reasoning(self):
        inp = [{'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'find fixture'}]},
               {'type': 'tool_search_call', 'call_id': 'search1', 'execution': 'client',
                'arguments': {'query': 'fixture fetch', 'limit': 1}}, loaded()]
        out = R.to_chat_request(body(input=inp))
        self.assertEqual([m['role'] for m in out['messages']], ['assistant', 'tool'])
        call = out['messages'][0]['tool_calls'][0]
        self.assertEqual(call['id'], out['messages'][1]['tool_call_id'])
        self.assertEqual(json.loads(call['function']['arguments'])['query'], 'fixture fetch')
        self.assertEqual(out['messages'][0]['reasoning_content'], 'find fixture')

    def test_history_only_search_remains_translatable(self):
        out = R.to_chat_request(body(tools=[], input=[{'type': 'tool_search_call',
            'call_id': 'search1', 'arguments': {'query': 'fixture'}}, loaded()]))
        self.assertEqual(out['messages'][0]['tool_calls'][0]['function']['name'], 'tool_search')

    def test_additional_tools_are_loaded_not_user_messages(self):
        out = R.to_chat_request(body(input=[{'type': 'additional_tools', 'tools': [tool('extra', True)]}]))
        self.assertEqual(out['messages'], [])
        self.assertIn('extra', [t['function']['name'] for t in out['tools']])

    def test_forced_search_maps_to_function(self):
        out = R.to_chat_request(body(tool_choice={'type': 'tool_search'}))
        self.assertEqual(out['tool_choice'], {'type': 'function', 'function': {'name': 'tool_search'}})

    def test_allowed_tools_restricts_catalog(self):
        out = R.to_chat_request(body(tool_choice={'type': 'allowed_tools', 'mode': 'required',
                                                  'tools': [{'type': 'tool_search'}]}))
        self.assertEqual([t['function']['name'] for t in out['tools']], ['tool_search'])
        self.assertEqual(out['tool_choice'], 'required')

    def test_ordinary_same_name_is_not_restored_as_search(self):
        bridge = R._ToolBridge([tool('tool_search'), SEARCH])
        self.assertNotEqual(bridge.search_name, 'tool_search')
        result = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [{'id': 'c1', 'function': {'name': 'tool_search', 'arguments': '{}'}}]}}]},
            'm', 'resp1', bridge=bridge)
        self.assertEqual(result['output'][0]['type'], 'function_call')

    def test_tool_choice_none_disarms_search_restoration(self):
        bridge = R._ToolBridge([SEARCH])
        R.to_chat_request(body(tools=[SEARCH], tool_choice='none'), bridge=bridge)
        self.assertFalse(bridge.search_names)

    def test_namespace_collision_and_history_round_trip(self):
        bridge = R._ToolBridge([SEARCH], [loaded('one'), loaded('two')])
        self.assertNotEqual(bridge.outbound_name('fetch', 'one'), bridge.outbound_name('fetch', 'two'))
        name = bridge.outbound_name('fetch', 'two')
        out = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [{'id': 'c1', 'function': {'name': name, 'arguments': '{"id":"x"}'}}]}}]},
            'm', 'resp1', bridge=bridge)
        self.assertEqual(out['output'][0]['namespace'], 'two')
        inp = [loaded('one'), loaded('two'), out['output'][0]]
        chat = R.to_chat_request(body(input=inp), bridge=bridge)
        self.assertEqual(chat['messages'][-1]['tool_calls'][0]['function']['name'], name)

    def test_json_response_restores_client_search(self):
        bridge = R._ToolBridge([SEARCH])
        out = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls', 'message': {
            'tool_calls': [{'id': 'search1', 'function': {'name': bridge.search_name,
                'arguments': '{"query":"fixture","limit":1}'}}]}}]}, 'm', 'r', bridge=bridge)
        item = out['output'][0]
        self.assertEqual((item['type'], item['execution'], item['call_id']),
                         ('tool_search_call', 'client', 'search1'))
        self.assertEqual(item['arguments']['query'], 'fixture')

    def test_fragmented_search_names_and_arguments_use_completed_item(self):
        bridge = R._ToolBridge([SEARCH])
        tr = R._StreamTranslator('m', 'r', bridge=bridge)
        raw = tr.feed(chunk('tool_', '{"query":', ident='search1'))
        raw += tr.feed(chunk('search', '"fixture"}', finish='tool_calls'))
        es = events(raw)
        done = next(e['item'] for e in es if e['type'] == 'response.output_item.done')
        self.assertEqual(done['type'], 'tool_search_call')
        self.assertEqual(done['arguments'], {'query': 'fixture'})
        self.assertEqual(es[-1]['type'], 'response.completed')
        self.assertFalse(any('function_call_arguments' in e['type'] for e in es))

    def test_truncated_search_is_not_executable(self):
        tr = R._StreamTranslator('m', 'r', bridge=R._ToolBridge([SEARCH]))
        es = events(tr.feed(chunk('tool_search', '{"query":"fixture"}', finish='length')))
        self.assertEqual(es[-1]['type'], 'response.incomplete')
        self.assertFalse(any(e['type'] == 'response.output_item.done' for e in es))

    def test_bad_search_is_a_terminal_failure(self):
        tr = R._StreamTranslator('m', 'r', bridge=R._ToolBridge([SEARCH]))
        es = events(tr.feed(chunk('tool_search', '{"query":', finish='tool_calls')))
        self.assertEqual(es[-1]['type'], 'response.failed')
        self.assertEqual(es[-1]['response']['error']['code'], 'invalid_tool_search_arguments')
        self.assertFalse(tr.items)

    def test_buffer_limit_is_a_terminal_failure(self):
        tr = R._StreamTranslator('m', 'r', bridge=R._ToolBridge([SEARCH]))
        es = events(tr.feed(chunk('tool_search', 'x' * (R.MAX_SSE_BUFFER + 1), finish='tool_calls')))
        self.assertEqual(es[-1]['type'], 'response.failed')

    def test_hosted_search_is_rejected(self):
        with self.assertRaises(R.ToolSearchArgumentsError):
            R._ToolBridge([{**SEARCH, 'execution': 'server'}])

    def test_default_hosted_search_and_hosted_history_are_rejected(self):
        with self.assertRaises(R.ToolSearchArgumentsError):
            R._ToolBridge([{'type': 'tool_search'}])
        with self.assertRaises(R.ToolSearchArgumentsError):
            R._ToolBridge([], [{'type': 'tool_search_call', 'execution': 'server'}])

    def test_client_schema_can_use_goal_instead_of_query(self):
        spec = {**SEARCH, 'parameters': {'type': 'object',
                'properties': {'goal': {'type': 'string'}}, 'required': ['goal']}}
        bridge = R._ToolBridge([spec])
        self.assertEqual(bridge.chat_tools[0]['function']['parameters'], spec['parameters'])
        out = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls',
            'message': {'tool_calls': [{'id': 's', 'function': {
                'name': bridge.search_name, 'arguments': '{"goal":"find shipping"}'}}]}}]},
            'm', 'r', bridge=bridge)
        self.assertEqual(out['output'][0]['arguments'], {'goal': 'find shipping'})

    def test_invalid_search_json_shapes_are_rejected(self):
        for raw in ('[]', 'null', '"text"', '{"query":"a","query":"b"}',
                    '{"query":NaN}', '{"query":Infinity}'):
            with self.subTest(raw=raw), self.assertRaises(R.ToolSearchArgumentsError):
                R._search_arguments(raw)

    def test_bad_search_does_not_partially_emit_a_tool_batch(self):
        tr = R._StreamTranslator('m', 'r', bridge=R._ToolBridge([tool(), SEARCH]))
        raw = tr.feed({'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'f', 'function': {'name': 'fetch', 'arguments': '{}'}},
            {'index': 1, 'id': 's', 'function': {'name': 'tool_search', 'arguments': '{'}}]},
            'finish_reason': 'tool_calls'}]})
        parsed = events(raw)
        self.assertEqual(parsed[-1]['type'], 'response.failed')
        self.assertFalse(any(e['type'] == 'response.output_item.done' for e in parsed))
        self.assertEqual(parsed[-1]['response']['output'], [])

    def test_ordinary_namespace_restoration_without_discovery(self):
        bridge = R._ToolBridge([{'type': 'namespace', 'name': 'one', 'tools': [tool()]}])
        out = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls',
            'message': {'tool_calls': [{'id': 'f', 'function': {
                'name': bridge.outbound_name('fetch', 'one'), 'arguments': '{}'}}]}}]},
            'm', 'r', bridge=bridge)
        self.assertEqual(out['output'][0]['namespace'], 'one')

    def test_allowed_tools_respects_same_named_namespace_tools(self):
        bridge = R._ToolBridge([{'type': 'namespace', 'name': n, 'tools': [tool()]}
                               for n in ('one', 'two')])
        out = R.to_chat_request(body(tools=[], tool_choice={'type': 'allowed_tools',
            'mode': 'required', 'tools': [{'type': 'function', 'name': 'fetch',
                                         'namespace': 'two'}]}), bridge=bridge)
        self.assertEqual([t['function']['name'] for t in out['tools']],
                         [bridge.outbound_name('fetch', 'two')])

    def test_loaded_nested_namespace_identity_is_preserved_in_history(self):
        overview = R._loaded_tool_overview([{'type': 'namespace', 'name': 'one',
            'tools': [{'type': 'namespace', 'name': 'two', 'tools': [tool()]}]}])
        self.assertEqual(overview, [{'name': 'fetch', 'namespace': 'one.two'}])

    def test_terminal_response_includes_late_usage_and_ignores_late_content(self):
        tr = R._StreamTranslator('m', 'r', bridge=R._ToolBridge([SEARCH]), defer_finish=True)
        raw = tr.feed(chunk('tool_search', '{"query":"fixture"}', finish='tool_calls'))
        self.assertFalse(any(e['type'] == 'response.completed' for e in events(raw)))
        raw += tr.feed({'choices': [{'delta': {'content': 'ignore-after-finish'}}],
            'usage': {'prompt_tokens': 1234, 'completion_tokens': 20,
                'prompt_tokens_details': {'cached_tokens': 1000}}})
        raw += tr.finish(None, force=True)
        es = events(raw)
        self.assertEqual(es[-1]['response']['usage']['input_tokens'], 1234)
        self.assertEqual(es[-1]['response']['usage']['input_tokens_details']['cached_tokens'], 1000)
        self.assertEqual(es[-1]['response']['output'][0]['type'], 'tool_search_call')
        self.assertFalse(any(e['type'] == 'response.output_text.delta' for e in es))
        self.assertFalse(tr.finish(None, force=True))


if __name__ == '__main__':
    unittest.main()
