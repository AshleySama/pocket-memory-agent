# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Packaged report-path probe, synthetic response only; not a user release.

Bundle with PyInstaller --copy-metadata llama-cpp-python and reports-v2.css
under frontend/. This deliberately has no model runtime, models or user data.
Explicit headings avoid bundling unrelated LlamaIndex splitting dependencies;
the nonempty focus still runs the affected model-selection source_brief_v2 path.
"""
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pocket_memory import report_brief
from pocket_memory.report_workspace import ReportWorkspace, export_html, valid_ref


class FixtureEngine:
    n_ctx = 3072
    n_threads = 4
    n_gpu_layers = 0

    def __init__(self, path):
        self.model_path = path
        self.requests = []

    def session(self):
        return nullcontext()

    def report_prompt_tokens(self, messages):
        return sum(len(m['content']) for m in messages)

    def report_completion(self, messages, max_tokens, schema=None):
        self.requests.append(dict(messages=messages, max_tokens=max_tokens, schema=schema))
        ids = schema['properties']['chapters']['items']['properties']['groups']['items']['enum']
        output = dict(scope=dict(include='流程与状态', exclude='无'),
                      decisions=[dict(group=gid, decision='include', reason='原文核对') for gid in ids],
                      chapters=[dict(title='发布条件与状态', groups=ids)])
        return dict(text=json.dumps(output, ensure_ascii=False), finish_reason='stop',
                    usage=dict(completion_tokens=50))


def run(folder):
    folder.mkdir(parents=True, exist_ok=True)
    model = folder / 'synthetic-model-marker.bin'
    if not model.exists():
        model.write_bytes(b'Not a real model; fixed response test only.')
    bodies = [('流程', '## 发布流程\n发布前须由负责人批准。\n未获批准不得发送，必须保留草稿。'),
              ('状态', '## 方案状态\n| 名称 | 状态 |\n| --- | --- |\n| 方案甲 | 已验证 |\n| 方案乙 | 规划 |')]
    units = [dict(unit_id=f'n1p{i}', kind='note', source_id=1, note_id=1,
                  title='虚构发布手册', tab=tab, field='content', body=body,
                  revision=hashlib.sha256(body.encode()).hexdigest())
             for i, (tab, body) in enumerate(bodies, 1)]
    engine = FixtureEngine(model)
    workspace = ReportWorkspace(SimpleNamespace(data_dir=folder), engine, threading.RLock())
    args = ('打包兼容验证 · 虚构资料', 'auto', [dict(kind='note', id=1)], units)
    kwargs = dict(focus='整理发布条件与方案状态，保留限制和表格。', composition_mode='source_compiled')
    report = workspace.build(*args, **kwargs)
    assert report['adaptive']['reading_mode'] == 'source_brief_v2'
    assert report['adaptive']['status'] == 'complete', report['adaptive'].get('issues')
    assert report['refs'] and all(valid_ref(ref, units) for ref in report['refs'])
    assert {ref['tab'] for ref in report['refs']} == {'流程', '状态'}
    workspace.write(report)
    reopened = workspace.read(report['id'])
    assert reopened == report
    html = export_html(reopened)
    assert '未获批准不得发送' in html and '方案甲' in html and '方案乙' in html
    for ref in report['refs']:
        assert f'id="source-{ref["id"]}"' in html
    (folder / '虚构文稿-打包验证.html').write_text(html, encoding='utf-8')
    calls_after_first = len(engine.requests)
    second = workspace.build(*args, **kwargs)
    assert len(engine.requests) == calls_after_first
    assert second['generation']['cache_reused_calls'] == 1
    assert second['adaptive']['chapters'] == report['adaptive']['chapters']
    assert second['refs'] == report['refs']
    frozen = bool(getattr(sys, 'frozen', False))
    source_exists = Path(report_brief.__file__).is_file()
    if frozen:
        assert not source_exists, report_brief.__file__
    result = dict(frozen=frozen, loader=type(report_brief.__loader__).__name__,
                  module_source_exists=source_exists, status=report['adaptive']['status'],
                  reading_mode=report['adaptive']['reading_mode'],
                  implementation=report_brief.implementation_contract(),
                  first_request_fixture_calls=calls_after_first,
                  first_request_cache_calls=report['generation']['cache_reused_calls'],
                  repeat_request_new_calls=len(engine.requests)-calls_after_first,
                  refs=len(report['refs']), tabs=sorted({r['tab'] for r in report['refs']}),
                  source_ranges_valid=True, save_reopen_equal=True, export_has_text_and_citation_anchors=True,
                  repeat_body_and_refs_equal=True, real_model_generations=0)
    (folder / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    run(Path(sys.argv[1]).resolve())
