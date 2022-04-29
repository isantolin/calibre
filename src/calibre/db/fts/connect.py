#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2022, Kovid Goyal <kovid at kovidgoyal.net>


import builtins, apsw
import hashlib
import os
import sys
from contextlib import suppress

from calibre.utils.date import EPOCH, utcnow
from calibre.db import FTSQueryError
from calibre.db.annotations import unicode_normalize

from .pool import Pool
from .schema_upgrade import SchemaUpgrade

# TODO: check that switching libraries with indexing enabled/disabled works
# TODO: db dump+restore
# TODO: calibre export/import
# TODO: check library and vacuuming of fts db


def print(*args, **kwargs):
    kwargs['file'] = sys.__stdout__
    builtins.print(*args, **kwargs)


class FTS:

    def __init__(self, dbref):
        self.dbref = dbref
        self.pool = Pool(dbref)

    def initialize(self, conn):
        main_db_path = os.path.abspath(conn.db_filename('main'))
        dbpath = os.path.join(os.path.dirname(main_db_path), 'full-text-search.db')
        conn.execute(f'ATTACH DATABASE "{dbpath}" AS fts_db')
        SchemaUpgrade(conn)
        conn.fts_dbpath = dbpath
        conn.execute('UPDATE fts_db.dirtied_formats SET in_progress=FALSE WHERE in_progress=TRUE')

    def get_connection(self):
        db = self.dbref()
        if db is None:
            raise RuntimeError('db has been garbage collected')
        ans = db.backend.get_connection()
        if ans.fts_dbpath is None:
            self.initialize(ans)
        return ans

    def dirty_existing(self):
        conn = self.get_connection()
        conn.execute('''
            INSERT OR IGNORE INTO fts_db.dirtied_formats(book, format)
            SELECT book, format FROM main.data;
        ''')

    def number_dirtied(self):
        conn = self.get_connection()
        return conn.get('''SELECT COUNT(*) from fts_db.dirtied_formats''')[0][0]

    def all_currently_dirty(self):
        conn = self.get_connection()
        return conn.get('''SELECT book, format from fts_db.dirtied_formats''', all=True)

    def clear_all_dirty(self):
        conn = self.get_connection()
        conn.execute('DELETE FROM fts_db.dirtied_formats')

    def remove_dirty(self, book_id, fmt):
        conn = self.get_connection()
        conn.execute('DELETE FROM fts_db.dirtied_formats WHERE book=? AND format=?', (book_id, fmt.upper()))

    def add_text(self, book_id, fmt, text, text_hash='', fmt_size=0, fmt_hash='', err_msg=''):
        conn = self.get_connection()
        ts = (utcnow() - EPOCH).total_seconds()
        fmt = fmt.upper()
        if err_msg:
            conn.execute(
                'INSERT OR REPLACE INTO fts_db.books_text '
                '(book, timestamp, format, format_size, format_hash, err_msg) VALUES '
                '(?, ?, ?, ?, ?, ?)', (
                    book_id, ts, fmt, fmt_size, fmt_hash, err_msg))
        elif text:
            conn.execute(
                'INSERT OR REPLACE INTO fts_db.books_text '
                '(book, timestamp, format, format_size, format_hash, searchable_text, text_size, text_hash) VALUES '
                '(?, ?, ?, ?, ?, ?, ?, ?)', (
                    book_id, ts, fmt, fmt_size, fmt_hash, text, len(text), text_hash))
        else:
            conn.execute('DELETE FROM fts_db.dirtied_formats WHERE book=? AND format=?', (book_id, fmt))

    def get_next_fts_job(self):
        conn = self.get_connection()
        for book_id, fmt in conn.get('SELECT book,format FROM fts_db.dirtied_formats WHERE in_progress=FALSE ORDER BY id'):
            return book_id, fmt
        return None, None

    def commit_result(self, book_id, fmt, fmt_size, fmt_hash, text, err_msg=''):
        conn = self.get_connection()
        text_hash = ''
        if text:
            text_hash = hashlib.sha1(text.encode('utf-8')).hexdigest()
            for x in conn.get('SELECT id FROM fts_db.books_text WHERE book=? AND format=? AND text_hash=?', (book_id, fmt, text_hash)):
                text = ''
                break
        self.add_text(book_id, fmt, text, text_hash, fmt_size, fmt_hash, err_msg)

    def queue_job(self, book_id, fmt, path, fmt_size, fmt_hash):
        conn = self.get_connection()
        fmt = fmt.upper()
        for x in conn.get('SELECT id FROM fts_db.books_text WHERE book=? AND format=? AND format_size=? AND format_hash=?', (
                book_id, fmt, fmt_size, fmt_hash)):
            break
        else:
            self.pool.add_job(book_id, fmt, path, fmt_size, fmt_hash)
            conn.execute('UPDATE fts_db.dirtied_formats SET in_progress=TRUE WHERE book=? AND format=?', (book_id, fmt))
            return True
        self.remove_dirty(book_id, fmt)
        with suppress(OSError):
            os.remove(path)
        return False

    def search(self,
        fts_engine_query, use_stemming, highlight_start, highlight_end, snippet_size, restrict_to_book_ids,
    ):
        fts_engine_query = unicode_normalize(fts_engine_query)
        fts_table = 'books_fts' + ('_stemmed' if use_stemming else '')
        text = 'books_text.searchable_text'
        if highlight_start is not None and highlight_end is not None:
            if snippet_size is not None:
                text = f'snippet({fts_table}, 0, "{highlight_start}", "{highlight_end}", "…", {max(1, min(snippet_size, 64))})'
            else:
                text = f'highlight("{fts_table}", 0, "{highlight_start}", "{highlight_end}")'
        query = 'SELECT {0}.id, {0}.book, {0}.format, {1} FROM {0} '
        query = query.format('books_text', text)
        query += f' JOIN {fts_table} ON fts_db.books_text.id = {fts_table}.rowid'
        query += f' WHERE "{fts_table}" MATCH ?'
        data = [fts_engine_query]
        query += f' ORDER BY {fts_table}.rank '
        conn = self.get_connection()
        try:
            for (rowid, book_id, fmt, text) in conn.execute(query, tuple(data)):
                if restrict_to_book_ids is not None and book_id not in restrict_to_book_ids:
                    continue
                yield {
                    'id': rowid,
                    'book_id': book_id,
                    'format': fmt,
                    'text': text,
                }
        except apsw.SQLError as e:
            raise FTSQueryError(fts_engine_query, query, e) from e

    def shutdown(self):
        self.pool.shutdown()