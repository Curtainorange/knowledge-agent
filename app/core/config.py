"""应用配置：全部通过 pydantic-settings 从环境变量 / .env 注入。

铁律：代码内不硬编码供应商细节（型号、base_url、单价、reasoning 字段名），
一律以 .env 为准，保证接入真实 DeepSeek 时可无感校准。
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 应用 ---
    app_name: str = "认知副驾 Cognitive Copilot"
    debug: bool = False

    # --- 数据库（P0 默认 SQLite，免基础设施；production 可切 PostgreSQL）---
    database_url: str = "sqlite:///./dev.db"

    # --- DeepSeek（OpenAI 兼容协议）---
    # 未配置 DEEPSEEK_API_KEY → 网关路由到 MockProvider（确定性、零成本）
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    # 官方模型名（2026-09 起）；旧名 deepseek-v4-flash 仍可调用，会重定向到同一模型
    deepseek_model: str = "deepseek-flash"
    deepseek_timeout_seconds: float = 60.0

    # 单价（每百万 token，人民币）—— DeepSeek 官方价，2026-09-13 核对
    # 计费分时段：高峰 = 北京时间周一至周五 9:00-12:00、14:00-18:00；其余为空闲（价格减半）。
    # 下表取**空闲时段**价，高峰由 deepseek_peak_multiplier 放大，避免把单价写死成单一值。
    deepseek_price_input_per_1m: float = 1.0        # 空闲 · 输入（缓存未命中）
    deepseek_price_output_per_1m: float = 4.0       # 空闲 · 输出
    # 缓存命中输入价：仅为未命中的五十分之一。L1/L2 的 system 提示词前缀稳定，
    # 命中率不低；忽略这一项会把成本估高一个量级
    deepseek_price_cache_hit_per_1m: float = 0.02   # 空闲 · 输入（缓存命中）
    deepseek_peak_multiplier: float = 2.0           # 高峰时段倍率
    # 高峰时段（北京时间，周一至周五），格式 "9-12,14-18"
    deepseek_peak_hours: str = "9-12,14-18"

    # --- 小米 MiMo（OpenAI 兼容协议；与 DeepSeek 二选一，MiMo 优先）---
    # 未配置 MIMO_API_KEY → 回落到 DeepSeek（或 Mock）。
    # Key 在 https://platform.xiaomimimo.com 控制台获取，按量付费为 sk- 前缀；
    # 订阅版 Token Plan 是 tp- 前缀且 base_url 不同，两者不可混用。
    mimo_api_key: str = ""
    mimo_base_url: str = "https://api.xiaomimimo.com/v1"
    # 官方模型名（2026-09 经 GET /v1/models 实测确认）：
    #   快档（对应 deepseek-flash）：mimo-v2.6-flash
    #   强档：mimo-v2.6-pro / mimo-v2.6-pro-ultraspeed / mimo-v2.5-pro
    # ★ 注意：mimo-v2-flash / mimo-v2.5-flash 均**不存在**，误填会返回 400 Unsupported model。
    # 另有 asr / tts / tts-voiceclone / tts-voicedesign 音频类模型，非本场景使用。
    mimo_model: str = "mimo-v2.6-flash"
    mimo_timeout_seconds: float = 60.0

    # MiMo 单价（每百万 token，人民币）—— ⚠️ 待核对官方定价后回填；
    # 填 0 表示成本统计暂不准确（只影响成本日志，不影响功能）。
    mimo_price_input_per_1m: float = 0.0
    mimo_price_output_per_1m: float = 0.0
    # MiMo 是否有自动上下文缓存折扣待官方确认；暂按 0（与输入同价，见 cost.py 兜底）
    mimo_price_cache_hit_per_1m: float = 0.0

    # --- 模型调用健壮性（供应商回退 / 结构化输出修复）---
    # 供应商回退：主供应商重试耗尽（限流、超时、5xx）或不可重试失败（鉴权、模型名错、
    # 配额耗尽）时，自动切到链上下一个已配 KEY 的供应商继续完成这次调用。
    # 需两个 KEY 都配才生效；置 false 可关闭（失败即上抛，便于把问题暴露出来）。
    llm_fallback_enabled: bool = True
    # 结构化输出修复：JSON 任务本地校验失败时，把「破损输出 + 失败原因」回灌给模型再要一次
    # （只修复一轮）。代价是一次调用，收益是避免整条能力降级（L3 简报退化 / L2 判定丢弃）。
    llm_json_repair_enabled: bool = True

    # --- Embedding（语义检索；独立于 LLM，DeepSeek 无标准 embedding 接口）---
    # backend: local（本地 ONNX 目录，离线可用，推荐）| bge（fastembed 自动下载）
    #          | hash（确定性零依赖，测试/离线兜底）
    embedding_backend: str = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    # local 后端读取的模型目录，用 scripts/fetch_embedding_model.py 预取
    embedding_local_dir: str = "models/bge-small-zh-v1.5"

    # --- 向量库（库内检索后端；ADR-08 起步 pgvector）---
    # memory（进程内索引，P0 默认，免基础设施）| pgvector（PostgreSQL + pgvector 扩展）
    vector_backend: str = "memory"
    # pgvector 后端使用的专用表名（与业务表隔离）
    vector_store_table: str = "embeddings"

    # --- 书籍（阅读器）---
    # 上传的电子书原始文件存放目录（体积大，不入库；已加入 .gitignore）
    books_dir: str = "data/books"

    # --- 微信读书同步（官方 AI Skills 接口，只读个人阅读数据）---
    # API Key 在 https://weread.qq.com/r/weread-skills 扫码获取，格式 wrk-xxxxxxxx。
    # 留空则同步入口不可用（不影响其它功能）。**Key 等同账号访问凭据，切勿提交到仓库。**
    weread_api_key: str = ""
    # 官方技能包版本：每次请求必须上报；回包出现 upgrade_info 时需按提示升级
    weread_skill_version: str = "1.0.4"
    weread_gateway_url: str = "https://i.weread.qq.com/api/agent/gateway"
    weread_timeout_seconds: float = 20.0
    # 单次同步的条目上限：超出部分不计入，前端提示「还有 N 条待同步」
    weread_max_items_per_sync: int = 300
    # 笔记本概览的游标分页大小（官方接口无 offset 分页）
    weread_page_size: int = 50
    # 请求间隔（秒）：官方接口有频次限制，逐本拉取时留出冷却
    weread_request_interval_seconds: float = 0.3

    # --- Hugging Face 模型下载（国内走镜像，见 .env.example 说明）---
    hf_endpoint: str = ""            # 例 https://hf-mirror.com
    hf_hub_disable_xet: bool = False # 禁 Xet，强制镜像普通 HTTP 下载

    # --- 认证（JWT，见系统设计 9.3.1）---
    # 生产环境务必配置 JWT_SECRET；留空则进程内随机生成，重启后所有 token 失效
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 120   # Access Token 有效期 2 小时
    refresh_token_days: int = 7       # Refresh Token 有效期 7 天
    password_pbkdf2_iterations: int = 600000  # OWASP 推荐量级；测试中调低以提速

    # --- 登录失败限流（进程内计数；多 worker 部署时各算各的，阈值等效放大）---
    login_throttle_enabled: bool = True
    login_max_attempts: int = 5        # 按账号：窗口内失败次数上限
    login_ip_max_attempts: int = 20    # 按来源 IP：阈值放宽，避免 NAT 后的正常用户被连带
    login_window_seconds: int = 300    # 失败计数滑动窗口
    login_lock_seconds: int = 300      # 触发上限后的锁定时长
    # 注册按来源 IP 限流：每次注册都要跑 PBKDF2，是可被利用的 CPU 消耗点；
    # 统计全部注册尝试（含成功），因此阈值按「正常用户不会在窗口内注册这么多次」设
    register_max_attempts_per_ip: int = 10

    # --- L2 冲突检测（漏斗参数，初值按架构设计 §7.2，实测后校准）---
    l2_max_claims_per_item: int = 5      # 每条目最多提取的主张数
    l2_max_pairs_per_scan: int = 20      # 单次扫描最多送 LLM 判定的候选对数（成本闸门）
    l2_min_confidence: float = 0.5       # 低于此置信度的冲突直接丢弃（架构 §7.3 阈值策略）
    l2_ignore_suppress_threshold: int = 3  # 同类型冲突被忽略 ≥N 次后收敛该类推荐（UC-L2-03）
    # 候选对语义距离带（L2-2）：BGE 余弦。太近≈重复表述、太远≈无关，两端都排除；
    # 实测：相关但立场不同的主张对多落在 0.4~0.85
    l2_pair_sim_lo: float = 0.35
    l2_pair_sim_hi: float = 0.95
    # 判定复核（判断层）：低置信「矛盾」判定自动换角度重判。区间横跨丢弃阈值 0.5——
    # 下沿是被丢弃判定的翻案候选，上沿是勉强入库判定的把关对象；高置信不复核
    l2_review_enabled: bool = True
    l2_review_max_per_scan: int = 5   # 单轮扫描复核调用预算（硬闸门，超出转 pending 下轮补）
    l2_review_lo: float = 0.35
    l2_review_hi: float = 0.75

    # --- L3 认知助产（主题分布 → 「该问但没问」的追问）---
    l3_max_items_per_analysis: int = 60  # 单次分析送入模型的条目上限（成本与上下文闸门）
    l3_recent_titles: int = 20           # UC-L3-02 衔接追问时携带的近期条目标题数
    l3_question_count: int = 3           # 简报产出的追问上限（需求：2-3 个，宁少勿滥）

    # --- L4 路径修正（计划生成 + 行为偏离检测）---
    l4_idle_days_threshold: int = 3    # 连续多少天无学习行为即视为偏离（需求默认 3）
    l4_window_days: int = 7            # 行为统计窗口（本窗口 / 上一窗口对比）
    l4_spike_factor: float = 3.0       # 新增内容突增倍数（上一窗口 ≥1 条时才判）
    l4_max_tasks: int = 12             # 计划任务数上限（需求：最多 12 周）
    l4_deadline_nudge_days: int = 3    # 目标截止前多少天开始催办（自主目标追踪）

    # --- 异步任务框架（幂等 + 重试 + 死信；单进程内轮询，不依赖 Redis/Celery）---
    worker_enabled: bool = True          # 测试里关闭，避免后台线程干扰
    worker_poll_seconds: float = 15.0    # 轮询间隔
    task_max_attempts: int = 3           # 重试上限，超出转死信
    task_retry_backoff_seconds: float = 1.0  # 重试退避基数（2^(n-1) 倍，测试设 0 即时重试）
    task_stale_seconds: float = 600.0    # running 超时回收阈值（须大于最长 handler 实测时长）

    # --- 自主巡检（第二阶段）：L4 偏离日巡检 + L2 弱真值周体检 ---
    patrol_enabled: bool = True            # 巡检总开关
    l4_intervention_repeat_days: int = 3   # 持续偏离时归因/干预的最小间隔天数（防刷屏）

    # --- 事件驱动反应（第二阶段）：learning_events → 任务/推送映射 ---
    event_reaction_enabled: bool = True     # 事件反应总开关
    event_reaction_window_hours: int = 48   # 只扫窗口内的事件（限查询量，幂等键兜底重复）

    # --- 主动学习教练（第二阶段）：L3/L4/L5 建议纯本地聚合为周教练提示 ---
    coach_enabled: bool = True     # 教练周聚合开关
    coach_max_items: int = 3       # 教练正文块数上限（防刷屏；聚合固定周频不随 push_frequency 变）

    # --- L2 产物的下游消费（把冲突从「台账」变成「推理原料」）---
    # 未解冲突注入各能力时取几条：开场只取 1 条（显式传入），
    # L3 简报 / L5 诊断 / L4 计划上下文共用此上限（成本与上下文闸门）
    conflict_context_limit: int = 3
    # L4：未解冲突与学习目标的语义相关阈值（BGE 余弦）。低于它视为「与当前目标无关」，
    # 不注入计划 / 归因上下文——否则会把无关矛盾塞进计划讨论，制造噪声
    l4_conflict_relevance: float = 0.45

    # --- L2 触发链路 ---
    l2_realtime_trigger_enabled: bool = True  # 录入/划词后按小时桶合并触发扫描
    l2_weekly_scan_enabled: bool = True       # 每周自动扫描（幂等键按 ISO 周去重）

    # --- 主动推送调度（ADR-14）---
    push_schedule_enabled: bool = True        # 每周简报 / 每月健康报告定时推送（幂等键按 ISO 周/月去重）

    @property
    def model_provider(self) -> str:
        """当前启用的供应商：mimo > deepseek > mock。"""
        if self.mimo_api_key:
            return "mimo"
        if self.deepseek_api_key:
            return "deepseek"
        return "mock"

    @property
    def active_model(self) -> str:
        """当前供应商的默认模型名（网关路由回落用）。"""
        provider = self.model_provider
        if provider == "mimo":
            return self.mimo_model
        if provider == "deepseek":
            return self.deepseek_model
        # Mock：名字只进日志与成本表，写实比借用别的供应商模型名更清楚
        return "mock"


settings = Settings()