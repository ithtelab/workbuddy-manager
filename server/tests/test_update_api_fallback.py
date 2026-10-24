"""API 被网关拦截时的 releases/latest 302 回退（issue #226）。"""
from __future__ import annotations

import unittest
import urllib.error
from unittest import mock

from server.services import updater


class RedirectFallbackTest(unittest.TestCase):
    def test_reads_tag_from_302_location(self) -> None:
        err = urllib.error.HTTPError(
            'https://github.com/o/r/releases/latest', 302, 'Found',
            {'Location': 'https://github.com/o/r/releases/tag/v9.9.9'}, None)
        with mock.patch.object(updater._NO_REDIRECT_OPENER, 'open', side_effect=err):
            out = updater._release_latest_via_redirect(
                'https://api.github.com/repos/o/r/releases/latest')
        self.assertEqual(out['tag_name'], 'v9.9.9')
        self.assertTrue(out['_fallback'])
        self.assertEqual(out['assets'], [])

    def test_non_api_url_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            updater._release_latest_via_redirect(
                'https://api.github.com/repos/o/r/commits')


if __name__ == '__main__':
    unittest.main()
