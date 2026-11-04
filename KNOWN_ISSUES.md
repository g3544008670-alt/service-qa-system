# Known issues and technical debt

**English** · [中文](#中文)

What is still wrong, ordered by how much it hurts. Some of it is a design bet I would make again, some is work I have not got to, and one is a data problem that a better model will not fix.

---

## 1. Pure vector search loses proper nouns

Terminal numbers, wire labels and model codes are where this breaks. Two of the three misses in the 50-query evaluation were exactly that: "which terminals are R/S/T" gets asked as "where does the incoming line go", and a 768-d embedding cannot tell those apart. A keyword channel would, which is the argument for BM25 next to the vectors, fused on score. The other option is entity extraction — pull the model code and terminal label out first and use them as filters.

Both cost something. A second index means a second thing to keep in sync, and fusion weights need evaluation data instead of guessing.

## 2. The corpus contradicts itself

Two rows answer the same question about swapping a braking resistor, one with 220 Ω and one with 120 Ω. The demo generator puts the parameter in the answer text rather than the question, so the two questions came out nearly identical while the answers disagree.

Fix at ingest: near-duplicate detection (vector proximity plus edit distance) and a human decision when they conflict. Until then this is a data problem, and swapping in a stronger model changes nothing.

## 3. No template inheritance

Nineteen templates, zero `{% extends %}`. The header skeleton is copied into thirteen of them, `base.css` has 95 `!important`, and the breakpoints use seven different values.

It grew page by page from a single screen, and by the time it was obvious, changing it meant touching everything. Extracting `base.html` and one breakpoint scale is the right move, and it needs a regression pass over four roles. I wrote it down instead of doing it, because at this size the rewrite costs more than it saves.

## 4. List pages crawl at a few thousand rows

`/manage_qa` takes 12–22 seconds at 3,000 rows. Renumbering display numbers is one `UPDATE` per row, so N rows means N round trips, and import encodes inside the request as well.

Two composite indexes — `(tenant_id, approved, id DESC)` and `(tenant_id, display_no DESC)` — plus nginx gzip and TLS session caching took the page from 35 s to 2.4 s. The remaining fix is `UPDATE ... FROM (VALUES ...)` in one statement, and moving import to a background job with a progress endpoint.

## 5. The connection pool never did anything

Every request opens a physical connection: 3–8 ms locally, 20–40 ms across the network. `psycopg2`'s `ThreadedConnectionPool._putconn` closes the connection instead of returning it whenever the idle count is at or above `minconn`, so the pool empties itself and every request pays for a handshake.

Either move to `psycopg_pool.ConnectionPool` or set `minconn` to 0 and return connections by hand. I found this by timing the same endpoint twice and not believing the second number.

## 6. The deployment script had credentials in it

An early `deploy_centos.py` carried the server address and the root password, and used `sed` to set `pg_hba.conf` to `trust`. That file is gone from the repository; deployment is a written procedure now.

The lesson is not subtle: anything that can log into the production box does not belong in the same directory as the application, and `trust` hands the database to every process on the host.

## 7. Default passwords for the first accounts

`db.py` used to hard-code a super-admin password and reset the demo accounts to it on every start. Now the passwords come from the environment; if unset, one is generated and printed once, and nothing plaintext sits in the repository.

Still wrong: "reset on start" should be "write on first creation", otherwise a password someone changed quietly reverts on the next restart.

## 8. Model loading at startup

The model used to load lazily, fall back to a `sha256` pseudo-vector when it failed, and cache that failure for the life of the process — so a bad start stayed bad until a restart. It now loads eagerly and exits on failure.

What is missing is a health signal. `/healthz` should report whether the model and the index are actually usable, and something outside the process should watch it. Right now you find out from a 504.

## 9. Smaller things

- The rich-text sanitiser is an allowlist, but `a[href]` does not check the scheme, so a `javascript:` URL could be stored, and nothing re-checks on read.
- `/debug_model` once wrote its auth check as `and not app.debug`, which is always false when `debug=True` — the debug flag switched authentication off. It now requires a session with the admin role.
- Attachment upload did not force the current tenant early on, which allowed reading another tenant's files.
- No automated tests. A script walks a four-account by nine-route permission matrix after changes.
- No CI, no formatter, no staged rollout.

## 10. The evaluation set is not published

The numbers in the README cannot be reproduced from this repository, because the 50 queries and the scorer are not in it. I wrote the queries against a client's corpus by hand, and there is still domain vocabulary in them I have not finished stripping.

The fix is a 50-query set written against the demo corpus plus a `make eval` target, so anyone can rerun it end to end. Until that exists, treat 94% / 96% / 0-10 / 68 ms as my own measurement, not a benchmark.

---

**Three things I would do first if I started over**: turn the permission matrix into tests, put model and index health on the startup path, and make import a background job instead of work inside a request.

---

## 中文

**中文** · [English](#known-issues-and-technical-debt)

还剩什么问题，按影响从大到小排。有几条是我现在还会这么选的设计赌注，有几条是没腾出手做的活，还有一条是换更强的模型也治不好的数据问题。

### 1. 纯向量在专有名词上会丢

端子号、线号、型号这几类东西最容易翻车。50 条评测里三条没命中，两条就是这么丢的："R/S/T 接哪"被客户说成"进线接哪几个端子"，768 维向量分不出这两句的差别，而关键词通道能。所以要么在向量旁边加一路 BM25、按分数融合，要么先做实体抽取，把型号和端子号拎出来当过滤条件。

两条路都有代价：多一路索引就多一份要同步的东西，融合权重得靠评测数据定，不能拍脑袋。

### 2. 语料自己打架

同一个"制动电阻能不能直接换"的问题，两行答案一个写 220Ω、一个写 120Ω。演示数据生成时把参数写在答案里而不是问题里，两条问句就长得几乎一样，答案却不一样。

要在入库时治：近重复检测（向量距离加编辑距离），冲突了强制人工裁决。在那之前这是数据问题，换模型没用。

### 3. 模板没有继承

19 个模板，0 处 `{% extends %}`。头部骨架复制在 13 个文件里，`base.css` 有 95 处 `!important`，断点用了 7 个不同的值。

它本来是一个页面，后来一页一页长出来，等看出来的时候，改一次要动全部。抽 `base.html`、统一断点是对的，但要回归四个角色。以现在的体量，重写的成本大于收益，所以我选择写下来。

### 4. 几千行数据时列表页就爬不动

3,000 行时 `/manage_qa` 要 12–22 秒。展示号重排是逐行 `UPDATE`，N 行就是 N 次往返，导入时又在请求里同步编码。

加了两个复合索引——`(tenant_id, approved, id DESC)` 和 `(tenant_id, display_no DESC)`——再加上 nginx 的 gzip 和 TLS 会话缓存，页面从 35 秒降到 2.4 秒。剩下的活是改成一条 `UPDATE ... FROM (VALUES ...)`，以及把导入挪到后台任务加进度查询。

### 5. 连接池其实一直没生效

每个请求都在新建物理连接：本地 3–8ms，跨网 20–40ms。`psycopg2` 的 `ThreadedConnectionPool._putconn` 在空闲连接数不小于 `minconn` 时直接 `close()` 而不是放回池子，于是池子自己把自己清空，每个请求都付一次握手钱。

要么换 `psycopg_pool.ConnectionPool`，要么把 `minconn` 设成 0、手动归还。这个是连着测两次同一个接口、第二次的数字不对劲才翻出来的。

### 6. 部署脚本里带着凭据

早期有个 `deploy_centos.py`，写着服务器地址和 root 口令，还用 `sed` 把 `pg_hba.conf` 改成 `trust`。这个文件已经从仓库删掉，部署改成了一份写下来的步骤。

教训不复杂：能登生产机的东西不该和业务代码住同一个目录；`trust` 等于把数据库交给这台机器上的任何进程。

### 7. 初始账号的默认口令

`db.py` 以前写死超管口令，而且每次启动都把演示账号重置回默认值。现在只从环境变量读，没设就随机生成、打印一次，仓库里没有明文。

还没改对的是"启动即重置"——应该只在首次创建时写入，否则谁改过口令，下次重启就悄悄变回去了。

### 8. 启动期的模型加载

模型以前是懒加载，失败就退化成 `sha256` 伪向量，还把失败结果缓存住，一个进程里永远不会再试——开局不好就一直不好。现在启动时强制加载，失败直接退出。

缺的是健康信号。`/healthz` 应该能回答"模型和索引现在到底能不能用"，并由进程外的东西盯着。现在是从 504 上知道出事了。

### 9. 零碎问题

- 富文本净化是白名单，但 `a[href]` 没校验协议，`javascript:` 理论上能存进去，读出来时也不复检。
- `/debug_model` 曾把鉴权写成 `and not app.debug`，`debug=True` 时恒为假——调试开关把鉴权短路了。现在必须登录且角色是管理员。
- 附件上传早期没强制当前租户，能读到别家的文件。
- 没有自动化测试。改完靠一个脚本跑"四账号 × 九路由"的权限矩阵。
- 没有 CI、没有格式化约束、没有灰度。

### 10. 评测集没公开

README 里那些数字没法从这个仓库复现，因为 50 条问句和评分脚本都不在里面。问句是我照着客户语料手写的，里面还有一些领域词我没脱敏干净。

要做的是基于演示语料重写 50 条，再加一个 `make eval`，让别人能从导入跑到出分。在那之前，94% / 96% / 0-10 / 68ms 就当是我自己的测量，不是基准。

---

**要是重来一次，我先做三件事**：把权限矩阵写成测试、把模型和索引的健康检查放进启动路径、把导入做成后台任务而不是请求里的同步活。
