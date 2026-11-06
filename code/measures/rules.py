"""Definition-driven whole-term candidates; exclusions remain visible for review.

Excluded safety senses still become candidates and are rejected by the model,
not deleted by proximity rules that might hide a genuine safety discussion.
Temperament's explicit ignore phrases suppress only hits contained within them.
"""
from __future__ import annotations

import re
from bisect import bisect_right

import ahocorasick

from .common import digest
from .meetings import readable


def phrase(term):
    return r'(?<!\w)' + r'\s+'.join(re.escape(w) for w in term.split()) + r'(?!\w)'


class Scanner:
    """Aho-Corasick matching with original offsets after whitespace folding."""
    def __init__(self, terms):
        self.automaton = ahocorasick.Automaton()
        for term in terms:
            self.automaton.add_word(term.lower(), term)
        self.automaton.make_automaton()

    def find(self, text):
        points, deltas, removed = [], [], 0
        def collapse(match):
            nonlocal removed
            position = match.start() - removed
            removed += len(match[0]) - 1
            points.append(position + 1)
            deltas.append(removed)
            return ' '
        # ASCII terms use ASCII case folding, which preserves Unicode offsets.
        normalized = re.sub(r'\s+', collapse, text).translate(str.maketrans(
            'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'))
        def original(position):
            i = bisect_right(points, position) - 1
            return position + (deltas[i] if i >= 0 else 0)
        def word(char):
            return char.isalnum() or char == '_'
        for end, term in self.automaton.iter(normalized):
            start, end = end - len(term) + 1, end + 1
            if (start and word(normalized[start - 1])) or (end < len(normalized) and word(normalized[end])):
                continue
            yield original(start), original(end), term


class Rules:
    def __init__(self, definitions):
        self.definitions = definitions
        self.terms = {}
        def add(term, measure, subtopic=None, excluded=None):
            self.terms.setdefault(term.lower(), []).append((measure, subtopic, excluded))
        for term in definitions['safety']['general_terms']:
            add(term, 'safety')
        for key, value in definitions['safety']['subtopics'].items():
            for term in value['terms']:
                add(term, 'safety', subtopic=key)
        for key, terms in definitions['safety']['excluded_senses'].items():
            for term in terms:
                add(term, 'safety', excluded=key)
        for terms in definitions['temperament']['terms'].values():
            for term in terms:
                add(term, 'temperament')
        for event in definitions['events']['names'].values():
            for term in event['terms']:
                add(term, 'event')
        for term in definitions['events']['generic_terms']:
            add(term, 'event')
        self.scanner = Scanner(self.terms)
        self.ignore = re.compile('|'.join(phrase(t) for t in definitions['temperament']['ignore_phrases']), re.I)
        self.closed = re.compile('|'.join(definitions['closed_session']['patterns']), re.I)
        self.event_terms = {}
        for key, event in definitions['events']['names'].items():
            for term in event['terms']:
                self.event_terms.setdefault(term, set()).add(key)
        self.event_scanner = Scanner(self.event_terms)

    def flags(self, documents):
        closed, events = False, set()
        for row in documents:
            if readable(row):
                text = row['text']
                closed |= bool(self.closed.search(text))
                for _, _, term in self.event_scanner.find(text):
                    events.update(self.event_terms[term])
        return closed, sorted(events)

    def passages(self, record, meeting):
        text = record['text']
        ignores = [m.span() for m in self.ignore.finditer(text)]
        hits = []
        for start, end, term in self.scanner.find(text):
            tags = [t for t in self.terms[term] if not (t[0] == 'temperament'
                    and any(a <= start and end <= b for a, b in ignores))]
            if tags:
                hits.append((start, end, term, tags))
        hits.sort()
        settings = self.definitions['passages']
        windows = []
        for start, end, _, _ in hits:
            a, b = max(0, start - settings['window_chars']), min(len(text), end + settings['window_chars'])
            if windows and a <= windows[-1][1] + settings['merge_gap_chars']:
                windows[-1][1] = max(windows[-1][1], b)
            else:
                windows.append([a, b])
        passages = []
        max_chars = settings['max_passage_chars']
        if max_chars <= 0:
            raise ValueError('max_passage_chars must be positive')
        for a, b in windows:
            start = a
            while start < b:
                end = min(start + max_chars, b)
                # Never split the trigger itself at a chunk boundary.
                for hstart, hend, _, _ in hits:
                    if hstart < end < hend:
                        end = hstart if hstart > start else hend
                        break
                local = [h for h in hits if h[0] < end and h[1] > start]
                if local:
                    tags = [tag for h in local for tag in h[3]]
                    passages.append(dict(passage_id=digest([record['record_id'], start, end]),
                        meeting_id=meeting['meeting_id'], record_id=record['record_id'], tea_id=meeting['tea_id'],
                        date=meeting['date'], detail=record.get('detail'), start=start, end=end, text=text[start:end],
                        candidate_for=sorted({t[0] for t in tags}), matched_terms=sorted({h[2] for h in local}),
                        subtopic_hits=sorted({t[1] for t in tags if t[1]}),
                        excluded_hits=sorted({t[2] for t in tags if t[2]}),
                        definition_version=self.definitions['version']))
                if end == b:
                    break
                # Context overlap keeps triggers and their nearby evidence intact.
                start = max(start + 1, end - min(settings['window_chars'], max_chars // 4))
        return passages

    def measure(self, meeting, documents, best):
        closed, events = self.flags(documents)
        meeting.update(closed_security_session=closed, named_events=events)
        chosen = [r for r in documents if readable(r)
                  and (r is best or r.get('detail') == 'Agenda')]
        passages = []
        seen = set()
        for row in sorted(chosen, key=lambda r: r['record_id']):
            fingerprint = digest(row['text'])
            if fingerprint not in seen:
                passages.extend(self.passages(row, meeting))
                seen.add(fingerprint)
        meeting['n_passages'] = len(passages)
        return passages
