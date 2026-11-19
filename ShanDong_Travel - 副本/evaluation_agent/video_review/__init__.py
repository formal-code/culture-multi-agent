"""成片理解与分析；不预测播放量，不改变脚本或视频。"""

from .pipeline import understand_video, review_video
from .config import ReviewSettings

__all__ = ["ReviewSettings", "understand_video", "review_video"]
