"""Separate named objects from broad cultural themes and derive retrieval vocabulary."""
from dataclasses import asdict, is_dataclass


# Retrieval synonyms are not claims about a city and never invent named locations.
# Each row is (user-facing topic names, search vocabulary for that topic).
THEME_GROUPS = (
    (("风土人情", "民风民俗", "民俗风情", "风俗习惯", "地方风俗", "人文风情"),
     ("民俗", "婚俗", "风俗", "民风", "非遗", "节庆", "庙会", "年俗", "传统技艺",
      "手艺", "戏曲", "舞龙", "舞狮", "秧歌", "大集", "赶集", "地方美食", "小吃")),
    (("民俗", "婚俗", "节庆", "年俗", "传统节日"),
     ("民俗", "婚俗", "风俗", "节庆", "庙会", "年俗", "端午", "龙舟", "元宵", "花灯", "舞龙", "秧歌")),
    (("非遗", "非遗文化", "传统技艺", "传统文化", "手工艺"),
     ("非遗", "传统技艺", "手艺", "手工艺", "传承", "剪纸", "刺绣", "木雕", "泥塑", "戏曲")),
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
    value.setdefault("districts", [])
    value.setdefault("exclude_terms", [])
    targets = list(value.get("targets", []))
    themes = list(value.get("themes", []))
    for term in targets:
        if term in BROAD_THEMES:
            themes.append(term)
    value["targets"] = list(dict.fromkeys(term for term in targets if term not in BROAD_THEMES))
    value["themes"] = list(dict.fromkeys(themes))
    excluded = value.get("exclude_terms", [])
    words = list(value["themes"])
    for names, synonyms in THEME_GROUPS:
        if any(theme in names for theme in value["themes"]):
            words.extend(synonyms)
    value["theme_keywords"] = [word for word in dict.fromkeys(words)
                               if not any(term in word for term in excluded)]
    return value
