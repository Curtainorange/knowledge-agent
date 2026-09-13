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

    # --- Embedding（语义检索；独立于 LLM，DeepSeek 无标准 embedding 接口）---
    # backend: local（本地 ONNX 目录，离线可用，推荐）| bge（fastembed 自动下载）
    #          | hash（确定性零依赖，测试/离线兜底）
    embedding_backend: str = "local"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512
    # local 后端读取的模型目录，用 scripts/fetch_embedding_model.py 预取
    embedding_local_dir: str = "models/bge-small-zh-v1.5"

    # --- 书籍（阅读器）---
    # 上传的电子书原始文件存放目录（体积大，不入库；已加入 .gitignore）
    books_dir: str = "data/books"

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

    # --- L3 认知助产（主题分布 → 「该问但没问」的追问）---
    l3_max_items_per_analysis: int = 60  # 单次分析送入模型的条目上限（成本与上下文闸门）
    l3_recent_titles: int = 20           # UC-L3-02 衔接追问时携带的近期条目标题数
    l3_question_count: int = 3           # 简报产出的追问上限（需求：2-3 个，宁少勿滥）

    # --- 异步任务框架（幂等 + 重试 + 死信；单进程内轮询，不依赖 Redis/Celery）---
    worker_enabled: bool = True          # 测试里关闭，避免后台线程干扰
    worker_poll_seconds: float = 15.0    # 轮询间隔
    task_max_attempts: int = 3           # 重试上限，超出转死信
    task_retry_backoff_seconds: float = 1.0  # 重试退避基数（2^(n-1) 倍，测试设 0 即时重试）

    # --- L2 触发链路 ---
    l2_realtime_trigger_enabled: bool = True  # 录入/划词后按小时桶合并触发扫描
    l2_weekly_scan_enabled: bool = True       # 每周自动扫描（幂等键按 ISO 周去重）

    @property
    def model_provider(self) -> str:
        """当前启用的供应商：有 KEY 走 deepseek，否则 mock。"""
        return "deepseek" if self.deepseek_api_key else "mock"


settings = Settings()