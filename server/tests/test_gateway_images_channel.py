"""网关兼容性：图片 part 归一（issue #134）与客户端渠道归类（issue #233）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.routers import gateway  # noqa: E402


class ImageNormalizeTest(unittest.TestCase):
    def test_image_url_string_is_wrapped(self) -> None:
        body = {'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': 'https://x.test/a.png'},
        ]}]}
        self.assertEqual(gateway.normalize_chat_images(body), 1)
        self.assertEqual(body['messages'][0]['content'][0],
                         {'type': 'image_url', 'image_url': {'url': 'https://x.test/a.png'}})

    def test_input_image_is_converted(self) -> None:
        body = {'messages': [{'role': 'user', 'content': [
            {'type': 'input_image', 'image_url': {'url': 'data:image/png;base64,AA'}},
        ]}]}
        gateway.normalize_chat_images(body)
        self.assertEqual(body['messages'][0]['content'][0]['type'], 'image_url')
        self.assertEqual(body['messages'][0]['content'][0]['image_url']['url'],
                         'data:image/png;base64,AA')

    def test_anthropic_base64_source(self) -> None:
        body = {'messages': [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64',
                                         'media_type': 'image/jpeg',
                                         'data': 'ZZZ'}},
        ]}]}
        gateway.normalize_chat_images(body)
        self.assertEqual(body['messages'][0]['content'][0]['image_url']['url'],
                         'data:image/jpeg;base64,ZZZ')

    def test_standard_part_is_untouched(self) -> None:
        part = {'type': 'image_url', 'image_url': {'url': 'https://x.test/a.png'}}
        body = {'messages': [{'role': 'user', 'content': [part]}]}
        self.assertEqual(gateway.normalize_chat_images(body), 0)

    def test_text_content_and_unknown_parts_untouched(self) -> None:
        body = {'messages': [
            {'role': 'user', 'content': 'plain text'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'hi'},
                                         {'type': 'custom', 'x': 1}]},
        ]}
        self.assertEqual(gateway.normalize_chat_images(body), 0)
        self.assertEqual(body['messages'][0]['content'], 'plain text')

    def test_non_dict_body_is_safe(self) -> None:
        self.assertEqual(gateway.normalize_chat_images(None), 0)
        self.assertEqual(gateway.normalize_chat_images([1, 2]), 0)


class ClientChannelTest(unittest.TestCase):
    def test_known_clients(self) -> None:
        cases = {
            'codex_cli_rs/0.31.0': 'codex',
            'Claude-Code/1.0': 'claude-code',
            'DeepSeek-Harness/2.0': 'dsh',
            'VSCode/1.90 (darwin)': 'vscode',
            'Cursor/0.42': 'vscode',
            'JetBrains-IntelliJ/2024': 'jetbrains',
            'WorkBuddy/1.0': 'wb',
            'Mozilla/5.0 (Windows NT 10.0) Chrome/131': 'web',
            'python-httpx/0.27': 'api',
            'curl/8.4.0': 'api',
        }
        for ua, want in cases.items():
            with self.subTest(ua=ua):
                self.assertEqual(gateway.client_channel(ua), want)

    def test_unknown_and_missing(self) -> None:
        self.assertEqual(gateway.client_channel(''), 'unknown')
        self.assertEqual(gateway.client_channel(None), 'unknown')
        self.assertEqual(gateway.client_channel('SomeCustomAgent/1'), 'other')


if __name__ == '__main__':
    unittest.main()
