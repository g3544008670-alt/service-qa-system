import argparse
import hashlib
import os
import sqlite3
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_SQLITE_PATH = os.path.join(BASE_DIR, 'app.db')


def now_iso():
    return datetime.utcnow().isoformat(timespec='seconds')


def normalize_database_url(url):
    value = (url or '').strip()
    if value.startswith('postgres://'):
        value = 'postgresql://' + value[len('postgres://'):]
    return value


def ensure_pg_schema(pg_conn):
    cur = pg_conn.cursor()
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            avatar_image TEXT NOT NULL DEFAULT '',
            top_image TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY,
            password TEXT NOT NULL,
            role TEXT NOT NULL,
            tenant_id INTEGER,
            public_only INTEGER NOT NULL DEFAULT 0,
            can_edit_qa INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            session_token TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS qa_pairs (
            id INTEGER PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            answer_html TEXT NOT NULL DEFAULT '',
            public INTEGER NOT NULL DEFAULT 1,
            category TEXT NOT NULL DEFAULT '未分类',
            approved INTEGER NOT NULL DEFAULT 1,
            author TEXT NOT NULL DEFAULT '',
            tenant_id INTEGER NOT NULL DEFAULT 1,
            image TEXT NOT NULL DEFAULT '',
            images_json TEXT NOT NULL DEFAULT '[]',
            attachments_json TEXT NOT NULL DEFAULT '[]',
            vector TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            question_id INTEGER,
            tenant_id INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL,
            filename TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL DEFAULT 'file',
            mimetype TEXT NOT NULL DEFAULT '',
            size INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS operation_logs (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL DEFAULT 1,
            username TEXT NOT NULL DEFAULT '',
            user_role TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '',
            qa_id INTEGER,
            question TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS customer_replies (
            id SERIAL PRIMARY KEY,
            tenant_id INTEGER NOT NULL DEFAULT 1,
            question_id INTEGER,
            question_text TEXT NOT NULL DEFAULT '',
            customer_username TEXT NOT NULL DEFAULT '',
            responder_name TEXT NOT NULL DEFAULT '',
            answer_text TEXT NOT NULL DEFAULT '',
            answer_html TEXT NOT NULL DEFAULT '',
            public_reply INTEGER NOT NULL DEFAULT 0,
            is_read INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT ''
        )
        '''
    )
    cur.execute('CREATE INDEX IF NOT EXISTS idx_qa_pairs_tenant_id ON qa_pairs(tenant_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_attachments_tenant_qid ON attachments(tenant_id, question_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_operation_logs_tenant_id ON operation_logs(tenant_id, id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_customer_replies_tenant_customer ON customer_replies(tenant_id, customer_username, is_read)')
    pg_conn.commit()


def compute_qa_id_signature(pg_conn):
    cur = pg_conn.cursor(cursor_factory=RealDictCursor)
    cur.execute('SELECT tenant_id, id FROM qa_pairs ORDER BY tenant_id, id')
    rows = cur.fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{int(row['tenant_id'])}:{int(row['id'])}|".encode('utf-8'))
    return digest.hexdigest()


def check_target_empty(pg_conn):
    cur = pg_conn.cursor()
    cur.execute('SELECT COUNT(*) FROM qa_pairs')
    qa_count = int(cur.fetchone()[0])
    cur.execute('SELECT COUNT(*) FROM users')
    user_count = int(cur.fetchone()[0])
    return qa_count == 0 and user_count == 0


def truncate_target(pg_conn):
    cur = pg_conn.cursor()
    cur.execute('TRUNCATE TABLE customer_replies RESTART IDENTITY CASCADE')
    cur.execute('TRUNCATE TABLE operation_logs RESTART IDENTITY CASCADE')
    cur.execute('TRUNCATE TABLE attachments RESTART IDENTITY CASCADE')
    cur.execute('TRUNCATE TABLE qa_pairs RESTART IDENTITY CASCADE')
    cur.execute('TRUNCATE TABLE users CASCADE')
    cur.execute('TRUNCATE TABLE tenants CASCADE')
    cur.execute('TRUNCATE TABLE meta CASCADE')
    pg_conn.commit()


def migrate(sqlite_path, database_url, force):
    if not os.path.exists(sqlite_path):
        raise FileNotFoundError(f'SQLite file not found: {sqlite_path}')

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(database_url, connect_timeout=10)
    try:
        ensure_pg_schema(pg_conn)

        if not check_target_empty(pg_conn) and not force:
            raise RuntimeError('PostgreSQL target is not empty. Use --force to overwrite target data.')
        if force:
            truncate_target(pg_conn)

        s_cur = sqlite_conn.cursor()
        p_cur = pg_conn.cursor()

        # tenants
        s_cur.execute('SELECT id, name, slug, avatar_image, top_image, status, created_at, updated_at FROM tenants ORDER BY id')
        for row in s_cur.fetchall():
            p_cur.execute(
                'INSERT INTO tenants(id, name, slug, avatar_image, top_image, status, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)',
                (row['id'], row['name'], row['slug'], row['avatar_image'] or '', row['top_image'] or '', row['status'] or 'active', row['created_at'] or '', row['updated_at'] or ''),
            )

        # users
        s_cur.execute('SELECT username, password, role, tenant_id, public_only, can_edit_qa, status, session_token FROM users ORDER BY username')
        for row in s_cur.fetchall():
            p_cur.execute(
                'INSERT INTO users(username, password, role, tenant_id, public_only, can_edit_qa, status, session_token) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)',
                (row['username'], row['password'], row['role'], row['tenant_id'], int(row['public_only'] or 0), int(row['can_edit_qa'] or 0), row['status'] or 'active', row['session_token'] or ''),
            )

        # qa id remap: positive ids are re-sequenced by created_at then id; negative temp ids are preserved
        s_cur.execute('SELECT * FROM qa_pairs ORDER BY tenant_id, id')
        qa_rows = s_cur.fetchall()
        positive_rows = [r for r in qa_rows if int(r['id']) > 0]
        positive_rows.sort(key=lambda r: (str(r['created_at'] or ''), int(r['id'])))

        id_map = {}
        next_id = 1
        for row in positive_rows:
            old_id = int(row['id'])
            id_map[old_id] = next_id
            next_id += 1

        for row in qa_rows:
            old_id = int(row['id'])
            new_id = id_map.get(old_id, old_id)
            p_cur.execute(
                '''
                INSERT INTO qa_pairs(id, question, answer, answer_html, public, category, approved, author, tenant_id, image, images_json, attachments_json, vector, created_at, updated_at)
                VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''',
                (
                    new_id,
                    row['question'] or '',
                    row['answer'] or '',
                    row['answer_html'] or '',
                    int(row['public'] or 0),
                    row['category'] or '未分类',
                    int(row['approved'] or 0),
                    row['author'] or '',
                    int(row['tenant_id'] or 1),
                    row['image'] or '',
                    row['images_json'] or '[]',
                    row['attachments_json'] or '[]',
                    row['vector'] or '[]',
                    row['created_at'] or '',
                    row['updated_at'] or '',
                ),
            )

        # attachments
        s_cur.execute('SELECT question_id, tenant_id, path, filename, kind, mimetype, size, position, created_at, updated_at FROM attachments ORDER BY tenant_id, question_id, position, id')
        for row in s_cur.fetchall():
            old_qid = row['question_id']
            new_qid = id_map.get(int(old_qid), int(old_qid)) if old_qid is not None else None
            p_cur.execute(
                'INSERT INTO attachments(question_id, tenant_id, path, filename, kind, mimetype, size, position, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (new_qid, int(row['tenant_id'] or 1), row['path'] or '', row['filename'] or '', row['kind'] or 'file', row['mimetype'] or '', int(row['size'] or 0), int(row['position'] or 0), row['created_at'] or '', row['updated_at'] or ''),
            )

        # operation logs
        s_cur.execute('SELECT tenant_id, username, user_role, action, qa_id, question, detail, created_at FROM operation_logs ORDER BY id')
        for row in s_cur.fetchall():
            old_qid = row['qa_id']
            new_qid = id_map.get(int(old_qid), int(old_qid)) if old_qid is not None else None
            p_cur.execute(
                'INSERT INTO operation_logs(tenant_id, username, user_role, action, qa_id, question, detail, created_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)',
                (int(row['tenant_id'] or 1), row['username'] or '', row['user_role'] or '', row['action'] or '', new_qid, row['question'] or '', row['detail'] or '', row['created_at'] or ''),
            )

        # customer replies
        s_cur.execute('SELECT tenant_id, question_id, question_text, customer_username, responder_name, answer_text, answer_html, public_reply, is_read, created_at FROM customer_replies ORDER BY id')
        for row in s_cur.fetchall():
            old_qid = row['question_id']
            new_qid = id_map.get(int(old_qid), int(old_qid)) if old_qid is not None else None
            p_cur.execute(
                'INSERT INTO customer_replies(tenant_id, question_id, question_text, customer_username, responder_name, answer_text, answer_html, public_reply, is_read, created_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                (int(row['tenant_id'] or 1), new_qid, row['question_text'] or '', row['customer_username'] or '', row['responder_name'] or '', row['answer_text'] or '', row['answer_html'] or '', int(row['public_reply'] or 0), int(row['is_read'] or 0), row['created_at'] or ''),
            )

        # meta
        s_cur.execute('SELECT key, value FROM meta')
        for row in s_cur.fetchall():
            key = str(row['key'] or '')
            value = str(row['value'] or '')
            if key in ('next_qa_id', 'qa_id_signature', 'temp_next_id'):
                continue
            p_cur.execute(
                'INSERT INTO meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                (key, value),
            )

        max_positive = next_id - 1
        p_cur.execute(
            'INSERT INTO meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            ('next_qa_id', str(max_positive + 1)),
        )

        min_temp = -1
        negative_ids = [int(r['id']) for r in qa_rows if int(r['id']) <= 0]
        if negative_ids:
            min_temp = min(negative_ids) - 1
        p_cur.execute(
            'INSERT INTO meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            ('temp_next_id', str(min_temp)),
        )

        pg_conn.commit()

        qa_signature = compute_qa_id_signature(pg_conn)
        p_cur.execute(
            'INSERT INTO meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value',
            ('qa_id_signature', qa_signature),
        )
        pg_conn.commit()

        print('Migration completed successfully.')
        print(f'SQLite: {sqlite_path}')
        print('PostgreSQL: target updated')
        print(f'Re-sequenced positive QA IDs: 1..{max_positive}')

    finally:
        sqlite_conn.close()
        pg_conn.close()


def main():
    parser = argparse.ArgumentParser(description='Migrate SQLite data to PostgreSQL and re-sequence QA IDs safely.')
    parser.add_argument('--sqlite-path', default=DEFAULT_SQLITE_PATH, help='Path to SQLite app.db')
    parser.add_argument('--database-url', default=normalize_database_url(os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL')), help='PostgreSQL DATABASE_URL')
    parser.add_argument('--force', action='store_true', help='Allow truncating non-empty PostgreSQL target before migration')
    args = parser.parse_args()

    database_url = normalize_database_url(args.database_url)
    if not database_url:
        raise RuntimeError('DATABASE_URL/POSTGRES_URL is required')

    migrate(args.sqlite_path, database_url, args.force)


if __name__ == '__main__':
    main()
