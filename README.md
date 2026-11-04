# Service QA System — multi-tenant knowledge-base QA for industrial equipment

**English** · [中文](#中文)

A knowledge-base QA system for small industrial equipment service companies. Staff turn the questions customers keep asking into reviewed question/answer pairs, so the next customer can ask in their own words and still land on the right answer.

`44 routes` · `19 page templates` · `7 tables` · `~3,000 LOC Python` · `3,000-document demo corpus` · `94% strict top-1` · `P50 68 ms`

> My first project built end to end. The requirements, the stack choices, the acceptance criteria and the debugging are mine; the code came out of AI pair-programming. What I traded away is under [Design trade-offs](#design-trade-offs), what is still broken is in [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

> Everything here is code plus made-up data. `seed_demo.py` seeds 300 rows so the thing runs out of the box, and the 3,000-document corpus I measured on was imported into a second tenant. No customer data, no server address, no passwords. The live instance needs an account, so mail me if you want to be walked through it.

---

## The problem

Industrial after-sales is mostly repetition. The same fault code arrives in ten different phrasings, and a keyword FAQ only answers the phrasing it was written for: "what can this drive actually do" will not match "which functions does this model support".

So the question has to be normalised before anything is compared. Encode it, take the nearest neighbours, and if nothing clears the threshold, say so. There is no generation step. These answers are equipment parameters, and a plausible-sounding wrong number is worse than no answer.

## What it does

| Role | Capabilities |
|---|---|
| Customer | Register (needs approval), ask, read question history, receive staff replies |
| Staff | Ask, author answer pairs (behind an admin switch), reply to customers, bulk import |
| Admin | Curate the knowledge base, approve staff entries and customer registrations, manage accounts, read the operation log, brand the tenant |
| Super admin | Cross-tenant: create and delete tenants, enter any tenant to troubleshoot |

A few behaviours worth calling out:

- One answer, many phrasings. "What can it do / what is it for / which functions" should all hit the same entry.
- Below the threshold the API returns an empty answer. It does not ask a model to try harder.
- Customer questions and staff edits both start as pending. Nothing reaches another customer before a person approves it.
- Every retrieval, list and attachment query carries a tenant filter. Tenants also get their own name and logo.
- One session per account. Signing in elsewhere kills the previous session.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Web | Flask (Jinja2 templates + vanilla JS) | Single-machine deployment, server-rendered pages, no front-end build chain |
| Database | PostgreSQL 16 | Reached via JSON file → SQLite → PG; chosen for concurrent writes and indexes |
| Vector store | ChromaDB (local persistence, cosine) | Embedded, so there is no extra service to operate |
| Embeddings | text2vec-base-chinese (768-d, offline) | Chinese semantics, small enough to run on 4 cores |
| Auth | Flask session + bcrypt + CSRF token | Matches server-rendered templates; no SPA split |
| Deployment | nginx reverse proxy + HTTPS, gunicorn ready | Starts as a single process, scales when needed |

## Architecture

```
browser ──HTTPS──> nginx ──> Flask (app.py)
                              ├── db.py      psycopg2 + pool → PostgreSQL 16
                              ├── vector.py  ChromaDB + text2vec → vector retrieval
                              └── utils.py   rich-text sanitising / password hashing / time formatting
```

Request path for a question:

```
POST /chat
  → read tenant_id from the session
  → encode the question with text2vec
  → ChromaDB top-5 (filtered by tenant)
  → drop anything below the similarity threshold
  → for a hit, fetch the answer text and attachments from PostgreSQL
  → return {answer, images, score}
```

## Quick start

Requires Python 3.10, PostgreSQL 16, and one-time access to the model cache (offline afterwards).

```bash
# 1. create the database
createdb knowledge_base

# 2. dependencies
pip install -r backend/requirements.txt

# 3. configuration
cp backend/.env.example backend/.env      # fill in SECRET_KEY and DATABASE_URL

# 4. schema + demo data (300 made-up Q&A pairs and three demo accounts)
python backend/seed_demo.py

# 5. run
cd backend && python app.py               # http://127.0.0.1:5000
```

The first start builds the vector index in a background thread; a few hundred rows take seconds. `seed_demo.py` prints the demo passwords, so change them before anything is exposed.

About the model: the first load needs HuggingFace, after which `HF_HUB_OFFLINE=1` keeps it offline. On a machine with no network and without that variable, startup sits on a TCP connect to huggingface.co until it times out. That is what took the production site down once — KNOWN_ISSUES 8 has the stack.

Windows + Conda: `conda create -n qa310 python=3.10 -c conda-forge`, then `conda install -c conda-forge openssl ca-certificates` if you hit `SSL module is not available`.

## Migrating an existing SQLite database

The project started on SQLite. The migration script rewrites question IDs into a contiguous `1..N` and remaps every `question_id` in attachments, operation logs and customer replies.

```bash
cp app.db app.db.bak                                        # back up first
export DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/knowledge_base"
python backend/scripts/migrate_sqlite_to_postgres.py        # target database must be empty
python backend/scripts/migrate_sqlite_to_postgres.py --force  # only if you accept overwriting
```

It runs once, not twice: aim it at a populated target and it dies on primary keys, which is why `--force` truncates first. Unset `DATABASE_URL` and the app falls back to SQLite.

## Design trade-offs

1. **Retrieve only, never generate.** Every answer on screen was written and approved by a person. One wrong parameter can burn a customer's motor. The bill comes due when someone asks something the corpus has never seen, and then "not found" is the correct output.
2. **Fail at startup instead of degrading.** If the embedding model will not load, the process exits. An earlier version fell back to sha256 pseudo-vectors; the results looked like search results and meant nothing.
3. **One database with a tenant column, not one database per tenant.** A dozen customers do not justify the operations work. The price is that every query has to remember `tenant_id`, and forgetting it is the easiest bug in this codebase.
4. **The super admin crosses tenants on purpose.** It renders the admin views with another tenant's context, so I can debug a customer's problem without their password. It still lives in the same trust domain as everyone else (same host, same login form, same cookie jar), so its password only comes from the environment.
5. **Approval sits in the path.** Customer questions and staff additions stay pending until a person publishes them. Slower than publishing first and cleaning up later, and that is the point.

## Results

50 colloquial paraphrases, written by hand against the 3,000-document demo corpus:

| Metric | Result |
|---|---|
| Strict top-1 hit rate | 47/50 = 94% |
| Including near-duplicates | 48/50 = 96% |
| Out-of-domain questions answered | 0/10 (all refused) |
| Retrieval latency | P50 68 ms / P95 88 ms |

47 of 50 land on the right entry at rank 1, 48 with near-duplicates counted, and all ten out-of-domain questions came back refused. Retrieval is 68 ms at P50, 88 ms at P95.

The three misses are worth more than the score. Two are proper nouns: "which terminals are R/S/T", rephrased as "where does the incoming line go", loses on vectors and would survive a keyword channel. The third is two near-identical rows in the corpus that disagree about a braking resistor value, 220 Ω in one and 120 Ω in the other. One of those is a retrieval problem, one is a data problem, and the second kind does not go away with a bigger model.

The 50 queries and the scorer are not in the repo yet. KNOWN_ISSUES 10 says why.

## Layout

```
backend/
  app.py            routes and business logic (44 routes)
  db.py             pool, schema, CRUD
  vector.py         vector-store sync and retrieval
  utils.py          rich-text sanitising, passwords, time
  templates/        19 Jinja templates (+1 shared partial)
  static/           styles and front-end scripts
  scripts/          model download, SQLite → PostgreSQL migration
  seed_demo.py      demo data
KNOWN_ISSUES.md     known issues and technical debt
```

## License

MIT

---

## 中文

**中文** · [English](#service-qa-system--multi-tenant-knowledge-base-qa-for-industrial-equipment)

面向中小工业设备服务商的知识库问答系统。员工把客户反复问的问题整理成审核过的问答对，下一个客户用自己的说法问，也能落到同一条答案上。

`44 条路由` · `19 个页面模板` · `7 张表` · `约 3,000 行 Python` · `3,000 条演示语料` · `94% 严格 top-1` · `P50 68 ms`

> 这是我第一个从零做完的项目。需求、选型、验收标准和排障是我做的，代码是跟 AI 结对写出来的。放弃了什么写在[设计取舍](#设计取舍)，还有什么没修写在 [KNOWN_ISSUES.md](KNOWN_ISSUES.md)。

> 仓库里只有代码和编出来的数据。`seed_demo.py` 灌 300 条让系统开箱能跑，下面评测用的 3,000 条语料是导进第二个租户的。没有客户数据、没有服务器地址、没有口令。线上环境要账号，想点一遍的话邮件找我。

### 它解决什么问题

工业售后大部分是重复劳动：同一个故障码，十个人有十种问法，而关键词 FAQ 只认得它被写下来的那一种——客户问"这变频器能干什么"，搜不到"该型号支持哪些功能"。

所以问题在比较之前得先归一化：编码成向量、取最近的几条、相似度不过阈值就直接说没找到。中间没有生成环节。答案都是设备参数，一个听起来合理的错数字，比没有答案更糟。

### 功能

| 角色 | 能做什么 |
|---|---|
| 客户 | 注册（需审批）、提问、查看历史问答、接收员工回复 |
| 员工 | 提问、录入问答（受管理员开关控制）、回复客户、批量导入 |
| 管理员 | 知识库增删改、审核员工录入与客户注册、账号管理、操作日志、品牌定制 |
| 超级管理员 | 跨租户管理：建租户、删租户、进入任意租户排障 |

几个值得单独说的行为：

- 一句话多种问法都要命中。"能做什么 / 有什么用 / 有哪些功能"应该落到同一条。
- 相似度不过线就返回空答案，不让模型再想想。
- 客户提问、员工录入都是待审状态。没人点头之前，别的客户看不到。
- 检索、列表、附件都带租户条件；租户还能改自己的名字和 Logo。
- 一个账号同时只有一个会话，别处登录会把前一个踢掉。

### 技术栈

| 层 | 选型 | 为什么 |
|---|---|---|
| Web | Flask（Jinja2 模板 + 原生 JS） | 单机部署、模板直出，不需要前端构建链 |
| 数据库 | PostgreSQL 16 | 从 JSON 文件 → SQLite → PG 迁移而来，看中的是并发写与索引 |
| 向量库 | ChromaDB（本地持久化，cosine） | 单机嵌入式，不必额外运维一个服务 |
| 嵌入模型 | text2vec-base-chinese（768 维，离线） | 中文语义、体量小、可在 4 核机器上跑 |
| 认证 | Flask session + bcrypt + CSRF token | 与模板渲染方式匹配，不引入前后端分离 |
| 部署 | nginx 反代 + HTTPS + gunicorn 预留 | 单进程起步，按需扩 |

### 架构

```
浏览器 ──HTTPS──> nginx ──> Flask(app.py)
                              ├── db.py      psycopg2 + 连接池 → PostgreSQL 16
                              ├── vector.py  ChromaDB + text2vec → 向量检索
                              └── utils.py   富文本净化 / 密码哈希 / 时间格式
```

请求链路（提问）：

```
POST /chat
  → 取 session 里的 tenant_id
  → text2vec 编码问题
  → ChromaDB 取 Top-5（按租户过滤）
  → 相似度 < 阈值则丢弃
  → 命中则回表取答案与附件
  → 返回 {answer, images, score}
```

### 快速开始

要求：Python 3.10、PostgreSQL 16、能访问一次模型缓存（之后可完全离线）。

```bash
# 1. 建库
createdb knowledge_base

# 2. 依赖
pip install -r backend/requirements.txt

# 3. 配置
cp backend/.env.example backend/.env      # 填 SECRET_KEY 与 DATABASE_URL

# 4. 建表 + 演示数据（300 条编造的问答 + 三个演示账号）
python backend/seed_demo.py

# 5. 启动
cd backend && python app.py               # http://127.0.0.1:5000
```

首次启动会在后台建索引，几百条几秒钟就完了。演示口令由 `seed_demo.py` 打印，对外之前先改掉。

模型第一次加载要连 HuggingFace，之后设 `HF_HUB_OFFLINE=1` 就能离线跑。机器没外网、又没设这个变量，启动会一直卡在连 huggingface.co 上直到超时——线上那台就是这么挂的，堆栈见 KNOWN_ISSUES 第 8 条。

Windows + Conda：`conda create -n qa310 python=3.10 -c conda-forge`；若报 `SSL module is not available`，再补一句 `conda install -c conda-forge openssl ca-certificates`。

### 迁移已有的 SQLite 数据库

项目最初跑在 SQLite 上。迁移脚本会把问题 ID 重排成连续的 `1..N`，顺手把附件、操作日志、客户回复里的 `question_id` 一起改掉。

```bash
cp app.db app.db.bak                                        # 先备份
export DATABASE_URL="postgresql://user:pass@127.0.0.1:5432/knowledge_base"
python backend/scripts/migrate_sqlite_to_postgres.py        # 目标库必须为空
python backend/scripts/migrate_sqlite_to_postgres.py --force  # 确认可覆盖时才用
```

它只能跑一次。对着已有数据的目标库再跑一遍会撞主键，所以 `--force` 开头先 TRUNCATE。不设 `DATABASE_URL` 就退回 SQLite。

### 设计取舍

1. **只检索，不生成。** 屏幕上每一条答案都是人写过、审过的：参数错一位，客户的电机可能就烧了。代价是语料里没有的问法答不出来，那时候"没找到"就是正确答案。
2. **启动失败好过降级运行。** 模型加载不了就直接退出。早期版本会退化成 sha256 伪向量，返回的东西看着像检索结果，其实没有任何语义。
3. **一个库加租户字段，不是一租户一库。** 十几家客户不值得那套运维。代价是每条查询都得记得带 `tenant_id`，而这个项目里最容易漏的就是它。
4. **超管故意跨租户。** 它借别的租户上下文渲染管理员页面，好让我不用客户密码就能看问题出在哪。但它和其他人住在同一个信任域里（同域名、同登录页、同 cookie），所以口令只从环境变量来。
5. **审核放在链路上。** 客户提问和员工录入都要等有人发布。比别人先发出去、出事再删要慢，但慢的就是目的。

### 效果与数据

在 3,000 条演示语料上，用 50 条手写的口语化改写问句测：

| 指标 | 结果 |
|---|---|
| 严格 top-1 命中率 | 47/50 = 94% |
| 含近重复命中 | 48/50 = 96% |
| 越界问题误答 | 0/10（全部拒答） |
| 检索延迟 | P50 68ms / P95 88ms |

50 条里 47 条排第一命中，算上近重复是 48 条，10 条域外问题全部拒答，检索 P50 68ms、P95 88ms。

三条没命中的反而更值得看。两条是专有名词："R/S/T 接哪"被说成"进线接哪几个端子"，纯向量就丢了，加一路关键词能救回来。第三条是语料里两条几乎一样的问题，一个写 220Ω、一个写 120Ω。前两条是检索问题，第三条是数据问题，换更大的模型也治不好。

50 条问句和评分脚本还没放进仓库，原因见 KNOWN_ISSUES 第 10 条。

### 目录

```
backend/
  app.py            路由与业务（44 个路由）
  db.py             连接池、Schema、CRUD
  vector.py         向量库同步与检索
  utils.py          富文本净化、密码、时间
  templates/        19 个 Jinja 模板（另有 1 个共用 partial）
  static/           样式与前端脚本
  scripts/          模型下载、SQLite→PostgreSQL 迁移
  seed_demo.py      演示数据
KNOWN_ISSUES.md     已知问题与技术债
```

### 许可

MIT
