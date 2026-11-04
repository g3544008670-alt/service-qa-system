import os
import json
import csv
import hashlib
import io
import uuid
import re
import secrets
import threading
from datetime import datetime, timedelta
from contextlib import contextmanager
from zoneinfo import ZoneInfo
from html import escape, unescape
from html.parser import HTMLParser
import numpy as np
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, session, redirect, url_for, render_template, flash, has_request_context
from werkzeug.routing import BaseConverter
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
import bcrypt
import chromadb

# ── 导入拆分的模块 ──
import utils
from utils import (
    _now_iso, _bool_to_int, _int_to_bool, normalize_answer_content, hash_password,
    verify_password, password_meets_policy, _save_uploaded_attachment,
    _save_uploaded_brand_image, _remove_attachment_from_answer_html,
    _extract_attachments, format_beijing_time, LEGACY_SHA256_RE, IMAGE_EXTENSIONS,
    RichAnswerSanitizer, AttachmentRefParser,
)
import db
from db import (
    _get_conn, _db_conn, _ensure_schema, check_auth,
    get_user_record_by_username, set_user_session_token, clear_user_session_token,
    get_tenant_by_id, load_tenants, _init_app_data, _ensure_default_tenant,
    load_data, save_data, load_data_with_schema, find_qa_by_id, ensure_qa_schema,
    _record_attachment, _reassign_question_id, _get_next_qa_id,
    _ensure_superadmin_seeded, _ensure_trial_accounts_seeded,
    _get_meta, _set_meta,
    log_operation, count_pending_customer_questions, count_new_logs_for_admin,
    count_unread_customer_replies, _query_tenant_counts, _repair_existing_qa_attachments,
    _renumber_qa_display_numbers, DEFAULT_TENANT_ID, DEFAULT_TENANT_NAME,
    DEFAULT_TENANT_SLUG, SUPERADMIN_USERNAME, SUPERADMIN_PASSWORD, SUPERADMIN_ROLE,
)
import vector
from vector import (
    get_sentence_model, encode_text, _get_chroma_collection,
    _rebuild_all_chroma_index, _sync_chroma_tenant, _compute_qa_id_signature,
    bootstrap_chroma_index, get_answer, sync_single_qa, delete_single_qa, CHROMA_PATH,
)

try:
    from openpyxl import load_workbook  # type: ignore
except ImportError:
    load_workbook = None

# 强制离线模式：text2vec 使用本地缓存，不联网下载
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.environ.setdefault('HF_HOME', os.path.join(os.path.expanduser('~'), '.cache', 'huggingface'))
# os.environ.setdefault('HF_ENDPOINT', 'https://huggingface.co')
os.environ.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')

MODEL_NAME = "shibing624/text2vec-base-chinese"
MODEL_CACHE_DIR = os.path.join(os.path.expanduser('~'), '.cache', 'huggingface', 'hub')
MODEL_CACHE_NAME = 'models--shibing624--text2vec-base-chinese'
MODEL_CACHE_PATH = os.path.join(MODEL_CACHE_DIR, MODEL_CACHE_NAME)
if os.path.isdir(MODEL_CACHE_PATH):
    print(f"[INFO] 离线模型已缓存: {MODEL_NAME}")
else:
    print(f"[WARN] 离线模型未缓存: {MODEL_NAME}")
    print(f"[WARN] 请联网运行: python -c \"from text2vec import SentenceModel; SentenceModel('{MODEL_NAME}')\"")

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

DATABASE_URL = (os.getenv('DATABASE_URL') or os.getenv('POSTGRES_URL') or 'postgresql://postgres@localhost:5432/sandbox_game').strip()
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]

# ── 初始化各模块 ──
utils.init_utils(UPLOAD_FOLDER, DEFAULT_TENANT_ID)
db.init_db({
    'DATABASE_URL': DATABASE_URL,
    'DEFAULT_TENANT_ID': 1,
    'DEFAULT_TENANT_NAME': '演示公司',
    'DEFAULT_TENANT_SLUG': 'demo-company',
    'SUPERADMIN_USERNAME': os.getenv('SUPERADMIN_USERNAME', 'superadmin'),
    'SUPERADMIN_PASSWORD': os.getenv('SUPERADMIN_PASSWORD', ''),
    'SUPERADMIN_ROLE': 'superadmin',
})
vector.init_vector_search(BASE_DIR)

# 启动期强校验：嵌入模型是检索链路的硬依赖，加载失败时宁可起不来，
# 也不要在运行时静默降级成哈希伪向量——那种降级会产出「看起来正常、实际没有语义」的结果。
_sentence_model = vector.get_sentence_model()
if _sentence_model is None:
    raise RuntimeError(
        '嵌入模型加载失败：%s。离线环境请设置 HF_HUB_OFFLINE=1，并确认本地已缓存模型。'
        % vector._sentence_model_load_error)

app = Flask(__name__)


class SignedIntConverter(BaseConverter):
    # Support both positive and negative integer path segments (e.g. -1 temporary ids).
    regex = r'-?\d+'

    def to_python(self, value):
        return int(value)

    def to_url(self, value):
        return str(int(value))


app.url_map.converters['sint'] = SignedIntConverter

secret_key = os.getenv('SECRET_KEY')
if not secret_key:
    raise RuntimeError('SECRET_KEY environment variable is required')
app.secret_key = secret_key  # 用于session加密

# ---------- CSRF 保护 ----------
CSRF_EXEMPT_ENDPOINTS = {'static', 'upload_attachment', 'upload_image', 'upload_brand_image', 'tenant_branding'}


@app.context_processor
def _inject_csrf():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(32)
    return {'csrf_token_value': session['csrf_token']}


@app.before_request
def _csrf_check():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return None
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return None

    token = request.form.get('csrf_token', '') or request.headers.get('X-CSRFToken', '') or ''
    expected = session.get('csrf_token', '')
    if not expected or not secrets.compare_digest(token, expected):
        if request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': 'CSRF token missing or invalid'}), 400
        flash('表单已过期，请重试', 'error')
        # 重定向回来源页或首页
        referrer = request.headers.get('Referer', '/')
        return redirect(referrer)


# ---------- 数据文件路径 ----------
os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ---- 模板过滤器 ----
@app.template_filter('beijing_time')
def _jinja_beijing_time(value):
    return format_beijing_time(value)


APP_BRAND_HTML = '''
<div class="brand-mark">
    <span class="brand-fallback">SC</span>
</div>
'''


def can_edit_knowledge(user=None):
    if user is None:
        user = get_current_user_record()
    if not user:
        return False
    if user.get('role') in ('admin', SUPERADMIN_ROLE):
        return True
    return bool(user.get('can_edit_qa'))


def get_current_tenant_id(default=DEFAULT_TENANT_ID):
    if session.get('role') == SUPERADMIN_ROLE:
        return None
    tenant_id = session.get('tenant_id')
    if tenant_id is not None:
        try:
            return int(tenant_id)
        except (TypeError, ValueError):
            return default
    username = session.get('username')
    user = get_user_record_by_username(username)
    if user:
        user_tenant = user.get('tenant_id')
        if user_tenant is not None:
            try:
                return int(user_tenant)
            except (TypeError, ValueError):
                return default
    return default


def get_current_user_record():
    username = session.get('username')
    if not username:
        return {}
    return get_user_record_by_username(username)


def get_current_brand_tenant_id():
    tenant_id = session.get('impersonate_tenant')
    if tenant_id is not None:
        try:
            return int(tenant_id)
        except (TypeError, ValueError):
            pass
    tenant_id = get_current_tenant_id()
    if tenant_id is not None:
        return int(tenant_id)
    return DEFAULT_TENANT_ID


def get_tenant_brand_assets(tenant_id=None):
    tenant = get_tenant_by_id(tenant_id if tenant_id is not None else get_current_brand_tenant_id()) or {}
    return {
        'avatar_image': str(tenant.get('avatar_image', '') or ''),
        'top_image': str(tenant.get('top_image', '') or ''),
    }


def build_brand_html(tenant_id=None):
    assets = get_tenant_brand_assets(tenant_id)
    avatar = assets.get('avatar_image') or ''
    if avatar:
        return f'''
<div class="brand-mark">
    <img class="brand-logo" src="{escape(avatar, quote=True)}" alt="品牌头像">
</div>
'''
    return '''
<div class="brand-mark">
    <span class="brand-fallback">SC</span>
</div>
'''


@app.context_processor
def inject_brand_assets():
    tenant_id = get_current_brand_tenant_id()
    assets = get_tenant_brand_assets(tenant_id)
    return {
        'tenant_brand_html': build_brand_html(tenant_id),
        'tenant_avatar_image': assets.get('avatar_image', ''),
        'tenant_top_image': assets.get('top_image', ''),
    }


def _row_to_qa(row, attachments_by_question_id=None):
    attachments_by_question_id = attachments_by_question_id or {}
    question_id = int(row['id'])
    attachments = attachments_by_question_id.get(question_id, [])
    images = [item['path'] for item in attachments if item['kind'] == 'image']
    files = [item['path'] for item in attachments if item['kind'] != 'image']
    if row['image'] and row['image'] not in images:
        images.insert(0, row['image'])
    try:
        images_json = json.loads(row['images_json']) if row['images_json'] else images
    except Exception:
        images_json = images
    # normalize images list to unique order
    seen_img = set()
    normalized_images = []
    for img in (images_json if isinstance(images_json, list) else images):
        if not img:
            continue
        if img in seen_img:
            continue
        seen_img.add(img)
        normalized_images.append(img)
    images_json = normalized_images
    try:
        attachments_json = json.loads(row['attachments_json']) if row['attachments_json'] else attachments
    except Exception:
        attachments_json = attachments
    try:
        vector = json.loads(row['vector']) if row['vector'] else []
    except Exception:
        vector = []
    return {
        'id': question_id,
        'display_no': int(row['display_no']) if 'display_no' in row.keys() and row['display_no'] is not None else 0,
        'tenant_id': int(row['tenant_id']) if row['tenant_id'] is not None else DEFAULT_TENANT_ID,
        'question': row['question'],
        'answer': row['answer'],
        'answer_html': row['answer_html'] or normalize_answer_content('', row['answer'])[0],
        'public': _int_to_bool(row['public']),
        'category': row['category'] or '未分类',
        'approved': _int_to_bool(row['approved']),
        'author': row['author'] or '',
        'image': row['image'] or (images[0] if images else ''),
        'images': images_json if isinstance(images_json, list) else images,
        'files': files,
        'attachments': attachments_json if isinstance(attachments_json, list) else attachments,
        'attachments_json': attachments_json if isinstance(attachments_json, list) else attachments,
        'images_json': images_json if isinstance(images_json, list) else images,
        'vector': vector if isinstance(vector, list) else [],
        'created_at': row['created_at'] or '',
        'updated_at': row['updated_at'] or '',
    }


@app.before_request
def enforce_single_session():
    username = session.get('username')
    if not username:
        return None
    # allow unauthenticated endpoints
    if request.endpoint in {'login', 'register_page', 'register', 'static'}:
        return None
    user = get_user_record_by_username(username)
    if not user:
        session.clear()
        return redirect(url_for('login'))
    if user.get('status', 'active') != 'active' and user.get('role') != SUPERADMIN_ROLE:
        session.clear()
        return redirect(url_for('login'))
    db_token = str(user.get('session_token') or '')
    current_token = str(session.get('session_token') or '')
    # session token must be present in both session and DB and match
    if not db_token or not current_token or db_token != current_token:
        session.clear()
        return redirect(url_for('login'))


def get_next_qa_id(data):
    stored_next_id = _get_meta('next_qa_id')
    if stored_next_id is not None:
        try:
            next_id = int(stored_next_id)
            if next_id > 0:
                _set_meta('next_qa_id', next_id + 1)
                return next_id
        except (TypeError, ValueError):
            pass

    qa_pairs = data.get('qa_pairs', []) if isinstance(data, dict) else []
    max_id = 0
    for qa in qa_pairs:
        qa_id = qa.get('id') if isinstance(qa, dict) else None
        if isinstance(qa_id, int) and qa_id > max_id:
            max_id = qa_id
    next_id = max_id + 1
    _set_meta('next_qa_id', next_id + 1)
    return next_id


def get_temp_qa_id():
    """返回一个唯一的临时负 ID，用于客户提交的待审核问题，避免占用正式自增 ID。"""
    stored = _get_meta('temp_next_id')
    try:
        if stored is None:
            val = -1
        else:
            val = int(stored)
    except Exception:
        val = -1
    # 保证始终向更小的负数移动，避免与正数 ID 冲突
    next_temp = val - 1 if val <= 0 else -1
    _set_meta('temp_next_id', next_temp)
    return next_temp


def is_superadmin_session():
    return session.get('role') == SUPERADMIN_ROLE


def _slugify_tenant_name(name):
    slug = re.sub(r'[^a-z0-9]+', '-', (name or '').strip().lower())
    slug = slug.strip('-')
    return slug or f'tenant-{uuid.uuid4().hex[:8]}'


@app.route('/debug_model')
def debug_model():
    if 'username' not in session:
        return jsonify({'error': 'unauthorized'}), 401
    if session.get('role') not in ('admin', SUPERADMIN_ROLE):
        return jsonify({'error': 'forbidden'}), 403
    user = get_current_user_record()
    tenant_id = user.get('tenant_id') if user else get_current_tenant_id()
    collection = _get_chroma_collection()
    sample = None
    try:
        sample = collection.get(limit=3, include=['metadatas', 'documents'])
    except Exception as exc:
        sample = {'error': str(exc)}
    query_text = request.args.get('q', '').strip()
    if query_text:
        answer, score, error = get_answer(query_text, user or {'tenant_id': tenant_id, 'public_only': False})
    else:
        answer, score, error = None, 0, None
    return jsonify({
        'model_loaded': get_sentence_model() is not None,
        'model_error': str(vector._sentence_model_load_error) if vector._sentence_model_load_error else '',
        'tenant_id': tenant_id,
        'query': query_text,
        'qa_count': 0,
        'chroma_count': collection.count(),
        'sample': sample,
        'answer': answer,
        'score': score,
        'error': error,
    })


@app.route('/debug_model_page')
def debug_model_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    if session.get('role') not in ('admin', SUPERADMIN_ROLE):
        return redirect(url_for('chat_page'))
    user = get_current_user_record()
    tenant_id = user.get('tenant_id') if user else get_current_tenant_id()
    collection = _get_chroma_collection()
    sample = None
    try:
        sample = collection.get(limit=5, include=['metadatas', 'documents'])
    except Exception as exc:
        sample = {'error': str(exc)}
    query_text = request.args.get('q', '').strip()
    answer = score = error = None
    if query_text:
        answer, score, error = get_answer(query_text, user or {'tenant_id': tenant_id, 'public_only': False})
    return render_template(
        'debug_model.html',
        username=session.get('username', ''),
        tenant_id=tenant_id,
        model_loaded=get_sentence_model() is not None,
        model_error=str(vector._sentence_model_load_error) if vector._sentence_model_load_error else '',
        qa_count=len(load_data(tenant_id)['qa_pairs']) if tenant_id is not None else len(load_data(DEFAULT_TENANT_ID)['qa_pairs']),
        chroma_count=collection.count(),
        sample=sample,
        query_text=query_text,
        answer=answer,
        score=score,
        error=error,
        brand_html=APP_BRAND_HTML,
        tenant_name=session.get('tenant_name', '演示公司'),
        role=session.get('role', ''),
    )


# ---------- 登录页面 ----------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        result = check_auth(username, password)
        if result is None:
            return redirect(url_for('login', error='账号或密码错误'))
        if isinstance(result, dict):
            if result.get('locked'):
                return redirect(url_for('login', error='账户已锁定，请15分钟后再试'))
            if result.get('wrong_password'):
                remaining = 5 - int(result.get('attempts', 0))
                return redirect(url_for('login', error=f'密码错误，还剩{remaining}次机会'))
            # 登录成功
            user = result
            if user.get('status', 'active') != 'active':
                return redirect(url_for('login', error='账号等待管理员批准'))
            login_token = uuid.uuid4().hex
            set_user_session_token(username, login_token)
            session['username'] = username
            session['role'] = user['role']
            session['tenant_id'] = user.get('tenant_id')
            tenant = get_tenant_by_id(user.get('tenant_id'))
            session['tenant_name'] = tenant['name'] if tenant else '演示公司'
            session['public_only'] = user['public_only']
            session['session_token'] = login_token
            session['chat_thread_id'] = uuid.uuid4().hex
            if user['role'] == SUPERADMIN_ROLE:
                return redirect(url_for('superadmin_dashboard'))
            return redirect(url_for('chat_page'))
        return redirect(url_for('login', error='账号或密码错误'))
    error = request.args.get('error', '')
    return render_template('login.html', error=error, brand_html=APP_BRAND_HTML)


# ---------- 注册页面 ----------
@app.route('/register_page')
def register_page():
    tenants = [tenant for tenant in load_tenants() if tenant.get('status', 'active') == 'active']
    tenants.sort(key=lambda item: int(item.get('id') or 0))
    return render_template('register.html', brand_html=APP_BRAND_HTML, tenants=tenants, default_tenant_id=DEFAULT_TENANT_ID, default_tenant_name=DEFAULT_TENANT_NAME)


@app.route('/register', methods=['POST'])
def register():
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    role = str(data.get("role", "customer") or 'customer').strip().lower()
    if role not in ('customer', 'employee'):
        return jsonify({"status": "error", "message": "仅支持注册客户或员工账号"})
    if not username or not password:
        return jsonify({"status": "error", "message": "用户名和密码不能为空"})
    if not password_meets_policy(password):
        return jsonify({"status": "error", "message": "密码至少 8 位，且必须包含字母和数字"})
    existing_user = get_user_record_by_username(username)
    if existing_user:
        return jsonify({"status": "error", "message": "用户名已存在"})
    try:
        tenant_id = int(data.get('tenant_id') or DEFAULT_TENANT_ID)
    except (TypeError, ValueError):
        tenant_id = DEFAULT_TENANT_ID
    tenant = get_tenant_by_id(tenant_id)
    if not tenant or tenant.get('status', 'active') != 'active':
        return jsonify({"status": "error", "message": "请选择有效的租户"})
    conn = _get_conn()
    conn.execute(
        'INSERT INTO users(username, password, role, tenant_id, public_only, can_edit_qa, status) VALUES(%s, %s, %s, %s, %s, %s, %s)',
        (username, hash_password(password), role, tenant_id, 1 if role == 'customer' else 0, 0, 'pending')
    )
    conn.commit()
    conn.close()
    message = f"用户 {username} 注册成功"
    message += "，等待管理员批准"
    return jsonify({"status": "ok", "message": message})


# ---------- 附件上传接口 ----------
@app.route('/upload_attachment', methods=['POST'])
def upload_attachment():
    if 'username' not in session:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    if is_superadmin_session():
        return jsonify({"status": "error", "message": "超级账号不使用普通附件上传"}), 403
    file = request.files.get('file')
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "未选择文件"})
    saved = _save_uploaded_attachment(file)
    if not saved:
        return jsonify({"status": "error", "message": "文件保存失败"})
    _record_attachment(saved, tenant_id=get_current_tenant_id())
    return jsonify({"status": "ok", **saved})


@app.route('/upload_image', methods=['POST'])
def upload_image():
    if 'username' not in session:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    if is_superadmin_session():
        return jsonify({"status": "error", "message": "超级账号不使用普通图片上传"}), 403
    file = request.files.get('image')
    if not file or file.filename == '':
        return jsonify({"status": "error", "message": "未选择文件"})
    saved = _save_uploaded_attachment(file)
    if not saved:
        return jsonify({"status": "error", "message": "文件保存失败"})
    _record_attachment(saved, tenant_id=get_current_tenant_id())
    return jsonify({"status": "ok", **saved})


# ---------- 聊天页面 ----------
@app.route('/')
def index():
    return redirect(url_for('login'))


@app.route('/home')
def chat_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    tenant_for_notice = get_current_tenant_id() or DEFAULT_TENANT_ID
    pending_customer_count = count_pending_customer_questions(tenant_for_notice) if session.get('role') in ('admin', 'employee') else 0
    new_log_count = count_new_logs_for_admin(tenant_for_notice)
    new_inbox_count = count_unread_customer_replies(tenant_for_notice, session.get('username', '')) if session.get('role') == 'customer' else 0
    # 允许超级管理员通过 impersonate 参数查看普通页面（只用于导航展示）
    if is_superadmin_session():
        if 'impersonate_tenant' in session:
            tenant = get_tenant_by_id(session.get('impersonate_tenant'))
            if tenant is None:
                return redirect(url_for('superadmin_dashboard'))
            if 'chat_thread_id' not in session:
                session['chat_thread_id'] = uuid.uuid4().hex
            return render_template(
                'chat.html',
                username=session['username'],
                role=session.get('role'),
                tenant_name=tenant.get('name', '演示公司'),
                thread_id=session['chat_thread_id'],
                brand_html=APP_BRAND_HTML,
                is_impersonating=True,
                impersonated_tenant_id=tenant.get('id'),
                pending_customer_count=0,
                new_log_count=0,
                new_inbox_count=0,
                can_edit_qa=can_edit_knowledge(),
            )
        if 'chat_thread_id' not in session:
            session['chat_thread_id'] = uuid.uuid4().hex
        tenant = get_tenant_by_id(session.get('tenant_id') or DEFAULT_TENANT_ID) or {'name': '演示公司'}
        return render_template(
            'chat.html',
            username=session.get('username', SUPERADMIN_USERNAME),
            role='admin',
            tenant_name=tenant.get('name', '演示公司'),
            thread_id=session['chat_thread_id'],
            brand_html=APP_BRAND_HTML,
            is_impersonating=False,
            impersonated_tenant_id=None,
            pending_customer_count=0,
            new_log_count=0,
            new_inbox_count=0,
            can_edit_qa=can_edit_knowledge(),
        )
    if 'chat_thread_id' not in session:
        session['chat_thread_id'] = uuid.uuid4().hex
    return render_template(
        'chat.html',
        username=session['username'],
        role=session['role'],
        tenant_name=session.get('tenant_name', '演示公司'),
        thread_id=session['chat_thread_id'],
        brand_html=APP_BRAND_HTML,
        is_impersonating=False,
        impersonated_tenant_id=None,
        pending_customer_count=pending_customer_count,
        new_log_count=new_log_count,
        new_inbox_count=new_inbox_count,
        can_edit_qa=can_edit_knowledge(),
    )


@app.route('/logout')
def logout():
    username = session.get('username')
    session_token = session.get('session_token')
    if username and session_token:
        clear_user_session_token(username, session_token)
    session.clear()
    return redirect(url_for('login'))


@app.route('/superadmin')
def superadmin_dashboard():
    if not is_superadmin_session():
        return redirect(url_for('login'))
    tenants = _query_tenant_counts()
    return render_template(
        'superadmin.html',
        username=session.get('username', SUPERADMIN_USERNAME),
        tenants=tenants,
        tenant_count=len(tenants),
        brand_html=APP_BRAND_HTML,
        superadmin_username=SUPERADMIN_USERNAME,
    )


@app.route('/superadmin/impersonate/<int:tenant_id>')
def superadmin_impersonate(tenant_id):
    if not is_superadmin_session():
        return redirect(url_for('login'))
    tenant = get_tenant_by_id(tenant_id)
    if tenant is None:
        flash('未找到租户')
        return redirect(url_for('superadmin_dashboard'))
    session['impersonate_tenant'] = tenant_id
    return redirect(url_for('chat_page'))


@app.route('/superadmin/stop_impersonate')
def superadmin_stop_impersonate():
    if not is_superadmin_session():
        return redirect(url_for('login'))
    session.pop('impersonate_tenant', None)
    return redirect(url_for('superadmin_dashboard'))


@app.route('/superadmin/tenants/<int:tenant_id>/users')
def superadmin_tenant_users(tenant_id):
    if not is_superadmin_session():
        return redirect(url_for('login'))
    conn = _get_conn()
    rows = conn.execute('SELECT username, role, tenant_id, status FROM users WHERE tenant_id = %s ORDER BY username', (tenant_id,)).fetchall()
    conn.close()
    users = [dict(r) for r in rows]
    tenant = get_tenant_by_id(tenant_id)
    return render_template('superadmin_users.html', tenant=tenant, users=users, superadmin_username=SUPERADMIN_USERNAME)


@app.route('/superadmin/tenants/<int:tenant_id>/qa')
def superadmin_tenant_qa(tenant_id):
    if not is_superadmin_session():
        return redirect(url_for('login'))
    conn = _get_conn()
    rows = conn.execute('SELECT id, display_no, question, answer, created_at FROM qa_pairs WHERE tenant_id = %s ORDER BY display_no DESC, id DESC', (tenant_id,)).fetchall()
    conn.close()
    qa = [dict(r) for r in rows]
    tenant = get_tenant_by_id(tenant_id)
    return render_template('superadmin_tenant_qa.html', tenant=tenant, qa=qa, superadmin_username=SUPERADMIN_USERNAME)


@app.route('/superadmin/tenants/<int:tenant_id>/users/<username>/delete', methods=['POST'])
def superadmin_delete_user(tenant_id, username):
    if not is_superadmin_session():
        return redirect(url_for('login'))
    # prevent deleting superadmin
    if username == SUPERADMIN_USERNAME:
        flash('不能删除超级管理员')
        return redirect(url_for('superadmin_tenant_users', tenant_id=tenant_id))
    conn = _get_conn()
    conn.execute('DELETE FROM users WHERE tenant_id = %s AND username = %s', (tenant_id, username))
    conn.commit()
    conn.close()
    flash(f'已删除用户 {username}')
    return redirect(url_for('superadmin_tenant_users', tenant_id=tenant_id))


@app.route('/superadmin/tenants/create', methods=['POST'])
def create_tenant():
    if not is_superadmin_session():
        return redirect(url_for('login'))
    tenant_name = request.form.get('tenant_name', '').strip()
    admin_username = request.form.get('admin_username', '').strip()
    admin_password = request.form.get('admin_password', '').strip()
    if not tenant_name:
        flash('租户名称不能为空')
        return redirect(url_for('superadmin_dashboard'))

    slug = _slugify_tenant_name(tenant_name)
    if not admin_username:
        admin_username = f'{slug}_admin'
    if not admin_password:
        admin_password = f'Tenant{uuid.uuid4().hex[:6]}2026'
    if not password_meets_policy(admin_password):
        flash('租户管理员密码至少 8 位，且必须包含字母和数字')
        return redirect(url_for('superadmin_dashboard'))
    existing_admin = get_user_record_by_username(admin_username)
    if existing_admin:
        flash('租户管理员用户名已存在')
        return redirect(url_for('superadmin_dashboard'))

    conn = _get_conn()
    existing = conn.execute('SELECT id FROM tenants WHERE name = %s OR slug = %s', (tenant_name, slug)).fetchone()
    if existing:
        conn.close()
        flash('租户名称已存在或已被占用')
        return redirect(url_for('superadmin_dashboard'))

    now = _now_iso()
    row = conn.execute('INSERT INTO tenants(name, slug, avatar_image, top_image, status, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s) RETURNING id',
                 (tenant_name, slug, '', '', 'active', now, now)).fetchone()
    tenant_id = row['id']
    conn.execute(
        'INSERT INTO users(username, password, role, tenant_id, public_only, can_edit_qa, status) VALUES(%s, %s, %s, %s, %s, %s, %s)',
        (admin_username, hash_password(admin_password), 'admin', tenant_id, 0, 1, 'active')
    )
    conn.commit()
    conn.close()
    flash(f'已创建租户 {tenant_name}，管理员 {admin_username}，初始密码 {admin_password}')
    return redirect(url_for('superadmin_dashboard'))


@app.route('/tenant_branding', methods=['GET', 'POST'])
def tenant_branding():
    if not has_request_context() or 'username' not in session:
        return redirect(url_for('login'))
    if session.get('role') not in ('admin', SUPERADMIN_ROLE):
        return redirect(url_for('chat_page'))
    if is_superadmin_session():
        tenant_id = request.args.get('tenant_id', type=int) or session.get('impersonate_tenant')
        if tenant_id is None:
            return redirect(url_for('superadmin_dashboard'))
    else:
        tenant_id = get_current_tenant_id()
    tenant = get_tenant_by_id(tenant_id)
    if not tenant:
        flash('未找到租户')
        return redirect(url_for('superadmin_dashboard' if is_superadmin_session() else 'chat_page'))

    if request.method == 'POST':
        avatar_file = request.files.get('avatar_image')
        top_file = request.files.get('top_image')
        avatar_path = tenant.get('avatar_image', '')
        top_path = tenant.get('top_image', '')
        if avatar_file and avatar_file.filename:
            saved_avatar = _save_uploaded_brand_image(avatar_file, tenant_id, 'avatar')
            if saved_avatar:
                avatar_path = saved_avatar
        if top_file and top_file.filename:
            saved_top = _save_uploaded_brand_image(top_file, tenant_id, 'top')
            if saved_top:
                top_path = saved_top
        conn = _get_conn()
        conn.execute('UPDATE tenants SET avatar_image = %s, top_image = %s, updated_at = %s WHERE id = %s', (avatar_path, top_path, _now_iso(), int(tenant_id)))
        conn.commit()
        conn.close()
        flash('租户图片已更新')
        if is_superadmin_session():
            return redirect(url_for('tenant_branding', tenant_id=int(tenant_id)))
        return redirect(url_for('tenant_branding'))

    return render_template('tenant_branding.html', tenant=tenant, username=session.get('username', ''), tenant_brand_html=build_brand_html(tenant_id), tenant_name=tenant.get('name', '演示公司'), tenant_avatar_image=tenant.get('avatar_image', ''), tenant_top_image=tenant.get('top_image', ''), superadmin_mode=is_superadmin_session())




@app.route('/upload_brand_image', methods=['POST'])
def upload_brand_image():
    if 'username' not in session:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    tenant_id = request.form.get('tenant_id', type=int) or get_current_tenant_id()
    purpose = request.form.get('purpose', 'avatar')
    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "未选择文件"})
    saved = _save_uploaded_brand_image(file, tenant_id, purpose)
    if not saved:
        return jsonify({"status": "error", "message": "文件保存失败"})
    conn = _get_conn()
    if purpose == 'avatar':
        conn.execute('UPDATE tenants SET avatar_image = %s, updated_at = %s WHERE id = %s', (saved, _now_iso(), int(tenant_id)))
    else:
        conn.execute('UPDATE tenants SET top_image = %s, updated_at = %s WHERE id = %s', (saved, _now_iso(), int(tenant_id)))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok", "path": saved})


@app.route('/superadmin/tenants/<int:tenant_id>/delete', methods=['POST'])
def delete_tenant(tenant_id):
    if not is_superadmin_session():
        return redirect(url_for('login'))
    if tenant_id == DEFAULT_TENANT_ID:
        flash('演示公司不能删除')
        return redirect(url_for('superadmin_dashboard'))

    conn = _get_conn()
    attachment_rows = conn.execute('SELECT path FROM attachments WHERE tenant_id = %s', (tenant_id,)).fetchall()
    conn.execute('DELETE FROM customer_replies WHERE tenant_id = %s', (tenant_id,))
    conn.execute('DELETE FROM operation_logs WHERE tenant_id = %s', (tenant_id,))
    conn.execute('DELETE FROM attachments WHERE tenant_id = %s', (tenant_id,))
    conn.execute('DELETE FROM qa_pairs WHERE tenant_id = %s', (tenant_id,))
    conn.execute('DELETE FROM users WHERE tenant_id = %s', (tenant_id,))
    conn.execute('DELETE FROM tenants WHERE id = %s', (tenant_id,))
    conn.commit()
    conn.close()

    try:
        collection = _get_chroma_collection()
        collection.delete(where={'tenant_id': tenant_id})
    except Exception:
        pass

    for row in attachment_rows:
        path = row['path']
        if not path:
            continue
        absolute_path = path if os.path.isabs(path) else os.path.join(BASE_DIR, path.lstrip('/\\'))
        try:
            if os.path.exists(absolute_path):
                os.remove(absolute_path)
        except Exception:
            pass

    flash(f'已删除租户 {tenant_id}')
    return redirect(url_for('superadmin_dashboard'))


# ---------- 问答录入页面（仅管理员/员工） ----------
@app.route('/add_page')
def add_page():
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        return redirect(url_for('chat_page'))
    role_for_view = session['role'] if session['role'] != SUPERADMIN_ROLE else 'admin'
    return render_template('add_page.html', username=session['username'], role=role_for_view, tenant_name=session.get('tenant_name', '演示公司'), brand_html=APP_BRAND_HTML)


# ---------- API接口 ----------
@app.route("/chat", methods=["POST"])
def chat():
    if 'username' not in session:
        return jsonify({"answer": "请先登录", "score": 0})
    data = request.get_json(silent=True) or {}
    question = data.get("question", "")
    if not question:
        return jsonify({"answer": "问题不能为空", "score": 0})

    # 获取当前用户信息
    user = get_user_record_by_username(session.get('username', ''))
    answer, score, error = get_answer(question, user)
    if error:
        return jsonify({"answer": error, "score": score})
    if isinstance(answer, dict):
        images = answer.get("images", [])
        if not isinstance(images, list):
            images = [images] if images else []
        return jsonify({
            "answer": answer.get("answer", ""),
            "answer_html": answer.get("answer_html", ""),
            "image": answer.get("image", ""),
            "images": images,
            "category": answer.get("category", "未分类"),
            "score": score,
        })
    return jsonify({"answer": answer, "image": "", "images": [], "score": score})


@app.route("/add_qa", methods=["POST"])
def add_qa():
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return jsonify({"status": "error", "message": "无权限"})
    if session.get('role') == 'employee' and not can_edit_knowledge():
        return jsonify({"status": "error", "message": "当前员工没有知识库编辑权限"})
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict()
    new_qa = data.get("qa") if isinstance(data, dict) else None  # 期望格式 { question: "...", answer: "...", public: true/false }
    if not new_qa and isinstance(data, dict) and data.get('question') and data.get('answer'):
        new_qa = data
    question = str((new_qa or {}).get("question", "")).strip()
    answer_text_raw = str((new_qa or {}).get("answer_text", (new_qa or {}).get("answer", ""))).strip()
    answer_html_raw = str((new_qa or {}).get("answer_html", "")).strip()
    if not new_qa or not question or (not answer_text_raw and not answer_html_raw):
        return jsonify({"status": "error", "message": "问题和答案不能为空"})
    responder_name = str((new_qa or {}).get('author', '')).strip()
    if session['role'] != 'customer' and not responder_name:
        responder_name = str(session.get('username', '')).strip()
    if not responder_name:
        responder_name = '奶龙'
    answer_html, answer_text = normalize_answer_content(answer_html_raw, answer_text_raw)
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    # 直接 DB 插入，不加载全量
    new_id = _get_next_qa_id()
    category = (new_qa or {}).get('category', '') or '未分类'
    is_public = 1 if (new_qa or {}).get('public', True) else 0
    now = _now_iso()
    conn = _get_conn()
    conn.execute(
        'INSERT INTO qa_pairs(id, display_no, question, answer, answer_html, public, category, approved, author, tenant_id, vector, created_at, updated_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
        (new_id, 0, question, answer_text, answer_html, is_public, category, 1, responder_name, int(tenant_id),
         json.dumps(encode_text(question).tolist()), now, now),
    )
    conn.commit()
    conn.close()
    sync_single_qa(int(tenant_id), new_id)
    # 重新编号
    conn_r = _get_conn()
    _renumber_qa_display_numbers(conn_r, tenant_id=int(tenant_id))
    conn_r.close()
    log_operation('add_qa', qa_id=new_id, question=question, detail=f"author={responder_name}")
    return jsonify({"status": "ok", "count": new_id, "message": "添加成功"})


@app.route('/manage_qa')
def manage_qa():
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))

    page = request.args.get('page', '1')
    per_page = request.args.get('per_page', '10')
    keyword = request.args.get('q', '').strip()
    filter_category = request.args.get('category', '').strip()
    filter_status = request.args.get('status', '').strip()
    try:
        page = max(int(page), 1)
    except ValueError:
        page = 1
    try:
        per_page = min(max(int(per_page), 5), 50)
    except ValueError:
        per_page = 10

    def normalize_category(value):
        return str(value or '未分类').strip().lower()

    # 使用 SQL 分页查询，不加载全量
    tenant_for_query = get_current_tenant_id() or DEFAULT_TENANT_ID
    result = db.load_qa_paginated(
        tenant_for_query, page=page, per_page=per_page,
        keyword=keyword, category=filter_category, status=filter_status,
    )
    page_items = result['items']
    total = result['total']
    total_pages = max((total + per_page - 1) // per_page, 1)
    if total_pages > 0 and page > total_pages:
        page = total_pages
    # 从 DB 查询分类列表（不依赖全量加载）
    try:
        conn = _get_conn()
        cat_rows = conn.execute('SELECT DISTINCT category FROM qa_pairs WHERE tenant_id = %s ORDER BY category', (int(tenant_for_query),)).fetchall()
        conn.close()
        categories = sorted({r['category'] or '未分类' for r in cat_rows})
    except Exception:
        categories = ['未分类']

    role_for_view = session['role'] if session['role'] != SUPERADMIN_ROLE else 'admin'
    tenant_for_notice = get_current_tenant_id() or DEFAULT_TENANT_ID
    return render_template(
        'manage_qa.html',
        username=session['username'],
        role=role_for_view,
        keyword=keyword,
        filter_category=filter_category,
        filter_status=filter_status,
        categories=categories,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        page_items=page_items,
        pages=range(1, total_pages + 1),
        brand_html=APP_BRAND_HTML,
        tenant_name=session.get('tenant_name', '演示公司'),
        pending_customer_count=count_pending_customer_questions(tenant_for_notice),
        new_log_count=count_new_logs_for_admin(tenant_for_notice),
        can_edit_qa=can_edit_knowledge(),
    )


@app.route('/edit_qa/<sint:qa_id>', methods=['GET', 'POST'])
def edit_qa(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        flash('当前员工没有知识库编辑权限')
        return redirect(url_for('manage_qa'))

    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    # 直接查单条，不加载全量
    conn = _get_conn()
    row = conn.execute('SELECT * FROM qa_pairs WHERE id = %s AND tenant_id = %s', (int(qa_id), int(tenant_id))).fetchone()
    conn.close()

    if not row:
        flash('未找到该问答')
        return redirect(url_for('manage_qa'))

    qa = dict(row)

    if request.method == 'POST':
        question = request.form.get('question', '').strip()
        answer_html = request.form.get('answer_html', '')
        answer_text = request.form.get('answer_text', request.form.get('answer', '')).strip()
        public = request.form.get('public', 'true') == 'true'
        responder_name = request.form.get('author', '').strip() or session.get('username', '')
        category = '未分类'
        if session.get('role') == 'admin':
            category = str(request.form.get('category', '')).strip() or '未分类'
        sanitized_html, plain_text = normalize_answer_content(answer_html, answer_text)
        if not question or not plain_text:
            flash('问题和答案不能为空')
            return redirect(url_for('edit_qa', qa_id=qa_id))

        now = _now_iso()
        vector = json.dumps(encode_text(question).tolist())
        conn = _get_conn()
        conn.execute(
            'UPDATE qa_pairs SET question = %s, answer = %s, answer_html = %s, public = %s, category = %s, '
            'author = %s, vector = %s, updated_at = %s WHERE id = %s AND tenant_id = %s',
            (question, plain_text, sanitized_html, 1 if public else 0, category,
             responder_name, vector, now, int(qa_id), int(tenant_id)),
        )
        conn.commit()
        conn.close()
        sync_single_qa(int(tenant_id), int(qa_id))
        log_operation('edit_qa', qa_id=qa_id, question=question, detail=f"author={responder_name}")
        flash('已更新问答')
        return redirect(url_for('manage_qa'))

    role_for_view = session['role'] if session['role'] != SUPERADMIN_ROLE else 'admin'
    return render_template('edit_qa.html', qa=qa, username=session['username'], role=role_for_view, brand_html=APP_BRAND_HTML)


@app.route('/approve_qa/<sint:qa_id>', methods=['POST'])
def approve_qa(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        flash('当前员工没有知识库编辑权限')
        return redirect(url_for('manage_qa'))
    data_all = load_data_with_schema()
    qa = find_qa_by_id(data_all, qa_id)
    if qa:
        tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
        # 如果这是一个临时负 ID，分配正式 ID 并迁移引用
        if isinstance(qa.get('id'), int) and qa.get('id') <= 0:
            old_id = int(qa.get('id'))
            new_id = _get_next_qa_id()
            _reassign_question_id(old_id, new_id, tenant_id)
            qa['id'] = new_id
        # 直接 DB 更新，不加载全量
        conn = _get_conn()
        conn.execute('UPDATE qa_pairs SET approved = 1, author = %s WHERE id = %s AND tenant_id = %s',
                     (session.get('username', ''), int(qa['id']), tenant_id))
        conn.commit()
        conn.close()
        sync_single_qa(int(tenant_id), int(qa['id']))
        log_operation('approve_qa', qa_id=qa.get('id'), question=qa.get('question', ''), detail=f"approver={session.get('username', '')}")
        flash('已通过该问答')
    else:
        flash('未找到该问答')
    return redirect(url_for('manage_qa'))


@app.route('/reject_qa/<sint:qa_id>', methods=['POST'])
def reject_qa(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        flash('当前员工没有知识库编辑权限')
        return redirect(url_for('manage_qa'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    result = conn.execute('DELETE FROM qa_pairs WHERE id = %s AND tenant_id = %s', (int(qa_id), int(tenant_id)))
    conn.commit()
    conn.close()
    if result.rowcount:
        delete_single_qa(int(tenant_id), int(qa_id))
        # 重新编号
        conn_r = _get_conn()
        _renumber_qa_display_numbers(conn_r, tenant_id=int(tenant_id))
        conn_r.close()
        log_operation('reject_qa', qa_id=qa_id, detail=f"reviewer={session.get('username', '')}")
        flash('已拒绝并删除该问答')
    else:
        flash('未找到该问答')
    return redirect(url_for('manage_qa'))


@app.route('/delete_qa/<sint:qa_id>', methods=['POST'])
def delete_qa(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        flash('当前员工没有知识库编辑权限')
        return redirect(url_for('manage_qa'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    result = conn.execute('DELETE FROM qa_pairs WHERE id = %s AND tenant_id = %s', (int(qa_id), int(tenant_id)))
    conn.commit()
    conn.close()
    if result.rowcount:
        delete_single_qa(int(tenant_id), int(qa_id))
        # 重新编号
        conn_r = _get_conn()
        _renumber_qa_display_numbers(conn_r, tenant_id=int(tenant_id))
        conn_r.close()
        log_operation('delete_qa', qa_id=qa_id, detail=f"operator={session.get('username', '')}")
        flash('已删除问答')
    else:
        flash('未找到该问答')
    return redirect(url_for('manage_qa'))


@app.route('/delete_attachment/<sint:qa_id>', methods=['POST'])
def delete_attachment(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    if session.get('role') == 'employee' and not can_edit_knowledge():
        flash('当前员工没有知识库编辑权限')
        return redirect(url_for('manage_qa'))

    payload = request.get_json(silent=True) or {}
    path = str(request.form.get('path', '') or payload.get('path', '')).strip()
    if not path:
        flash('附件路径不能为空')
        return redirect(url_for('edit_qa', qa_id=qa_id))

    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    # 直接删除附件记录
    conn.execute('DELETE FROM attachments WHERE tenant_id = %s AND question_id = %s AND path = %s',
                 (int(tenant_id), int(qa_id), path))
    # 更新 qa_pairs 中的 answer_html 移除图片/文件引用
    row = conn.execute('SELECT answer_html, image, images_json, attachments_json FROM qa_pairs WHERE id = %s AND tenant_id = %s',
                       (int(qa_id), int(tenant_id))).fetchone()
    if row:
        new_html = _remove_attachment_from_answer_html(row['answer_html'] or '', path)
        conn.execute('UPDATE qa_pairs SET answer_html = %s WHERE id = %s AND tenant_id = %s',
                     (new_html, int(qa_id), int(tenant_id)))
    conn.commit()
    conn.close()
    flash('已删除附件')
    return redirect(url_for('edit_qa', qa_id=qa_id))


# ---------- 管理员用户审核界面 ----------
@app.route('/review_users')
def review_users():
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    current_tenant_id = get_current_tenant_id()
    conn = _get_conn()
    rows = conn.execute(
        'SELECT username, role, tenant_id, public_only, can_edit_qa, status FROM users WHERE tenant_id = %s AND status = %s AND role != %s ORDER BY username',
        (int(current_tenant_id), 'pending', SUPERADMIN_ROLE),
    ).fetchall()
    conn.close()
    pending = [dict(row) for row in rows]
    tenant_for_notice = get_current_tenant_id() or DEFAULT_TENANT_ID
    return render_template('review_users.html', username=session['username'], role='admin', pending=pending, brand_html=APP_BRAND_HTML, tenant_name=session.get('tenant_name', '演示公司'), pending_customer_count=count_pending_customer_questions(tenant_for_notice), new_log_count=count_new_logs_for_admin(tenant_for_notice))


@app.route('/approve_user/<username>', methods=['POST'])
def approve_user(username):
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    current_tenant_id = get_current_tenant_id()
    conn = _get_conn()
    result = conn.execute(
        'UPDATE users SET status = %s WHERE username = %s AND tenant_id = %s AND status = %s AND role != %s',
        ('active', username, int(current_tenant_id), 'pending', SUPERADMIN_ROLE),
    )
    conn.commit()
    conn.close()
    if result.rowcount:
        flash(f'已批准用户 {username}')
    else:
        flash('未找到待审核用户')
    return redirect(url_for('review_users'))


@app.route('/reject_user/<username>', methods=['POST'])
def reject_user(username):
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    current_tenant_id = get_current_tenant_id()
    conn = _get_conn()
    result = conn.execute(
        'DELETE FROM users WHERE username = %s AND tenant_id = %s AND status = %s AND role != %s',
        (username, int(current_tenant_id), 'pending', SUPERADMIN_ROLE),
    )
    conn.commit()
    conn.close()
    if result.rowcount:
        flash(f'已拒绝并删除用户 {username}')
    else:
        flash('未找到待审核用户')
    return redirect(url_for('review_users'))


# ---------- 客户提问 ----------
@app.route('/submit_page')
def submit_page():
    if 'username' not in session:
        return redirect(url_for('login'))
    if is_superadmin_session():
        return redirect(url_for('chat_page'))
    return render_template('submit_page.html', username=session['username'], brand_html=APP_BRAND_HTML, tenant_name=session.get('tenant_name', '演示公司'))


@app.route('/submit_qa', methods=['POST'])
def submit_qa():
    if 'username' not in session:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    if is_superadmin_session():
        return jsonify({"status": "error", "message": "超级账号不提交普通问答"}), 403
    data = request.get_json()
    qa_in = data.get('qa')
    if not qa_in or not qa_in.get('question'):
        return jsonify({"status": "error", "message": "问题不能为空"})
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    question = qa_in.get('question')
    now = _now_iso()
    conn = _get_conn()
    conn.execute(
        'INSERT INTO qa_pairs(id, question, answer, answer_html, public, category, approved, author, tenant_id, vector, created_at, updated_at) '
        'VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
        (get_temp_qa_id(), question, qa_in.get('answer', ''), normalize_answer_content('', qa_in.get('answer', ''))[0],
         1 if qa_in.get('public', False) else 0, str(qa_in.get('category', '未分类') or '未分类'), 0,
         session['username'], int(tenant_id), json.dumps(encode_text(question).tolist()), now, now),
    )
    conn.commit()
    conn.close()
    log_operation('submit_question', qa_id=0, question=question, detail=f"客户={session.get('username', '')}")
    return jsonify({"status": "ok", "message": "提交成功，等待管理员审核"})


@app.route('/reply_customer_question/<sint:qa_id>', methods=['GET', 'POST'])
def reply_customer_question(qa_id):
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('login'))
    conn_qa = _get_conn()
    row_qa = conn_qa.execute('SELECT * FROM qa_pairs WHERE id = %s', (int(qa_id),)).fetchone()
    conn_qa.close()
    if not row_qa:
        flash('未找到该问题')
        return redirect(url_for('manage_qa'))
    qa = dict(row_qa)
    # 获取附件
    conn_att = _get_conn()
    att_rows = conn_att.execute('SELECT * FROM attachments WHERE question_id = %s ORDER BY position', (int(qa_id),)).fetchall()
    conn_att.close()
    qa['attachments'] = [dict(a) for a in att_rows]
    qa['images'] = [a['path'] for a in qa['attachments'] if a['kind'] == 'image']

    if request.method == 'POST':
        responder_name = request.form.get('responder_name', '').strip() or session.get('username', '')
        reply_mode = request.form.get('reply_mode', 'private')
        answer_html = request.form.get('answer_html', '').strip()
        answer_text = request.form.get('answer_text', request.form.get('answer', '')).strip()
        sanitized_html, plain_text = normalize_answer_content(answer_html, answer_text)
        if not plain_text:
            flash('答案不能为空')
            return redirect(url_for('reply_customer_question', qa_id=qa_id))
        if reply_mode == 'public' and session.get('role') == 'employee' and not can_edit_knowledge():
            flash('当前员工没有知识库编辑权限，只能私下回复')
            return redirect(url_for('reply_customer_question', qa_id=qa_id))

        tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
        customer_username = qa.get('author', '')
        conn = _get_conn()
        conn.execute(
            'INSERT INTO customer_replies(tenant_id, question_id, question_text, customer_username, responder_name, answer_text, answer_html, public_reply, is_read, created_at) VALUES(%s, %s, %s, %s, %s, %s, %s, %s, 0, %s)',
            (int(tenant_id), int(qa_id), str(qa.get('question', '') or ''), str(customer_username), str(responder_name), plain_text, sanitized_html, 1 if reply_mode == 'public' else 0, _now_iso()),
        )
        conn.commit()
        conn.close()

        if reply_mode == 'public':
            new_id = int(qa_id)
            # 如果是临时负 ID，分配正式 ID 并重新插入
            if int(qa_id) <= 0:
                new_id = _get_next_qa_id()
                _reassign_question_id(int(qa_id), new_id, tenant_id)
                # 插入新 ID 的问答
                vector = encode_text(qa.get('question', '')).tolist()
                conn2 = _get_conn()
                conn2.execute(
                    '''INSERT INTO qa_pairs(id, display_no, question, answer, answer_html, public, category, approved, author, tenant_id, image, images_json, attachments_json, vector, created_at, updated_at)
                    VALUES(%s, 0, %s, %s, %s, 1, %s, 1, %s, %s, '', '[]', '[]', %s, %s, %s)''',
                    (new_id, qa.get('question', ''), plain_text, sanitized_html,
                     qa.get('category', '未分类'), customer_username, int(tenant_id),
                     json.dumps(vector), _now_iso(), _now_iso()),
                )
                conn2.commit()
                conn2.close()
            else:
                # 已入库的问答，直接更新
                vector = encode_text(qa.get('question', '')).tolist()
                conn2 = _get_conn()
                conn2.execute(
                    'UPDATE qa_pairs SET answer = %s, answer_html = %s, approved = 1, public = 1, vector = %s, updated_at = %s WHERE id = %s AND tenant_id = %s',
                    (plain_text, sanitized_html, json.dumps(vector), _now_iso(), new_id, int(tenant_id)),
                )
                conn2.commit()
                conn2.close()
            sync_single_qa(int(tenant_id), new_id)
            # 重新编号
            conn_r = _get_conn()
            _renumber_qa_display_numbers(conn_r, tenant_id=int(tenant_id))
            conn_r.close()
            log_operation('reply_public', qa_id=new_id, question=qa.get('question', ''), detail=f"回复人={responder_name}")
            flash('已公开回复并录入知识库，同时客户收件箱已收到回复')
        else:
            # 私下回复后删除此问答（不录入知识库）
            conn3 = _get_conn()
            conn3.execute('DELETE FROM qa_pairs WHERE id = %s AND tenant_id = %s', (int(qa_id), int(tenant_id)))
            conn3.commit()
            conn3.close()
            # 重新编号
            conn_r = _get_conn()
            _renumber_qa_display_numbers(conn_r, tenant_id=int(tenant_id))
            conn_r.close()
            log_operation('reply_private', qa_id=qa_id, question=qa.get('question', ''), detail=f"回复人={responder_name}")
            flash('已私下回复客户，该回复不会录入知识库')
        return redirect(url_for('question_review'))

    role_for_view = session.get('role', '') if session.get('role') != SUPERADMIN_ROLE else 'admin'
    return render_template('reply_customer_question.html', qa=qa, username=session.get('username', ''), role=role_for_view, brand_html=APP_BRAND_HTML, tenant_name=session.get('tenant_name', '演示公司'), can_edit_qa=can_edit_knowledge())


@app.route('/delete_reply/<int:reply_id>', methods=['POST'])
def delete_reply(reply_id):
    if 'username' not in session or session.get('role') != 'customer':
        return redirect(url_for('login'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    username = session.get('username', '')
    conn = _get_conn()
    conn.execute('DELETE FROM customer_replies WHERE id = %s AND tenant_id = %s AND customer_username = %s',
                 (reply_id, int(tenant_id), str(username)))
    conn.commit()
    conn.close()
    flash('回复已删除')
    return redirect(url_for('customer_inbox'))


@app.route('/customer_inbox')
def customer_inbox():
    if 'username' not in session:
        return redirect(url_for('login'))
    if session.get('role') != 'customer':
        return redirect(url_for('chat_page'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    username = session.get('username', '')
    sort_order = request.args.get('sort', 'desc').lower()
    if sort_order not in ('asc', 'desc'):
        sort_order = 'desc'
    conn = _get_conn()
    conn.execute('UPDATE customer_replies SET is_read = 1 WHERE tenant_id = %s AND customer_username = %s AND is_read = 0', (int(tenant_id), username))
    conn.commit()

    question_rows = conn.execute(
        'SELECT id, question, created_at, approved, answer, answer_html, public, category FROM qa_pairs WHERE tenant_id = %s AND author = %s ORDER BY id DESC',
        (int(tenant_id), username),
    ).fetchall()
    reply_rows = conn.execute(
        f'SELECT id, question_id, question_text, responder_name, answer_text, answer_html, public_reply, is_read, created_at FROM customer_replies WHERE tenant_id = %s AND customer_username = %s ORDER BY question_id ASC, id {"DESC" if sort_order == "desc" else "ASC"}',
        (int(tenant_id), username),
    ).fetchall()
    conn.close()
    thread_map = {}

    for row in question_rows:
        item = dict(row)
        qid = int(item.get('id') or 0)
        thread_map[qid] = {
            'question_id': qid,
            'question_text': str(item.get('question') or '').strip(),
            'question_created_at': item.get('created_at', ''),
            'replies': [],
            'latest_created_at': item.get('created_at', ''),
            'unread_count': 0,
        }

    for row in reply_rows:
        item = dict(row)
        qid = int(item.get('question_id') or 0)
        thread = thread_map.get(qid)
        if thread is None:
            thread = {
                'question_id': qid,
                'question_text': str(item.get('question_text') or '').strip(),
                'question_created_at': item.get('created_at', ''),
                'replies': [],
                'latest_created_at': item.get('created_at', ''),
                'unread_count': 0,
            }
            thread_map[qid] = thread
        if not thread.get('question_text'):
            thread['question_text'] = str(item.get('question_text') or '').strip()
        thread['replies'].append(item)
        if str(item.get('created_at') or '') > str(thread.get('latest_created_at') or ''):
            thread['latest_created_at'] = item.get('created_at', '')
        if not bool(item.get('is_read')):
            thread['unread_count'] += 1

    groups = list(thread_map.values())
    groups.sort(key=lambda g: g.get('latest_created_at', ''), reverse=(sort_order == 'desc'))
    return render_template('customer_inbox.html', groups=groups, username=session.get('username', ''), tenant_name=session.get('tenant_name', '演示公司'), brand_html=APP_BRAND_HTML, sort_order=sort_order)


@app.route('/operation_logs')
def operation_logs_page():
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    rows = conn.execute(
        'SELECT id, username, action, qa_id, question, detail, created_at FROM operation_logs WHERE tenant_id = %s AND (user_role = %s OR action = %s) ORDER BY id DESC LIMIT 200',
        (int(tenant_id), 'employee', '客户提问'),
    ).fetchall()
    conn.close()
    logs = [dict(r) for r in rows]
    if logs:
        session['last_seen_log_id'] = max(int(item.get('id', 0) or 0) for item in logs)
    return render_template('operation_logs.html', logs=logs, username=session.get('username', ''), role=session.get('role', ''), tenant_name=session.get('tenant_name', '演示公司'), brand_html=APP_BRAND_HTML)


@app.route('/question_review')
def question_review():
    if 'username' not in session or session.get('role') not in ('admin', 'employee'):
        return redirect(url_for('login'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    rows = conn.execute(
        '''
        SELECT q.id, q.display_no, q.question, q.answer, q.author, q.created_at, q.category
        FROM qa_pairs q
        WHERE q.tenant_id = %s AND q.approved = 0
        ORDER BY q.display_no DESC, q.id DESC
        ''',
        (int(tenant_id),),
    ).fetchall()
    conn.close()
    items = [dict(r) for r in rows]
    if items:
        session['last_seen_pending_question_id'] = max(int(item.get('id', 0) or 0) for item in items)
    return render_template('question_review.html', items=items, username=session.get('username', ''), role=session.get('role', ''), tenant_name=session.get('tenant_name', '演示公司'), brand_html=APP_BRAND_HTML)


@app.route('/ops_center')
def ops_center():
    if 'username' not in session:
        return redirect(url_for('login'))
    if session.get('role') not in ('admin', 'employee', SUPERADMIN_ROLE):
        return redirect(url_for('chat_page'))
    return render_template('ops_center.html',
        can_edit=can_edit_knowledge())


@app.route('/member_accounts')
def member_accounts():
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    rows = conn.execute(
        'SELECT username, role, status, public_only, can_edit_qa FROM users WHERE tenant_id = %s AND role IN (%s, %s) ORDER BY role, username',
        (int(tenant_id), 'employee', 'customer'),
    ).fetchall()
    conn.close()
    accounts = [dict(r) for r in rows]
    return render_template('member_accounts.html', accounts=accounts, username=session.get('username', ''), tenant_name=session.get('tenant_name', '演示公司'), brand_html=APP_BRAND_HTML)


@app.route('/member_accounts/<username>/delete', methods=['POST'])
def delete_member_account(username):
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    if username == session.get('username'):
        flash('不能删除当前登录管理员账号')
        return redirect(url_for('member_accounts'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    conn.execute('DELETE FROM users WHERE tenant_id = %s AND username = %s AND role IN (%s, %s)', (int(tenant_id), username, 'employee', 'customer'))
    conn.commit()
    conn.close()
    flash(f'已删除账号 {username}')
    return redirect(url_for('member_accounts'))


@app.route('/member_accounts/<username>/toggle_qa_permission', methods=['POST'])
def toggle_member_qa_permission(username):
    if 'username' not in session or session.get('role') != 'admin':
        return redirect(url_for('login'))
    tenant_id = get_current_tenant_id() or DEFAULT_TENANT_ID
    conn = _get_conn()
    row = conn.execute('SELECT username, role, can_edit_qa FROM users WHERE tenant_id = %s AND username = %s', (int(tenant_id), username)).fetchone()
    if not row or row['role'] != 'employee':
        conn.close()
        flash('未找到可授权的员工账号')
        return redirect(url_for('member_accounts'))
    new_value = 0 if int(row['can_edit_qa'] or 0) else 1
    conn.execute('UPDATE users SET can_edit_qa = %s WHERE tenant_id = %s AND username = %s', (new_value, int(tenant_id), username))
    conn.commit()
    conn.close()
    flash(f'已更新员工 {username} 的知识库编辑权限')
    return redirect(url_for('member_accounts'))


@app.route('/import_qa', methods=['POST'])
def import_qa():
    if 'username' not in session or session['role'] not in ('admin', 'employee', SUPERADMIN_ROLE):
        return jsonify({"status": "error", "message": "无权限"})

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({"status": "error", "message": "请选择 CSV 或 Excel 文件"})

    filename = file.filename.lower()
    rows = []

    def normalize_row(row):
        normalized = {}
        for key, value in row.items():
            normalized[str(key).strip().lower()] = value.strip() if isinstance(value, str) else value
        return normalized

    def pick_value(row, aliases):
        for alias in aliases:
            value = row.get(alias)
            if value is None:
                continue
            if isinstance(value, str):
                value = value.strip()
            if value != '':
                return value
        return ''

    def parse_public(value):
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in ('false', '0', 'no', 'off', '否', '内部', 'private'):
            return False
        return True

    try:
        if filename.endswith('.csv'):
            text_stream = io.StringIO(file.read().decode('utf-8-sig', errors='ignore'))
            reader = csv.DictReader(text_stream)
            rows = [normalize_row(row) for row in reader]
        elif filename.endswith('.xlsx') or filename.endswith('.xlsm'):
            if load_workbook is None:
                return jsonify({"status": "error", "message": "缺少 openpyxl，请先安装后再导入 Excel"})
            workbook = load_workbook(file, read_only=True, data_only=True)
            worksheet = workbook.active
            raw_rows = list(worksheet.iter_rows(values_only=True))
            if not raw_rows:
                return jsonify({"status": "error", "message": "文件为空"})
            headers = [str(item).strip().lower() if item is not None else '' for item in raw_rows[0]]
            for values in raw_rows[1:]:
                row = {}
                for index, header in enumerate(headers):
                    if not header or index >= len(values):
                        continue
                    cell_value = values[index]
                    row[header] = '' if cell_value is None else str(cell_value).strip()
                rows.append(row)
        else:
            return jsonify({"status": "error", "message": "仅支持 CSV、XLSX、XLSM 文件"})
    except Exception as exc:
        return jsonify({"status": "error", "message": f"读取文件失败：{exc}"})

    conn_i = _get_conn()
    existing_rows = conn_i.execute('SELECT question, answer FROM qa_pairs WHERE tenant_id = %s', (int(get_current_tenant_id() or DEFAULT_TENANT_ID),)).fetchall()
    conn_i.close()
    existing_pairs = {(str(r['question'] or '').strip(), str(r['answer'] or '').strip()) for r in existing_rows}
    imported = 0
    skipped = 0
    imported_qas = []

    for row in rows:
        question = pick_value(row, ('question', '问题', '标准问法', '标准问题', '提问'))
        answer = pick_value(row, ('answer', '答案', '回复'))
        public_value = pick_value(row, ('public', '是否公开', '公开'))
        category_value = pick_value(row, ('category', '分类'))
        if not question or not answer:
            skipped += 1
            continue

        pair_key = (question.strip(), answer.strip())
        if pair_key in existing_pairs:
            skipped += 1
            continue

        new_qa = {
            'id': _get_next_qa_id(),
            'question': question.strip(),
            'answer': answer.strip(),
            'answer_html': normalize_answer_content('', answer.strip())[0],
            'public': parse_public(public_value if public_value != '' else True),
            'category': str(category_value).strip() if str(category_value).strip() else '未分类',
            'vector': encode_text(question.strip()).tolist(),
            'approved': True,
            'tenant_id': get_current_tenant_id(),
        }
        imported_qas.append(new_qa)
        existing_pairs.add(pair_key)
        imported += 1

    if imported:
        save_data({"qa_pairs": imported_qas}, tenant_id=get_current_tenant_id())

    return jsonify({
        "status": "ok",
        "message": f"导入完成，成功 {imported} 条，跳过 {skipped} 条",
        "count": imported
    })


# ---------- 启动 ── 初始化数据 + 异步重建 ChromaDB ----------
_init_app_data()
threading.Thread(target=bootstrap_chroma_index, daemon=True, name='chroma-bootstrap').start()
print("[INFO] ChromaDB 后台重建中，首次搜索可能稍慢...")

if __name__ == "__main__":
    import logging
    class SilentRequestFilter(logging.Filter):
        def filter(self, record):
            msg = record.getMessage()
            return not (msg.startswith('"') and ('GET /' in msg or 'POST /' in msg))
    logging.getLogger('werkzeug').addFilter(SilentRequestFilter())
    app.run(host="0.0.0.0", port=5000,
            debug=os.getenv('FLASK_DEBUG') == '1', use_reloader=False)
