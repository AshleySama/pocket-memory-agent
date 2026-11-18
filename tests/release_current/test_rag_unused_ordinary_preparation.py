# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Routing protection for an unused long-note probe; no model or timing test."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from pocket_memory.rag.retained import coverage_candidate
from pocket_memory.rag.retained.preserved_units import MIN_NOTE_CHARS,OriginalUnits


QUESTION='设备为什么报警，应该如何核对处理？'


def fixture(*,ordinary=True,route='retained',notes=None):
    if notes is None:
        notes={1:dict(content='第一份短正文。'),2:dict(content='第二份短正文。')}
    hits=[SimpleNamespace(node=SimpleNamespace(metadata=dict(
        source_id=sid,source_key=f'note:{sid}'))) for sid in (1,2)]
    preparation=dict(route=route,ordinary_context_contract=ordinary,
        retrieved=hits,sources=[dict(source_key='note:1')])
    ordinary_prepared=([dict(source_key='note:1')],['ordinary messages'],{'ordinary':True})
    messages=[dict(role='system',content='unchanged system'),dict(role='user',content='unchanged evidence')]
    contract=SimpleNamespace(prepare=Mock(return_value=ordinary_prepared),
        messages=Mock(return_value=messages),count=Mock(return_value=100))
    stable=SimpleNamespace(notes=notes,prepare=Mock(return_value=preparation),contract=contract)
    candidate=object.__new__(coverage_candidate.Candidate)
    candidate.stable=stable
    candidate.original_units=OriginalUnits(stable)
    candidate.original_units.prepare=Mock(return_value=None)
    candidate.coverage=SimpleNamespace(prepare=Mock(return_value=None))
    bridge_sources=[dict(source_key='note:1',evidence_id='whole:1'),
        dict(source_key='note:2',evidence_id='whole:2')]
    bridge_trace={'selected':['whole:1','whole:2']}
    candidate.bridge=SimpleNamespace(ocr={},prepare=Mock(return_value=dict(
        sources=bridge_sources,evidence_bridge=bridge_trace)))
    return candidate,preparation,ordinary_prepared,bridge_sources,bridge_trace,messages


class UnusedOrdinaryPreparationTests(unittest.TestCase):
    def prepare(self,candidate,question=QUESTION):
        with patch.object(coverage_candidate,'assemble',side_effect=lambda sources:(sources,[])), \
                patch.object(coverage_candidate,'locations',side_effect=lambda sources,*args:sources):
            return candidate.prepare(question)

    def test_necessary_condition_uses_shared_boundary_and_fails_closed(self):
        for length,expected in ((MIN_NOTE_CHARS-1,False),(MIN_NOTE_CHARS,True),(MIN_NOTE_CHARS+1,True)):
            with self.subTest(length=length):
                adapter=OriginalUnits(SimpleNamespace(notes={1:dict(content='字'*length)}))
                self.assertEqual(adapter.may_apply_to_current_notes(),expected)
        for notes in (None,[],{1:{}},{1:{'content':None}},{1:{'content':b'bytes'}},{1:object()}):
            with self.subTest(notes_type=type(notes).__name__):
                self.assertTrue(OriginalUnits(SimpleNamespace(notes=notes)).may_apply_to_current_notes())
        self.assertFalse(OriginalUnits(SimpleNamespace(notes={})).may_apply_to_current_notes())

    def test_short_notes_skip_only_unused_probe_and_preserve_bridge_input(self):
        c,p,ordinary,sources,trace,messages=fixture()
        result=self.prepare(c)
        c.stable.contract.prepare.assert_not_called()
        c.original_units.prepare.assert_not_called()
        c.coverage.prepare.assert_not_called()
        c.bridge.prepare.assert_called_once_with(QUESTION,p)
        self.assertIs(c.bridge.prepare.call_args.args[1],p)
        self.assertIs(result['sources'],sources)
        self.assertIs(result['messages'],messages)
        self.assertEqual(result['evidence_bridge']['selected'],trace['selected'])
        self.assertNotIn('ordinary_preparation',trace)
        audit=result['evidence_bridge']['ordinary_preparation']
        self.assertTrue(audit['skipped'])
        self.assertEqual(audit['minimum_note_chars'],MIN_NOTE_CHARS)
        self.assertEqual(audit['consumer'],'OriginalUnits.prepare')
        self.assertEqual(result['mode'],'single_free_text')

    def test_unretrieved_long_note_prevents_shortcut(self):
        notes={1:dict(content='短文甲'),2:dict(content='短文乙'),
            3:dict(content='字'*MIN_NOTE_CHARS)}
        c,p,ordinary,*_=fixture(notes=notes)
        result=self.prepare(c)
        c.stable.contract.prepare.assert_called_once_with(QUESTION,p['retrieved'])
        c.original_units.prepare.assert_called_once_with(QUESTION,p['retrieved'],ordinary)
        c.bridge.prepare.assert_called_once_with(QUESTION,p)
        self.assertNotIn('ordinary_preparation',result['evidence_bridge'])

    def test_unknown_body_retains_original_probe(self):
        for body in (None,b'bytes'):
            with self.subTest(body_type=type(body).__name__):
                c,p,ordinary,*_=fixture(notes={1:dict(content='短文'),2:dict(content=body)})
                self.prepare(c)
                c.stable.contract.prepare.assert_called_once_with(QUESTION,p['retrieved'])
                c.original_units.prepare.assert_called_once_with(QUESTION,p['retrieved'],ordinary)

    def test_nonaffected_paths_still_consume_ordinary_preparation(self):
        for one_family,question in ((False,'怎样核对处理设备报警'),(True,QUESTION)):
            with self.subTest(one_family=one_family):
                c,p,ordinary,*_=fixture()
                if one_family:
                    for note in c.stable.notes.values():note['parent_note_id']=7
                c.original_units.may_apply_to_current_notes=Mock(side_effect=AssertionError('unneeded preflight'))
                result=self.prepare(c,question)
                c.stable.contract.prepare.assert_called_once_with(question,p['retrieved'])
                c.coverage.prepare.assert_called_once_with(question,ordinary)
                c.bridge.prepare.assert_not_called()
                self.assertIs(result['ordinary_prepared'],ordinary)
                self.assertEqual(result['mode'],'preserved_v19')

    def test_successful_long_note_adapter_keeps_priority_over_bridge(self):
        c,p,ordinary,*_=fixture(notes={1:dict(content='字'*MIN_NOTE_CHARS),2:dict(content='短文')})
        revised=([dict(evidence_id='original-unit:1')],['revised messages'],{'policy':'complete_numbered_parent'})
        c.original_units.prepare.return_value=revised
        result=self.prepare(c)
        c.original_units.prepare.assert_called_once_with(QUESTION,p['retrieved'],ordinary)
        c.bridge.prepare.assert_not_called()
        self.assertIs(result['sources'],revised[0])
        self.assertIs(result['messages'],revised[1])
        self.assertIs(result['evidence_bridge'],revised[2])

    def test_nonordinary_bridge_does_not_run_new_preflight(self):
        c,p,ordinary,sources,trace,*_=fixture(ordinary=False,route='standard_retrieval')
        c.original_units.may_apply_to_current_notes=Mock(side_effect=AssertionError('unneeded preflight'))
        result=self.prepare(c)
        c.stable.contract.prepare.assert_not_called()
        c.original_units.prepare.assert_not_called()
        c.bridge.prepare.assert_called_once_with(QUESTION,p)
        self.assertIs(result['evidence_bridge'],trace)
        self.assertIs(result['sources'],sources)


if __name__=='__main__':unittest.main()
