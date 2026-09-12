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
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 60.0

    # 单价（每百万 token，人民币）；接入实测后回填 .env
    deepseek_price_input_per_1m: float = 1.0
    deepseek_price_output_per_1m: float = 4.0

    # --- Embedding（语义检索；独立于 LLM，DeepSeek 无标准 embedding 接口）---
    # backend: bge（本地真语义，默认）| hash（确定性零依赖，测试/离线）
    embedding_backend: str = "bge"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dim: int = 512

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

    @property
    def model_provider(self) -> str:
        """当前启用的供应商：有 KEY 走 deepseek，否则 mock。"""
        return "deepseek" if self.deepseek_api_key else "mock"


settings = Settings()