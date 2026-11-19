"""模型别名映射（设置页「模型映射」）的查表语义与兜底模型（issue #173）。

为什么要单独一个模块：映射表被三处消费 —— 转发时把下游的名字换成真名
（`routers/gateway.py::_map_model`，chat / responses / anthropic 三条路都走它）、
密钥白名单里的别名补条目（`routers/gateway.py` 的别名条目生成）、别名合法性检查
（`routers/keys.py`）。**兜底那条（键为 `*`）三处必须一致**，否则会出现「能调用
但客户端清单里没有」「清单里有但一调就报模型不可用」这类只有用户能发现的错位。

兜底是什么：没命中任何别名的模型名一律交给它。副作用是**拼错的模型名也会被接住**
（`glm-5.2` 手滑写成 `glm-5.3` 不再明确报错，而是静默按兜底模型回答）——这一点在
设置页文案里对用户写明了，是刻意的取舍。
"""
from __future__ import annotations

# 兜底模型的键。选 `*` 是因为它**不是合法的模型名**（各家模型 id 都不用这个字符），
# 因此不必担心与真实别名撞车 —— 键空间不重叠，用户也就不可能"误把某个模型映射成兜底"。
FALLBACK_KEY = '*'


def load(mapping: object) -> dict[str, str]:
    """把数据库里的原始值归一成 `{别名: 目标}`。

    **坏数据只该影响它自己**：设置存成非对象（历史脏数据、手工改库）时按「没有映射」
    处理，不能让整个网关 5xx —— 与 `_map_model` 原有注释同一原则。
    """
    if not isinstance(mapping, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in mapping.items():
        name = str(key).strip()
        target = str(value).strip() if value is not None else ''
        # 空值条目等于没写：留着会让「这个名字被映射了」与「没映射」两种语义重叠
        if name and target:
            out[name] = target
    return out


def aliases(mapping: object) -> list[str]:
    """真实别名（**不含兜底那条**）。

    白名单校验用这个：`*` 不是模型名，不该被当成"用户写的别名是对的"而放行。
    """
    return [name for name in load(mapping) if name != FALLBACK_KEY]


def fallback(mapping: object) -> str | None:
    """兜底模型名；没配返回 None。"""
    return load(mapping).get(FALLBACK_KEY)


def target_for(mapping: object, model: str | None) -> str | None:
    """下游传来的模型名 → 实际要调用的模型名。

    顺序：**精确别名** → **兜底** → **原样返回**（没有兜底时保持旧行为）。
    """
    if not model:
        return model
    table = load(mapping)
    exact = table.get(model)
    if exact:
        return exact
    return table.get(FALLBACK_KEY) or model


def validate(raw: object) -> dict[str, str]:
    """校验并归一化要落库的映射表；非法值抛 `ValueError`。

    唯一会被拦的是「目标写成兜底」：`{"*": "*"}` 会让查表把 `*` 当成模型名送给上游
    （用户看到的是模型不可用，而配置界面显示"已设置兜底"）。存进去之前就拒绝，
    比事后从报错反推容易得多。
    """
    if not isinstance(raw, dict):
        raise ValueError('模型映射必须是一个对象')
    clean: dict[str, str] = {}
    for key, value in raw.items():
        name = str(key).strip()
        target = str(value).strip() if value is not None else ''
        if not name or not target:
            continue
        if target == FALLBACK_KEY:
            raise ValueError(
                '不能把兜底模型自己当成目标 —— 请填一个真实的模型名（兜底的作用是'
                '「没命中映射时交给它」，它本身不能是映射目标）'
            )
        clean[name] = target
    return clean
