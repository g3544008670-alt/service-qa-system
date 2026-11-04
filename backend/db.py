"""
数据库模块：连接管理、Schema 初始化、CRUD 操作
"""
import os
import json
import hashlib
import secrets
import threading
from contextlib import contextmanager

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor

from utils import _now_iso, _int_to_bool, _bool_to_int, hash_password, verify_password, LEGACY_SHA256_RE, normalize_answer_content

# ── 全局配置（由 app 初始化时设置） ──
DATABASE_URL = 'postgresql://postgres@localhost:5432/sandbox_game'
DEFAULT_TENANT_ID = 1
DEFAULT_TENANT_NAME = '演示公司'
DEFAULT_TENANT_SLUG = 'demo-company'
SUPERADMIN_USERNAME = 'superadmin'
SUPERADMIN_PASSWORD = os.environ.get('SUPERADMIN_PASSWORD', '')
SUPERADMIN_ROLE = 'superadmin'


def _seed_password(env_key):
    """初始口令只从环境变量读；没设就随机生成并打印一次。

    仓库里不保留任何明文默认口令——默认口令一旦公开，等于把部署实例的入口公开。
    """
    value = os.environ.get(env_key)
    if value:
        return value
    generated = secrets.token_urlsafe(9)
    print(f'[init] {env_key} 未设置，本次使用随机口令：{generated}', flush=True)
    return generated


# ── 连接池 ──
_pool: pool.ThreadedConnectionPool | None = None
POOL_MIN = 2
POOL_MAX = 30


def init_db(config):
    """初始化数据库配置（app 启动时调用）"""
    global DATABASE_URL, DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME, DEFAULT_TENANT_SLUG
    global SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD, SUPERADMIN_ROLE, _pool
    DATABASE_URL = config.get('DATABASE_URL', DATABASE_URL)
    DEFAULT_TENANT_ID = config.get('DEFAULT_TENANT_ID', DEFAULT_TENANT_ID)
    DEFAULT_TENANT_NAME = config.get('DEFAULT_TENANT_NAME', DEFAULT_TENANT_NAME)
    DEFAULT_TENANT_SLUG = config.get('DEFAULT_TENANT_SLUG', DEFAULT_TENANT_SLUG)
    SUPERADMIN_USERNAME = config.get('SUPERADMIN_USERNAME', SUPERADMIN_USERNAME)
    SUPERADMIN_PASSWORD = config.get('SUPERADMIN_PASSWORD', SUPERADMIN_PASSWORD)
    SUPERADMIN_ROLE = config.get('SUPERADMIN_ROLE', SUPERADMIN_ROLE)
    # 初始化连接池：最小2，最大30，100并发够用
    if _pool is None:
        _pool = pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, DATABASE_URL)


# ── 连接管理 ──
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False


class _DBCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return getattr(self._cursor, 'lastrowid', None)


class _DBConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        cursor = self._conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(sql, tuple(params or ()))
        return _DBCursorWrapper(cursor)

    def commit(self):
        self._conn.commit()

    def close(self):
        if _pool is not None:
            _pool.putconn(self._conn)
        else:
            self._conn.close()

    def rollback(self):
        self._conn.rollback()


def _raw_connect():
    if _pool is not None:
        return _pool.getconn()
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def _get_conn():
    return _DBConnectionWrapper(_raw_connect())


@contextmanager
def _db_conn():
    """安全连接上下文管理器，自动返回连接池。"""
    conn = _get_conn()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        conn.close()
        raise
    else:
        conn.commit()
        conn.close()


# ── Schema 初始化 ──
def _table_columns(conn, table_name):
    rows = conn.execute(
        'SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = %s',
        (table_name,),
    ).fetchall()
    return {row['column_name'] for row in rows}


def _ensure_schema(force=False):
    global _SCHEMA_READY
    if _SCHEMA_READY and not force:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY and not force:
            return

    conn = _get_conn()
    conn.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS tenants (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL UNIQUE, slug TEXT NOT NULL UNIQUE,
            avatar_image TEXT NOT NULL DEFAULT '', top_image TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, password TEXT NOT NULL, role TEXT NOT NULL,
            tenant_id INTEGER, public_only INTEGER NOT NULL DEFAULT 0,
            can_edit_qa INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'active',
            session_token TEXT NOT NULL DEFAULT '',
            login_attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS qa_pairs (
            id INTEGER PRIMARY KEY, display_no INTEGER NOT NULL DEFAULT 0,
            question TEXT NOT NULL, answer TEXT NOT NULL, answer_html TEXT NOT NULL DEFAULT '',
            public INTEGER NOT NULL DEFAULT 1, category TEXT NOT NULL DEFAULT '未分类',
            approved INTEGER NOT NULL DEFAULT 1, author TEXT NOT NULL DEFAULT '',
            tenant_id INTEGER NOT NULL DEFAULT 1, image TEXT NOT NULL DEFAULT '',
            images_json TEXT NOT NULL DEFAULT '[]', attachments_json TEXT NOT NULL DEFAULT '[]',
            vector TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute('ALTER TABLE qa_pairs ADD COLUMN IF NOT EXISTS display_no INTEGER NOT NULL DEFAULT 0')
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY, question_id INTEGER, tenant_id INTEGER NOT NULL DEFAULT 1,
            path TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'file',
            mimetype TEXT NOT NULL DEFAULT '', size INTEGER NOT NULL DEFAULT 0,
            position INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS operation_logs (
            id SERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL DEFAULT 1,
            username TEXT NOT NULL DEFAULT '', user_role TEXT NOT NULL DEFAULT '',
            action TEXT NOT NULL DEFAULT '', qa_id INTEGER, question TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS customer_replies (
            id SERIAL PRIMARY KEY, tenant_id INTEGER NOT NULL DEFAULT 1, question_id INTEGER,
            question_text TEXT NOT NULL DEFAULT '', customer_username TEXT NOT NULL DEFAULT '',
            responder_name TEXT NOT NULL DEFAULT '', answer_text TEXT NOT NULL DEFAULT '',
            answer_html TEXT NOT NULL DEFAULT '', public_reply INTEGER NOT NULL DEFAULT 0,
            is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT '')'''
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_qa_pairs_tenant_id ON qa_pairs(tenant_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_attachments_tenant_qid ON attachments(tenant_id, question_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_operation_logs_tenant_id ON operation_logs(tenant_id, id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_customer_replies_tenant_customer ON customer_replies(tenant_id, customer_username, is_read)')
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS login_attempts INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locked_until TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT NOT NULL DEFAULT ''")
    # 确保 tenants.id 自增
    conn.execute("CREATE SEQUENCE IF NOT EXISTS tenants_id_seq")
    conn.execute("ALTER TABLE tenants ALTER COLUMN id SET DEFAULT nextval('tenants_id_seq')")
    conn.execute("SELECT setval('tenants_id_seq', COALESCE((SELECT MAX(id) FROM tenants), 1))")

    conn.execute(
        'UPDATE customer_replies '
        'SET question_text = COALESCE(('
        '  SELECT question FROM operation_logs ol'
        '  WHERE ol.tenant_id = customer_replies.tenant_id'
        '    AND ol.qa_id = customer_replies.question_id'
        '    AND ol.question != %s'
        '  ORDER BY ol.id DESC LIMIT 1'
        '), question_text)'
        'WHERE question_text = %s AND question_id IS NOT NULL',
        ('', '')
    )
    conn.execute(
        'INSERT INTO tenants(id, name, slug, status, created_at, updated_at) '
        'VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
        (DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME, DEFAULT_TENANT_SLUG, 'active', _now_iso(), _now_iso())
    )
    conn.execute(
        'UPDATE tenants SET name = %s, slug = %s WHERE id = %s AND (name = %s OR slug = %s OR name = %s)',
        (DEFAULT_TENANT_NAME, DEFAULT_TENANT_SLUG, DEFAULT_TENANT_ID, '演示公司', 'default', '')
    )
    conn.execute('UPDATE users SET tenant_id = %s WHERE tenant_id IS NULL AND role != %s', (DEFAULT_TENANT_ID, SUPERADMIN_ROLE))
    conn.execute('UPDATE qa_pairs SET tenant_id = %s WHERE tenant_id IS NULL OR tenant_id = 0', (DEFAULT_TENANT_ID,))
    conn.execute('UPDATE attachments SET tenant_id = %s WHERE tenant_id IS NULL OR tenant_id = 0', (DEFAULT_TENANT_ID,))
    conn.execute('UPDATE users SET tenant_id = NULL WHERE role = %s', (SUPERADMIN_ROLE,))
    conn.execute("UPDATE users SET can_edit_qa = 1 WHERE role = 'admin'")

    needs_display_no_backfill = False
    try:
        needs_display_no_backfill = conn.execute('SELECT 1 FROM qa_pairs WHERE display_no IS NULL OR display_no <= 0 LIMIT 1').fetchone() is not None
    except Exception:
        needs_display_no_backfill = True
    if needs_display_no_backfill:
        _renumber_qa_display_numbers(conn)

    _repair_existing_qa_attachments(conn)

    conn.commit()
    conn.close()
    _SCHEMA_READY = True


def _ensure_default_tenant():
    conn = _get_conn()
    conn.execute(
        'INSERT INTO tenants(id, name, slug, avatar_image, top_image, status, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT DO NOTHING',
        (DEFAULT_TENANT_ID, '演示公司', 'default', '', '', 'active', _now_iso(), _now_iso()),
    )
    conn.commit()
    conn.close()


# ── Meta ──
def _get_meta(key):
    conn = _get_conn()
    row = conn.execute('SELECT value FROM meta WHERE key = %s', (key,)).fetchone()
    conn.close()
    return row['value'] if row else None


def _set_meta(key, value):
    conn = _get_conn()
    conn.execute('INSERT INTO meta(key, value) VALUES(%s, %s) ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))
    conn.commit()
    conn.close()


# ── 用户 ──
def _ensure_superadmin_seeded():
    superadmin_password = SUPERADMIN_PASSWORD or _seed_password('SUPERADMIN_PASSWORD')
    conn = _get_conn()
    conn.execute('DELETE FROM users WHERE role = %s AND username != %s', (SUPERADMIN_ROLE, SUPERADMIN_USERNAME))
    row = conn.execute('SELECT username FROM users WHERE username = %s', (SUPERADMIN_USERNAME,)).fetchone()
    if row:
        conn.execute(
            'UPDATE users SET password = %s, role = %s, tenant_id = NULL, public_only = 0, can_edit_qa = 0, status = %s WHERE username = %s',
            (hash_password(superadmin_password), SUPERADMIN_ROLE, 'active', SUPERADMIN_USERNAME),
        )
    else:
        conn.execute(
            'INSERT INTO users(username, password, role, tenant_id, public_only, can_edit_qa, status) VALUES(%s, %s, %s, NULL, 0, 0, %s)',
            (SUPERADMIN_USERNAME, hash_password(superadmin_password), SUPERADMIN_ROLE, 'active'),
        )
    conn.commit()
    conn.close()


def _ensure_trial_accounts_seeded():
    default_admin_password = _seed_password('DEFAULT_ADMIN_PASSWORD')
    default_employee_password = _seed_password('DEFAULT_EMPLOYEE_PASSWORD')
    default_customer_password = _seed_password('DEFAULT_CUSTOMER_PASSWORD')
    conn = _get_conn()
    for username, raw_password, role in (
        ('admin', default_admin_password, 'admin'),
        ('employee', default_employee_password, 'employee'),
        ('customer', default_customer_password, 'customer'),
    ):
        row = conn.execute('SELECT username FROM users WHERE username = %s', (username,)).fetchone()
        if row:
            existing = conn.execute('SELECT can_edit_qa, status FROM users WHERE username = %s', (username,)).fetchone()
            can_edit_qa = int(existing['can_edit_qa'] or 0) if existing else (1 if role == 'admin' else 0)
            conn.execute(
                'UPDATE users SET password = %s, role = %s, tenant_id = %s, public_only = %s, can_edit_qa = %s, status = %s WHERE username = %s',
                (hash_password(raw_password), role, DEFAULT_TENANT_ID, 1 if role == 'customer' else 0,
                 can_edit_qa if role == 'employee' else (1 if role == 'admin' else can_edit_qa),
                 existing['status'] if existing and existing['status'] else 'active', username),
            )
        else:
            conn.execute(
                'INSERT INTO users(username, password, role, tenant_id, public_only, can_edit_qa, status) VALUES(%s, %s, %s, %s, %s, %s, %s)',
                (username, hash_password(raw_password), role, DEFAULT_TENANT_ID, 1 if role == 'customer' else 0, 1 if role == 'admin' else 0, 'active'),
            )
    conn.commit()
    conn.close()


def _init_app_data():
    _ensure_schema()
    _ensure_default_tenant()
    _ensure_superadmin_seeded()
    _ensure_trial_accounts_seeded()
    # 设置默认邮箱（仅首次注册时）
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET email = %s WHERE username = 'admin' AND email = ''",
        (os.environ.get('DEFAULT_ADMIN_EMAIL', ''),)
    )
    conn.commit()
    conn.close()


def get_user_record_by_username(username):
    if not username:
        return {}
    conn = _get_conn()
    row = conn.execute(
        'SELECT username, password, role, tenant_id, public_only, can_edit_qa, status, session_token, login_attempts, locked_until, email FROM users WHERE username = %s',
        (str(username),),
    ).fetchone()
    conn.close()
    if not row:
        return {}
    return {
        'username': row['username'],
        'password': row['password'],
        'role': row['role'],
        'tenant_id': row['tenant_id'],
        'public_only': _int_to_bool(row['public_only']),
        'can_edit_qa': _int_to_bool(row['can_edit_qa']),
        'status': row['status'],
        'session_token': row['session_token'] or '',
        'login_attempts': int(row['login_attempts'] or 0) if row['login_attempts'] is not None else 0,
        'locked_until': str(row['locked_until'] or '') if row['locked_until'] is not None else '',
        'email': str(row['email'] or '') if row['email'] is not None else '',
    }


def set_user_session_token(username, token):
    if not username:
        return
    conn = _get_conn()
    conn.execute('UPDATE users SET session_token = %s WHERE username = %s', (str(token or ''), str(username)))
    conn.commit()
    conn.close()


def clear_user_session_token(username, token=None):
    if not username:
        return
    conn = _get_conn()
    if token is None:
        conn.execute('UPDATE users SET session_token = %s WHERE username = %s', ('', str(username)))
    else:
        conn.execute('UPDATE users SET session_token = %s WHERE username = %s AND session_token = %s', ('', str(username), str(token or '')))
    conn.commit()
    conn.close()


def check_auth(username, password):
    from datetime import datetime, timedelta
    user = get_user_record_by_username(username)
    if not user:
        return None

    conn = _get_conn()

    # 检查是否锁定
    locked_until = str(user.get('locked_until', '') or '')
    if locked_until:
        try:
            lock_time = datetime.fromisoformat(locked_until)
            if lock_time > datetime.utcnow():
                conn.close()
                return {'locked': True, 'locked_until': lock_time.isoformat()}
        except Exception:
            pass

    if not verify_password(user['password'], password):
        # 增加失败计数
        attempts = int(user.get('login_attempts', 0) or 0) + 1
        if attempts >= 5:
            lock_time = (datetime.utcnow() + timedelta(minutes=15)).isoformat(timespec='seconds')
            conn.execute('UPDATE users SET login_attempts = %s, locked_until = %s WHERE username = %s',
                         (attempts, lock_time, str(username)))
            conn.commit()
            conn.close()
            return {'locked': True, 'locked_until': lock_time}
        conn.execute('UPDATE users SET login_attempts = %s WHERE username = %s', (attempts, str(username)))
        conn.commit()
        conn.close()
        return {'wrong_password': True, 'attempts': attempts, 'max_attempts': 5}

    # 登录成功：重置计数
    conn.execute('UPDATE users SET login_attempts = 0, locked_until = %s WHERE username = %s', ('', str(username)))
    conn.commit()
    conn.close()

    if LEGACY_SHA256_RE.fullmatch(str(user.get('password', ''))):
        new_password = hash_password(password)
        conn2 = _get_conn()
        conn2.execute('UPDATE users SET password = %s WHERE username = %s', (new_password, str(username)))
        conn2.commit()
        conn2.close()
    return user


# ── 租户 ──
def load_tenants():
    conn = _get_conn()
    rows = conn.execute('SELECT * FROM tenants ORDER BY id').fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_tenant_by_id(tenant_id):
    if tenant_id is None:
        return None
    conn = _get_conn()
    row = conn.execute('SELECT * FROM tenants WHERE id = %s', (int(tenant_id),)).fetchone()
    conn.close()
    return dict(row) if row else None


# ── 问答数据 ──
def load_qa_paginated(tenant_id, page=1, per_page=20, keyword='', category='', status=''):
    """分页加载问答，不加载附件（太多时分页查附件）。"""
    conn = _get_conn()
    tenant_id = int(tenant_id)
    conditions = ['tenant_id = %s']
    params = [tenant_id]

    if keyword:
        conditions.append('(question ILIKE %s OR answer ILIKE %s)')
        params.extend([f'%{keyword}%', f'%{keyword}%'])
    if category:
        conditions.append('category = %s')
        params.append(category)
    if status == 'pending':
        conditions.append('approved = 0')
    elif status == 'approved':
        conditions.append('approved = 1')

    where = ' AND '.join(conditions)
    # 总数
    count_row = conn.execute(f'SELECT COUNT(*) AS cnt FROM qa_pairs WHERE {where}', tuple(params)).fetchone()
    total = count_row['cnt'] if count_row else 0

    # 页数据（未审核优先）
    offset = (page - 1) * per_page
    rows = conn.execute(
        f'SELECT * FROM qa_pairs WHERE {where} ORDER BY approved ASC, id DESC LIMIT %s OFFSET %s',
        tuple(params) + (per_page, offset),
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    return {'items': items, 'total': total}


def load_data(tenant_id=None):
    conn = _get_conn()
    if tenant_id:
        tenant_id = int(tenant_id)
        question_rows = conn.execute('SELECT * FROM qa_pairs WHERE tenant_id = %s ORDER BY id', (tenant_id,)).fetchall()
        attachment_rows = conn.execute('SELECT * FROM attachments WHERE tenant_id = %s ORDER BY question_id, position, id', (tenant_id,)).fetchall()
    else:
        question_rows = conn.execute('SELECT * FROM qa_pairs ORDER BY id').fetchall()
        attachment_rows = conn.execute('SELECT * FROM attachments ORDER BY question_id, position, id').fetchall()
    conn.close()

    attachments_by_qid = {}
    for a in attachment_rows:
        qid = int(a['question_id']) if a['question_id'] else 0
        attachments_by_qid.setdefault(qid, []).append({
            'path': a['path'],
            'filename': a['filename'],
            'kind': a['kind'],
            'mimetype': a['mimetype'],
            'size': int(a['size'] or 0) if a['size'] else 0,
            'position': int(a['position'] or 0),
        })

    qa_pairs = []
    for q in question_rows:
        qd = dict(q)
        qid = int(q['id'])
        qd['attachments'] = attachments_by_qid.get(qid, [])
        qa_pairs.append(qd)

    return {'qa_pairs': qa_pairs}


def save_data(data, tenant_id=None):
    """批量保存问答数据（使用 PostgreSQL UPSERT，不预加载全部 ID）"""
    conn = _get_conn()
    if tenant_id is not None:
        tenant_id = int(tenant_id)

    qa_pairs = data.get('qa_pairs', [])
    if not qa_pairs:
        conn.close()
        return

    now = _now_iso()
    # 收集本次操作涉及的 ID，用于后续清理
    touched_ids = set()
    display_no_counter = 1

    for item in qa_pairs:
        qid = item.get('id')
        if qid is None or qid <= 0:
            qid = _get_next_qa_id()
        qid = int(qid)
        touched_ids.add(qid)

        question = str(item.get('question', '')).strip()
        answer = str(item.get('answer', '')).strip()
        answer_html = item.get('answer_html', '')
        answer_html, plain_text = normalize_answer_content(answer_html, answer)
        category = str(item.get('category', '未分类') or '未分类')
        tenant = int(item.get('tenant_id', tenant_id or DEFAULT_TENANT_ID))
        display_no = int(item.get('display_no', 0) or display_no_counter)
        if not display_no:
            display_no = display_no_counter
        display_no_counter += 1
        public = _bool_to_int(item.get('public', True))
        approved = _bool_to_int(item.get('approved', True))
        author = str(item.get('author', '') or '')
        image = str(item.get('image', '') or '')
        images_json = json.dumps(item.get('images', item.get('images_json', '[]')) if isinstance(item.get('images'), list) else item.get('images_json', '[]'), ensure_ascii=False)
        attachments_json = json.dumps(item.get('attachments', item.get('attachments_json', '[]')) if isinstance(item.get('attachments'), list) else item.get('attachments_json', '[]'), ensure_ascii=False)
        vector = json.dumps(item.get('vector', '[]'), ensure_ascii=False) if item.get('vector') else '[]'

        # PostgreSQL UPSERT：插入或更新，一次往返
        conn.execute(
            '''INSERT INTO qa_pairs(id, display_no, question, answer, answer_html, public, category, approved, author, tenant_id, image, images_json, attachments_json, vector, created_at, updated_at)
            VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                question = EXCLUDED.question, answer = EXCLUDED.answer,
                answer_html = EXCLUDED.answer_html, public = EXCLUDED.public,
                category = EXCLUDED.category, approved = EXCLUDED.approved,
                author = EXCLUDED.author, tenant_id = EXCLUDED.tenant_id,
                image = EXCLUDED.image, images_json = EXCLUDED.images_json,
                attachments_json = EXCLUDED.attachments_json, vector = EXCLUDED.vector,
                updated_at = EXCLUDED.updated_at''',
            (qid, display_no, question, answer, answer_html, public, category, approved, author, tenant, image, images_json, attachments_json, vector, now, now),
        )

        # 同步附件：替换式（删旧插新）
        conn.execute('DELETE FROM attachments WHERE tenant_id = %s AND question_id = %s', (tenant, qid))
        attachment_list = item.get('attachments') or item.get('attachments_json') or []
        if isinstance(attachment_list, str):
            try:
                attachment_list = json.loads(attachment_list)
            except Exception:
                attachment_list = []
        if isinstance(attachment_list, list):
            for pos, att in enumerate(attachment_list):
                if isinstance(att, dict):
                    att_path = str(att.get('path', att.get('src', '')) or '')
                    conn.execute(
                        'INSERT INTO attachments(question_id, tenant_id, path, filename, kind, mimetype, size, position, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
                        (qid, tenant, att_path, str(att.get('filename', att.get('name', '')) or ''), str(att.get('kind', 'file') or 'file'), str(att.get('mimetype', '') or ''), int(att.get('size', 0) or 0) if att.get('size') else 0, int(att.get('position', pos) or pos), now, now),
                    )

    conn.commit()
    conn.close()


def _get_next_qa_id():
    conn = _get_conn()
    row = conn.execute('SELECT MAX(id) AS max_id FROM qa_pairs').fetchone()
    conn.close()
    return int(row['max_id']) + 1 if row and row['max_id'] else 1


def ensure_qa_schema(data):
    """兼容旧 JSON 格式"""
    changed = False
    for item in data.get('qa_pairs', []):
        if 'id' not in item:
            item['id'] = _get_next_qa_id()
            changed = True
        if 'display_no' not in item or not item['display_no']:
            item['display_no'] = 0
            changed = True
        if 'tenant_id' not in item:
            item['tenant_id'] = DEFAULT_TENANT_ID
            changed = True
    return changed


def load_data_with_schema():
    data = load_data()
    if ensure_qa_schema(data):
        save_data(data)
    return data


def find_qa_by_id(data, qa_id):
    for qa in data.get('qa_pairs', []):
        if int(qa.get('id', 0)) == int(qa_id):
            return qa
    return None


# ── 问答 CRUD ──
def _record_attachment(saved, question_id=None, tenant_id=None, position=0):
    if not saved or not saved.get('path'):
        return
    _ensure_schema()
    conn = _get_conn()
    conn.execute('DELETE FROM attachments WHERE question_id IS NULL AND path = %s', (saved['path'],))
    conn.execute(
        'INSERT INTO attachments(question_id, tenant_id, path, filename, kind, mimetype, size, position, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
        (question_id, tenant_id if tenant_id is not None else DEFAULT_TENANT_ID, saved['path'], saved.get('filename', ''), saved.get('kind', 'file'), saved.get('mimetype', ''), int(saved.get('size', 0) or 0), position, _now_iso(), _now_iso()),
    )
    conn.commit()
    conn.close()


def _reassign_question_id(old_qid, new_qid, tenant_id=None):
    conn = _get_conn()
    conn.execute('UPDATE attachments SET question_id = %s WHERE tenant_id = %s AND question_id = %s', (int(new_qid), tenant_id or DEFAULT_TENANT_ID, int(old_qid)))
    conn.execute('UPDATE operation_logs SET qa_id = %s WHERE tenant_id = %s AND qa_id = %s', (int(new_qid), tenant_id or DEFAULT_TENANT_ID, int(old_qid)))
    conn.execute('UPDATE customer_replies SET question_id = %s WHERE tenant_id = %s AND question_id = %s', (int(new_qid), tenant_id or DEFAULT_TENANT_ID, int(old_qid)))
    conn.commit()
    conn.close()


def _renumber_qa_display_numbers(conn, tenant_id=None):
    if tenant_id:
        rows = conn.execute('SELECT tenant_id, id FROM qa_pairs WHERE tenant_id = %s ORDER BY id', (int(tenant_id),)).fetchall()
    else:
        rows = conn.execute('SELECT tenant_id, id FROM qa_pairs ORDER BY tenant_id, id').fetchall()
    current_tenant = None
    display_no = 1
    for row in rows:
        row_tenant_id = int(row['tenant_id'])
        if row_tenant_id != current_tenant:
            current_tenant = row_tenant_id
            display_no = 1
        conn.execute('UPDATE qa_pairs SET display_no = %s WHERE tenant_id = %s AND id = %s', (display_no, row_tenant_id, int(row['id'])))
        display_no += 1
    conn.commit()


def _repair_existing_qa_attachments(conn):
    """清除 qa_pairs 中引用的但已不存在的附件记录"""
    conn.execute(
        "UPDATE qa_pairs SET image = '' WHERE image != '' AND image NOT IN (SELECT path FROM attachments WHERE path = qa_pairs.image)"
    )
    # answer_html 是保存时的源数据；不要根据旧的附件记录重建 answer_html
    pass


# ── 操作日志 ──
def log_operation(action, qa_id=None, question='', detail=''):
    from flask import session, has_request_context
    if not has_request_context():
        return
    username = session.get('username', '')
    user_role = str(session.get('role', '') or '')
    tenant_id = session.get('tenant_id', DEFAULT_TENANT_ID)
    if tenant_id is None:
        tenant_id = DEFAULT_TENANT_ID

    action_label_map = {
        'add_qa': '新增问答', 'edit_qa': '编辑问答', 'delete_qa': '删除问答',
        'approve_qa': '审核通过', 'reject_qa': '审核拒绝',
        'submit_question': '客户提问', 'reply_public': '公开回复', 'reply_private': '私下回复',
    }
    action_label = action_label_map.get(str(action or ''), str(action or ''))

    if user_role == 'customer' and action_label != '客户提问':
        return
    if user_role == 'employee' and action_label not in ('新增问答', '编辑问答', '删除问答', '审核通过', '审核拒绝', '公开回复', '私下回复'):
        return

    conn = _get_conn()
    conn.execute(
        'INSERT INTO operation_logs(tenant_id, username, user_role, action, qa_id, question, detail, created_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)',
        (int(tenant_id), username, user_role, action_label, qa_id, str(question or ''), str(detail or ''), _now_iso()),
    )
    conn.commit()
    conn.close()


def count_pending_customer_questions(tenant_id):
    conn = _get_conn()
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM qa_pairs WHERE tenant_id = %s AND approved = 0',
        (int(tenant_id),),
    ).fetchone()
    conn.close()
    return int(row['c']) if row else 0


def count_new_logs_for_admin(tenant_id):
    from flask import session
    if session.get('role') != 'admin':
        return 0
    last_seen = int(session.get('last_seen_log_id', 0) or 0)
    conn = _get_conn()
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM operation_logs WHERE tenant_id = %s AND id > %s AND (user_role = %s OR action = %s)',
        (int(tenant_id), last_seen, 'employee', '客户提问'),
    ).fetchone()
    conn.close()
    return int(row['c']) if row else 0


def count_unread_customer_replies(tenant_id, username):
    conn = _get_conn()
    row = conn.execute(
        'SELECT COUNT(*) AS c FROM customer_replies WHERE tenant_id = %s AND customer_username = %s AND is_read = 0',
        (int(tenant_id), str(username or '')),
    ).fetchone()
    conn.close()
    return int(row['c']) if row else 0


def _query_tenant_counts():
    conn = _get_conn()
    rows = conn.execute(
        '''SELECT t.id, t.name, t.status, t.created_at, u.admin_username, u.user_count, q.qa_count
        FROM tenants t
        LEFT JOIN (SELECT tenant_id, COUNT(*) AS qa_count FROM qa_pairs GROUP BY tenant_id) q ON q.tenant_id = t.id
        LEFT JOIN (SELECT tenant_id, COUNT(*) AS user_count, MAX(CASE WHEN role = 'admin' THEN username END) AS admin_username FROM users WHERE role != %s GROUP BY tenant_id) u ON u.tenant_id = t.id
        ORDER BY t.id''',
        (SUPERADMIN_ROLE,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
