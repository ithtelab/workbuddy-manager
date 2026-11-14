"""前端多语言（web/lib/i18n）字典的结构守卫。

与 test_readme_i18n.py 同一思路：不评判译文质量（那没法自动判定），只锁
**机械可查的一致性**——漏翻、键名写错、占位符对不上、把中文抄进英文这些
问题都会在构建前暴露，而不是等到用户切语言时才发现某个角落还是中文。

检查项：
1. 各语言文件存在、可解析、键集与源语言完全一致（多一个键也算错）；
2. 没有空译文；
3. 占位符集合一致（`{n}` 这类，译文漏掉或写错名字都会导致界面渲染出花括号）；
4. 英文版不含中日韩汉字（复制粘贴中文原文最常见的症状）；
5. 复数形式写成了对象时，必须带 other（渲染兜底依赖它）；
6. 源码里**写死的** t('...') 键在字典里真的存在。
"""
from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_LOCALES_DIR = _ROOT / 'web' / 'lib' / 'i18n' / 'locales'

# 源语言：其它语言以它的键集为基准
_SOURCE = 'zh-CN'
_LOCALES = ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']

# 复数分类（Intl.PluralRules）。写成对象时 other 必备，否则渲染没有兜底。
_PLURAL_KEYS = {'zero', 'one', 'two', 'few', 'many', 'other'}

_PLACEHOLDER = re.compile(r'\{(\w+)\}')
_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')

# t('a.b.c') / t("a.b.c")：只认写死的字符串键，变量与模板串无法静态校验。
# 前面不允许是字母、数字、`.`、`$`——否则 foo.t('x') 这类别的对象的方法会被算进来。
_T_CALL_KEY = re.compile(r"""(?<![\w.$])t\(\s*['\"]([A-Za-z0-9_.]+)['\"]""")

# 别名形式：`import {t as tStatic} from '@/lib/i18n'` 之后写的 tStatic('a.b.c')。
#
# 必须单独认出来：上面那条要求 `t` 后面紧跟 `(`，而别名是 `tStatic(` —— `t` 后面
# 是 `S`，一条也匹配不上。于是两个文件（lib/i18n/taskrun.ts / tasklog.ts，合计数
# 十条文案规则）的键全部绕过了守卫：把其中某个键写成 `tasks.runExitCod`，
# 字典校验、短语校验、本条守卫会一起通过，用户却在界面上直愣愣看到键名。
# 键名是写死的，只是换了绑定的局部名——静态查得出来，就该查。
_T_ALIAS_KEY = re.compile(
    r"""(?<![\w.$])(?:tStatic|tGlobal|translate)\(\s*['\"]([A-Za-z0-9_.]+)['\"]""")


def _load(locale: str) -> dict:
    return json.loads((_LOCALES_DIR / f'{locale}.json').read_text(encoding='utf-8'))


def _backend_403_details() -> tuple[dict[str, str], list[str]]:
    """扫出后端所有 `HTTPException(status_code=403, …)` 的 detail 文案。

    返回 (字面量文案 → 出处, 非字面量的位置)。用 ast 而不是正则：`keys.py`
    里有跨行相邻字符串字面量的写法，正则要额外处理隐式拼接，而 ast 已经把相邻
    字面量合并成一个 Constant 了。

    非字面量的（f-string 之类）单独返回而不是忽略——否则将来加一条拼接出来的
    403 文案就会静默绕过这道守卫，而「静默绕过」正是本文件要防的东西。
    """
    literal: dict[str, str] = {}
    dynamic: list[str] = []
    for path in sorted((_ROOT / 'server').rglob('*.py')):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        rel = path.relative_to(_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', '')
            if name != 'HTTPException':
                continue
            # 状态码与 detail 既可能写成关键字，也可能按位置传。
            status = detail = None
            if len(node.args) >= 2:
                status, detail = node.args[0], node.args[1]
            for kw in node.keywords:
                if kw.arg == 'status_code':
                    status = kw.value
                elif kw.arg == 'detail':
                    detail = kw.value
            if status is None or detail is None:
                continue
            try:
                if ast.literal_eval(status) != 403:
                    continue
            except (ValueError, TypeError):
                continue
            if isinstance(detail, ast.Constant) and isinstance(detail.value, str):
                literal.setdefault(detail.value, rel)
            else:
                dynamic.append(f'{rel}:{node.lineno}')
    return literal, dynamic


def _flatten(node, prefix: str = '') -> dict[str, object]:
    """把嵌套字典压成 a.b.c → 值，便于逐键比对。

    phrases 子树单独校验（见 WebPhraseTest）：它的键是中文源文，
    源语言自身不重复维护一份，因此不参与「键集完全一致」的比对。
    """
    flat: dict[str, object] = {}
    for key, value in node.items():
        path = f'{prefix}{key}'
        if prefix == '' and key == 'phrases':
            continue
        if isinstance(value, dict) and not _is_plural(value):
            flat.update(_flatten(value, f'{path}.'))
        else:
            flat[path] = value
    return flat


def _is_plural(value: dict) -> bool:
    """值是不是复数形式对象：键必须全部落在复数分类里。"""
    return bool(value) and set(value).issubset(_PLURAL_KEYS)


def _texts(value: object) -> list[str]:
    """取出该条目所有可能渲染出来的文本（复数形式会展开成多条）。"""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, str)]
    return []


def _placeholders(value: object) -> set[str]:
    found: set[str] = set()
    for text in _texts(value):
        found.update(_PLACEHOLDER.findall(text))
    return found


class WebLocaleTest(unittest.TestCase):
    """字典结构必须各语言对齐。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.dicts = {locale: _load(locale) for locale in _LOCALES}
        cls.flat = {locale: _flatten(cls.dicts[locale]) for locale in _LOCALES}
        cls.source_keys = set(cls.flat[_SOURCE])

    def test_files_exist(self) -> None:
        for locale in _LOCALES:
            self.assertTrue((_LOCALES_DIR / f'{locale}.json').is_file(),
                            f'缺少语言文件 {locale}.json')

    def test_same_key_set(self) -> None:
        """键集必须与源语言完全一致：漏翻与多写的键都报出来。"""
        for locale in _LOCALES:
            if locale == _SOURCE:
                continue
            missing = sorted(self.source_keys - set(self.flat[locale]))
            extra = sorted(set(self.flat[locale]) - self.source_keys)
            self.assertEqual(missing, [], f'{locale} 缺少 {len(missing)} 个键：{missing[:10]}')
            self.assertEqual(extra, [], f'{locale} 多出 {len(extra)} 个键：{extra[:10]}')

    def test_no_empty_translation(self) -> None:
        for locale in _LOCALES:
            for key, value in self.flat[locale].items():
                texts = _texts(value)
                self.assertTrue(texts, f'{locale}.{key} 没有任何文本')
                for text in texts:
                    self.assertTrue(text.strip(), f'{locale}.{key} 是空译文')

    def test_placeholders_match_source(self) -> None:
        """占位符必须与源语言一致：漏了会让译文缺值，多了会渲染出花括号。"""
        for locale in _LOCALES:
            if locale == _SOURCE:
                continue
            for key, value in self.flat[locale].items():
                want = _placeholders(self.flat[_SOURCE].get(key))
                got = _placeholders(value)
                self.assertEqual(
                    got, want,
                    f'{locale}.{key} 占位符不一致（源语言 {sorted(want)} / 译文 {sorted(got)}）',
                )

    def test_plural_forms_have_other(self) -> None:
        for locale in _LOCALES:
            for key, value in self.flat[locale].items():
                if isinstance(value, dict):
                    self.assertIn('other', value, f'{locale}.{key} 的复数形式缺少 other 兜底')

    def test_english_has_no_han(self) -> None:
        """英文版混进汉字 = 忘了翻译，直接复制了原文。"""
        offenders = [
            key for key, value in self.flat['en'].items()
            if any(_HAN.search(text) for text in _texts(value))
        ]
        self.assertEqual(offenders, [], f'en 里有 {len(offenders)} 处未翻译的中文：{offenders[:10]}')

    def test_account_pending_copy_is_translated(self) -> None:
        """日志详情的「账号尚未回填」不能复用「非流式请求」文案。"""
        for locale in _LOCALES:
            text = self.flat[locale].get('logs.accountPending')
            self.assertIsInstance(text, str, f'{locale} 缺少 logs.accountPending')
            self.assertNotIn('非流式', str(text), f'{locale} 又把它说成非流式请求了')

    def test_no_duplicate_keys(self) -> None:
        """同一个段里**不能出现重复键**——JSON 会静默取最后一条，界面于是显示错的那句。

        为什么单独一条：上面所有检查都走 `json.loads`，而它遇到重复键**不报错**、
        直接保留最后一个（`object_pairs_hook` 才看得到）。所以「两个分支各加了同名
        键、合并时挤在一起」这种事故，键集、空译文、占位符全都查不出来，
        只有肉眼看 diff 才发现 —— 评审一次真实合并时就撞上过（同一个键一个写
        「缓存命中」一个写「缓存命中 Token」，最终库里留下的取决于行序）。

        做法是**按文本**逐行找，不用 json：每行 `"key": ...` 记一次出现。
        """
        offenders: dict[str, list[str]] = {}
        for locale in _LOCALES:
            path = _LOCALES_DIR / f'{locale}.json'
            section = '?'
            seen: dict[tuple[str, str], int] = {}
            for lineno, line in enumerate(
                    path.read_text(encoding='utf-8').splitlines(), start=1):
                m = re.match(r'^  "([^"]+)": \{\s*$', line)   # 顶层段名
                if m:
                    section = m.group(1)
                    continue
                m = re.match(r'^    "([^"]+)":', line)        # 段内键
                if not m:
                    continue
                key = (section, m.group(1))
                if key in seen:
                    offenders.setdefault(f'{locale}.{section}.{m.group(1)}',
                                         []).append(f'第 {seen[key]} 行与第 {lineno} 行')
                else:
                    seen[key] = lineno
        self.assertEqual(offenders, {},
                         f'语言文件里有重复键（JSON 只会保留最后一条）：{offenders}')


class WebKeyUsageTest(unittest.TestCase):
    """源码里写死的 t('...') 键必须真的存在于字典。

    字典自洽（键集对齐、无空译文）并不能保证**界面用对了键**：键名写错一个字母，
    字典校验照样全绿，用户却在界面上直愣愣看到 `accounts.expiryColumn`。
    本测试补上这一环——扫描前端源码里的字面量键，逐个回字典里查。

    （动态拼出来的键、短语表 tp('中文') 不在此列；前者静态不可知，后者另有守卫。
    别名导入的调用（tStatic / tGlobal / translate）由 _T_ALIAS_KEY 一并扫描。）
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.flat = _flatten(_load(_SOURCE))
        cls.used: dict[str, list[str]] = {}
        cls.aliased: dict[str, list[str]] = {}
        for path in sorted((_ROOT / 'web').rglob('*')):
            if path.suffix not in ('.ts', '.tsx') or 'node_modules' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            # 一律用 posix 分隔符：断言里写的路径是 `web/lib/...` 这种形态，
            # 而 Windows 上 str(Path) 会给反斜杠，直接比会永远对不上。
            rel = path.relative_to(_ROOT).as_posix()
            for key in _T_CALL_KEY.findall(text):
                cls.used.setdefault(key, []).append(rel)
            for key in _T_ALIAS_KEY.findall(text):
                cls.aliased.setdefault(key, []).append(rel)

    def test_scan_is_not_vacuous(self) -> None:
        """先确认扫描真的抓到了东西，免得正则失效后测试空转照样绿。"""
        self.assertGreater(len(self.used), 100, '没扫到几个键，正则可能失配了')
        self.assertIn('security.auditLog', self.used)

    def test_alias_scan_is_not_vacuous(self) -> None:
        r"""别名的扫描同样要确认抓到了东西。

        这条正是缺口本身：加了别名扫描但正则没生效（比如写成 ``t\w+`` 却漏了
        左边界的负向断言），下面那条测试仍会通过——因为 ``used`` 有内容。
        所以这里要求别名键**确实**抓到了相当数量，并且覆盖到两个新文件。
        """
        self.assertGreater(len(self.aliased), 20,
                           f'别名键只扫到 {len(self.aliased)} 个，正则可能失配')
        files = {f for fs in self.aliased.values() for f in fs}
        for expect in ('web/lib/i18n/taskrun.ts', 'web/lib/i18n/tasklog.ts'):
            self.assertIn(expect, files, f'{expect} 的键没被扫到 —— 别名守卫没生效')

    def test_all_literal_keys_exist(self) -> None:
        for name, table in (('t()', self.used), ('别名', self.aliased)):
            with self.subTest(form=name):
                missing = {
                    key: sorted(set(files))
                    for key, files in table.items()
                    if key not in self.flat
                }
                detail = '; '.join(f'{k} ← {v[0]}' for k, v in sorted(missing.items())[:10])
                self.assertEqual(missing, {},
                                 f'{name} 有 {len(missing)} 个键在字典里不存在：{detail}')


class WebPhraseTest(unittest.TestCase):
    """短语表（gettext 风格，键为中文源文）的结构守卫。

    设置页的配置项说明以中文原文当键，因此源语言不维护自身副本；
    这里改为要求**各目标语言的短语键集完全一致**，并逐条校验非空与未混入中文。
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.dicts = {locale: _load(locale) for locale in _LOCALES}
        cls.phrases = {locale: cls.dicts[locale].get('phrases', {}) for locale in _LOCALES}

    def test_target_locales_share_phrase_keys(self) -> None:
        reference = set(self.phrases['en'])
        self.assertTrue(reference, 'en 的 phrases 表为空')
        for locale in _LOCALES:
            if locale in ('zh-CN', 'en'):
                continue
            keys = set(self.phrases[locale])
            missing = sorted(reference - keys)
            extra = sorted(keys - reference)
            self.assertEqual(missing, [], f'{locale} 缺少 {len(missing)} 条短语：{missing[:5]}')
            self.assertEqual(extra, [], f'{locale} 多出 {len(extra)} 条短语：{extra[:5]}')

    def test_phrase_values_are_non_empty(self) -> None:
        for locale, table in self.phrases.items():
            for key, value in table.items():
                self.assertIsInstance(value, str, f'{locale}.phrases[{key}] 必须是字符串')
                self.assertTrue(value.strip(), f'{locale}.phrases[{key}] 是空译文')

    def test_source_phrase_is_not_duplicated(self) -> None:
        """源语言不该维护 phrases 副本：否则改文案时要同时改两处，必然漂移。"""
        self.assertEqual(self.phrases['zh-CN'], {}, 'zh-CN 不应包含 phrases（中文原文即键）')

    def test_english_phrases_have_no_han(self) -> None:
        offenders = [key for key, value in self.phrases['en'].items() if _HAN.search(value)]
        self.assertEqual(offenders, [], f'en.phrases 里有 {len(offenders)} 条未翻译：{offenders[:5]}')

    def test_backend_403_details_are_translatable(self) -> None:
        """后端每条 403 的 detail 文案都必须在短语表里有译文。

        为什么单列一条：403 是「你被拒绝了」，用户必须看懂**为什么**被拒才知道
        下一步做什么——去找管理员开权限、换会话登录、去 .env 里打开开关，处置
        方式完全不同。这些文案写在后端、由 `errText` 过短语表翻译，而短语表是
        **按整串精确匹配**的：少一个字、多一个标点就静默退回中文原文，不报错、
        不显示键名，只能靠人在英文界面上逐条肉眼比对。

        实测就漏过两条（做 P0-5 时发现）：
          · `刷新积分需要管理员权限` —— 与已收录的 `需要管理员权限` 只差三个字，
            精确匹配于是命中不了；
          · `该接口不接受 API Token，请用会话登录后操作`。

        这也解释了为什么 403 **不能**在前端统一改写成「权限不足」：本项目的 403
        有四种含义，其中「本机导入开关没开」「调用方不是面板所在机器」两条原文
        就是可照做的操作说明，改写会把用户唯一能照着做的那句话抹掉。
        """
        literal, dynamic = _backend_403_details()
        self.assertEqual(dynamic, [],
                         f'有 {len(dynamic)} 处 403 文案是拼出来的，守卫查不了：{dynamic}')
        # 先确认扫描真的抓到了东西，免得正则/AST 失配后测试空转照样绿
        self.assertIn('需要管理员权限', literal, '没扫到已知的 403 文案 —— 扫描失效了（会假通过）')
        for code in ('en', 'ja', 'ko', 'zh-TW'):
            missing = sorted(text for text in literal if text not in self.phrases[code])
            detail = '; '.join(f'{text[:24]}… ← {literal[text]}' for text in missing[:5])
            self.assertEqual(
                missing, [],
                f'{code} 的短语表缺 {len(missing)} 条 403 文案'
                f'（这些在非中文界面下会退回中文）：{detail}')

    def test_setting_field_concats_have_translations(self) -> None:
        """设置页里**用 `+` 拼接**的字段文案必须在短语表里有条目。

        为什么单列一条：短语表是「按渲染后的整串」查的，而设置页有若干处
        desc 用 `+` 把多行拼起来。拼接少打一个字、多一个标点，短语表就**静默
        匹配不上**——非中文界面下直接显示中文原文（表现为「漏翻」），
        不报错、不显示键名，只能靠人在英文界面里逐条肉眼比对。

        实测就漏过一条（`请求体上限（旧版上游）`，随上游移除 max_body_mb 引入），
        直到做这次多语言核对才发现。这里把它钉成机械检查。

        本测试直接复用 `dev/check_phrase_concat.py` 的解析逻辑——两处各写一遍
        迟早漂移，而它已经有自己的反证验证（改一个字即报出第几字不符）。
        """
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'check_phrase_concat', _ROOT / 'dev' / 'check_phrase_concat.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # 批次 4 起设置页的外壳在 `layout.tsx`（7 个 Tab 变成 `/settings/<tab>`
        # 子路由，取数与表单状态必须留在不随子路由重挂载的 layout 上）
        src = (_ROOT / 'web/app/(main)/settings/layout.tsx').read_text(encoding='utf-8')
        concats = mod._concat_strings(src)
        self.assertTrue(concats, '没解析到任何拼接文案 —— 解析器失效了（会假通过）')
        for text in concats:
            for code in ('en', 'ja', 'ko', 'zh-TW'):
                self.assertIn(text, self.phrases[code],
                              f'{code} 的短语表缺这条拼接文案（界面会漏翻）：{text[:40]}…')


class AlwaysFailingBadgeTest(unittest.TestCase):
    """账号页「一直失败」徽章的判据（issue #14 第二点）。

    上游对**未命中它那几条规则**的 4xx（例如被 WAF 拦下的 403）只「换号不罚」
    ——不冷却、不熔断、不禁用（其 applyErrorPolicy 的 default 分支，为防雪崩
    刻意如此）。这种账号在面板上一直显示「正常」，实际每次请求都失败、
    可持续几小时，用户完全无从下手。我们能做的是把它标出来。

    判据必须用 `success_count`：上游 `last_success` 是 Go 的 `time.Time` 配
    `omitempty`，而 **`omitempty` 对结构体类型不生效** —— 从未成功过的账号会
    序列化成 `"0001-01-01T00:00:00Z"`，在 JS 里是**真值**，拿它判空永远
    命中不了（已在 Go 侧实测确认零值 time.Time 照样被序列化）。
    """

    ZERO_TIME = '0001-01-01T00:00:00Z'

    @staticmethod
    def _always_failing(acct: dict) -> bool:
        """与 accounts/page.tsx 的 renderStatus 保持同一判据。"""
        errs = acct.get('err_total') if isinstance(acct.get('err_total'), int) else 0
        oks = acct.get('success_count') if isinstance(acct.get('success_count'), int) else 0
        return errs > 0 and oks == 0

    def test_never_succeeded_is_flagged(self) -> None:
        self.assertTrue(self._always_failing(
            {'err_total': 5, 'last_success': self.ZERO_TIME,
             'last_err': '2026-09-16T11:00:00+08:00'}))
        self.assertTrue(self._always_failing({'err_total': 1, 'last_success': self.ZERO_TIME}))

    def test_healthy_account_not_flagged(self) -> None:
        """成功过的账号不该被误标 —— 正常账号也会有偶发错误。"""
        self.assertFalse(self._always_failing(
            {'err_total': 9, 'success_count': 40,
             'last_success': '2026-09-16T10:00:00+08:00'}))
        self.assertFalse(self._always_failing({'success_count': 12}))
        self.assertFalse(self._always_failing({}), '从没跑过的账号不该标')

    def test_last_success_alone_would_miss_it(self) -> None:
        """把「为什么不能用 last_success 判空」固化成测试，免得后人改回去。"""
        acct = {'err_total': 5, 'last_success': self.ZERO_TIME}
        self.assertTrue(self._always_failing(acct), '正确判据应标出')
        naive = (acct.get('err_total') or 0) > 0 and not acct.get('last_success')
        self.assertFalse(naive, '用 last_success 判空的写法会漏标 —— 正是要避免的')


if __name__ == '__main__':
    unittest.main()


class PopoverInDialogScrollTest(unittest.TestCase):
    """弹窗内浮层的滚轮不能被 Dialog 的滚动锁吞掉（issue #438）。

    源码级守卫：Radix Dialog 的 RemoveScroll 只放行「被锁元素子树内」的滚轮，
    Popover 默认 Portal 到 body 会被判为外部。修法是把 container 指到
    dialog-content 上；这两处任何一处被改回去，滚轮都会重新失效（拖滚动条仍能用，
    所以肉眼很难发现）。
    """

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[2]
        self.popover = (root / 'web' / 'components' / 'ui' / 'popover.tsx').read_text(encoding='utf-8')
        self.whitelist = (root / 'web' / 'components' / 'common' / 'keys'
                          / 'ModelWhitelistInput.tsx').read_text(encoding='utf-8')

    def test_popover_accepts_container(self) -> None:
        self.assertIn('container', self.popover, 'PopoverContent 必须支持指定 Portal 落点')
        self.assertIn('Portal container=', self.popover, 'container 要真的传给 Portal')

    def test_whitelist_uses_dialog_content_as_container(self) -> None:
        self.assertIn('data-slot="dialog-content"', self.whitelist,
                      '浮层落点必须找 dialog-content 子树')
        self.assertIn('container={portalContainer}', self.whitelist,
                      'PopoverContent 必须接收算出来的落点')
