# 开发与安装说明

日常使用见[README](../README.md)。这里保留安装、代码入口和维护命令。

## 安装

需要 Python 3.12+ 和 Node.js 22.12+。在项目目录执行：

~~~powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps -e .
Set-Location frontend
npm.cmd ci
npm.cmd run build
Set-Location ..
powershell -ExecutionPolicy Bypass -File .\start.ps1
~~~

Python 依赖版本保存在 `requirements.lock`，前端依赖版本保存在 `frontend/package-lock.json`。

模型调用（含摘要与自动标题）、向量生成和网页/PDF 原文读取默认直连，不继承 HTTP_PROXY、HTTPS_PROXY 或 ALL_PROXY。需要使用环境代理时，在 `.env` 设置 `STOCK_USE_ENV_PROXY=true` 后重启后端；默认值为 false，启动日志记录当前模式。这不会修改电脑或其他软件的代理。股票数据、公开证券名称和本机 Qdrant 保持各自的直连配置；DDGS 搜索使用该库独立的网络设置。

## 代码入口

后端使用 Python、FastAPI、LangGraph、SQLite 和 Qdrant；前端使用 TypeScript、React、Vite。

| 文件 | 职责 |
| --- | --- |
| `src/stock_research/app.py` | HTTP 接口、进展订阅和应用资源管理 |
| `src/stock_research/research.py` | 消息与工具循环、停止和恢复回复 |
| `src/stock_research/model_client.py` | 云端模型协议适配 |
| `src/stock_research/timings.py` | 阶段耗时、HTTP传输时间和终端查看入口 |
| `src/stock_research/tools.py` | 股票工具分发、搜索、网页/PDF 读取和算术计算 |
| `src/stock_research/market_models.py` | 股票基本面与跨市场行情查询参数 |
| `src/stock_research/market_catalog.py` | 品种目录、市场身份、报价单位及合约规则 |
| `src/stock_research/market_global.py` | 境外指数、境内外期货与黄金现货来源适配 |
| `src/stock_research/market_series.py` | 来源日线校验、质量标注与周月聚合 |
| `src/stock_research/market_fields.py` | 稳定财务字段及单位映射 |
| `src/stock_research/market_sources.py` | 公开F10、完整报表和腾讯K线适配 |
| `src/stock_research/market_data.py` | 数据服务生命周期、缓存与失败重试 |
| `src/stock_research/market_tools.py` | 股票工具、提示词及原始响应归档 |
| `src/stock_research/market_api.py` | 只读股票查询HTTP接口 |
| `src/stock_research/technicals.py` | MA、MACD、RSI及量价信号计算 |
| `src/stock_research/storage.py` | 模型配置、任务、进展和来源存储 |
| `src/stock_research/watchlist_source.py` | 远航版缓存发现、协议解析及本地名称字典读取 |
| `src/stock_research/security_names.py` | 公开证券名称补全、缓存与失败重试 |
| `src/stock_research/watchlist.py` | 自选后台同步、快照持久化、分页查询与模型规则 |
| `src/stock_research/watchlist_api.py` | 只读自选列表接口 |
| `src/stock_research/titles.py` | 自动标题提示词、独立后台命名与手动覆盖保护 |
| `src/stock_research/memory.py` | 长期记忆存储、版本检查和管理筛选 |
| `src/stock_research/memory_tools.py` | 记忆提示词、三个管理工具与本轮指令校验 |
| `src/stock_research/memory_api.py` | 记忆管理页面的增删改查接口 |
| `src/stock_research/embeddings.py` | 云端向量协议和脱敏诊断 |
| `src/stock_research/recall.py` | 首次分析前自动召回、近期语境与结果去重 |
| `src/stock_research/rag.py` | 历史增量编码、语义检索和原文有效性校验 |
| `src/stock_research/vector_store.py` | Qdrant 嵌入式或独立服务存储 |
| `src/stock_research/qdrant_service.py` | 官方 Windows 服务安装、启动和每日日志 |
| `src/stock_research/qdrant_migration.py` | 现有向量迁移和逐点核对 |
| `src/stock_research/config.py` | 本地配置和凭据加密 |
| `frontend/src/App.tsx` | 对话历史、任务订阅和页面状态 |
| `frontend/src/Conversation.tsx` | 提问、报告、过程、来源展示及原消息定位 |
| `frontend/src/QuestionHistory.tsx` | 独立提问列表、关键词搜索与分支导航 |
| `frontend/src/conversation_navigation.ts` | 区分定位目标与分支末尾，保留后续消息 |
| `frontend/src/Composer.tsx` | 消息输入、发送及暂停/继续操作 |
| `frontend/src/ModelPicker.tsx` | 输入框模型选择、浮层定位及键盘操作 |

模型连接使用支持工具调用的 Chat Completions 兼容接口。每条新消息使用当时选择的模型连接；已在处理或恢复的消息保留创建时的配置快照。

研究轮数默认 12 轮，最后一轮收尾；检测到正文中的未执行工具请求时额外纠正一次，仍失败则标为部分完成。轮数可以在模型连接里调整，尚未实现追加预算后继续原任务的机制。

## 本地开发与检查

分别在两个终端运行后端和前端：

~~~powershell
# 项目目录
.\.venv\Scripts\python.exe -m stock_research

# frontend 目录
npm.cmd run dev
~~~

前端开发服务会将 `/api` 请求转发到本机 8765 端口。更新正常启动使用的页面，需要在 `frontend` 目录运行 `npm.cmd run build`。

后端检查命令在项目目录执行：

~~~powershell
.\.venv\Scripts\python.exe -m ruff check src tests
New-Item -ItemType Directory -Path data/test-runs -Force | Out-Null
$testRun = 'data/test-runs/' + [guid]::NewGuid().ToString('N')
.\.venv\Scripts\python.exe -m pytest -q --basetemp=$testRun
~~~

自动化测试隔离模型和网络，不代表真实研究结论已得到验证。

## 查日志

日志按北京时间日期保存：

- `logs/research-YYYY-MM-DD.log`：服务启停、任务状态和运行异常。
- `logs/requests-YYYY-MM-DD.jsonl`：请求诊断、服务商错误、耗时和 Token 用量。

在项目目录查看最近失败：

~~~powershell
.\.venv\Scripts\python.exe -m stock_research.diagnostics --limit 10
~~~

追加 `--date YYYY-MM-DD` 可指定日期，`--all` 可包含成功记录。页面错误中的诊断编号用于定位对应记录。日志会过滤密钥和推理正文，但可能保留研究问题与原文片段。

### 流程耗时

分阶段计时只保存到每日 `requests-YYYY-MM-DD.jsonl`（`timing.*` 记录），并在运行日志中输出阶段耗时，不增加页面事件。计时使用单调时钟，按 task_id 和父子 span_id 关联，成功、失败和取消都记录结束时间；只有 started 的阶段表示尚无结束记录，可能仍在等待或被强制中断。

~~~powershell
.\.venv\Scripts\python.exe -m stock_research.timings
.\.venv\Scripts\python.exe -m stock_research.timings --task-id 任务编号
.\.venv\Scripts\python.exe -m stock_research.timings --task-id 任务编号 --json
~~~

无参数查看最近有计时的任务；可追加 `--date YYYY-MM-DD`。终端读取最近最多 200 条符合任务和日期条件的诊断记录，完整记录仍在每日文件中。父阶段包含子阶段时间，不能重复相加。覆盖任务排队、自动回忆、RAG 排队与配置、索引更新和文档编码、查询向量生成、Qdrant 查询、原文核对、上下文准备/压缩、逐轮模型及工具执行。

`model.call` 和 `embedding.request` 另含 `network_timings`：连接、TLS、请求发送、等待响应头和读取响应体。连接复用时没有握手计时；代理 CONNECT 响应不算 API 响应。非流式等待响应的时间包含服务商排队/生成及网络等待，不能由本地记录进一步分开。模型记录包含全部输入按角色统计的字符数；HTTP 200 的输出截断也保留用量、finish_reason、正文和推理字符数，不保存推理正文。旧请求无法补出原先未记录的内部计时。

## 配置与数据

环境变量使用 `STOCK_` 前缀，也可写入项目根目录的 `.env`：

| 变量 | 用途 |
| --- | --- |
| `STOCK_DATA_DIR` | 研究数据目录 |
| `STOCK_LOG_DIR` | 日志目录 |
| `STOCK_FRONTEND_DIR` | 构建后的前端目录 |
| `STOCK_REQUEST_TIMEOUT` | 模型请求超时秒数 |
| `STOCK_MARKET_DATA_TIMEOUT` | 一次股票数据查询的总超时，默认30秒，范围3–120秒 |
| `STOCK_QDRANT_URL` | 留空使用本地模式；设置后连接 Qdrant 服务 |
| `STOCK_QDRANT_API_KEY` | 远程 Qdrant 的服务密钥，可选 |
| `STOCK_THS_INSTALL_DIR` | 可选，同花顺远航版安装目录；默认从 Windows 安装登记自动发现 |
| `STOCK_THS_ACCOUNT` | 可选，bin/users 下的账号目录名；多个账号时必须指定，禁止自动混合 |
| `STOCK_WATCHLIST_POLL_INTERVAL` | 自选文件检查间隔，默认 10 秒，范围 1–300 秒 |
| `STOCK_MAX_PARALLEL_TASKS` | 同时执行的研究任务数，默认 2 |

API Key 在数据库中加密；`data/credentials.key` 是解密所需的本机密钥，备份和迁移时必须保留。不要同时运行多个后端进程共用这份数据。本版只监听本机地址，尚未实现登录鉴权。

搜索使用 DDGS；网页读取不执行 JavaScript，PDF 读取不支持扫描件 OCR。统一消息入口为 POST /api/tasks，输入 message、model_id 和可选 parent_id。模型默认可直接聊天，调用 begin_research 后才启用公司研究的原文要求。当前分支的历史提问和回复按顺序传给模型，子记录保存前次回复快照；上下文接近本地预算时压缩旧消息，保留完整长期记忆和当前问题，原始记录不删除。


## 对话管理

- `PATCH /api/conversations/{identifier}`：输入 `title`（去除首尾空白后非空，最多 80 字符），从任意提问找到根对话并同步所有分支标题。
- `DELETE /api/conversations/{identifier}`：删除整段对话，先取消后台任务，再清理每轮的 LangGraph 检查点、原文目录和业务记录；模型连接和运行诊断日志保留。

新增消息、停止、恢复、重命名和删除通过同一进程内锁协调，避免删除期间新增子消息。原文目录在删除前校验其位于数据目录的 sources 子目录。文件和两个数据库无法组成单个事务；清理失败会返回错误，业务记录保留，已经停止的回复或已经清理的归档可能需要重新研究。业务记录的最终删除在 SQLite 单个事务中完成。

新建聊天默认开启自动命名，回复完成后由 titles.py 独立请求所选模型，依据整段对话最早 3 次提问生成不超过 32 字符的标题；每条提问最多取前 1000 字符，不附带报告、长期记忆或工具。命名最多尝试 3 次，每次输出预算 512 token、超时 20 秒，并发限制为 1，不占研究工具轮数，也不等待命名结束才显示回复。快速追问会合并到同一根对话的命名任务。

tasks 表新增 title_auto、title_attempt、title_pending：分别控制自动命名、已尝试的提问数量和后台命名状态。启动迁移时旧对话关闭自动命名，保留原名；失效 pending 状态清零，保留已有标题，后续早期提问可重新尝试。手动重命名关闭整段对话的自动命名，迟到的模型结果会在写事务中被拒绝；追问从数据库继承最新标题及控制字段。自动标题同步所有分支，并使相关历史向量索引状态失效。删除时先停止回复，再取消命名任务；服务关闭也会取消命名请求。失败记录 conversation.title 诊断，保留正常回复和已有标题。

任务摘要和详情返回布尔 title_pending；前端只在有命名任务时每秒刷新列表，同时更新当前对话标题。

最近对话列表返回全部任务摘要，以免只截取最近 200 条提问时拆散较长对话或丢失根节点。前端对话管理弹窗位于 `frontend/src/ConversationDialog.tsx`。

## 长期记忆

页面为单框条目列表，新增只需 content，编辑携带 version 并保留系统已有的内部信息。GET /api/memories 支持 q、category、offset、limit；POST 新增、PUT /{id} 编辑、DELETE /{id}?version=... 删除。版本过时返回 409。

系统通过 save_memory、update_memory、delete_memory 自动保存和整理，无候选确认流程。原有 status 字段和确认接口已移除；启动时迁移旧表，保留原有条目和版本。记忆存于 research.sqlite 的 memories 表，独立于对话和模型连接。

每轮请求带入全部长期记忆，不按条数或相关性截断；过期条目仍带入并按历史参考理解。模型直接从已提供条目取得 id、version；查询记忆工具已移除。内部类别、日期和出处由系统按需维护；事实仍需原文核验。删除的条目不再自动注入，历史消息和检索快照保留。

提示词位于 memory_tools.py 的 MEMORY_PROMPT。恢复旧检查点时刷新记忆规则。前端 MemoryManager.tsx 每五秒刷新列表，编辑中的文字不被后台刷新覆盖。检索快照展示位于 MemoryUsage.tsx。记忆会随请求发送到所选云端模型。

## 对话压缩与历史检索

context.py 在每轮请求及输出纠正前估算上下文大小。模型连接的 context_budget 默认 64000，可在“模型连接”调整；旧连接未设置时使用默认值。预算包含工具定义、输出预留和消息开销，中文按字、ASCII 约三字符估算，不代表服务商精确分词。

超过输入预算的 85% 时，较早消息和工具结果压缩为摘要，优先保留近期内容。系统规则、完整长期记忆和当前问题不压缩；工具调用与对应结果作为整体，不能拆开。固定内容本身超过预算时明确报错，提示提高预算，不自动删除记忆。

压缩额外调用当前模型，单次摘要输出预算最多 1800，不占研究工具轮数。长原文分块合并，摘要保存到 context_summaries，按原文指纹和当前分支复用；修改后的回复不会误用旧摘要。取消向上传递；无效摘要不当作成功。原始提问、回复、工具进展和来源保持。

历史检索使用 Qdrant 向量语义索引，旧 FTS5 关键词索引在启动迁移时移除。search_history 支持自然语言查询、公司及 UTC 日期范围，空查询查近期；read_history 按 task_id、section（question/report/tools）、start、max_chars 分段读取；task_id=current 表示当前任务。工具结果无需预先进入长期记忆。历史检索不读取模型配置、密钥或推理正文。

历史向量以问答为逻辑单元：回复不超过 2000 字时只生成一个 Point，长回复按 2000 字分段、重叠 200 字。Payload 的 question 保存原问题，content 保存回答片段，嵌入输入和检索结果 text 都带“用户提问／模型回答”标签。section/start/end 始终对应原始回复，total_chars/next_start 用于续读；没有最终回复时只索引问题并明确标注。自动召回按 task_id 去重。编码版本 history-qa-v2 使用独立集合，首次检索自动重编码旧历史，旧集合保留但不参与新检索。

删除整段对话时，先在业务事务中清理原文、索引状态及所属摘要缓存，再清理 Qdrant 的相关向量；检索仍按现存原文核对，不返回已删除内容。原文读取返回范围、总字数和继续位置，结果明确标注为历史材料。处理过程中的 context_compression_started/context_compressed 显示进度、规模和摘要；历史工具结果可展开核查。

历史原文仍保存在 SQLite。日常工作台通过 STOCK_QDRANT_URL=http://127.0.0.1:6333 连接本机独立 Qdrant，数据保存在 `data/qdrant/storage/`，管理界面为 http://127.0.0.1:6333/dashboard。`start.ps1` 会先启动独立服务并检查首次迁移，再启动后端。程序和官方管理界面从固定版本发布包安装，解压前验证 SHA-256；服务只监听本机回环地址，日志按日期写入 `logs/qdrant-YYYY-MM-DD.log`。
原 `data/vectors/` 作为迁移前的嵌入式数据保留。迁移读取已有向量和元数据，不重新调用 embedding API；按记录核对后写入 `data/qdrant/migration.json`，以后启动不再导入旧快照。Windows 官方程序保存在 `data/qdrant/bin/`，管理界面资源在 `data/qdrant/static/`。服务退出研究后端后仍保持运行。测试强制使用各自临时目录中的嵌入式库，避免访问正式服务。
切换到其他服务器时设置 STOCK_QDRANT_URL 和可选的 STOCK_QDRANT_API_KEY。缺失的集合会在检索时补建；手动重建属于维护操作，通过后台接口 `POST /api/embeddings/reindex` 执行，日常页面不提供重建按钮。embedding 配置仍保存在业务库，密钥复用已有模型连接。自动召回、工具补查及对话压缩逻辑保持一致；请求失败可在每日诊断日志中查找 `embedding.request`。


## 自选股自动同步

自选同步由后台服务独立运行，前端不提供独立自选页面，也不依赖页面轮询维持同步；用户通过聊天按需查询。只读 API 保留用于查询与排查。自选来源与行情、财务数据源分开。当前只读同花顺远航版的 bin/users/<账号>/SelfStockCache.json 和 blockstockV3.xml，不访问登录凭据、不改写客户端文件。自动发现一个安装目录和一个缓存账号；多个安装目录或账号时须配置来源，不推测最近使用账号。

Selfstock 格式是“代码序列,市场序列”，两个序列内部用 | 分隔，数量必须一致并匹配 Count；不能把市场编号误算成证券。按市场和代码去重并保留顺序、前导零；未知市场保留原始标识。分组保持独立成员和空分组，sort_list 为十六进制、Block.id 为十进制。名称优先从本机 stocknameV2.db 和 stocks.db 以只读模式补全；Realtime 优先、Base 回退，不使用 History。常用指数按明确的市场与代码使用离线回退字典，其余缺失名称通过 security_names.py 从东方财富及新浪财经公开接口补充。未知市场或无精确匹配的品种继续显示代码，不模糊搜索。

服务启动读取一次，后台每 10 秒检查文件时间、大小和来源指纹，名称字典的 WAL 也参与变化检查。读取期间文件变化、格式不完整或权限错误保留上次成功结果；相同持续失败只记录一次诊断，原因变化或恢复后再次失败重新记录。watchlist.sync 诊断与运行日志可用于排查。取消正常向上传递，关闭时取消后台轮询。

公开名称独立使用直连 HTTP 客户端，不改变模型接口的代理设置，不需要模型或付费账户。watchlist_names 表按 market、code 缓存 name、provider、updated_at；有效期 7 天，最多每 5 分钟尝试更新一次。名称源独立失败处理，watchlist.names 记录可恢复诊断；失败保留本地及已有缓存名称。异步取消向上传递。名称补全成功即使客户端文件未变也会更新快照；API item 可包含 name_provider 与 name_updated_at，它们只是名称出处和抓取时间，不代表行情时间。

research.sqlite 的 watchlist_snapshots 表保存单份成功快照、来源指纹及 synced_at，完整快照事务替换；不进入 LangGraph 检查点或长期记忆。GET /api/watchlist 接收 group（分组编号或完整名称）、q、offset、limit（1–200）；返回 status（ok/cached/unavailable）、warning、全部分组与数量、分页 items、has_more 和来源时间。工具 get_watchlist 使用相同查询，工具调用前检查文件，AI 不从旧聊天或长期记忆推断当前自选。列表不是持仓，缺失数据不当作实时行情、财务或研究证据。

self_updated_at、groups_updated_at 是本机文件写入时间，client_modified_at 是缓存原始 ModifyTime，synced_at 是成功写入本系统的时间，checked_at 是最近检查时间；任何一个时间都不能单独证明云端实时性。电脑端未同步手机改动时，系统读到的仍是本机旧列表。前端不设置自选页面；AI查询和后台轮询不修改同花顺。行情与财务数据由独立股票工具提供，不能将自选缓存解释成行情或财务数据。

常用指数回退名称依据：[AKShare 全球指数映射](https://github.com/akfamily/akshare/blob/main/akshare/index/cons.py)、[中证全指编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208175438-000985_Index_Methodology_cn.pdf)、[北证50介绍](https://www.bse.cn/market_data/bse_indices/bse_bz50.html)。纳斯达克综合指数 IXIC 与纳斯达克100 NDX 分别处理，不能互相替代。


## 股票及跨市场数据查询

基本面继续通过get_stock_fundamentals查询公开F10公司概况、主营、财务指标和完整报表，保留标准字段与fields扩展、累计报告期、公告日、公司类型和严格市场身份校验。基本面仅限内地A股公司，字段缺失保留null。收入、利润和现金流为报告期累计值，资产负债表为期末值，当前来源版本不是历史时点数据库。

行情工具为search_market_symbols、get_market_bars和get_market_technicals；旧get_stock_bars/get_stock_technicals保留执行兼容，但新模型定义使用通用名称。GET /api/market/instruments查询目录，POST /api/market/bars与/technicals查询行情和指标；旧/api/stocks/bars与/technicals仍兼容。目录不是报价，不生成行情证据，具体合约能否查询以实际来源为准。

新增market_catalog统一境内外指数及商品身份，market_global读取腾讯境外指数、新浪全球指数、境内外期货与伦敦金，market_series按来源交易日期校验日线及聚合周月线。纳斯达克100因来源附带身份错误暂未启用，目录unavailable明确原因，不用综合指数替代。不新增SDK，数据服务使用独立直连客户端，不改变模型代理。

[完整品种、单位、接口、日期、质量和连续合约限制](market-data.md)。当前已验证46个新增规范代码的真实响应样本，覆盖日周月138次解析；在线测试了多晶硅、碳酸锂、生猪的连续与月份合约、纽约金、布伦特及代表性境外指数，不代表已逐条对照交易所原始行情。

research.sqlite的stock_data_cache保存成功结果与原始响应。公司资料24小时、主营与财务1小时、行情5分钟，失败60秒内避免重复请求；基本面回退最多30天、行情最多1天。返回stale保留fetched_at与原质量警告，无缓存失败不冒充empty。行情缓存键版本为2，公司财务保持版本1；字段裁剪不重复下载，取消正常传播。

OHLC关系异常保留原值、quality_flags与warning日志；非有限数值、重复日期、身份错误及非法格式失败并记录stock_data.fetch/cache诊断。缺失或未核实成交量不算量比，有影响的异常K线不生成突破；主力连续换月映射未核实，连续行情停用突破判断。数据按需查询，不自动下载全部自选。

行情数据和原始返回写入data/sources/<task_id>/<hash>.json并登记application/json来源；目录不归档为行情来源。研究模式仍须非JSON财报／公告原文，不能用结构化来源绕过证据要求。当前未实现实时逐笔、自动监控调度、通知或交易执行。
