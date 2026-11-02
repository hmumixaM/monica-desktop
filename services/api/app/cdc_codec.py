"""Strict pgoutput protocol-v1 decoder for PostgreSQL's binary SQL interface.

One peek returns complete committed transactions and a fresh relation stream.
Unknown messages fail closed: no WAL acknowledgement can follow a decode error.
"""
import json
import struct
from datetime import UTC, datetime, timedelta


def lsn(value):
    return f'{value >> 32:X}/{value & 0xFFFFFFFF:X}'


class Reader:
    def __init__(self, data):
        self.data = bytes(data)
        self.offset = 0

    def take(self, count):
        result = self.data[self.offset:self.offset + count]
        if count < 0 or len(result) != count:
            raise ValueError('Truncated pgoutput message')
        self.offset += count
        return result

    def number(self, code):
        return struct.unpack('!' + code, self.take(struct.calcsize('!' + code)))[0]

    def string(self):
        end = self.data.find(b'\0', self.offset)
        if end < 0:
            raise ValueError('Unterminated pgoutput string')
        return self.take(end - self.offset + 1)[:-1].decode('utf-8')

    def tuple(self, columns):
        if self.number('H') != len(columns):
            raise ValueError('Unexpected pgoutput tuple width')
        value, toasted = {}, []
        for name, oid in columns:
            kind = self.take(1)
            if kind == b'n':
                value[name] = None
            elif kind == b'u':
                toasted.append(name)
            elif kind == b't':
                raw = self.take(self.number('I')).decode('utf-8')
                value[name] = json.loads(raw) if oid in (114, 3802) else raw
            else:
                raise ValueError('Unsupported pgoutput tuple encoding')
        if toasted:
            value['_unchanged_toast_columns'] = toasted
        return value


def transactions(messages):
    relations, changes, result = {}, [], []
    active = False
    for data in messages:
        r = Reader(data)
        kind = r.take(1)
        if kind == b'B':
            if active:
                raise ValueError('Nested pgoutput transaction')
            r.take(20)
            active = True
            changes = []
        elif kind == b'R':
            relation = r.number('I')
            schema, name = r.string(), r.string()
            r.take(1)
            columns = []
            for _ in range(r.number('H')):
                r.take(1)
                column, oid = r.string(), r.number('I')
                r.take(4)
                columns.append((column, oid))
            relations[relation] = (schema, name, columns)
        elif kind in (b'I', b'U', b'D'):
            if not active:
                raise ValueError('Row outside pgoutput transaction')
            schema, name, columns = relations[r.number('I')]
            old, new = None, None
            marker = r.take(1)
            if marker in (b'K', b'O'):
                old = r.tuple(columns)
                marker = r.take(1) if kind == b'U' else None
            if marker == b'N':
                new = r.tuple(columns)
            elif kind != b'D':
                raise ValueError('Missing pgoutput new tuple')
            if new is not None:
                # pgoutput omits unchanged external TOAST values in the new
                # tuple. FULL replica identity supplies the previous value;
                # preserve it instead of inventing a deletion in the history.
                for column in new.pop('_unchanged_toast_columns', []):
                    if old is None or column not in old:
                        raise ValueError('Unchanged TOAST value has no full before image')
                    new[column] = old[column]
            changes.append({'relation': name, 'schema': schema, 'operation': kind.decode(),
                            'before': old, 'after': new})
        elif kind == b'T':
            if not active:
                raise ValueError('Truncate outside pgoutput transaction')
            count = r.number('I')
            r.take(1)
            for _ in range(count):
                schema, name, _columns = relations[r.number('I')]
                changes.append({'relation': name, 'schema': schema, 'operation': 'T', 'before': None, 'after': None})
        elif kind == b'C':
            if not active:
                raise ValueError('Commit outside pgoutput transaction')
            r.take(1)
            commit, end = r.number('Q'), r.number('Q')
            committed = datetime(2000, 1, 1, tzinfo=UTC) + timedelta(microseconds=r.number('q'))
            result.append({'commit_lsn': lsn(commit), 'end_lsn': lsn(end), 'committed_at': committed.isoformat(), 'changes': changes})
            active = False
        elif kind == b'Y':
            r.take(4)
            r.string()
            r.string()
        elif kind == b'O':
            r.take(8)
            r.string()
        else:
            raise ValueError('Unsupported pgoutput message: ' + repr(kind))
        if r.offset != len(r.data):
            raise ValueError('Trailing pgoutput bytes')
    if active:
        raise ValueError('Incomplete pgoutput transaction')
    return result
