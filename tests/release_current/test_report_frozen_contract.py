# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""No-LLM regression for source-free report modules and safe cache reuse."""
import copy
import importlib.machinery
import importlib.util
from pathlib import Path
import py_compile
from unittest import mock
import unittest

import test_report_brief as fixtures
from pocket_memory import report_brief as brief


class FrozenReportContractTests(unittest.TestCase):
    setUp = fixtures.ReportBriefTests.setUp
    generate = fixtures.ReportBriefTests.generate

    def test_missing_module_source_does_not_change_request_body_or_citations(self):
        original_engine = fixtures.Engine(self.model)
        before, before_claims, _ = self.generate(original_engine, cache_dir=None)
        engine = fixtures.Engine(self.model)
        absent = self.path / '_internal/pocket_memory/report_brief.py'
        self.assertFalse(absent.exists())
        with mock.patch.object(brief, '__file__', str(absent)):
            after, claims, receipt = self.generate(engine, cache_dir=None)
        self.assertEqual(after['status'], 'complete')
        self.assertEqual(receipt['calls'], 1)
        self.assertEqual(engine.requests, original_engine.requests)
        for key in ('facts', 'chapters', 'selection', 'selected_coverage'):
            self.assertEqual(before[key], after[key])
        self.assertEqual(claims, before_claims)

    def test_real_sourceless_loader_generates_and_reuses_cache(self):
        pyc = self.path / 'report_brief.pyc'
        py_compile.compile(brief.__file__, cfile=str(pyc), doraise=True)
        loader = importlib.machinery.SourcelessFileLoader(brief.__name__, str(pyc))
        spec = importlib.util.spec_from_loader(brief.__name__, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        self.assertIsNone(loader.get_source(brief.__name__))
        module.__file__ = str(self.path / '_internal/pocket_memory/report_brief.py')
        self.assertFalse(Path(module.__file__).exists())
        sources = fixtures.sources()
        engine = fixtures.Engine(self.model)
        report, claims, receipt = module.generate(engine, '资料', self.focus, sources,
                                                 fixtures.Registry(), cache_dir=self.path/'bytecode-cache')
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(claims)
        engine = fixtures.Engine(self.model)
        cached, _, reused = module.generate(engine, '资料', self.focus, sources,
                                            fixtures.Registry(), cache_dir=self.path/'bytecode-cache')
        self.assertEqual(engine.requests, [])
        self.assertEqual(reused['cache_reused_calls'], 1)
        self.assertEqual(cached['chapters'], report['chapters'])

    def test_implementation_change_invalidates_cache_and_saved_outline(self):
        prepared, _, _ = self.generate(stop_after_outline=True)
        stable_engine = fixtures.Engine(self.model)
        _, _, reused = self.generate(stable_engine, stop_after_outline=True)
        self.assertEqual(reused['cache_reused_calls'], 1)
        old = brief.implementation_contract()
        changed = dict(old, sha256='f'*64 if old['sha256'] != 'f'*64 else 'e'*64)
        with mock.patch.object(brief, 'implementation_contract', return_value=changed):
            engine = fixtures.Engine(self.model)
            _, _, generated = self.generate(engine)
            self.assertEqual(generated['calls'], 1)
            with self.assertRaisesRegex(ValueError, '已变化'):
                self.generate(prepared=prepared)
        # Removing the old file hash is a contract migration, not a wildcard.
        old_shape = copy.deepcopy(prepared)
        model = old_shape['planning_contract']['model']
        model['source_code_sha256'] = model.pop('implementation')['sha256']
        with self.assertRaisesRegex(ValueError, '已变化'):
            self.generate(prepared=old_shape)

    def test_digest_ignores_install_location_but_detects_nested_code_change(self):
        source = 'VALUE = 2\ndef transform():\n    return VALUE + 1\n'
        first = compile(source, 'C:/old/_internal/report_brief.py', 'exec')
        moved = compile(source, 'D:/new/_internal/report_brief.py', 'exec')
        changed = compile(source.replace('+ 1', '+ 3'), 'C:/old/_internal/report_brief.py', 'exec')
        self.assertEqual(brief._code_digest(first), brief._code_digest(moved))
        self.assertNotEqual(brief._code_digest(first), brief._code_digest(changed))
        with self.assertRaises(ValueError):
            brief._code_digest(None)
        with mock.patch.object(brief, '__loader__', object()), self.assertRaises(ValueError):
            brief.implementation_contract()


if __name__ == '__main__':
    unittest.main()
