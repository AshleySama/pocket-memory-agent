# Copyright 2026 AshleySama
# SPDX-License-Identifier: Apache-2.0

"""Presentation-only focus inside an already cited, verified original span.

Never selects model evidence, changes an answer/citation, or claims entailment.
Broad context stays on the payload. Uncertain prose has context, not a guessed
highlight; structured records and complete-range proofs remain indivisible.
"""
import difflib
import hashlib
import re
from collections import Counter


def normalized(text):
    return ''.join(re.findall(r'[\w\u3400-\u9fff]', text)).casefold()


# This parser is only for finding display anchors. It does not replace the
# answer/citation guard's claim parser, change citation numbers or rewrite text.
_CITATIONS = re.compile(r'(?:\[\s*\d+(?:\s*[,，、]\s*\d+)*\s*\]\s*)+')
_LIST_PREFIX = re.compile(r'^\s*(?:[-+*]\s+|(?:\d+|[一二三四五六七八九十]+)[、．]\s*|\d+\.\s+|[（(](?:\d+|[一二三四五六七八九十]+)[）)]\s*)')
_REFERENCE_LABELS = {
    '支持资料编号', '参考资料编号', '引用资料编号', '引用编号', '来源编号',
    '参考资料', '资料来源',
}


def display_claims(answer):
    """Bind suffix citations to their preceding text, including after a period.

    Citation metadata and list decoration are not factual claims. A citation
    ends a local text run, so ``A[1]，B[2]`` never attributes A and B to both
    sources. Text without a suffix citation remains unassigned.
    """
    claims = []
    for raw in str(answer).splitlines():
        line = raw.strip().lstrip('> ').strip()
        label = _CITATIONS.sub('', line).strip(' \t*_`#：:。.;；，,、')
        if label in _REFERENCE_LABELS and _CITATIONS.search(line):
            continue
        offset = 0
        for marker in _CITATIONS.finditer(line):
            run = line[offset:marker.start()].strip(' \t*_`，,；;。！？!?')
            run = _LIST_PREFIX.sub('', run).strip(' \t*_`')
            ids = sorted({int(n) for n in re.findall(r'\d+', marker.group())})
            for text in re.split(r'[。！？!?]+|(?<=\.)\s+', run):
                text = text.strip(' \t*_`，,；;。！？!?')
                if normalized(text):
                    claims.append(dict(text=text, citations=ids))
            offset = marker.end()
    return claims


def literal_focus(claim, quote):
    a, b = normalized(claim), normalized(quote)
    if len(a) < 6:
        return False
    if a in b:
        return True
    # Allow a small word-order paraphrase only with all answer characters,
    # a substantial contiguous anchor, and matching numbers/identifiers.
    # This is a reading aid, not a semantic-support validator.
    if re.search(r'没有|不是|并非|未|不|仅|除|比较|相同|不同|高于|低于', claim):
        return False
    if Counter(a) - Counter(b):
        return False
    markers = re.findall(r'[A-Za-z_][A-Za-z_\d.]*|\d+(?:\.\d+)?', claim)
    if any(marker.casefold() not in b for marker in markers):
        return False
    anchor = difflib.SequenceMatcher(None, a, b, autojunk=False).find_longest_match().size
    pairs = {a[i:i+2] for i in range(len(a)-1)}
    return anchor >= 6 and sum(pair in b for pair in pairs) >= len(pairs) * .5


# Display alignment is not entailment. Only admit small, distributed edits in
# otherwise closely aligned, multi-clause text. No synonym/domain dictionary,
# retrieval changes, re-citation or second model call is involved.
_OPERATORS = re.compile(
    r'除非|否则|如果|假如|只要|只有|即使|未经|无需|不得|不能|不必|不再|'
    r'尚未|尚不|没有|并非|不是|必须|应当|允许|禁止|至少|最多|超过|高于|低于|'
    r'全部|所有|仅|只|每|均|都|前|后')
_MARKERS = re.compile(r'[A-Za-z_][A-Za-z_\d.]*|\d+(?:\.\d+)?')
_HAN_QUANTITY = re.compile(r'[零〇一二三四五六七八九十百千万亿两]+(?:元|万|亿|年|月|日|天|小时|分钟|次|项|个|人|%|％)')
_HEADING = re.compile(r'^([^，,；;。！？!?\n：:]{1,16})[：:]')
_ASSIGNMENT = re.compile(r'(?:名为|称为|叫做|是|为|由)(.+)')


def _clause_text(text):
    # A physical heading is decoration, not part of a sentence's subject.
    # Return original quote/coordinates unchanged; this is matching text only.
    text = _HEADING.sub('', text.strip())
    return [normalized(x) for x in re.split(r'[，,；;]', text) if normalized(x)]


def _same_markers(a, b):
    if Counter(m.casefold() for m in _MARKERS.findall(a)) != Counter(m.casefold() for m in _MARKERS.findall(b)):
        return False
    if Counter(_HAN_QUANTITY.findall(a)) != Counter(_HAN_QUANTITY.findall(b)):
        return False
    # Keep the immediate numeric field as well as the digits: principal is not
    # interest merely because both contain the same number. Conservative by
    # design; changed numeric wording remains context-only.
    for left, right in zip(re.finditer(r'\d+(?:\.\d+)?', a), re.finditer(r'\d+(?:\.\d+)?', b)):
        if a[max(0,left.start()-6):left.start()] != b[max(0,right.start()-6):right.start()]:
            return False
        if a[left.end():left.end()+2] != b[right.end():right.end()+2]:
            return False
    return True


def aligned_focus(claim, quote):
    """Conservative reading-location fallback for lightly rewritten prose.

    Every clause must align to the same original sentence in order. Stable
    leading anchors protect subjects; operators and numeric roles must agree.
    Polarity is checked per clause, including Chinese difficulty expressions.
    This is deliberately not a general paraphrase or semantic-support judge.
    """
    label_a, label_b = _HEADING.match(claim.strip()), _HEADING.match(quote.strip())
    if label_a and (not label_b or normalized(label_a[1]) != normalized(label_b[1])):
        return False  # an explicit answer subject is not disposable decoration
    a, b = _clause_text(claim), _clause_text(quote)
    if not (2 <= len(a) == len(b) <= 6) or sum(map(len,a)) < 24:
        return False
    edits, total_common = 0, 0
    for i, (left, right) in enumerate(zip(a,b)):
        if min(len(left),len(right)) < 8 or not _same_markers(left,right):
            return False
        if _OPERATORS.findall(left) != _OPERATORS.findall(right):
            return False
        # Assignment tails carry names/field values. Do not infer that a
        # substituted value is synonymous because surrounding prose matches.
        value_a, value_b = _ASSIGNMENT.search(left), _ASSIGNMENT.search(right)
        if (bool(value_a) != bool(value_b)
                or (value_a and value_a.group() != value_b.group())):
            return False
        matcher = difflib.SequenceMatcher(None,left,right,autojunk=False)
        blocks = [x for x in matcher.get_matching_blocks() if x.size]
        neg_a, neg_b = list(re.finditer(r'不|未|无|难',left)), list(re.finditer(r'不|未|无|难',right))
        if len(neg_a) != len(neg_b):
            return False
        # A polarity word may be rephrased beside a short verb, but must not
        # move across an unchanged predicate/object anchor to negate something
        # else in the same clause.
        for na,nb in zip(neg_a,neg_b):
            prior_a=[k for k,x in enumerate(blocks) if x.size>=3 and x.a+x.size<=na.start()]
            prior_b=[k for k,x in enumerate(blocks) if x.size>=3 and x.b+x.size<=nb.start()]
            if prior_a != prior_b:
                return False
        # The first clause carries the primary subject. Later clauses still
        # require an unchanged leading anchor; a shared tail is insufficient.
        head_size = 6 if i==0 else (1 if left[0] in '会但却而并也' else 2)
        if not blocks or blocks[0].a != 0 or blocks[0].b != 0 or blocks[0].size < head_size:
            return False
        if max(x.size for x in blocks) < 4:
            return False
        common = sum(x.size for x in blocks)
        # A short predicate clause may contain several small connective edits;
        # require local anchors AND strong coverage of the entire statement.
        if common/max(len(left),len(right)) < .55:
            return False
        total_common += common
        for tag,a0,a1,b0,b1 in matcher.get_opcodes():
            if tag=='equal': continue
            if max(a1-a0,b1-b0) > 3:
                return False
            edits += 1
    # One tiny replacement can be a new person/role. This fallback targets
    # distributed prose rewrites, not isolated fact substitutions.
    return (edits >= 2 and total_common/sum(map(len,a)) >= .75
            and total_common/sum(map(len,b)) >= .70)


def focused_matches(claim, candidates):
    matches = [s for s in candidates if literal_focus(claim,s['quote'])]
    if matches:
        return matches, 'literal'
    # Equivalent registry members may repeat the same physical span. Dedup by
    # identity before ambiguity detection, never by quote across locations.
    hits = {(s.get('source_id'),s.get('field'),s['char_start'],s['char_end_exclusive']):s
            for s in candidates if aligned_focus(claim,s['quote'])}
    return (list(hits.values()), 'aligned') if len(hits)==1 else ([], 'unresolved')


def document_presentation(source, answer, store):
    """Focus saved PDF/text sentences, never use the entire chunk as a highlight.

    Coordinates are local to a persisted chunk (not PDF byte/page coordinates).
    A partial literal match is only a reading aid; all supplied context remains.
    """
    base = dict(display_evidence='', display_spans=[], display_evidence_count=0,
                display_context=source.get('evidence', ''), display_evidence_kind='context_only',
                display_document_spans=[])
    if store is None:
        return base
    try:
        chunk = store.get_document_chunk(int(source['chunk_id']))
        if chunk.document_id != source.get('source_id'):
            return base
    except (KeyError, TypeError, ValueError):
        return base
    # Whitespace changes are permitted for matching PDF line wraps, but every
    # returned quote/offset is taken from the persisted original, never rebuilt.
    body = chunk.text
    positions = [i for i, char in enumerate(body) if not char.isspace()]
    compact_body = ''.join(body[i] for i in positions)
    evidence = ''.join(str(source.get('evidence', '')).split())
    at = compact_body.find(evidence) if evidence else -1
    if at < 0 or compact_body.find(evidence, at + 1) >= 0:
        return base
    a, b = positions[at], positions[at + len(evidence) - 1] + 1
    revision = hashlib.sha256(body.encode('utf-8')).hexdigest()
    candidates = []
    # PDF visual newlines are not sentence boundaries (e.g. 多线\n程).
    for match in re.finditer(r'[^。！？!?]+[。！？!?]?', body[a:b]):
        quote = match.group().strip()
        start = a + match.start() + len(match.group()) - len(match.group().lstrip())
        if quote:
            candidates.append(dict(field='document_text', chunk_id=chunk.id,
                document_id=chunk.document_id, char_start=start,
                char_end_exclusive=start+len(quote), quote=quote, source_revision=revision))
    claims = [c['text'] for c in display_claims(answer)
              if source.get('index') in c['citations']]
    focused, matched = [], 0
    for claim in claims:
        # Match explicit answer clauses; never a query term alone. Other claims
        # remain available through full context, not silently certified.
        clauses = [claim] + [c.strip() for c in re.split(r'[，,；;]', claim) if len(normalized(c)) >= 8]
        def literal_anchor(clause, quote):
            if literal_focus(clause, quote):
                return True
            # A long unchanged phrase can locate an original sentence even if
            # the surrounding connective was paraphrased. This does not change
            # citation support or certify the rest of the model's claim.
            if re.search(r'没有|不是|并非|未|不|仅|除|比较|相同|不同|高于|低于', clause):
                return False
            left, right = normalized(clause), normalized(quote)
            markers = re.findall(r'[A-Za-z_][A-Za-z_\d.]*|\d+(?:\.\d+)?', clause)
            if any(normalized(m) not in right for m in markers):
                return False
            match = difflib.SequenceMatcher(None, left, right, autojunk=False).find_longest_match()
            return match.size >= 12 and match.size >= len(left) * .5
        hits = [s for s in candidates if any(literal_anchor(c, s['quote']) for c in clauses)]
        matched += bool(hits)
        focused.extend(hits)
    unique = {(s['char_start'], s['char_end_exclusive']): s for s in focused}
    spans = [unique[k] for k in sorted(unique)]
    if spans:
        base.update(display_evidence='\n'.join(s['quote'] for s in spans),
                    display_spans=spans, display_document_spans=spans,
                    display_evidence_count=len(spans),
                    display_evidence_kind='literal_sentence_focus',
                    display_partial_focus=matched < len(claims))
    return base


def presentation(source, answer, *, direct=False, store=None):
    """Return additional display fields; leave the supplied source untouched."""
    if source.get('source_type') == 'document' and source.get('kind') == 'document' and not direct:
        # Keep tables and complete-range proofs indivisible.
        if not (source.get('coverage_complete') or source.get('table_headers')
                or source.get('structured_rows') or re.search(r'<(?:table|tr)\b|\|[^\n]+\|', source.get('evidence', ''), re.I)):
            return document_presentation(source, answer, store)
    if (direct or source.get('kind') != 'content' or source.get('coverage_complete')
            or source.get('table_headers') or source.get('structured_rows')
            or re.search(r'<(?:table|tr)\b|\|[^\n]+\|', source.get('evidence', ''), re.I)):
        return {}  # preserve table headers, rows, OCR structure and range proofs
    base = dict(display_evidence='', display_spans=[], display_evidence_count=0,
                display_context=source.get('evidence', ''), display_evidence_kind='context_only')
    claims = [c['text'] for c in display_claims(answer)
              if source.get('index') in c['citations']]
    if not claims:
        return base
    candidates = []
    # The registry describes the captured revision. Check the saved revision
    # again before emitting positions for an interactive note viewer.
    note = None
    body = None
    if store is not None:
        try:
            note = store.get_note(int(source['source_id']))
            body = note.content
            if note.id != source.get('source_id') or not isinstance(body, str):
                return base
        except (AttributeError, KeyError, TypeError, ValueError):
            return base
        revision = hashlib.sha256(body.encode('utf-8')).hexdigest()
    for member in source.get('citation_members', []):
        if member.get('verification') != 'exact_saved_original':
            continue
        if source.get('source_id') != member.get('source_id', source.get('source_id')):
            continue
        if note is not None and any(
                key in member and member[key] != getattr(note, key, None)
                for key in ('parent_note_id', 'tab_name')):
            continue
        for span in member.get('spans', []):
            if span.get('field') != 'content':
                continue  # never guess OCR row/sentence boundaries
            a, b, quote = span.get('char_start'), span.get('char_end_exclusive'), span.get('quote')
            if not (isinstance(a, int) and isinstance(b, int) and isinstance(quote, str)
                    and isinstance(span.get('source_revision'), str)
                    and 0 <= a < b and b - a == len(quote)):
                continue
            if body is not None and (span.get('source_revision') != revision
                                     or body[a:b] != quote):
                continue
            for match in re.finditer(r'[^。！？!?\n]+[。！？!?]?', quote):
                text = match.group().strip()
                if not text:
                    continue
                start = span['char_start'] + match.start() + len(match.group()) - len(match.group().lstrip())
                candidates.append(dict(field='content', source_id=source.get('source_id'),
                    parent_note_id=member.get('parent_note_id'), tab_name=member.get('tab_name'),
                    char_start=start,
                    char_end_exclusive=start+len(text), quote=text,
                    source_revision=span['source_revision']))
    focused, matched, methods = [], 0, Counter()
    # A complete-list request has an original, numbered evidence group. Its
    # navigation range is structural, not a guess about paraphrased wording.
    # Validate every member against the live revision AND the cited registry;
    # never upgrade unrelated retrieval hits or OCR into answer support.
    requested = source.get('requested_list_spans') or []
    if body is not None and len(requested) >= 2:
        verified = [span for member in source.get('citation_members', [])
                    if member.get('verification') == 'exact_saved_original'
                    and member.get('source_id') == source.get('source_id')
                    for span in member.get('spans', []) if span.get('field') == 'content']
        def current_member(span):
            a,b = span.get('char_start'),span.get('char_end_exclusive')
            return (span.get('field') == 'content' and span.get('source_id') == note.id
                    and span.get('parent_note_id') == note.parent_note_id
                    and span.get('tab_name') == note.tab_name
                    and span.get('source_revision') == revision
                    and isinstance(a,int) and isinstance(b,int) and 0<=a<b<=len(body)
                    and body[a:b] == span.get('quote')
                    and any(s.get('source_revision')==revision and
                            s['char_start']<=a<b<=s['char_end_exclusive']
                            and body[s['char_start']:s['char_end_exclusive']]==s.get('quote')
                            for s in verified))
        if all(current_member(s) for s in requested):
            ordered = sorted(requested,key=lambda s:s['char_start'])
            base.update(display_evidence='\n'.join(s['quote'] for s in ordered),
                        display_spans=ordered,display_evidence_count=len(ordered),
                        display_evidence_kind='requested_list_evidence',
                        display_alignment_semantics='requested_original_group_not_semantic_entailment')
            return base
    for claim in claims:
        matches, method = focused_matches(claim, candidates)
        if matches:
            methods[method] += 1
        matched += bool(matches)
        focused.extend(matches)
    unique = {(s['field'], s['char_start'], s['char_end_exclusive']): s for s in focused}
    spans = [unique[key] for key in sorted(unique)]
    if spans:
        base.update(display_evidence='\n'.join(s['quote'] for s in spans), display_spans=spans,
                    display_evidence_count=len(spans),
                    display_evidence_kind='aligned_sentence_focus' if methods['aligned'] else 'literal_sentence_focus',
                    display_partial_focus=matched < len(claims),
                    display_focus_methods=dict(methods),
                    display_alignment_semantics='reading_location_only')
    return base
