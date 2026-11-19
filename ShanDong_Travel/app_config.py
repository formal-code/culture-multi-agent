"""后台配置：普通用户页面不显示路径、JSON 或 API Key。"""
from pathlib import Path
import os

PROJECT_DIR = Path(__file__).resolve().parent
# 正式接入用 agents；local 仅供开发者使用已存在的后台文件联调。
PIPELINE_MODE = os.getenv("TRAVEL_PIPELINE_MODE", "agents").strip().lower()
RESEARCH_PATH = PROJECT_DIR / "Json" / "research.json"
EVALUATED_SCRIPT_PATH = PROJECT_DIR / "Json" / "selected_script.json"
# 可选：local 模式下直接读取已有适配结果，避免重复调用模型。
ADAPTATIONS_PATH = PROJECT_DIR / "Json" / "platform_adaptations.json"
GENERATED_DIR = PROJECT_DIR / "generated_videos"

# 本地联调：A/B 使用本地数据，C 使用真实程序。
USE_REAL_C_IN_LOCAL = True

# 测评前的多个候选脚本。
CANDIDATES_SCRIPT_PATH = PROJECT_DIR / "Json" / "candidate_scripts.json"

# C 的模型和搜索配置。
C_ENV_FILE = PROJECT_DIR / "c_agent.env"

# C 本次运行产生的研究记录与筛选结果。
C_OUTPUT_DIR = PROJECT_DIR / "c_agent_outputs"

# 成片理解/报告独立保存；默认在视频完成后运行，不影响原视频下载。
VIDEO_REVIEW_DIR = PROJECT_DIR / "video_reviews"
VIDEO_REVIEW_AUTO = os.getenv("VIDEO_REVIEW_AUTO", "true").lower() == "true"
VIDEO_REVIEW_SEARCH = os.getenv("VIDEO_REVIEW_SEARCH", "true").lower() == "true"
