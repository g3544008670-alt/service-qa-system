import re

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity


_NORMALIZE_RULES = (
    ('你的名字', '你叫什么'),
    ('名字', '叫什么'),
    ('姓名', '叫什么'),
    ('称呼', '叫什么'),
    ('如何', '怎么'),
    ('怎样', '怎么'),
    ('咋', '怎么'),
    ('重置', '更改'),
    ('找回', '更改'),
    ('修改', '更改'),
    ('变更', '更改'),
    ('登录', '登陆'),
    ('账号', '账户'),
    ('帐号', '账户'),
)


def _normalize_text(text):
    if not text:
        return ''
    normalized = text.strip().lower()
    normalized = re.sub(r'[\s\u3000]+', '', normalized)
    for source, target in _NORMALIZE_RULES:
        normalized = normalized.replace(source, target)
    normalized = re.sub(r'[！？。,.、;；:：\-_/\\()（）\[\]{}<>“”"\'`~@#$%^&*+=|]', '', normalized)
    return normalized


def find_best_match(query, candidates, top_k=1):
    """
    返回 (index, score) 的最佳匹配。candidates 为 list of strings。
    """
    if not candidates:
        return None, 0.0
    normalized_candidates = [_normalize_text(candidate) for candidate in candidates]
    normalized_query = _normalize_text(query)
    texts = normalized_candidates + [normalized_query]
    vectorizer = TfidfVectorizer(analyzer='char', ngram_range=(2, 4)).fit_transform(texts)
    sims = cosine_similarity(vectorizer[-1], vectorizer[:-1])[0]
    if len(sims) == 0:
        return None, 0.0
    best_idx = int(sims.argmax())
    best_score = float(sims[best_idx])
    return best_idx, best_score


# ═══════════════════════════════════════════
# 以下为核心工具函数（从 app.py 提取）
# ═══════════════════════════════════════════

import os
import json
import hashlib
from html import escape, unescape
from html.parser import HTMLParser
from datetime import datetime
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename
import bcrypt

LEGACY_SHA256_RE = re.compile(r'^[0-9a-f]{64}$', re.IGNORECASE)
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
RICH_TEXT_TAGS = {'p', 'br', 'strong', 'b', 'em', 'i', 'u', 'ul', 'ol', 'li', 'blockquote', 'pre', 'code', 'span', 'div', 'a', 'img'}

# 路径（由 app 初始化时设置）
UPLOAD_FOLDER = None
DEFAULT_TENANT_ID = 1


def init_utils(upload_folder, default_tenant_id):
    global UPLOAD_FOLDER, DEFAULT_TENANT_ID
    UPLOAD_FOLDER = upload_folder
    DEFAULT_TENANT_ID = default_tenant_id


class RichAnswerSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts = []
        self.text_parts = []
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.skip_depth += 1
            return
        if self.skip_depth or tag not in RICH_TEXT_TAGS:
            return
        attr_map = {key.lower(): (value or '') for key, value in attrs}
        if tag == 'br':
            self.parts.append('<br>')
            self.text_parts.append('\n')
            return
        if tag == 'img':
            src = attr_map.get('src', '').strip()
            if not src:
                return
            alt_text = attr_map.get('alt', '').strip() or '图片'
            self.parts.append(f'<img src="{escape(src, quote=True)}" alt="{escape(alt_text, quote=True)}">')
            self.text_parts.append('[图片]')
            return
        if tag == 'a':
            href = attr_map.get('href', '').strip()
            if not href:
                return
            self.parts.append(f'<a href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer">')
            return
        if tag == 'span' and attr_map.get('class', '').strip() == 'file-chip':
            self.parts.append('<span class="file-chip">')
            return
        self.parts.append(f'<{tag}>')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            if self.skip_depth > 0:
                self.skip_depth -= 1
            return
        if self.skip_depth or tag not in RICH_TEXT_TAGS:
            return
        if tag not in ('br', 'img'):
            self.parts.append(f'</{tag}>')

    def handle_data(self, data):
        if self.skip_depth:
            return
        if data:
            self.parts.append(escape(data))
            self.text_parts.append(unescape(data))

    def handle_entityref(self, name):
        if self.skip_depth:
            return
        entity = f'&{name};'
        self.parts.append(entity)
        self.text_parts.append(unescape(entity))

    def handle_charref(self, name):
        if self.skip_depth:
            return
        entity = f'&#{name};'
        self.parts.append(entity)
        self.text_parts.append(unescape(entity))

    def get_html(self):
        return ''.join(self.parts)

    def get_text(self):
        return ''.join(self.text_parts)


def normalize_answer_content(answer_html='', answer_text=''):
    raw_html = str(answer_html or '').strip()
    raw_text = str(answer_text or '').strip()
    if raw_html:
        sanitizer = RichAnswerSanitizer()
        sanitizer.feed(raw_html)
        sanitizer.close()
        sanitized_html = sanitizer.get_html().strip()
        plain_text = sanitizer.get_text().strip()
        if not plain_text and raw_text:
            plain_text = raw_text
        if not sanitized_html:
            sanitized_html = ''.join(f'<p>{escape(line)}</p>' for line in raw_text.splitlines() if line.strip()) or escape(raw_text).replace('\n', '<br>')
        return sanitized_html, plain_text
    plain_text = raw_text
    sanitized_html = ''.join(f'<p>{escape(line)}</p>' for line in raw_text.splitlines() if line.strip()) or escape(raw_text).replace('\n', '<br>')
    return sanitized_html, plain_text


def _save_uploaded_attachment(file):
    if not file or not getattr(file, 'filename', ''):
        return None
    filename = secure_filename(file.filename)
    if not filename:
        return None
    dest = os.path.join(UPLOAD_FOLDER, filename)
    base, ext = os.path.splitext(filename)
    idx = 1
    while os.path.exists(dest):
        filename = f"{base}_{idx}{ext}"
        dest = os.path.join(UPLOAD_FOLDER, filename)
        idx += 1
    file.save(dest)
    size = os.path.getsize(dest)
    mimetype = getattr(file, 'mimetype', '') or ''
    kind = 'image' if ext.lower() in IMAGE_EXTENSIONS or mimetype.startswith('image/') else 'file'
    return {
        'path': f"/static/uploads/{filename}",
        'filename': filename,
        'kind': kind,
        'mimetype': mimetype,
        'size': size,
    }


def _save_uploaded_brand_image(file, tenant_id, purpose):
    if not file or not getattr(file, 'filename', ''):
        return None
    filename = secure_filename(file.filename)
    if not filename:
        return None
    # 统一保存为 .png（裁剪器输出的都是 PNG）
    brand_dir = os.path.join(UPLOAD_FOLDER, 'branding')
    os.makedirs(brand_dir, exist_ok=True)
    import time
    ts = int(time.time())
    safe_name = f"tenant_{int(tenant_id)}_{purpose}_{ts}.png"
    dest = os.path.join(brand_dir, safe_name)
    file.save(dest)
    return f"/static/uploads/branding/{safe_name}"


def _remove_attachment_from_answer_html(answer_html, path):
    html = str(answer_html or '')
    target = re.escape(str(path or '').strip())
    if not target:
        return html
    html = re.sub(rf'<img\b[^>]*\bsrc=["\"]{target}["\"][^>]*\s*/?>', '', html, flags=re.IGNORECASE)
    html = re.sub(rf'<a\b[^>]*\bhref=["\"]{target}["\"][^>]*>.*?</a>', '', html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r'<p>(\s*(?:<br\s*/?>)?\s*)</p>', '', html, flags=re.IGNORECASE)
    return html.strip()


def hash_password(password):
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def password_meets_policy(password):
    return len(password) >= 8 and bool(re.search(r'[A-Za-z]', password)) and bool(re.search(r'\d', password))


def verify_password(stored_password, password):
    if not stored_password:
        return False
    if stored_password.startswith(('$2a$', '$2b$', '$2y$')):
        return bcrypt.checkpw(password.encode('utf-8'), stored_password.encode('utf-8'))
    if LEGACY_SHA256_RE.fullmatch(str(stored_password)):
        return hashlib.sha256(password.encode('utf-8')).hexdigest() == stored_password.lower()
    return False


def _now_iso():
    return datetime.utcnow().isoformat(timespec='seconds')


def format_beijing_time(value):
    if not value:
        return ''
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return str(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo('UTC'))
    beijing_time = parsed.astimezone(ZoneInfo('Asia/Shanghai'))
    return beijing_time.strftime('%Y-%m-%d %H:%M:%S')


def _bool_to_int(value):
    return 1 if bool(value) else 0


def _int_to_bool(value):
    return bool(value) if value is not None else False


class AttachmentRefParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.items = []
        self.seen = set()

    def _add(self, path, kind, position):
        path = str(path or '').strip()
        if not path.startswith('/static/uploads/'):
            return
        key = (path, kind)
        if key in self.seen:
            return
        self.seen.add(key)
        self.items.append({'path': path, 'filename': os.path.basename(path), 'kind': kind, 'position': position})

    def handle_starttag(self, tag, attrs):
        attrs_map = {k.lower(): (v or '') for k, v in attrs}
        if tag == 'img':
            self._add(attrs_map.get('src', ''), 'image', len(self.items))
        elif tag == 'a':
            self._add(attrs_map.get('href', ''), 'file', len(self.items))


def _extract_attachments(answer_html, fallback_image='', fallback_images=None):
    parser = AttachmentRefParser()
    parser.feed(str(answer_html or ''))
    parser.close()
    seen = set()
    items = []
    for it in parser.items:
        path = str(it.get('path', '') or '').strip()
        if not path or path in seen:
            continue
        seen.add(path)
        items.append({'path': path, 'filename': os.path.basename(path), 'kind': it.get('kind', 'file'), 'position': len(items)})
    fallback_items = [fallback_image] + list(fallback_images or [])
    for fallback in fallback_items:
        fallback = str(fallback or '').strip()
        if fallback.startswith('/static/uploads/') and fallback not in seen:
            seen.add(fallback)
            items.append({'path': fallback, 'filename': os.path.basename(fallback), 'kind': 'image', 'position': len(items)})
    image_paths = [item['path'] for item in items if item['kind'] == 'image']
    return items, image_paths
