"""Progress messages keep their phase and place beside the tools they describe."""
import json
import unittest

from server.routers import responses as R


def events(raw):
    return [json.loads(line[5:]) for frame in raw for line in frame.decode().splitlines()
            if line.startswith('data:')]


def frame(content=None, tool=None, reasoning=None, finish=None):
    delta = {}
    if content is not None:
        delta['content'] = content
    if reasoning is not None:
        delta['reasoning_content'] = reasoning
    if tool:
        delta['tool_calls'] = [{'index': 0, 'id': 'call_step',
            'function': {'name': 'verify_step', 'arguments': '{"step":1}'}}]
    return {'choices': [{'delta': delta, 'finish_reason': finish}]}


class MessageOrderTest(unittest.TestCase):
    def test_json_progress_precedes_tool_and_is_commentary(self):
        result = R.to_responses_object({'choices': [{'finish_reason': 'tool_calls',
            'message': {'content': '先检查。', 'reasoning_content': '选择检查工具。',
                'tool_calls': [{'id': 'c', 'function': {'name': 'verify_step', 'arguments': '{}'}}]}}]},
            'm', 'r')
        self.assertEqual([i['type'] for i in result['output']],
                         ['reasoning', 'message', 'function_call'])
        self.assertEqual(result['output'][1]['phase'], 'commentary')

    def test_tool_preamble_phase_does_not_depend_on_finish_reason(self):
        result = R.to_responses_object({'choices': [{'finish_reason': 'stop',
            'message': {'content': '先检查。', 'tool_calls': [
                {'id': 'c', 'function': {'name': 'verify_step', 'arguments': '{}'}}]}}]}, 'm', 'r')
        self.assertEqual(result['output'][0]['phase'], 'commentary')

    def test_stream_progress_and_terminal_array_keep_order(self):
        bridge = R._ToolBridge([{'type': 'tool_search', 'execution': 'client'}])
        tr = R._StreamTranslator('m', 'r', bridge=bridge, defer_finish=True)
        raw = tr.feed(frame(content='先检查。'))
        raw += tr.feed(frame(tool=True))
        raw += tr.feed(frame(content='检查请求已发出。', finish='tool_calls'))
        raw += tr.finish(None, force=True)
        parsed = events(raw)
        completed = [e for e in parsed if e['type'] == 'response.output_item.done']
        ordered = sorted(completed, key=lambda e: e['output_index'])
        self.assertEqual([e['item']['type'] for e in ordered],
                         ['message', 'function_call', 'message'])
        self.assertEqual(parsed[-1]['response']['output'], [e['item'] for e in ordered])
        messages = [e['item'] for e in ordered if e['item']['type'] == 'message']
        self.assertEqual([i['phase'] for i in messages], ['commentary', 'commentary'])
        self.assertEqual([i['content'][0]['text'] for i in messages],
                         ['先检查。', '检查请求已发出。'])

    def test_completed_answer_has_final_phase_in_json_and_stream(self):
        result = R.to_responses_object({'choices': [{'finish_reason': 'stop',
            'message': {'content': '检查完成。'}}]}, 'm', 'r')
        self.assertEqual(result['output'][0]['phase'], 'final_answer')
        tr = R._StreamTranslator('m', 'r', defer_finish=True)
        raw = tr.feed(frame(content='检查完成。', finish='stop'))
        raw += tr.finish(None, force=True)
        parsed = events(raw)
        added = next(e['item'] for e in parsed if e['type'] == 'response.output_item.added')
        done = next(e['item'] for e in parsed if e['type'] == 'response.output_item.done')
        self.assertEqual(added['id'], done['id'])
        self.assertEqual(done['phase'], 'final_answer')
        self.assertEqual(parsed[-1]['response']['output'], [done])

    def test_incomplete_text_is_not_a_final_answer(self):
        tr = R._StreamTranslator('m', 'r')
        parsed = events(tr.feed(frame(content='检查到一半', finish='length')))
        self.assertEqual(parsed[-1]['type'], 'response.incomplete')
        self.assertEqual(parsed[-1]['response']['output'][0]['phase'], 'commentary')

    def test_reopened_reasoning_does_not_repeat_previous_content(self):
        tr = R._StreamTranslator('m', 'r')
        raw = tr.feed(frame(reasoning='第一项分析。'))
        raw += tr.feed(frame(content='第一项说明。'))
        raw += tr.feed(frame(reasoning='第二项分析。'))
        raw += tr.feed(frame(content='检查完成。', finish='stop'))
        output = events(raw)[-1]['response']['output']
        self.assertEqual([i['summary'][0]['text'] for i in output if i['type'] == 'reasoning'],
                         ['第一项分析。', '第二项分析。'])
        self.assertEqual([i['content'][0]['text'] for i in output if i['type'] == 'message'],
                         ['第一项说明。', '检查完成。'])


    def test_history_preamble_reasoning_and_calls_share_one_assistant(self):
        out = R.to_chat_request({'model': 'm', 'input': [
            {'type': 'reasoning', 'summary': [{'type': 'summary_text', 'text': 'inspect'}]},
            {'type': 'message', 'role': 'assistant', 'content': '先检查。'},
            {'type': 'function_call', 'call_id': 'f', 'name': 'fetch', 'arguments': '{}'},
            {'type': 'function_call_output', 'call_id': 'f', 'output': 'done'}]})
        self.assertEqual([m['role'] for m in out['messages']], ['assistant', 'tool'])
        assistant = out['messages'][0]
        self.assertEqual(assistant['content'], '先检查。')
        self.assertEqual(assistant['reasoning_content'], 'inspect')
        self.assertEqual(assistant['tool_calls'][0]['id'], 'f')


if __name__ == '__main__':
    unittest.main()
