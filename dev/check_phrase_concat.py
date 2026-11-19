"""全量校验：设置页所有 `+` 拼接的文案，都必须能在短语表里命中。

上面那个 `check_phrase_concat.py` 只查我新加的 admin 分组。而设置页有 8 处
desc 是拼接写法，其中任何一处少一个空格/标点都会**静默漏翻**（显示中文原文，
不报错、不显示键名）。这个脚本把全部拼接文案都查一遍，把这类问题一次性暴露。

不查的话，漏翻只能靠人在英文界面里逐条看——而设置页有 100+ 个字段。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 设置页的字段定义与拼接文案在外壳里（批次 4 起 Tab 变成子路由，取数与表单状态
# 都在 `layout.tsx` 上，见那个文件顶部的说明）。
SRC = ROOT / 'web/app/(main)/settings/layout.tsx'
LOCALES = ('en', 'ja', 'ko', 'zh-TW')
STR_RE = re.compile(r"'((?:[^'\\]|\\.)*)'")


def _concat_strings(text: str) -> list[str]:
    """挑出所有「单引号串用 + 连接」的**文案**表达式，返回拼接结果。

    两种拼接形态都要认（第一版只认了前者，于是返回 0 处、**假通过**）：
      · 首行以 `+` 结尾：  desc: 'a' +
                          'b',
      · 首行是完整串、续行以 `+` 开头：
                          desc: 'a'
                            + 'b',

    只取**字段文案**（label/desc/title/placeholder 等）：`className` 也常用 `+`
    拼字符串，那不是给用户看的文案，混进来只会产生假警报。
    """
    # 只在这些字段名后面找拼接串（用户可见文案）
    fields = ('label', 'desc', 'title', 'placeholder', 'unit', 'note', 'hint')
    out: list[str] = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.search(rf"\b({'|'.join(fields)}):\s*'(?:[^'\\]|\\.)*'", line)
        if not m:
            i += 1
            continue
        ends_with_plus = line.rstrip().endswith('+')
        has_cont = i + 1 < len(lines) and lines[i + 1].lstrip().startswith('+')
        if not (ends_with_plus or has_cont):
            i += 1
            continue
        chunk = [line]
        j = i + 1
        # 只要**下一行**以 `+` 开头就继续收：`+` 可能写在行尾（形态一）也可能写在
        # 行首（形态二）。用「行尾是否 +」当终止条件会漏掉形态二的中段
        # （那些行行尾没有 +，`+` 在下一行行首），导致拼出来的串偏短、误报未命中。
        while j < len(lines) and lines[j].lstrip().startswith('+'):
            chunk.append(lines[j])
            j += 1
        parts = STR_RE.findall('\n'.join(chunk))
        if len(parts) > 1:
            out.append(''.join(p.replace("\\'", "'") for p in parts))
        i = j
    return out


def main() -> int:
    src = SRC.read_text(encoding='utf-8')
    concats = _concat_strings(src)
    print(f'设置页拼接式文案：{len(concats)} 处\n')

    tables = {}
    for code in LOCALES:
        d = json.loads((ROOT / f'web/lib/i18n/locales/{code}.json').read_text(encoding='utf-8'))
        tables[code] = d.get('phrases') or {}

    missing = 0
    for text in concats:
        hits = [c for c in LOCALES if text in tables[c]]
        head = text[:34].replace('\n', ' ')
        if len(hits) == len(LOCALES):
            print(f'  ✓ {head}…')
            continue
        missing += 1
        print(f'  ✗ {head}…  （命中 {hits or "无"}）')
        # 定位首个差异，便于直接改
        for cand in tables['en']:
            if cand[:10] == text[:10]:
                for n, (a, b) in enumerate(zip(cand, text)):
                    if a != b:
                        print(f'      第 {n} 字：表={a!r} 源码={b!r}')
                        break
                else:
                    print(f'      前缀一致，长度差 {len(cand) - len(text)}')
                break

    print()
    if missing:
        print(f'✗ {missing} 处未命中：这些文案在非中文界面下**不会翻译**（静默退回中文）')
        return 1
    print('✓ 全部拼接文案都能命中短语表')
    return 0


if __name__ == '__main__':
    sys.exit(main())
