# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0
"""Zero-generation selection checks; source facts are synthetic and distinct."""
import unittest
import copy
import hashlib
from types import SimpleNamespace
from pocket_memory.rag.retained.coverage_contract import CoverageContract
from pocket_memory.rag.citation_display import presentation


class NumberedEvidenceUnitsTests(unittest.TestCase):
    def source(self, sid, body, fragment):
        note=dict(id=sid,title=f'资料{sid}',content=body,parent_note_id=None,tab_name='正文')
        a=body.index(fragment);b=a+len(fragment)
        unit=dict(source_id=sid,source_key=f'note:{sid}',span=[a,b],anchors=[str(sid)],
                  evidence=fragment,text=fragment,kind='content',source_verification='unique_original_span')
        return note,unit

    def setup_pool(self):
        first='''风险一：甲。

风险二：乙；仅在签字后生效。

风险三：丙。'''
        second='''风险一：丁。

风险二：戊。'''
        n1,u1=self.source(101,first,'风险二：乙；仅在签字后生效。')
        n2,u2=self.source(202,second,'风险二：戊。')
        owner=SimpleNamespace(notes={101:n1,202:n2})
        return CoverageContract(owner),{('a',):u1,('b',):u2}

    def test_complete_siblings_are_atomic_for_original_and_paraphrase(self):
        contract,pool=self.setup_pool()
        for question in ('项目有哪些风险','请问，项目有什么风险呢？'):
            result=contract.complete_list_units(question,pool)
            self.assertEqual({u['source_id'] for u in result.values()},{101,202})
            for u in result.values():
                body=contract.owner.notes[u['source_id']]['content']
                self.assertEqual(u['evidence'],body)
                self.assertEqual(u['raw_evidence'][0]['quote'],body)
                self.assertEqual(u['span'],[0,len(body)])
                self.assertEqual(u['numbered_members'],3 if u['source_id']==101 else 2)

    def test_condition_and_named_entity_do_not_expand_to_whole_list(self):
        contract,pool=self.setup_pool()
        for q in ('项目中需要签字的风险是什么','项目中只列风险二','项目风险扫描Agent的核心功能是什么'):
            self.assertEqual(contract.complete_list_units(q,pool),pool)

    def test_missing_number_does_not_claim_complete_list(self):
        n,u=self.source(77,'风险一：甲。\n\n风险三：乙。','风险三：乙。')
        contract=CoverageContract(SimpleNamespace(notes={77:n}));pool={('x',):u}
        self.assertEqual(contract.complete_list_units('项目有哪些风险',pool),pool)

    def test_document_ocr_and_unverified_identity_are_not_substituted(self):
        contract,pool=self.setup_pool()
        doc=dict(pool[('a',)],source_type='document',source_key='document:101')
        ocr=dict(pool[('a',)],kind='ocr',source_key='note:101')
        unknown=dict(pool[('a',)],source_verification='legacy_fragment_unmapped')
        pool={('doc',):doc,**pool,('ocr',):ocr,('unknown',):unknown}
        result=contract.complete_list_units('项目有哪些风险',pool)
        self.assertEqual(result[('doc',)],doc)
        self.assertEqual(result[('ocr',)],ocr)
        self.assertEqual(result[('unknown',)],unknown)
        self.assertEqual(next(iter(result)),('doc',))

    def test_labels_come_from_source_not_risk_dictionary(self):
        n,u=self.source(78,'材料一：纸张。\n\n材料二：标签。','材料二：标签。')
        contract=CoverageContract(SimpleNamespace(notes={78:n}))
        result=contract.complete_list_units('资料有哪些材料',{('x',):u})
        self.assertEqual(next(iter(result.values()))['evidence'],n['content'])

    def test_navigation_retains_exact_group_without_fuzzy_claim_guessing(self):
        contract,pool=self.setup_pool()
        result=contract.complete_list_units('项目有哪些风险',pool)
        unit=next(u for u in result.values() if u['source_id']==101)
        note=SimpleNamespace(**contract.owner.notes[101])
        revision=hashlib.sha256(note.content.encode()).hexdigest()
        source=dict(unit,index=1,citation_members=[dict(source_id=101,
            parent_note_id=None,tab_name='正文',verification='exact_saved_original',
            spans=[dict(r,source_revision=revision) for r in unit['raw_evidence']])])
        store=SimpleNamespace(get_note=lambda _:note)
        answer='概括性改写[1]。'
        before=copy.deepcopy(source)
        display=presentation(source,answer,store=store)
        self.assertEqual(display['display_evidence_count'],3)
        self.assertIn('仅在签字后生效',display['display_evidence'])
        for span in display['display_spans']:
            self.assertEqual(note.content[span['char_start']:span['char_end_exclusive']],span['quote'])
        self.assertEqual(source,before)
        self.assertEqual(presentation(source,'其他来源[2]',store=store)['display_spans'],[])
        note.content+='已更新'
        self.assertEqual(presentation(source,answer,store=store)['display_spans'],[])


if __name__=='__main__': unittest.main()
