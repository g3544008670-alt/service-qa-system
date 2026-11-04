"""
向量搜索模块：ChromaDB + SentenceModel
完全离线运行，不依赖网络。
"""
import os
import hashlib
import threading
import numpy as np
from text2vec import SentenceModel
import chromadb

# ── 模型配置 ──
MODEL_NAME = "shibing624/text2vec-base-chinese"
FALLBACK_VECTOR_SIZE = 768
_sentence_model = None
_sentence_model_load_error = None
_INDEX_LOCK = threading.Lock()

# ── 路径（由 app.py 在初始化时设置） ──
CHROMA_PATH = None
CHROMA_HTTP_HOST = None
CHROMA_HTTP_PORT = None


def init_vector_search(base_dir, http_host=None, http_port=None):
    """初始化向量搜索路径（在 app 启动时调用）。
    设置 http_host 则连接远程 ChromaDB 服务（独立进程，适合生产）。
    不设置则使用嵌入式文件存储（默认，适合单机）。
    """
    global CHROMA_PATH, CHROMA_HTTP_HOST, CHROMA_HTTP_PORT
    CHROMA_PATH = os.path.join(base_dir, 'chroma_db')
    CHROMA_HTTP_HOST = http_host
    CHROMA_HTTP_PORT = http_port
    if http_host:
        print(f"[INFO] ChromaDB HTTP mode: {http_host}:{http_port}")
    else:
        print(f"[INFO] ChromaDB file mode: {CHROMA_PATH}")


def get_sentence_model():
    global _sentence_model, _sentence_model_load_error
    if _sentence_model is not None:
        return _sentence_model
    if _sentence_model_load_error is not None:
        return None
    try:
        _sentence_model = SentenceModel(MODEL_NAME)
        return _sentence_model
    except Exception as exc:
        _sentence_model_load_error = exc
        print(f"[WARN] sentence model unavailable: {exc}")
        return None


def encode_text(text):
    model = get_sentence_model()
    if model is not None:
        return model.encode(text)

    normalized = str(text or "").strip()
    if not normalized:
        return np.zeros(FALLBACK_VECTOR_SIZE, dtype=np.float32)

    values = []
    counter = 0
    while len(values) < FALLBACK_VECTOR_SIZE:
        digest = hashlib.sha256(f"{normalized}|{counter}".encode("utf-8")).digest()
        values.extend(((byte / 255.0) * 2.0 - 1.0) for byte in digest)
        counter += 1

    vector = np.array(values[:FALLBACK_VECTOR_SIZE], dtype=np.float32)
    norm = np.linalg.norm(vector)
    if norm > 0:
        vector = vector / norm
    return vector


_chroma_collection = None
_chroma_lock = threading.Lock()


def _get_chroma_collection():
    """进程内共享的 Chroma collection（每请求新建 client 会重复打开 sqlite，导致请求挂死）。"""
    global _chroma_collection
    if _chroma_collection is not None:
        return _chroma_collection
    with _chroma_lock:
        if _chroma_collection is not None:
            return _chroma_collection
        if CHROMA_HTTP_HOST:
            client = chromadb.HttpClient(host=CHROMA_HTTP_HOST, port=CHROMA_HTTP_PORT or 8000)
        else:
            client = chromadb.PersistentClient(path=CHROMA_PATH)
        _chroma_collection = client.get_or_create_collection(
            name="qa_questions", metadata={"hnsw:space": "cosine"})
        return _chroma_collection


def _rebuild_all_chroma_index():
    """全量重建 ChromaDB 索引"""
    from db import _get_conn
    conn = _get_conn()
    # 读取所有问答
    rows = conn.execute(
        'SELECT q.id, q.question, q.answer_html, q.answer, q.public, q.approved, q.category, q.tenant_id, q.vector '
        'FROM qa_pairs q ORDER BY q.tenant_id, q.id'
    ).fetchall()
    conn.close()

    questions = []
    for row in rows:
        questions.append(dict(row))

    # 按租户分组重建
    from collections import defaultdict
    by_tenant = defaultdict(list)
    for q in questions:
        by_tenant[q['tenant_id']].append(q)

    collection = _get_chroma_collection()
    # 清空后重建
    try:
        all_ids = collection.get()['ids']
        if all_ids:
            collection.delete(ids=all_ids)
    except Exception:
        pass

    for tenant_id, tenant_questions in by_tenant.items():
        _sync_chroma_tenant(tenant_id, tenant_questions)


def _sync_chroma_tenant(tenant_id, questions):
    """同步某个租户的问答到 ChromaDB"""
    collection = _get_chroma_collection()
    ids = []
    embeddings = []
    documents = []
    metadatas = []
    # 批量编码：先收集需要编码的文本，一次 encode，避免逐条调用
    pending, pending_idx = [], []
    vectors = [None] * len(questions)
    for i, qa in enumerate(questions):
        if not isinstance(qa, dict):
            continue
        q_vector = qa.get('vector')
        if isinstance(q_vector, list) and q_vector:
            vectors[i] = q_vector
        else:
            q = str(qa.get('question', '')).strip()
            if q:
                pending.append(q)
                pending_idx.append(i)
    if pending:
        model = get_sentence_model()
        if model is not None:
            encoded = model.encode(pending, batch_size=64)
            for k, i in enumerate(pending_idx):
                vectors[i] = encoded[k].tolist()
        else:
            for k, i in enumerate(pending_idx):
                vectors[i] = encode_text(pending[k]).tolist()

    for i, qa in enumerate(questions):
        if not isinstance(qa, dict):
            continue
        qid = qa.get('id')
        question = str(qa.get('question', '')).strip()
        if not question or not isinstance(qid, int):
            continue
        vector = vectors[i]
        ids.append(f'{int(tenant_id)}:{int(qid)}')
        embeddings.append(vector)
        documents.append(question)
        metadatas.append({
            'question_id': int(qid),
            'tenant_id': int(tenant_id),
            'public': int(bool(qa.get('public', True))),
            'approved': int(bool(qa.get('approved', True))),
            'category': str(qa.get('category', '未分类') or '未分类'),
        })
    if ids:
        collection.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)


def sync_single_qa(tenant_id, qa_id):
    """增量同步单条问答到 ChromaDB"""
    from db import _get_conn
    from utils import _int_to_bool
    conn = _get_conn()
    row = conn.execute(
        'SELECT id, question, answer_html, answer, public, approved, category, tenant_id, vector '
        'FROM qa_pairs WHERE id = %s AND tenant_id = %s', (int(qa_id), int(tenant_id))
    ).fetchone()
    conn.close()
    if not row:
        return
    qa = dict(row)
    _sync_chroma_tenant(tenant_id, [qa])


def delete_single_qa(tenant_id, qa_id):
    """增量删除 ChromaDB 中的单条索引"""
    try:
        collection = _get_chroma_collection()
        collection.delete(ids=[f'{int(tenant_id)}:{int(qa_id)}'])
    except Exception:
        pass


def _compute_qa_id_signature(conn=None):
    """计算当前所有问答的签名（用于检测索引是否过期）"""
    if conn is None:
        from db import _get_conn
        conn = _get_conn()
        close_conn = True
    else:
        close_conn = False
    rows = conn.execute('SELECT tenant_id, id FROM qa_pairs ORDER BY tenant_id, id').fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(f"{int(row['tenant_id'])}:{int(row['id'])}|".encode('utf-8'))
    if close_conn:
        conn.close()
    return digest.hexdigest()


def bootstrap_chroma_index():
    """启动时校验并重建 ChromaDB 索引（后台线程运行）。

    只按「条数」判断是否需要重建：向量库返回顺序不稳定，逐 id 摘要比较会在
    每次启动时误判为过期，触发全量重建（数千条要十几分钟，期间检索全部超时）。
    增删改已在写路径上做增量同步，这里只需兜底“数量不一致”这一种情况。
    """
    from db import _ensure_schema, _get_conn
    _ensure_schema()
    conn = _get_conn()
    total_questions = conn.execute('SELECT COUNT(*) AS count FROM qa_pairs').fetchone()['count']
    conn.close()
    try:
        collection = _get_chroma_collection()
        current = collection.count()
        if current != total_questions:
            print(f'[INFO] ChromaDB {current} != QA {total_questions}, rebuilding...', flush=True)
            _rebuild_all_chroma_index()
        else:
            print(f'[INFO] ChromaDB index ok ({current} vectors)', flush=True)
    except Exception as exc:
        print(f'[WARN] ChromaDB bootstrap failed: {exc}', flush=True)


def get_answer(question, user, threshold=0.78):
    """向量搜索最佳匹配答案"""
    from utils import normalize_answer_content, _int_to_bool
    if not question or not str(question).strip():
        return None, 0, "问题不能为空"

    tenant_id = user.get('tenant_id') if user else None
    query_text = str(question).strip()

    try:
        query_embedding = encode_text(query_text)
        collection = _get_chroma_collection()
        where_filter = {'tenant_id': int(tenant_id)} if tenant_id else {}
        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=5,
            where=where_filter,
            include=['documents', 'distances', 'metadatas'],
        )
    except Exception as exc:
        return None, 0, f"search error: {exc}"

    if not results or not results.get('ids') or not results['ids'][0]:
        return None, 0, None

    from db import _get_conn
    conn = _get_conn()
    current_tenant = tenant_id
    try:
        for i in range(len(results['ids'][0])):
            distance = results['distances'][0][i]
            score = max(0.0, 1.0 - distance / 2.0)
            if score < threshold:
                continue
            meta = results['metadatas'][0][i]
            if current_tenant is not None and int(meta.get('tenant_id', 0)) != int(current_tenant):
                continue
            if not _int_to_bool(meta.get('approved', 1)):
                continue
            qid = int(meta.get('question_id', 0))
            if not qid:
                continue
            row = conn.execute(
                'SELECT q.*, (SELECT json_agg(row_to_json(a)) FROM attachments a WHERE a.question_id = q.id) AS attachments_json_raw '
                'FROM qa_pairs q WHERE q.id = %s', (qid,)
            ).fetchone()
            if row:
                from utils import _extract_attachments
                attachments, images = _extract_attachments(row['answer_html'] or '', row['image'] or '')
                answer_html, plain_text = normalize_answer_content(row['answer_html'], row['answer'])
                return {
                    'id': qid,
                    'question': row['question'],
                    'answer': plain_text,
                    'answer_html': answer_html,
                    'image': row.get('image') or (images[0] if images else ''),
                    'images': images,
                    'category': row.get('category', '未分类'),
                    'score': score,
                    'public': _int_to_bool(row.get('public', True)),
                }, score, None
    finally:
        conn.close()
    return None, 0, None
