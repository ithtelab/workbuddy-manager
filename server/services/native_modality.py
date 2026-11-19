"""Reviewed vendor-native capabilities, independent of gateway image handling.

Exact model IDs only. Unknown aliases stay unknown; new models never inherit a
classification from supportsImages or a fuzzy name match. Sources and review
 dates are returned so this maintained snapshot is not mistaken for live sync.
"""
import json
from pathlib import Path

_RECORDS = json.loads(Path(__file__).with_name('native_modalities.json').read_text(encoding='utf-8'))

def describe(model_id: str) -> dict:
    mid = model_id.lower()
    for prefix in ('cn:', 'global:'):
        if mid.startswith(prefix):
            mid = mid[len(prefix):]
            break
    if mid == 'auto':
        return {'native_modality': 'router', 'native_modality_source': '', 'native_modality_verified_at': ''}
    record = _RECORDS.get(mid)
    if record is None:
        return {'native_modality': 'unknown', 'native_modality_source': '', 'native_modality_verified_at': ''}
    return {'native_modality': record['kind'], 'native_modality_source': record['source'],
            'native_modality_verified_at': record['verified_at']}
