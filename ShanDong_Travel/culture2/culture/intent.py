"""Separate named objects from broad cultural themes and derive retrieval vocabulary."""
from dataclasses import asdict, is_dataclass
import re


# Retrieval synonyms are not claims about a city and never invent named locations.
# Each row is (user-facing topic names, search vocabulary for that topic).
THEME_GROUPS = (
    (("风土人情", "民风民俗", "民俗风情", "风俗习惯", "地方风俗", "人文风情"),
     ("民俗", "婚俗", "风俗", "民风", "非遗", "节庆", "庙会", "年俗", "传统技艺",
      "手艺", "戏曲", "舞龙", "舞狮", "秧歌", "大集", "赶集", "地方美食", "小吃")),
    (("民俗", "婚俗", "节庆", "年俗", "传统节日"),
     ("民俗", "婚俗", "风俗", "节庆", "庙会", "年俗", "端午", "龙舟", "元宵", "花灯", "舞龙", "秧歌")),
    (("非遗", "非遗文化", "非物质文化遗产", "传统技艺", "传统文化", "手工艺"),
     ("非遗", "非物质文化遗产", "木版年画", "葫芦雕刻", "剪纸", "戏曲",
      "东昌府木版年画", "东昌雕刻葫芦", "雕刻葫芦", "茌平剪纸", "临清肘捶", "聊城杂技",
      "山东快书", "柳子戏", "面塑", "传统技艺", "手艺", "手工艺", "传承", "刺绣", "木雕", "泥塑")),
    (("美食", "地方美食", "饮食文化", "特色小吃", "小吃"),
     ("美食", "小吃", "饮食", "传统名吃", "老字号", "烹饪", "菜肴", "面食", "特产")),
    (("自然风光", "自然景观", "山水风光", "风景", "风光", "湖光山色"),
     ("风光", "风景", "湖", "河", "湿地", "山水", "荷塘", "自然景观", "落日", "日出")),
    (("古建筑", "古建", "历史建筑", "古城风貌"),
     ("古建筑", "古建", "古城", "古楼", "城楼", "古街", "古桥", "古镇", "传统建筑")),
    (("历史文化", "人文历史", "地方历史", "历史故事"),
     ("历史", "文化遗产", "文物", "博物馆", "古迹", "历史人物", "历史故事")),
    (("市井生活", "烟火气", "市井烟火", "百姓生活", "乡土生活"),
     ("市井", "烟火", "老街", "集市", "赶集", "大集", "小吃", "乡村", "乡土", "民俗")),
    (("文旅", "文旅宣传", "旅游", "文化"),
     ("文旅", "旅游", "景点", "景区", "风光", "古城", "文化", "非遗", "美食", "博物馆", "民俗")),
)
BROAD_THEMES = {name for names, _ in THEME_GROUPS for name in names}


def retrieval_term(term):
    """Remove production requirements from a proposed topic, retaining the named subject."""
    term = re.sub(r'(?:面向|目标受众(?:为|是)?|受众(?:为|是)?)\s*[^，,。；;\n]*', '', term)
    term = re.sub(r'(?:总时长|时长|片长|长度)\s*[:：]?\s*(?:\d{1,2}:){1,2}\d{2}', '', term)
    number = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百半]+)'
    term = re.sub(r'(?:总时长|时长|片长|长度)?\s*[:：]?\s*(?:约|大约|为|控制在)?\s*'
                  + number + r'\s*(?:至|到|[-—~～])?\s*(?:' + number + r')?\s*'
                  r'(?:分钟|小时|秒钟?|[smh](?![a-z]))\s*(?:左右|以内|以上|以下)?', '', term, flags=re.I)
    term = re.sub(r'短视频|视频|宣传片|纪录片|短片|成片|图片|照片|图像|vlog|总时长|时长|片长', '', term, flags=re.I)
    term = term.strip(' ，,。；;：:、')
    term = re.sub(r'^(?:关于|的)\s*|\s*的$', '', term)
    if term in {'儿童', '大众', '青少年', '年轻游客', '游客', '小学生', '中学生', '老年人',
                '生成', '制作', '素材', '拍摄', '秒', '分钟', '小时'}:
        return ''
    return term


def source_media_type(raw):
    """A requested video is a deliverable; restrict inputs only for explicit media requests."""
    kinds = {'image': r'图片|照片|图像', 'video': r'视频'}
    requested, excluded = set(), set()
    for kind, words in kinds.items():
        word = '(?:' + words + ')'
        if re.search(r'(?:不要|不用|不需要|排除|不收集|不搜集|不采集|不搜索)\s*' + word, raw):
            excluded.add(kind)
        if (re.search(word + r'\s*(?:素材|资源|文件|片段|直链)', raw)
                or re.search(r'(?:收集|搜集|采集|搜索|查找|寻找)[^，。；;\n]{0,24}?' + word, raw)
                or re.search(r'(?:只|仅)(?:需要|要|收集|搜集|采集|搜索|查找|使用|用)?\s*' + word, raw)):
            requested.add(kind)
    if re.search(r'(?:(?:图片|照片|图像)\s*[和、及与/]\s*视频|视频\s*[和、及与/]\s*(?:图片|照片|图像))\s*(?:素材|资源|文件)', raw):
        requested.update(kinds)
    if len(excluded) == 1:
        return next(iter(set(kinds) - excluded))
    requested -= excluded
    return next(iter(requested)) if len(requested) == 1 else 'all'


def museum_displays_requested(plan):
    """Require an affirmative, verbatim request for displays, not just a museum name."""
    evidence = plan.get("museum_display_request_evidence", "")
    if (plan.get("museum_display_requested") is not True or not isinstance(evidence, str)
            or not evidence.strip() or evidence.strip() not in plan.get("raw_request", "")):
        return False
    display_words = ("展品", "藏品", "馆藏", "展柜", "展板", "陈列", "展陈", "展览", "展出",
                     "标本", "蜡像", "文物", "瓷器", "陶器", "青铜器", "藏画")
    return (any(word in evidence for word in display_words)
            and not any(word in evidence for word in ("不要", "不需要", "排除", "拒绝", "不想要", "不包括", "不包含")))


def finalize_intent(plan):
    """Repair a model's category mistake without broadening any named-object constraint."""
    value = asdict(plan) if is_dataclass(plan) else dict(plan)
    for key in ('districts', 'targets', 'themes', 'exclude_terms'):
        value[key] = list(dict.fromkeys(cleaned for term in value.get(key, [])
                                        if (cleaned := retrieval_term(term))))
    targets = list(value.get("targets", []))
    themes = list(value.get("themes", []))
    normalized_targets = []
    for term in targets:
        topic = term
        for prefix in (value.get('city', '聊城') + '市', value.get('city', '聊城'), *value['districts']):
            if topic.startswith(prefix):
                topic = topic[len(prefix):].lstrip('的 ')
                break
        if topic in BROAD_THEMES:
            themes.append(topic)
        else:
            normalized_targets.append(term)
    # Recover an explicitly named broad theme even if the model omits that field.
    raw = value.get('raw_request', '')
    if not themes:
        matches = [name for name in BROAD_THEMES if name in raw
                   and not any(term in name for term in value['exclude_terms'])]
        if matches:
            themes.append(max(matches, key=lambda name: (len(name), name)))
    value["targets"] = list(dict.fromkeys(normalized_targets))
    value["themes"] = list(dict.fromkeys(themes))
    excluded = value.get("exclude_terms", [])
    words = list(value["themes"])
    for names, synonyms in THEME_GROUPS:
        if any(theme in names for theme in value["themes"]):
            words.extend(synonyms)
    value["theme_keywords"] = [word for word in dict.fromkeys(words)
                               if not any(term in word for term in excluded)]
    return value
