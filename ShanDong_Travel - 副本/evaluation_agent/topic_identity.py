"""Conservative research identity: a city or a scene is not a topic."""
import re
import unicodedata


def normalized(value):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", value or "").casefold())


def primary_topic(packet):
    return packet.topic or packet.brief.spot_name


def topic_matches(text, packet):
    topic = normalized(primary_topic(packet))
    city = normalized(packet.brief.city)
    names = [topic]
    if city and topic.startswith(city) and len(topic[len(city):]) >= 2:
        names.append(topic[len(city):])
    # Aliases are only accepted for the actual primary subject, never broad scene names.
    if not packet.topic or normalized(packet.topic) == normalized(packet.brief.spot_name):
        names.extend(normalized(alias) for alias in packet.brief.subject_aliases
                     if topic in normalized(alias))
    return any(len(name) >= 2 and name in normalized(text) for name in names)


def same_topic(previous, packet):
    brief = previous.get("brief", {})
    old = previous.get("topic") or brief.get("spot_name", "")
    return (bool(normalized(old)) and normalized(old) == normalized(primary_topic(packet))
            and all(normalized(brief.get(key, "")) == normalized(getattr(packet.brief, key))
                    for key in ("spot_name", "city", "province")))


def audience_context(packet):
    explicit_audience = packet.brief.target_audience
    explicit_goal = packet.brief.communication_goal
    city = packet.brief.city
    return {
        "target_audience": explicit_audience or f"对{city or '当地'}文化或旅游感兴趣的潜在游客",
        "audience_basis": "user_provided" if explicit_audience else "system_assumption",
        "communication_goal": explicit_goal or f"围绕{primary_topic(packet)}开展文旅内容传播",
        "goal_basis": "user_provided" if explicit_goal else "system_assumption",
        "policy": "逐条依据角度和脚本推断可能受众；系统假设不是用户需求。受众不明确时降低此项影响，以其他有依据的维度比较，不拒绝筛选，不承诺特定人群偏好。",
    }
