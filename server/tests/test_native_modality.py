import unittest
from server.services import modelcatalog

class NativeModalityTests(unittest.TestCase):
 def test_platform_image_flag_does_not_override_native_type(self):
  for mid,want in [('glm-5.3','text'),('glm-5.3-flash','multimodal'),('deepseek-v4-pro','text'),('auto','router'),('new-model','unknown')]:
   with self.subTest(mid=mid):
    item=modelcatalog._decorate([{'id':mid,'supports_images':True}])[0]
    self.assertEqual(item.get('native_modality'),want)
 def test_source_and_date_are_exposed(self):
  item=modelcatalog._decorate([{'id':'glm-5.3'}])[0]
  self.assertTrue(item.get('native_modality_source','').startswith('https://docs.bigmodel.cn/'))
  self.assertEqual(item.get('native_modality_verified_at'),'2026-09-25')
 def test_unknown_alias_not_inferred(self):
  for mid in ['glm-5.3-unknown','hy4-preview-f','kimi-k2.8-preview']:
   self.assertEqual(modelcatalog._decorate([{'id':mid,'supports_images':True}])[0].get('native_modality'),'unknown')

class FrontendUsesNativeModalityTest(unittest.TestCase):
    """前端只能按 `native_modality` 判定，不能再拿平台图片开关当原生能力（评审补）。

    评审时发现这一环只有手工验证（PR 里写的是 Edge/Playwright 走过一遍），没有自动
    守卫 —— 而它正是用户可见的那一半：筛选与标签。源码级钉住足够：这两处的判据只要
    回退成 `supports_images`，下面的断言立刻红。
    """

    def _page(self) -> str:
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        return (root / 'web' / 'app' / '(main)' / 'models' / 'page.tsx').read_text(encoding='utf-8')

    def test_filter_uses_native_modality(self) -> None:
        src = self._page()
        line = next((l for l in src.splitlines() if "cap === 'vision'" in l), '')
        self.assertIn('native_modality', line,
                      '多模态筛选的判据必须用 native_modality（平台 supports_images 只说明平台支持）')
        self.assertNotIn('supports_images', line, '筛选回退到了平台开关')

    def test_badge_uses_native_modality(self) -> None:
        src = self._page()
        self.assertIn("m.native_modality === 'multimodal'", src, '标签判据必须是 native_modality')

