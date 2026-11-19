"""运行配置：在 PyCharm 中运行 main.py；密钥只从环境变量读取。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CITY = "聊城"
# 模型名称从环境变量读取
MODEL = os.getenv("CULTURE_MODEL", "qwen4-max")
# 延用旧项目的北京业务空间主机名；这不是 API 密钥。
API_HOST = os.getenv("DASHSCOPE_API_HOST", "ws-vclbcdqdhsclgzcb.cn-beijing.maas.aliyuncs.com")
# 固定使用A原始交付的接口
API_BASE = "https://ws-vclbcdqdhsclgzcb.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
# 密钥从这个环境变量读取
API_KEY_ENV = "DATA_OPENAI_API_KEY"

INITIAL_PAGES = 35                # 每站5页，7站完成一轮；之后继续扩展
MAX_PAGES = 280                   # 页面总上限；不设素材或视频最低数量
MAX_CANDIDATES = 500              # 下载失败、review、reject也计入候选尝试
MAX_MODEL_CALLS = 501             # 1次需求解析 + 最多500次素材审核
MAX_REQUESTS = 1800               # 含规则文件、跳转、网页和下载
MAX_SECONDS = 90 * 60
MAX_DOWNLOAD_BYTES = 15 * 1024 ** 3  # 整轮媒体已读取数据最多15GiB（含失败及丢弃文件）
SITE_BATCH = 5
MAX_LINKS_PER_PAGE = 300
MAX_SEARCH_QUERIES = 6
MAX_EMPTY_PAGINATION_PAGES = 3    # 同一栏目连续没有新相关文章时停止该栏翻页
PAGE_TIMEOUT = 20                 # HTTP 单页总读取时限
MODEL_CONNECT_TIMEOUT = 8         # 建立接口连接的等待上限
MODEL_TIMEOUT = 45                # 等待回复数据的读取超时，不是整轮运行时长
DOWNLOAD_TIMEOUT = 120
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_VIDEO_BYTES = 250 * 1024 * 1024
OUTPUT_DIR = ROOT / "data" / CITY

# 用户明确要求尝试这些页面提供的视频地址：仅此域名、路径和视频类型
# 将可读取的 robots 禁止声明改为提示；不会忽略媒体HTTP错误或登录限制。
# 改为空字典即可恢复全部遵守 robots 禁止规则。
VIDEO_ROBOTS_WARNING_ONLY = {"api.lcxw.cn": ("/upload/mp4/",)}

# 新增官网主要在这里加入口；不要把媒体 CDN 当作文章网站加入。
OFFICIAL_SITES = [
    {"id": "news", "name": "聊城新闻网", "url": "https://www.lcxw.cn/",
     "hosts": ["www.lcxw.cn", "lcxw.cn"], "search_enabled": False,
     "prefer_https": True,
     "entry_urls": ["https://www.lcxw.cn/shiting/", "https://www.lcxw.cn/shipin/shupingshipin/"]},
    {"id": "gov", "name": "聊城市人民政府", "url": "http://www.liaocheng.gov.cn/",
     "hosts": ["www.liaocheng.gov.cn"], "search_enabled": True,
     "entry_urls": ["http://www.liaocheng.gov.cn/channel_t_156_11744/"]},
    {"id": "tourism", "name": "聊城市文化和旅游局", "url": "http://wlj.liaocheng.gov.cn/",
     "hosts": ["wlj.liaocheng.gov.cn"], "search_enabled": True,
     "entry_urls": ["http://wlj.liaocheng.gov.cn/channel_t_296_26904/",
                    "http://wlj.liaocheng.gov.cn/channel_t_296_26879/"]},
    {"id": "dongchangfu", "name": "东昌府区人民政府", "url": "http://www.dongchangfu.gov.cn/",
     "hosts": ["www.dongchangfu.gov.cn", "dongchangfu.gov.cn"], "search_enabled": True},
    {"id": "dazhong", "name": "大众网聊城", "url": "https://liaocheng.dzwww.com/",
     "hosts": ["liaocheng.dzwww.com"], "search_enabled": True},
    {"id": "natural_resources", "name": "聊城市自然资源和规划局", "url": "http://zrghj.liaocheng.gov.cn/",
     "hosts": ["zrghj.liaocheng.gov.cn"], "search_enabled": True},
    {"id": "canal_museum", "name": "中国运河文化博物馆", "url": "https://yunhebowuguan.cn/",
     "hosts": ["yunhebowuguan.cn", "www.yunhebowuguan.cn"], "search_enabled": True},
]

TOURISM_TERMS = ("文旅", "旅游", "景区", "景点", "风光", "古城", "古建筑", "非遗",
                 "传统技艺", "民俗", "美食", "博物馆", "夜景", "夜游", "文化", "运河")
NEWS_NOISE = ("会议", "调研", "工作部署", "表彰", "招聘", "招标", "采购", "小学", "教学",
              "养老服务", "照护", "贯彻", "条例", "公示", "通知公告", "人事任免")
