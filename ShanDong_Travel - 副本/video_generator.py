"""通用静音视频合成：现有 JSON 和三个公开调用入口保持兼容。

流程：校验素材 -> 规划镜头/字幕 -> 按固定时间线编码 -> 验证 -> 发布。
不调用模型，不识别图片主体，不自动改写脚本。
"""
from __future__ import annotations

from collections import Counter, defaultdict, OrderedDict
from contextlib import closing
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache, wraps
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
from threading import RLock, Timer
from typing import Any, Callable, Iterator
import unicodedata
from uuid import uuid4

import imageio.v2 as imageio
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

PLATFORM_SIZE = {"9:16": (1080, 1920), "3:4": (1080, 1440)}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
LOGGER = logging.getLogger(__name__)

# 这是项目的默认设计参数，不是平台官方规则；无需在前端填写。
TITLE_SECONDS = 3.0
TRANSITION_SECONDS = 0.25
TARGET_SHOT_SECONDS = 6.0
MIN_SHOT_SECONDS = 3.0
MIN_CAPTION_SECONDS = 1.3
IMAGE_MOTION = 0.045
MAX_NEAR_RATIO_CROP = 0.06
CAPTION_CUT_TOLERANCE_SECONDS = 1.5
CAPTION_IMAGE_GAP = 0.025
MATCH_MIN_SCORE = 6.0
VIDEO_CHECK_SAMPLE_COUNT = 5
PREFERRED_CAPTION_SECONDS = 2.2
MIN_DETAIL_SECONDS = 12.0
PREFLIGHT_CACHE_ENTRIES = 128
IMAGE_CACHE_BYTES = 96 * 1024 * 1024
IMAGE_CACHE_ENTRIES = 12
AUDIT_WIDTH = 160
AUDIT_INTERVAL_SECONDS = .5
BLACK_MIN_SECONDS = .5
STILL_MIN_SECONDS = 3.0
MAX_AUDIT_EVENTS = 200


class _BoundedCache:
    """按LRU淘汰，只有成功结果入缓存。锁只保护缓存，不串行编码任务。"""
    def __init__(self, max_entries, max_bytes):
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.items, self.bytes, self.lock = OrderedDict(), 0, RLock()

    def get(self, key):
        with self.lock:
            found = self.items.pop(key, None)
            if found is not None:
                self.items[key] = found
                return found[0]
        return None

    def put(self, key, value, size):
        size = max(1, int(size))
        with self.lock:
            old = self.items.pop(key, None)
            if old is not None:
                self.bytes -= old[1]
            if size > self.max_bytes:
                return
            self.items[key] = value, size
            self.bytes += size
            while len(self.items) > self.max_entries or self.bytes > self.max_bytes:
                _, (_, removed) = self.items.popitem(last=False)
                self.bytes -= removed

    def clear(self):
        with self.lock:
            self.items.clear()
            self.bytes = 0

    def info(self):
        with self.lock:
            return {'entries':len(self.items),'bytes':self.bytes,
                    'max_entries':self.max_entries,'max_bytes':self.max_bytes}


_PROBE_CACHE = _BoundedCache(PREFLIGHT_CACHE_ENTRIES, 8 * 1024 * 1024)
_IMAGE_CACHE = _BoundedCache(IMAGE_CACHE_ENTRIES, IMAGE_CACHE_BYTES)
_CURRENT_METRICS = ContextVar('travel_video_metrics', default=None)


def _metric(key, count=1):
    metrics = _CURRENT_METRICS.get()
    if metrics is not None:
        metrics[key] = metrics.get(key, 0) + count


def clear_video_caches():
    """仅供后台/测试调用；重启程序也会清空。不新增前端设置。"""
    _PROBE_CACHE.clear()
    _IMAGE_CACHE.clear()


def _file_identity(path):
    stat = Path(path).stat()
    return str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _assert_file_identity(path, expected):
    if _file_identity(path) != expected:
        raise ValueError(f'合成期间素材发生变化，请素材准备完成后重新生成：{Path(path).name}')


@dataclass(frozen=True)
class RenderStyle:
    name: str = 'neutral'
    accent: tuple = (180, 208, 219)
    backdrop: tuple = (10, 18, 24)
    panel: tuple = (10, 18, 24, 225)
    ink: tuple = (255, 255, 255)
    font_scale: float = .047


# 项目视觉预设，不是平台强制规范。无平台参数的旧调用保持中性样式。
STYLES = {
    'neutral': RenderStyle(),
    'douyin': RenderStyle('douyin', (80, 232, 222), (9, 20, 30), (9, 20, 30, 235)),
    'kuaishou': RenderStyle('kuaishou', (255, 183, 83), (35, 24, 15), (35, 24, 15, 235)),
    'xiaohongshu': RenderStyle('xiaohongshu', (225, 83, 103), (44, 35, 41),
                              (255, 249, 241, 246), (42, 34, 37), .044),
}
_CURRENT_STYLE = ContextVar('travel_video_style', default=STYLES['neutral'])


def _with_style(function):
    @wraps(function)
    def scoped(*args, **kwargs):
        platform = kwargs.get('platform')
        if platform is not None and platform not in {'douyin', 'kuaishou', 'xiaohongshu'}:
            raise ValueError('不支持的视频平台')
        token = _CURRENT_STYLE.set(STYLES[platform or 'neutral'])
        metrics_token = _CURRENT_METRICS.set({'preflight_hits':0,'preflight_misses':0,
            'image_hits':0,'image_misses':0,'video_read_retries':0})
        try:
            return function(*args, **kwargs)
        finally:
            _CURRENT_METRICS.reset(metrics_token)
            _CURRENT_STYLE.reset(token)
    return scoped

# 通用词可用于辅助排序，不能单凭这些词认定具体画面匹配。
COMMON_TERMS = frozenset(('风景','风光','美景','景色','旅游','旅行','文旅','文旅宣传',
    '城市','城市风光','城市名片','自然景观','山东','聊城','画面','图片','视频','展示','介绍'))
PROCESS_TERMS = ('刻刀','雕刻','刻版','刷墨','刷色','覆纸','套印','拓印','印制',
                 '制作过程','操作过程','手工制作','制作步骤','揉面','烹饪','炒制','编织','剪纸过程')
DISPLAY_TERMS = ('成品','展品','展陈','展柜','静态展','作品展示','成品展示','finished product','exhibit')


@dataclass(frozen=True)
class ResolvedAsset:
    asset_id: str
    media_type: str
    path: Path
    title: str
    description: str
    keywords: tuple[str, ...]
    width: int = 0
    height: int = 0
    source_fps: float = 0.0
    source_frames: int = 0
    preserve_full: bool = False
    content_kind: str = 'unknown'
    quality: dict[str, Any] | None = None
    focus_box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Shot:
    asset: ResolvedAsset
    frames: int
    reason: str
    video_start_seconds: float = 0.0
    motion_direction: int = 1
    motion_start: float = 0.0
    motion_end: float = 1.0
    caption_boundary_aligned: bool = False
    view: str = 'overview'


@lru_cache(maxsize=32)
def _font(size: int):
    for path in (
        Path('C:/Windows/Fonts/msyh.ttc'),
        Path('C:/Windows/Fonts/simhei.ttf'),
        Path('C:/Windows/Fonts/simsun.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'),
        Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf'),
    ):
        if path.is_file():
            return ImageFont.truetype(str(path), size=size)
    raise ValueError('未找到中文字体，请安装微软雅黑、黑体或 Noto Sans CJK。')


@lru_cache(maxsize=4096)
def _has_glyph(character: str, size: int) -> bool:
    font = _font(size)
    mask, missing = font.getmask(character), font.getmask(chr(0x10FFFF))
    return (mask.size, bytes(mask)) != (missing.size, bytes(missing))


def _display_text(text: str, size: int) -> str:
    output = []
    for char in str(text):
        if char in '\r\n\t':
            output.append(' ')
        elif char in ('\ufe0e', '\ufe0f', '\u200d') or unicodedata.category(char) in {'Cc', 'Cf'}:
            continue
        elif char.isspace() or _has_glyph(char, size):
            output.append(char)
        elif unicodedata.category(char).startswith('S') or 0x1F000 <= ord(char) <= 0x1FAFF:
            continue
        else:
            raise ValueError(f'当前中文字体无法显示字符 {char!r}，请更换支持该字符的字体。')
    return re.sub(r' +', ' ', ''.join(output)).strip()


def _caption_metrics(size):
    width, height = size
    font = _font(max(18, round(width * _CURRENT_STYLE.get().font_scale)))
    left, right = round(width * 0.045), round(width * 0.90)
    padding = max(8, round(width * 0.018))
    return font, left, right, padding, right - left - 2 * padding


def _wrap_text(draw, text, font, max_width):
    """保留全部字符，尽量不拆英文单词、不让闭合标点单独占行。"""
    tokens = re.findall(r'[A-Za-z0-9]+(?:[.\'-][A-Za-z0-9]+)*|\s+|.', text, re.S)
    lines, current = [], ''
    closing_marks = set('，。！？；：、,.!?;:）》】」』”’')
    for token in tokens:
        if draw.textlength(current + token, font=font) <= max_width:
            current += token
            continue
        if current.strip():
            if token in closing_marks and len(current) > 1:
                lines.append(current[:-1])
                current = current[-1] + token
                continue
            lines.append(current)
            current = ''
        # 超长英文词也必须能落在画面内。
        for char in token:
            if current and draw.textlength(current + char, font=font) > max_width:
                lines.append(current)
                current = ''
            current += char
    if current:
        lines.append(current)
    return lines


def _reading_units(text):
    words = re.findall(r'[A-Za-z0-9]+', text)
    others = re.sub(r'[A-Za-z0-9]+', '', text)
    # 超长英文词不能和一个短词等量；这里是阅读负担估计，不是 TTS 时长。
    return max(1.0, sum(c.isalnum() for c in others) +
               sum(max(1.6,len(word)/3.5) for word in words))


def _caption_weight(text):
    pauses=sum(.35 if c in '。！？!?；;' else .15
               for c in text if c in '，。！？；：、,.!?;:')
    return _reading_units(text)+pauses*3


def _caption_pages(text: str, size: tuple[int, int]) -> list[str]:
    font, _, _, _, available = _caption_metrics(size)
    text = _display_text(text, font.size)
    if not text:
        return ['']
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    pages, current = [], ''
    # 中文短句、英文标点保留；小数中的点不作为分句符。
    parts = re.split(r'([，。！？；：、!?;:]|(?<!\d)[,.]|[,.](?!\d))', text)
    clauses = []
    for part in parts:
        if not part:
            continue
        if len(part) == 1 and part in '，。！？；：、,.!?;:' and clauses:
            clauses[-1] += part
        else:
            clauses.append(part)
    for clause in clauses:
        combined = current + clause
        if current and (len(_wrap_text(draw, combined, font, available)) > 2 or
                        _reading_units(combined) > 23):
            pages.append(current)
            current = ''
        # 若单句太长，优先在单词边界拆分；最终仍逐字保证两行上限。
        for token in re.findall(r'[A-Za-z0-9]+(?:[.\'-][A-Za-z0-9]+)*|\s+|.', clause, re.S):
            if current and len(_wrap_text(draw, current + token, font, available)) > 2:
                pages.append(current)
                current = ''
            for char in token:
                if current and len(_wrap_text(draw, current + char, font, available)) > 2:
                    pages.append(current)
                    current = ''
                current += char
        if current and current[-1] in '。！？!?；;' and _reading_units(current) >= 6:
            pages.append(current)
            current = ''
    if current:
        pages.append(current)
    # 空格属于原文，但不能单独占一页。将其并回相邻页，保持全文顺序。
    for index in range(len(pages)-1,-1,-1):
        if not pages[index].strip() and len(pages)>1:
            whitespace=pages.pop(index)
            if index<len(pages):
                pages[index]=whitespace+pages[index]
            else:
                pages[index-1]+=whitespace
    return pages or ['']


def _allocate_frames(total: int, weights: list[float], minimum: int = 1) -> list[int]:
    if not weights or total < len(weights):
        raise ValueError('固定时长不足以显示全部内容，请缩短文字或增加分镜时长。')
    baseline = min(minimum, total // len(weights))
    remaining = total - baseline * len(weights)
    weight_sum = sum(weights)
    result, cumulative = [], 0.0
    previous = 0
    for index, weight in enumerate(weights):
        cumulative += weight
        boundary = remaining if index == len(weights) - 1 else int(remaining * cumulative / weight_sum)
        result.append(baseline + boundary - previous)
        previous = boundary
    return result


def _caption_schedule(segment, size, frame_count, fps=20):
    text = str(segment.get('vo') or '').strip() or str(segment.get('subtitle') or '')
    pages = _caption_pages(text, size)
    font, _, _, _, available = _caption_metrics(size)
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    # 短分镜合并能放得下的相邻短句，减少闪字幕，不丢失正文。
    preferred_count = max(1, int(frame_count / fps / PREFERRED_CAPTION_SECONDS))
    while len(pages) > preferred_count:
        pairs = [(len(pages[i]) + len(pages[i+1]), i) for i in range(len(pages)-1)
                 if len(_wrap_text(draw, pages[i] + pages[i+1], font, available)) <= 2]
        if not pairs:
            break
        _, index = min(pairs)
        pages[index:index+2] = [pages[index] + pages[index+1]]
    counts = _allocate_frames(frame_count, [_caption_weight(p) for p in pages],
                              max(1, round(MIN_CAPTION_SECONDS * fps)))
    end, result = 0, []
    for count, page in zip(counts, pages):
        end += count
        result.append((end, page))
    return result


def _short_title(title, size):
    width = size[0]
    font = _font(max(18, round(width * 0.046)))
    draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
    text = _display_text(title, font.size)
    available = round(width * 0.85) - 2 * max(8, round(width * .018))
    while len(_wrap_text(draw, text, font, available)) > 2 and font.size > max(16, round(width*.032)):
        font = _font(font.size - 1)
    lines = _wrap_text(draw, text, font, available)
    if len(lines) > 2:
        lines = lines[:2]
        while lines[-1] and draw.textlength(lines[-1] + '…', font=font) > available:
            lines[-1] = lines[-1][:-1]
        lines[-1] += '…'
    return font, lines


def _foreground_bounds(asset,size,progress=1.0,direction=1):
    """与实际绘图共享几何计算，字幕不用猜图片实际放在哪里。"""
    width,height=size
    if _layout(asset,size)=='near_ratio_fill':
        return (0,0,width,height)
    box=(round(width*.98),round(height*.70))
    motion=0.0 if asset.preserve_full or asset.media_type=='video' else IMAGE_MOTION
    eased=.5-.5*math.cos(math.pi*max(0.0,min(1.0,progress)))
    if direction<0:
        eased=1-eased
    scale=1-motion+motion*eased
    box_width,box_height=max(1,round(box[0]*scale)),max(1,round(box[1]*scale))
    # 与 ImageOps.contain 的长宽取整一致；不同尺寸、EXIF纠正后均适用。
    if asset.width/asset.height>box_width/box_height:
        fitted_width=box_width
        fitted_height=max(1,round(asset.height/asset.width*box_width))
    else:
        fitted_height=box_height
        fitted_width=max(1,round(asset.width/asset.height*box_height))
    left=(width-fitted_width)//2
    top=round(height*.43-fitted_height/2)
    return left,top,left+fitted_width,top+fitted_height


def _caption_box(size,subtitle,asset=None):
    font,left,right,padding,available=_caption_metrics(size)
    text=_display_text(subtitle,font.size)
    draw=ImageDraw.Draw(Image.new('RGB',(1,1)))
    lines=_wrap_text(draw,text,font,available)
    if len(lines)>2:
        raise ValueError('内部字幕分页超过两行，未缩小字号或截断文字。')
    height=size[1]
    box_height=len(lines)*round(font.size*1.26)+2*padding
    top=round(height*.90)-box_height
    if asset is not None and _layout(asset,size)=='preserve_full':
        full_bottom=_foreground_bounds(asset,size,1.0)[3]
        # 静态横图有空位时紧贴画面下方；不覆盖图片本身或原有海报文字。
        ideal_top=full_bottom+round(height*CAPTION_IMAGE_GAP)
        if ideal_top+box_height<=round(height*.90):
            top=ideal_top
    # 短句不再挂一条空的大黑底；中心仍位于原字幕安全区内。
    widest=max((draw.textlength(line,font=font) for line in lines),default=0)
    box_width=min(right-left,max(round(size[0]*.30),math.ceil(widest)+2*padding))
    box_left=left+(right-left-box_width)//2
    return font,lines,(box_left,top,box_left+box_width,top+box_height),padding


def _make_caption_overlay(size, title, subtitle, asset=None):
    """横图字幕贴近画面；铺满竖屏的画面仍用底部字幕。"""
    width, height = size
    overlay = Image.new('RGBA', size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    font, left, right, padding, available = _caption_metrics(size)
    font,lines,box,padding=_caption_box(size,subtitle,asset)
    if lines:
        line_height = round(font.size * 1.26)
        box_left,top,box_right,bottom=box
        style = _CURRENT_STYLE.get()
        draw.rounded_rectangle(box, radius=max(6, width//90), fill=style.panel)
        if style.name != 'neutral':
            accent_width = max(3, round(width*.005))
            draw.rounded_rectangle((box_left,top,box_left+accent_width,bottom),
                                   radius=accent_width//2,fill=(*style.accent,255))
        for index, line in enumerate(lines):
            text_left=box_left+padding
            if len(lines)==1:
                text_left=(box_left+box_right-draw.textlength(line,font=font))/2
            draw.text((text_left, top+padding+index*line_height), line, font=font,
                      fill=style.ink, anchor='lt')
    if title:
        title_font, title_lines = _short_title(title, size)
        top = round(height * .065)
        line_height = round(title_font.size * 1.28)
        bottom = top + len(title_lines)*line_height + 2*padding
        style = _CURRENT_STYLE.get()
        draw.rounded_rectangle((left, top, right, bottom),radius=max(6,width//90),fill=style.panel)
        if style.name != 'neutral':
            draw.line((left+padding,top,right-padding,top),fill=(*style.accent,255),width=max(3,width//220))
        for index, line in enumerate(title_lines):
            draw.text((left+padding,top+padding+index*line_height),line,font=title_font,fill=style.ink,anchor='lt')
    return overlay


def _draw_caption(frame, overlay):
    return np.asarray(Image.alpha_composite(Image.fromarray(frame).convert('RGBA'), overlay).convert('RGB'))


def _number(value, default=0.0):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _positive_terms(text):
    """只作有限的文字消歧，不代表理解事实或识别画面。"""
    text=unicodedata.normalize('NFKC',str(text)).casefold()
    # “不是制作过程/未展示雕刻”不能变成支持制作镜头的正证据。
    text=re.sub(r'(?:不是|并非|不含|没有|未显示|未展示|未提供|不涉及|不生成|不新增|而非)[^，。；;！？!?\n]*',' ',text)
    return text


def _declared_kind(title,description,keywords):
    raw=' '.join([title,description,*keywords]).casefold()
    text=_positive_terms(raw)
    if any(k in text for k in ('海报','poster')):
        return 'poster'
    if any(k in text for k in ('地图','图表','截图','文字','map','infographic','screenshot')):
        return 'text_graphic'
    if any(k in raw for k in ('新闻','主播','news','带字幕','含字幕','原有字幕')):
        return 'edited_video'
    # 展柜/成品配上“不显示过程”的描述，仍归为静态展示。
    if any(k in text for k in DISPLAY_TERMS):
        return 'display'
    if any(k in text for k in PROCESS_TERMS):
        return 'process'
    if any(k in text for k in ('人物','人像','portrait','合影')):
        return 'people'
    if any(k in text for k in ('湖','山','风景','风光','公园','建筑','航拍','landscape')):
        return 'scenery'
    return 'unknown'


def _quality_sample(image):
    """有限像素统计：亮度、边缘变化、近纯色。不能识别主体或判断真实美感。"""
    sample=ImageOps.contain(image.convert('RGB'),(320,320),Image.Resampling.BILINEAR)
    gray=np.asarray(sample.convert('L'),dtype=np.float32)
    dx=np.diff(gray,axis=1);dy=np.diff(gray,axis=0)
    edge=float((np.mean(dx*dx)+np.mean(dy*dy))/2) if dx.size and dy.size else 0.0
    mean=float(gray.mean());std=float(gray.std())
    return {'brightness':round(mean,2),'luminance_std':round(std,2),'edge_energy':round(edge,2),
            'near_black':bool(mean<8 and np.percentile(gray,95)<16),
            'near_white':bool(mean>248 and np.percentile(gray,5)>240),
            'nearly_uniform':bool(std<2.0),
            'softness_suspected':bool(std>8 and edge<3.0)}


def _quality_preference(asset):
    """只在匹配程度接近时参考；不会让高清无关图超过相关图。"""
    q=asset.quality or {}
    samples=[s for s in q.get('samples',[]) if s.get('readable',True)]
    risk=q.get('decode_failures',0)*2
    if any(s.get('near_black') or s.get('near_white') for s in samples):
        risk+=1
    if asset.width and asset.height:
        risk+=int(min(asset.width,asset.height)<240)
    return -risk


def _quality_notes(asset):
    q=asset.quality or {}
    samples=q.get('samples',[])
    notes=[]
    if min(asset.width,asset.height)<240:
        notes.append('源素材短边不足240像素，放大可能不清晰')
    if max(asset.width/asset.height,asset.height/asset.width)>3:
        notes.append('超宽或超长比例，完整保留时留白会较多')
    if any(s.get('near_black') for s in samples):
        notes.append('抽样发现近黑画面，可能为转场或异常')
    if any(s.get('near_white') for s in samples):
        notes.append('抽样发现近白画面，可能为设计或异常')
    if any(s.get('softness_suspected') for s in samples):
        notes.append('抽样边缘变化较弱，可能模糊或原图本就平滑')
    if any(s.get('nearly_uniform') for s in samples):
        notes.append('抽样画面近纯色，可能为背景设计或异常')
    if q.get('decode_failures'):
        notes.append('部分抽样位置解码失败，合成时仍需检查')
    if asset.content_kind=='edited_video':
        notes.append('上游标注为新闻或已有字幕成片；原有文字/画面会保留')
    return notes


def _validated_focus_box(value):
    """仅接受上游人工/程序提供的区域，不做主体猜测。坐标按EXIF纠正后的图像。"""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) for n in value):
        return None
    try:
        left, top, right, bottom = map(float, value)
    except (ValueError, OverflowError):
        return None
    if not all(math.isfinite(n) for n in (left, top, right, bottom)):
        return None
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1):
        return None
    if right-left < .20 or bottom-top < .20 or (right-left)*(bottom-top) < .12:
        return None
    if (right-left)*(bottom-top) > .95:
        return None
    return left, top, right, bottom


def _probe_asset(path, media_type):
    identity = _file_identity(path)
    key = ('probe_v24', identity, media_type, VIDEO_CHECK_SAMPLE_COUNT)
    cached = _PROBE_CACHE.get(key)
    if cached is not None:
        _metric('preflight_hits')
        return deepcopy(cached)
    _metric('preflight_misses')
    fps, count = 0.0, 0
    quality = {'method':'sampled_pixels_and_decode','samples':[],
               'decode_failures':0,'full_decode_verified':False}
    if media_type == 'image':
        with Image.open(path) as image:
            image.load()
            orientation = image.getexif().get(274, 1)
            image = ImageOps.exif_transpose(image)
            width, height = image.size
            quality['orientation_corrected'] = orientation not in (None, 1)
            quality['samples'].append({'frame_index':0,'readable':True,**_quality_sample(image)})
            quality['full_decode_verified'] = True
    else:
        signatures = []
        with closing(imageio.get_reader(str(path), format='FFMPEG')) as reader:
            frame = reader.get_next_data()
            height, width = frame.shape[:2]
            meta = reader.get_meta_data()
            fps = _number(meta.get('fps'), 20.0)
            count = int(_number(meta.get('nframes')))
            if not count:
                count = round(_number(meta.get('duration')) * fps)
            if not count:
                count = reader.count_frames()
            if count <= 0:
                raise ValueError('视频没有可解码画面')
            indices = sorted({round((count-1)*i/(VIDEO_CHECK_SAMPLE_COUNT-1))
                              for i in range(VIDEO_CHECK_SAMPLE_COUNT)})
            for sample_index in indices:
                try:
                    sample = frame if sample_index == 0 else reader.get_data(sample_index)
                    image = Image.fromarray(sample)
                    quality['samples'].append({'frame_index':sample_index,'readable':True,
                                              **_quality_sample(image)})
                    signatures.append(np.asarray(image.convert('L').resize((32, 18)), dtype=np.float32))
                except (IndexError, RuntimeError, OSError, StopIteration) as exc:
                    quality['decode_failures'] += 1
                    quality['samples'].append({'frame_index':sample_index,'readable':False,
                                              'error_type':type(exc).__name__})
        if len(signatures) > 1:
            delta = max(float(np.abs(s-signatures[0]).mean()) for s in signatures[1:])
            quality['sampled_motion'] = {'max_difference':round(delta, 3),
                'status':'sampled_static' if delta < .6 else 'sampled_changes',
                'basis':'five_sparse_positions_not_full_source_motion_analysis'}
    _assert_file_identity(path, identity)
    result = width, height, fps, count, quality
    # 有解码失败的结果不缓存，以便下次重试，而非一直复用失败状态。
    if quality['decode_failures'] == 0:
        _PROBE_CACHE.put(key, deepcopy(result), len(json.dumps(quality).encode('utf-8'))+256)
    return result


def _resolve_assets(media_assets, base_dir, diagnostics=None):
    if not isinstance(media_assets, list):
        raise ValueError('A 的 media_assets 必须是数组')
    base_dir = Path(base_dir).resolve()
    resolved, problems, seen_ids = [], [], set()
    for index, item in enumerate(media_assets):
        try:
            if not isinstance(item, dict):
                raise ValueError('不是对象')
            identifier = str(item.get('id') or f'asset_{index+1}')
            if identifier in seen_ids:
                raise ValueError(f'重复的素材 id：{identifier}')
            file_path = item.get('file_path')
            if not isinstance(file_path, str) or not file_path.strip():
                raise ValueError('缺少 file_path')
            path = Path(file_path)
            path = (path if path.is_absolute() else base_dir/path).resolve()
            if not path.is_file():
                raise ValueError(f'文件不存在：{path}')
            media_type = str(item.get('media_type', '')).lower()
            if media_type not in {'image', 'video'}:
                media_type = 'image' if path.suffix.lower() in IMAGE_EXTENSIONS else 'video'
            if path.suffix.lower() not in (IMAGE_EXTENSIONS if media_type == 'image' else VIDEO_EXTENSIONS):
                raise ValueError('文件扩展名与素材类型不匹配或不支持')
            width,height,fps,count,quality = _probe_asset(path,media_type)
            keywords = item.get('keywords', [])
            keywords = tuple(str(k) for k in keywords if k) if isinstance(keywords,list) else ()
            text = ' '.join([str(item.get('title','')),str(item.get('description','')),*keywords]).casefold()
            # 仅按上游文字保守判断，不声称自动识别了图片中的文字/人脸。
            kind=_declared_kind(str(item.get('title','')),str(item.get('description','')),keywords)
            preserve = kind in {'poster','text_graphic','edited_video'}
            quality['source_size']=[width,height]
            quality['content_kind_basis']='upstream_text_rules'
            focus = _validated_focus_box(item.get('focus_box'))
            small_focus=focus is not None and (round(focus[2]*width)-round(focus[0]*width)<64 or
                                                round(focus[3]*height)-round(focus[1]*height)<64)
            if 'focus_box' in item and (focus is None or small_focus or media_type != 'image' or preserve or kind == 'people'):
                message=f'素材 {identifier}：局部区域无效或素材需完整保护，继续使用全景。'
                if diagnostics is not None:
                    diagnostics.append(message)
                LOGGER.warning(message)
                focus=None
            resolved.append(ResolvedAsset(identifier,media_type,path,str(item.get('title','')),
                str(item.get('description','')),keywords,width,height,fps,count,preserve,kind,quality,focus))
            for note in _quality_notes(resolved[-1]):
                message=f'素材 {identifier}：{note}'
                if diagnostics is not None:
                    diagnostics.append(message)
                LOGGER.warning(message)
            seen_ids.add(identifier)
        except Exception as exc:
            problem = f'media_assets[{index}] 已跳过：{exc}'
            problems.append(problem)
            LOGGER.warning(problem)
    if diagnostics is not None:
        diagnostics.extend(problems)
    if not resolved:
        raise ValueError('素材包中没有可用的本地图片或视频：'+('；'.join(problems) or 'media_assets 为空'))
    return resolved


def _phrase_parts(phrase):
    compact = ''.join(c for c in phrase.casefold() if c.isalnum())
    return [compact[i:i+2] for i in range(len(compact)-1)] if len(compact)>1 else ([compact] if compact else [])


def _scene_text(segment):
    return _positive_terms(' '.join(str(segment.get(k) or '') for k in ('visual','vo','subtitle')))


def _term_in(term,text):
    if re.fullmatch(r'[a-z0-9 ]+',term):
        return re.search(r'(?<![a-z0-9])'+re.escape(term)+r'(?![a-z0-9])',text) is not None
    return term in text


def _informative_phrase(phrase):
    for term in sorted(COMMON_TERMS,key=len,reverse=True):
        phrase=phrase.replace(term,'')
    return sum(c.isalnum() for c in phrase)>=2


def _text_kind(segment):
    # visual 权重大于 vo：口播可能提到多个对象，画面指令更接近当前镜头。
    text=_positive_terms(segment.get('visual') or '')
    if not text.strip():
        text=_scene_text(segment)
    return _declared_kind(text,'',())


def _match_evidence(segment,asset):
    visual=_positive_terms(segment.get('visual') or '')
    narration=_positive_terms(' '.join(str(segment.get(k) or '') for k in ('vo','subtitle')))
    combined=visual+' '+narration
    positive_description=_positive_terms(asset.description)
    # 描述明确否定某动作时，不让 keywords 中的同一词抵消否定。
    negated=re.findall(r'(?:不是|并非|不含|没有|未显示|未展示|未提供|不涉及|而非)[^，。；;！？!?\n]*',asset.description.casefold())
    keywords=[]
    for keyword in dict.fromkeys(k.strip().casefold() for k in asset.keywords):
        if not keyword or any(keyword in clause for clause in negated):
            continue
        keywords.append(keyword)
    # 长短嵌套关键词只计一次，避免“木版/木版年画/年画”人为叠高分。
    matched=[k for k in keywords if _term_in(k,combined)]
    matched=[k for k in matched if not any(k!=other and k in other for other in matched)]
    specific=[]
    generic=[]
    score=0.0
    for term in matched:
        if term in COMMON_TERMS or len(term)<2:
            generic.append(term)
            score+=.3
        else:
            specific.append(term)
            score+=(6+min(len(term),8))*(1.0 if _term_in(term,visual) else .6)
    phrases=[]
    for field,phrase in (('title',asset.title),('description',positive_description)):
        phrase=_positive_terms(phrase).strip()
        if not phrase or phrase in {'无','暂无','none','n/a'}:
            continue
        if len(phrase)>=3 and _informative_phrase(phrase) and _term_in(phrase,combined):
            phrases.append({'field':field,'text':phrase})
            score+=8 if _term_in(phrase,visual) else 5
        else:
            shared=set(_phrase_parts(phrase)) & set(_phrase_parts(visual or combined))
            shared={s for s in shared if s not in COMMON_TERMS}
            if len(shared)>=3:
                score+=min(3.0,len(shared)*.4)
    desired=_text_kind(segment)
    conflicts=[]
    if desired=='process' and asset.content_kind in {'display','poster','text_graphic','edited_video'}:
        conflicts.append('script_requests_process_but_asset_is_display_or_edited')
    elif desired=='people' and asset.content_kind in {'poster','text_graphic'}:
        conflicts.append('script_requests_people_but_asset_is_graphic')
    elif desired=='scenery' and asset.content_kind in {'poster','text_graphic','edited_video'}:
        conflicts.append('script_requests_scenery_but_asset_is_graphic_or_edited')
    if conflicts:
        score-=18
    score=round(score,2)
    strong=bool((specific or phrases) and score>=MATCH_MIN_SCORE and not conflicts)
    return {'score':score,'qualified':strong,'specific_keywords':specific,
        'generic_keywords':generic,'exact_phrases':phrases,'desired_kind':desired,
        'asset_kind':asset.content_kind,'conflicts':conflicts,
        'quality_preference':_quality_preference(asset),
        'basis':'upstream_text_not_visual_recognition'}


def _asset_match_score(segment,asset):
    return _match_evidence(segment,asset)['score']


def _keyword_hits(segment, asset):
    return set(_match_evidence(segment,asset)['specific_keywords'])


def _specified_asset(segment, assets, base_dir):
    value = segment.get('asset')
    if not isinstance(value,str) or not value.strip():
        return None
    # 只从 A 清单中挑选，绝不按脚本路径任意读取其他文件。
    if base_dir is not None:
        path = Path(value)
        path = (path if path.is_absolute() else Path(base_dir)/path).resolve()
        return next((a for a in assets if a.path==path),None)
    canonical = value.replace('\\','/').casefold()
    return next((a for a in assets if a.path.as_posix().casefold()==canonical or
                 a.path.as_posix().casefold().endswith('/'+canonical)),None)


def _choose_primary(segments, assets, base_dir=None,decisions=None):
    selected, notices, reasons, use_count = [], [], [], Counter()
    for index, segment in enumerate(segments):
        designated = _specified_asset(segment,assets,base_dir)
        evaluated=[(asset,_match_evidence(segment,asset)) for asset in assets]
        def rank(pair):
            item,evidence=pair
            return (evidence['qualified'],evidence['score'],evidence['quality_preference'],-use_count[item.path])
        ranked=sorted(evaluated,key=rank,reverse=True)
        if designated is not None:
            asset, reason = designated, 'script_asset'
        else:
            if segment.get('asset'):
                notices.append(f'分镜 {segment.get("no",index+1)} 指定素材不在可用清单中，已启动自动匹配。')
            if ranked[0][1]['qualified']:
                asset, reason = ranked[0][0], 'keyword_match'
            else:
                # 不把低置信匹配当作“精确匹配”；只有弱相关时明确标为替代。
                weak=[pair for pair in ranked if pair[1]['score']>0 and not pair[1]['conflicts']]
                if weak:
                    asset=weak[0][0]
                    reason='weak_match_fallback'
                else:
                    # 尽可能避免明显类型冲突；类型仍只依据上游文字。
                    compatible=[a for a,e in ranked if not e['conflicts']]
                    previous=selected[-1] if selected else None
                    asset=previous if previous is not None and previous in compatible else (compatible[0] if compatible else (previous or assets[0]))
                    reason='reuse_fallback'
                notices.append(f'分镜 {segment.get("no",index+1)} 缺少充分匹配依据，已使用「{asset.title or asset.path.name}」作为替代画面。')
        selected.append(asset)
        reasons.append(reason)
        use_count[asset.path]+=1
        evidence=_match_evidence(segment,asset)
        if decisions is not None:
            decisions.append({'no':segment.get('no',index+1),'selected_asset_id':asset.asset_id,
                'reason':reason,'selected_evidence':evidence,
                'top_candidates':[{'asset_id':a.asset_id,**e} for a,e in ranked[:5]],
                'declared_asset':segment.get('asset')})
    return selected,notices,reasons


def _select_assets(segments,assets,base_dir=None):
    selected,notices,_ = _choose_primary(segments,assets,base_dir)
    return selected,notices


def _plan_shots(segments,assets,base_dir,fps,decisions=None):
    primary,notices,reasons = _choose_primary(segments,assets,base_dir,decisions)
    plans,video_clock,use_count = [],defaultdict(float),Counter()
    for segment,main,reason in zip(segments,primary,reasons):
        duration = segment['duration']
        chosen = [main]
        # 明确指定素材时尊重作者；连续视频不为凑节奏随意拆散。
        needs_alternatives=(main.media_type=='image' or
                            (main.source_frames/main.source_fps<duration if main.source_fps else False))
        if reason=='keyword_match' and needs_alternatives and duration>=2*MIN_SHOT_SECONDS:
            limit = min(4,max(1,int(duration/TARGET_SHOT_SECONDS+.5)))
            main_score = _asset_match_score(segment,main)
            hits = _keyword_hits(segment,main)
            ranked = sorted((a for a in assets if a.path!=main.path),
                key=lambda a:(_asset_match_score(segment,a),-use_count[a.asset_id]),reverse=True)
            for asset in ranked:
                if len(chosen)>=limit:
                    break
                # 不能只凭两个公共字或“风光”这类宽泛描述插入无关图片。
                if (_match_evidence(segment,asset)['qualified'] and
                    _asset_match_score(segment,asset)>=max(12,main_score*.75) and
                    len(_keyword_hits(segment,asset))>=2 and
                    hits & _keyword_hits(segment,asset)):
                    chosen.append(asset)
                    if len(chosen)>=limit:
                        break
        counts = _allocate_frames(duration*fps,[1.0]*len(chosen))
        shots=[]
        for index,(asset,count) in enumerate(zip(chosen,counts)):
            start = video_clock[asset.asset_id] if asset.media_type=='video' else 0.0
            shots.append(Shot(asset,count,reason if index==0 else 'related_alternative',start,
                              1 if use_count[asset.asset_id]%2==0 else -1))
            use_count[asset.asset_id]+=1
            if asset.media_type=='video':
                video_clock[asset.asset_id]+=count/fps
        plans.append(shots)
    return plans,primary,notices


def _add_detail_views(plans, fps):
    """长单图只在有可信区域标注时插入同图局部；首尾全景保留。"""
    result=[]
    for shots in plans:
        first=shots[0]
        if (len(shots)==1 and first.asset.media_type=='image' and first.asset.focus_box is not None
                and first.frames>=round(MIN_DETAIL_SECONDS*fps)):
            counts=_allocate_frames(first.frames,[1.0,1.2,1.0],round(MIN_SHOT_SECONDS*fps))
            result.append([replace(first,frames=counts[0]),
                           replace(first,frames=counts[1],view='detail'),
                           replace(first,frames=counts[2])])
        else:
            result.append(shots)
    return result


def _view_asset(shot):
    if shot.view!='detail' or shot.asset.focus_box is None:
        return shot.asset
    left,top,right,bottom=shot.asset.focus_box
    return replace(shot.asset,
        width=max(1,round(right*shot.asset.width)-round(left*shot.asset.width)),
        height=max(1,round(bottom*shot.asset.height)-round(top*shot.asset.height)),
        preserve_full=True)


def _refine_timeline(plans,schedules,fps):
    """调整内部镜头，原始分镜边界、总帧数和指定素材均不变。"""
    refined=[]
    for shots,schedule in zip(plans,schedules):
        total=sum(s.frames for s in shots)
        minimum=max(1,min(round(MIN_SHOT_SECONDS*fps),total//len(shots)))
        cuts=[end for end,_ in schedule[:-1]]
        cursor=target=0
        aligned=[]
        for index,shot in enumerate(shots):
            target+=shot.frames
            if index==len(shots)-1:
                end=total
                snap=False
            else:
                low=cursor+minimum
                high=total-(len(shots)-index-1)*minimum
                candidates=[cut for cut in cuts if low<=cut<=high and
                    abs(cut-target)<=round(CAPTION_CUT_TOLERANCE_SECONDS*fps)]
                end=min(candidates,key=lambda x:abs(x-target)) if candidates else max(low,min(high,target))
                snap=end in cuts
            aligned.append(replace(shot,frames=end-cursor,caption_boundary_aligned=snap))
            cursor=end
        refined.append(aligned)
    # 镜头时长变化后，重新计算相同源视频的接续起点。
    video_clock=defaultdict(float)
    for row,shots in enumerate(refined):
        for index,shot in enumerate(shots):
            if shot.asset.media_type=='video':
                clock=video_clock[shot.asset.path]
                refined[row][index]=replace(shot,video_start_seconds=clock)
                video_clock[shot.asset.path]+=shot.frames/fps
    # 连续相同图片作为一个运动段，避免每到下一分镜突然缩回起点。
    flat=[(row,index,shot) for row,shots in enumerate(refined) for index,shot in enumerate(shots)]
    head=0
    while head<len(flat):
        row,index,first=flat[head]
        if first.asset.media_type!='image':
            head+=1
            continue
        tail=head+1
        while (tail<len(flat) and flat[tail][2].asset.path==first.asset.path
               and flat[tail][2].asset.media_type=='image' and flat[tail][2].view==first.view):
            tail+=1
        group=flat[head:tail]
        total=sum(shot.frames for _,_,shot in group)
        position=0
        for row,index,shot in group:
            # 用 total-1 对齐相邻帧，而不是把两段分别做完一轮缩放。
            start=position/max(1,total-1)
            end=(position+shot.frames-1)/max(1,total-1)
            refined[row][index]=replace(shot,motion_direction=first.motion_direction,
                                       motion_start=start,motion_end=end)
            position+=shot.frames
        head=tail
    return refined


def _fit_cover(image,size):
    return ImageOps.fit(image.convert('RGB'),size,method=Image.Resampling.LANCZOS)


def _scene_background(image,size):
    small=(max(1,size[0]//5),max(1,size[1]//5))
    background=_fit_cover(image,small).filter(ImageFilter.GaussianBlur(max(3,small[0]//26)))
    background=background.resize(size,Image.Resampling.BILINEAR)
    style=_CURRENT_STYLE.get()
    return Image.blend(background,Image.new('RGB',size,style.backdrop),.52 if style.name=='xiaohongshu' else .46)


def _layout(asset,size):
    source_ratio=asset.width/asset.height
    target_ratio=size[0]/size[1]
    retained=min(source_ratio/target_ratio,target_ratio/source_ratio)
    # 仅裁去很少边缘，且上游未标明海报/文字时，才可铺满。
    if not asset.preserve_full and retained>=1-MAX_NEAR_RATIO_CROP:
        return 'near_ratio_fill'
    return 'preserve_full'


def _fit_scene(image,size,progress=1.0,background=None,asset=None,direction=1):
    source=ImageOps.exif_transpose(image).convert('RGB')
    if asset is None:
        asset=ResolvedAsset('', 'image',Path('.'),'','',(),source.width,source.height)
    if _layout(asset,size)=='near_ratio_fill':
        return _fit_cover(source,size)
    width,height=size
    canvas=background.copy() if background is not None else _scene_background(source,size)
    left,top,right,bottom=_foreground_bounds(asset,size,progress,direction)
    fitted=source.resize((right-left,bottom-top),Image.Resampling.LANCZOS)
    canvas.paste(fitted,(left,top))
    return canvas


def _image_frames(asset,size,frame_count):
    yield from _shot_image_frames(Shot(asset,frame_count,'direct_image'),size)


def _prepared_image(shot,size):
    identity=_file_identity(shot.asset.path)
    key=('image_v24',identity,tuple(size),_CURRENT_STYLE.get(),shot.view,
         shot.asset.focus_box if shot.view=='detail' else None)
    cached=_IMAGE_CACHE.get(key)
    if cached is not None:
        _metric('image_hits')
        return cached
    _metric('image_misses')
    with Image.open(shot.asset.path) as image:
        source=ImageOps.exif_transpose(image).convert('RGB')
    if shot.view=='detail' and shot.asset.focus_box is not None:
        left,top,right,bottom=shot.asset.focus_box
        source=source.crop((round(left*source.width),round(top*source.height),
                            round(right*source.width),round(bottom*source.height)))
    source.thumbnail((size[0]*2,size[1]*2),Image.Resampling.LANCZOS)
    background=_scene_background(source,size)
    _assert_file_identity(shot.asset.path,identity)
    value=(source,background)
    _IMAGE_CACHE.put(key,value,source.width*source.height*3+background.width*background.height*3)
    return value


def _shot_image_frames(shot,size):
    source,background=_prepared_image(shot,size)
    view_asset=_view_asset(shot)
    if view_asset.preserve_full or _layout(view_asset,size)=='near_ratio_fill':
        # 静态海报/地图/局部镜头和无需运动的竖图只布局一次，不缓存整段帧。
        raw=np.asarray(_fit_scene(source,size,background=background,asset=view_asset))
        for _ in range(shot.frames):
            yield raw
        return
    for index in range(shot.frames):
        local=index/max(1,shot.frames-1)
        progress=shot.motion_start+(shot.motion_end-shot.motion_start)*local
        yield np.asarray(_fit_scene(source,size,progress,background,view_asset,shot.motion_direction))


def _shot_key(shot):
    return shot.asset.path,shot.view


def _transition_strength(index, count, fps, enter, leave):
    """只淡化当前画面；不会把上一分镜的画面放在新字幕下。"""
    length=min(max(1,round(TRANSITION_SECONDS*fps/2)),count//5)
    if not length:
        return 1.0
    distance=min(index if enter else length,count-1-index if leave else length)
    return .70+.30*min(1.0,distance/length)


def _decorate_frame(frame, size, completed, total, boundaries, detail=False, caption_box=None):
    style=_CURRENT_STYLE.get()
    if style.name=='neutral' and not detail:
        return frame
    canvas=Image.fromarray(frame)
    draw=ImageDraw.Draw(canvas)
    width,height=size
    if style.name!='neutral':
        left,right=round(width*.045),round(width*.90)
        top=round(height*.032)
        thickness=max(3,round(width*.004))
        draw.rounded_rectangle((left,top,right,top+thickness),radius=thickness//2,fill=(83,82,89))
        end=left+round((right-left)*(completed+1)/total)
        if end>left:
            draw.rectangle((left,top,end,top+thickness),fill=style.accent)
        for boundary in boundaries[:-1]:
            x=left+round((right-left)*boundary/total)
            draw.line((x,top-1,x,top+thickness+1),fill=style.backdrop,width=max(1,width//400))
    if detail and caption_box is not None:
        font=_font(max(14,round(width*.024)))
        label='局部画面'
        padding=max(4,round(width*.008))
        text_width=draw.textlength(label,font=font)
        left=caption_box[0]
        top=caption_box[3]+round(height*.009)
        draw.rounded_rectangle((left,top,left+text_width+padding*2,top+font.size+padding*2),
                               radius=padding,fill=style.panel[:3])
        draw.text((left+padding,top+padding),label,font=font,fill=style.ink,anchor='lt')
    return np.asarray(canvas)


def _video_frames(asset,size,frame_count,fps,start_seconds=0.0,diagnostics=None,audit_events=None):
    """按源帧率取样，复用时接续播放；短片循环，源声音不参与合成。"""
    reader=imageio.get_reader(str(asset.path),format='FFMPEG')
    cached,last_index=None,-1
    count=max(1,asset.source_frames)
    source_fps=asset.source_fps or fps
    frozen=False
    consecutive_failures=0
    def event(kind,index,source_index,**extra):
        if audit_events is not None and len(audit_events)<MAX_AUDIT_EVENTS:
            audit_events.append({'kind':kind,'local_frame':index,'source_frame':source_index,**extra})
    try:
        for index in range(frame_count):
            wanted=int((start_seconds+index/fps)*source_fps+1e-7)%count
            if wanted!=last_index and not frozen:
                try:
                    frame=reader.get_data(wanted)
                except (IndexError,RuntimeError,OSError,StopIteration) as exc:
                    # 对同一帧只重开一次读取器，恢复临时解码错误，不无限重试。
                    _metric('video_read_retries')
                    reader.close()
                    reader=imageio.get_reader(str(asset.path),format='FFMPEG')
                    try:
                        frame=reader.get_data(wanted)
                        event('decode_retry_recovered',index,wanted)
                    except (IndexError,RuntimeError,OSError,StopIteration):
                        frame=None
                    # 容器时长可能包含额外音轨尾部，最后一帧索引可能偏大。
                    if frame is None and wanted>=max(1,count-2):
                        count=max(1,wanted)
                        wanted=int((start_seconds+index/fps)*source_fps+1e-7)%count
                        reader.close()
                        reader=imageio.get_reader(str(asset.path),format='FFMPEG')
                        try:
                            frame=reader.get_data(wanted)
                        except (IndexError,RuntimeError,OSError,StopIteration):
                            frame=None
                    if frame is None:
                        if cached is None:
                            raise ValueError(f'视频在指定位置无法解码：{asset.path.name}') from exc
                        consecutive_failures+=1
                        frozen=consecutive_failures>=3
                        event('decode_hold',index,wanted,remaining_frames=frame_count-index if frozen else 1,
                              permanent=frozen)
                        if consecutive_failures==1 or frozen:
                            message=f'视频 {asset.path.name} 第 {index/fps:.2f} 秒读取失败，'+(
                                '后续镜头复用最后可读画面。' if frozen else '暂用上一可读画面，下一帧继续尝试。')
                            LOGGER.warning(message)
                            if diagnostics is not None:
                                diagnostics.append(message)
                        yield cached
                        continue
                if not frozen and frame is not None:
                    consecutive_failures=0
                    cached=np.asarray(_fit_scene(Image.fromarray(frame),size,asset=asset))
                    last_index=wanted
            if cached is None:
                raise ValueError(f'视频没有可读取的画面：{asset.path.name}')
            yield cached
    finally:
        reader.close()


def _validate_segments(segments):
    if not isinstance(segments,list) or not segments:
        raise ValueError('segments 必须是非空数组')
    seen=set()
    for index,segment in enumerate(segments):
        if not isinstance(segment,dict):
            raise ValueError(f'分镜 {index+1} 必须是对象')
        if type(segment.get('duration')) is not int or segment['duration']<=0:
            raise ValueError(f'分镜 {index+1} 的 duration 必须是正整数秒')
        no=segment.get('no',index+1)
        if not isinstance(no,(int,str)) or isinstance(no,bool) or no in seen:
            raise ValueError('分镜编号必须有效且唯一')
        seen.add(no)
        for key in ('vo','subtitle','visual'):
            if key in segment and not isinstance(segment[key],str):
                raise ValueError(f'分镜 {no} 的 {key} 必须是字符串')


def _warn(diagnostics,message):
    diagnostics.append(message)
    LOGGER.warning(message)


def _verify_timeline(segments, plans, schedules, size, fps):
    if not (len(segments)==len(plans)==len(schedules)):
        raise ValueError('内部时间线分镜数量不一致，未开始编码')
    for segment,shots,schedule in zip(segments,plans,schedules):
        expected=segment['duration']*fps
        if sum(s.frames for s in shots)!=expected or any(s.frames<1 for s in shots):
            raise ValueError('内部镜头时间线不完整，未开始编码')
        if schedule[-1][0]!=expected or any(a[0]>=b[0] for a,b in zip(schedule,schedule[1:])):
            raise ValueError('内部字幕时间线不完整，未开始编码')
        text=str(segment.get('vo') or '').strip() or str(segment.get('subtitle') or '')
        if ''.join(page for _,page in schedule)!=_display_text(text,_caption_metrics(size)[0].size):
            raise ValueError('内部字幕缺失或改写，未开始编码')


def _check_caption_geometry(plans,schedules,size,title):
    checked=0
    for shots,schedule in zip(plans,schedules):
        start=0
        for shot in shots:
            end=start+shot.frames
            page_start=0
            for page_end,page in schedule:
                if page_end>start and page_start<end:
                    font,lines,box,padding=_caption_box(size,page,_view_asset(shot))
                    left,top,right,bottom=box
                    if lines and not (0<=left<right<=size[0] and 0<=top<bottom<=round(size[1]*.90)):
                        raise ValueError('字幕超出安全区，未开始编码')
                    draw=ImageDraw.Draw(Image.new('RGB',(1,1)))
                    if len(lines)>2 or any(draw.textlength(line,font=font)>right-left-2*padding+.5 for line in lines):
                        raise ValueError('字幕文字超出底板，未开始编码')
                    if shot.view=='detail' and lines:
                        label_height=max(14,round(size[0]*.024))+2*max(4,round(size[0]*.008))
                        if bottom+round(size[1]*.009)+label_height>size[1]:
                            raise ValueError('局部提示超出画面，未开始编码')
                    checked+=1
                page_start=page_end
            start=end
    if title:
        font,lines=_short_title(title,size)
        _,left,right,padding,_=_caption_metrics(size)
        bottom=round(size[1]*.065)+len(lines)*round(font.size*1.28)+2*padding
        if not (0<=left<right<=size[0] and bottom<size[1]):
            raise ValueError('标题超出画面，未开始编码')
    return {'checked_caption_layouts':checked,'subtitle_geometry_verified':True,
            'method':'shared_render_geometry_not_OCR'}


class _FrameAudit:
    """编码成片缩略灰度抽样；只保留当前镜头的锚帧，事件有上限。"""
    def __init__(self,plans,report_segments,size,fps):
        self.size,self.fps=size,fps
        self.stride=max(1,round(AUDIT_INTERVAL_SECONDS*fps))
        self.entries=[]
        for shots,row in zip(plans,report_segments):
            for shot,report in zip(shots,row['shots']):
                self.entries.append((shot,report,row))
        self.cursor=-1
        self.events=[]
        self.suppressed=0
        self.sampled=0
        self.black_start=self.black_end=None
        self.still_start=self.still_end=None
        self.anchor=self.anchor_mask=None

    def add_event(self,kind,start,end,severity='warning',**extra):
        shot,report,row=self.entries[self.cursor]
        event={'kind':kind,'severity':severity,'no':row['no'],'asset_id':shot.asset.asset_id,
               'file_path':str(shot.asset.path),'start_frame':start,'end_frame':end,
               'start_seconds':round(start/self.fps,3),'end_seconds':round(end/self.fps,3),**extra}
        if len(self.events)<MAX_AUDIT_EVENTS:
            self.events.append(event)
        else:
            self.suppressed+=1

    def _flush_black(self):
        if self.black_start is not None and (self.black_end-self.black_start)/self.fps>=BLACK_MIN_SECONDS:
            shot,_,_=self.entries[self.cursor]
            samples=(shot.asset.quality or {}).get('samples',[])
            known_dark=bool(samples) and all(s.get('near_black',False) for s in samples if s.get('readable',True))
            self.add_event('source_dark_picture' if known_dark else 'suspected_black_picture',
                           self.black_start,self.black_end,'info' if known_dark else 'warning',
                           basis='encoded_foreground_sparse_samples_not_semantic_judgment')
        self.black_start=self.black_end=None

    def _flush_still(self):
        if self.still_start is not None and (self.still_end-self.still_start)/self.fps>=STILL_MIN_SECONDS:
            shot,_,_=self.entries[self.cursor]
            if shot.asset.media_type=='video':
                source_static=(shot.asset.quality or {}).get('sampled_motion',{}).get('status')=='sampled_static'
                self.add_event('source_static_video' if source_static else 'suspected_video_freeze',
                    self.still_start,self.still_end,'info' if source_static else 'warning',
                    basis='similarity_to_anchor_after_masking_added_text_not_adjacent_frame_only')
        self.still_start=self.still_end=None
        self.anchor=self.anchor_mask=None

    def observe(self,index,gray):
        if index%self.stride:
            return
        if self.cursor<0 or index>=self.entries[self.cursor][1]['end_frame']:
            self._flush_black()
            self._flush_still()
            while self.cursor+1<len(self.entries) and index>=self.entries[self.cursor+1][1]['start_frame']:
                self.cursor+=1
        shot,report,row=self.entries[self.cursor]
        view_asset=_view_asset(shot)
        fraction=(index-report['start_frame'])/max(1,shot.frames-1)
        progress=shot.motion_start+(shot.motion_end-shot.motion_start)*fraction
        left,top,right,bottom=_foreground_bounds(view_asset,self.size,progress,shot.motion_direction)
        height,width=gray.shape
        sx,sy=width/self.size[0],height/self.size[1]
        left,top=max(0,round(left*sx)+1),max(0,round(top*sy)+1)
        right,bottom=min(width,round(right*sx)-1),min(height,round(bottom*sy)-1)
        if right<=left or bottom<=top:
            return
        mask=np.ones(gray.shape,dtype=bool)
        # 去掉新增标题、进度线；避免字幕翻页掩盖卡住的源视频。
        mask[:max(1,round(height*.24)),:]=False
        caption=next((p for p in row['captions'] if p['start_frame']<=index<p['end_frame']),None)
        if caption:
            box=_caption_box(self.size,caption['text'],view_asset)[2]
            x0,y0,x1,y1=box
            mask[max(0,round(y0*sy)-2):min(height,round(y1*sy)+2),
                 max(0,round(x0*sx)-2):min(width,round(x1*sx)+2)]=False
        pixels=gray[top:bottom,left:right]
        valid=mask[top:bottom,left:right]
        if valid.sum()<max(8,valid.size*.15):
            return
        self.sampled+=1
        values=pixels[valid]
        black=float(values.mean())<8 and float(np.percentile(values,95))<16
        end=min(report['end_frame'],index+self.stride)
        if black:
            if self.black_start is None:
                self.black_start=index
            self.black_end=end
        else:
            self._flush_black()
        # 比较固定大小的前景；保留锚帧，缓慢变化也能累计超过阈值。
        signature=np.asarray(Image.fromarray(pixels).resize((64,48),Image.Resampling.BILINEAR),dtype=np.float32)
        valid_small=np.asarray(Image.fromarray(valid).resize((64,48),Image.Resampling.NEAREST),dtype=bool)
        if self.anchor is None:
            self.anchor,self.anchor_mask=signature,valid_small
            self.still_start,self.still_end=index,end
        else:
            common=valid_small & self.anchor_mask
            delta=np.abs(signature-self.anchor)[common]
            similar=delta.size>=200 and float(delta.mean())<.7 and float(np.percentile(delta,95))<3
            if similar:
                self.still_end=end
            else:
                self._flush_still()
                self.anchor,self.anchor_mask=signature,valid_small
                self.still_start,self.still_end=index,end

    def finish(self):
        self._flush_black()
        self._flush_still()
        return {'events':self.events,'suppressed_events':self.suppressed,
            'samples_checked':self.sampled,'sample_interval_seconds':self.stride/self.fps,
            'method':'encoded_grayscale_foreground_samples_with_added_text_masked',
            'limits':'抽样会漏过短暂异常；夜景和固定机位可能正常。相似画面不等于解码故障；不做OCR或事实判断。'}


def _reuse_audit(plans,report_segments,fps):
    by_path=defaultdict(lambda:{'frames':0,'segments':set(),'media_type':None})
    total=sum(s.frames for row in plans for s in row)
    longest=0
    last_path=None
    run=0
    for shots,row in zip(plans,report_segments):
        for shot in shots:
            entry=by_path[str(shot.asset.path)]
            entry['frames']+=shot.frames
            entry['segments'].add(row['no'])
            entry['media_type']=shot.asset.media_type
            run=run+shot.frames if last_path==shot.asset.path else shot.frames
            longest=max(longest,run)
            last_path=shot.asset.path
    result=[]
    for path,data in by_path.items():
        fraction=data['frames']/total
        result.append({'file_path':path,'segment_numbers':sorted(data['segments'],key=str),
            'duration_seconds':data['frames']/fps,'fraction':round(fraction,3),
            'media_type':data['media_type'],
            'frequent_reuse':len(data['segments'])>=3 and fraction>=.65})
    return {'assets':result,'longest_same_source_seconds':longest/fps,
            'policy':'reuse_permitted_report_only_not_rejected'}


def _verify_full_decode(path, expected_frames, fps, audit=None, on_progress=None):
    if audit is not None:
        return _decode_with_audit(path,expected_frames,fps,audit,on_progress)
    command=[imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-nostdin','-v','error','-xerror',
             '-i',str(path),'-map','0:v:0','-progress','pipe:1','-nostats','-f','null','-']
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',
        timeout=max(120,expected_frames/fps*3+60),creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    counts=re.findall(r'^frame=(\d+)\s*$',result.stdout,re.M)
    if result.returncode or not counts or int(counts[-1])!=expected_frames:
        raise ValueError('成片完整解码或帧数校验失败，未发布成片：'+result.stderr[-600:])
    return {'full_decode_verified':True,'decoded_frames':int(counts[-1]),
            'timeline_checked':True,'caption_text_preserved':True}


def _decode_with_audit(path,expected_frames,fps,audit,on_progress):
    width=AUDIT_WIDTH
    height=max(2,round(width*audit.size[1]/audit.size[0]))
    command=[imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-nostdin','-v','error','-xerror',
        '-i',str(path),'-map','0:v:0','-an','-vf',f'scale={width}:{height}',
        '-fps_mode','passthrough','-pix_fmt','gray','-f','rawvideo','pipe:1']
    count=0
    timed_out=[]
    # stderr写临时文件避免缓冲区互锁；stdout逐帧读取，不保存整条视频。
    with tempfile.TemporaryFile() as errors:
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=errors,stdin=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def expire():
            timed_out.append(True)
            try:
                process.kill()
            except OSError:
                pass
        timer=Timer(max(120,expected_frames/fps*3+60),expire)
        timer.daemon=True
        timer.start()
        try:
            while True:
                data=process.stdout.read(width*height)
                if not data:
                    break
                if len(data)!=width*height or count>=expected_frames:
                    raise ValueError('完整解码得到不完整帧或额外帧，未发布成片')
                audit.observe(count,np.frombuffer(data,dtype=np.uint8).reshape(height,width))
                count+=1
                if on_progress and count%max(1,fps*2)==0:
                    on_progress(.95+.04*count/expected_frames,'正在检查成片画面与完整解码……')
            code=process.wait(timeout=15)
            errors.seek(0,2)
            errors.seek(max(0,errors.tell()-1500))
            detail=errors.read().decode('utf-8',errors='replace')
            if code or timed_out or count!=expected_frames:
                raise ValueError('成片完整解码、超时或帧数校验失败，未发布成片：'+detail)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.wait(timeout=15)
    return {'full_decode_verified':True,'decoded_frames':count,'timeline_checked':True,
            'caption_text_preserved':True,'visual_audit':audit.finish()}


@_with_style
def render_video(*,title,segments,media_assets,aspect_ratio,output_path,assets_base_dir,
                 fps=20,on_progress=None,platform=None,caption_schedules=None):
    """返回 (MP4路径, 每个原分镜的主素材ID, 素材替代提示)，兼容旧前端。"""
    started=time.perf_counter()
    if aspect_ratio not in PLATFORM_SIZE:
        raise ValueError(f'不支持的画幅：{aspect_ratio}')
    if not isinstance(title,str):
        raise ValueError('视频标题必须是字符串')
    if type(fps) is not int or fps<=0:
        raise ValueError('fps 必须为正整数')
    _validate_segments(segments)
    if on_progress:
        on_progress(0.0,'正在校验素材并安排镜头与字幕……')
    base_dir=Path(assets_base_dir).resolve()
    output_path=Path(output_path)
    if output_path.suffix.lower()!='.mp4':
        raise ValueError('输出文件必须使用 .mp4 扩展名')
    diagnostics=[]
    assets=_resolve_assets(media_assets,base_dir,diagnostics)
    preflight_finished=time.perf_counter()
    input_identities={a.path:_file_identity(a.path) for a in assets}
    if any(a.path==output_path.resolve() for a in assets):
        raise ValueError('输出路径不能覆盖输入素材')
    decisions=[]
    plans,primary,notices=_plan_shots(segments,assets,base_dir,fps,decisions)
    size=PLATFORM_SIZE[aspect_ratio]
    schedules=([_caption_schedule(segment,size,segment['duration']*fps,fps) for segment in segments]
               if caption_schedules is None else deepcopy(caption_schedules))
    plans=_add_detail_views(plans,fps)
    plans=_refine_timeline(plans,schedules,fps)
    _verify_timeline(segments,plans,schedules,size,fps)
    geometry_validation=_check_caption_geometry(plans,schedules,size,title)
    report_segments=[]
    position=0
    for index,(segment,shots,schedule) in enumerate(zip(segments,plans,schedules)):
        decision=decisions[index]
        if decision['reason']=='script_asset' and decision['selected_evidence']['conflicts']:
            _warn(diagnostics,f'分镜 {segment.get("no",index+1)} 的指定素材描述与脚本画面类型可能冲突；仍尊重指定路径，请核对素材。')
        text=str(segment.get('vo') or '').strip() or str(segment.get('subtitle') or '')
        if _reading_units(text)/segment['duration']>6 or any(
            (end-(schedule[i-1][0] if i else 0))/fps<MIN_CAPTION_SECONDS
            for i,(end,_) in enumerate(schedule)):
            _warn(diagnostics,f'分镜 {segment.get("no",index+1)} 文字相对时长较多；保留全文，建议缩短文案或增加时长。')
        clip_start=position
        clip_report=[]
        for shot in shots:
            view_asset=_view_asset(shot)
            layout=_layout(view_asset,size)
            ratio=min(size[0]*.98/view_asset.width,size[1]*.70/view_asset.height)
            if ratio>2:
                _warn(diagnostics,f'素材 {shot.asset.path.name} 分辨率较低，放大可能模糊。')
            clip_report.append({'asset_id':shot.asset.asset_id,'file_path':str(shot.asset.path),
                'media_type':shot.asset.media_type,'reason':shot.reason,'layout':layout,
                'preserve_full':shot.asset.preserve_full,'start_frame':clip_start,
                'end_frame':clip_start+shot.frames,'video_start_seconds':shot.video_start_seconds,
                'motion_start':shot.motion_start,'motion_end':shot.motion_end,
                'motion_direction':shot.motion_direction,
                'view':shot.view,'focus_box':list(shot.asset.focus_box) if shot.view=='detail' else None,
                'view_size':[view_asset.width,view_asset.height],
                'detail_basis':'upstream_focus_box' if shot.view=='detail' else None,
                'cut_on_caption_boundary':shot.caption_boundary_aligned})
            clip_report[-1]['match_evidence']=_match_evidence(segment,shot.asset)
            clip_report[-1]['content_kind']=shot.asset.content_kind
            clip_start+=shot.frames
        start=0
        caption_report=[]
        for end,page in schedule:
            caption_report.append({'start_frame':position+start,'end_frame':position+end,'text':page,
                'reading_units_per_second':round(_reading_units(page)/((end-start)/fps),2)})
            start=end
        report_segments.append({'no':segment.get('no',index+1),'start_frame':position,
            'end_frame':clip_start,'shots':clip_report,'captions':caption_report,
            'selection':decision})
        position=clip_start
    # 布局与页数完全确定后才开始编码，避免写到一半才发现超长字幕。
    output_path.parent.mkdir(parents=True,exist_ok=True)
    temp_path=output_path.with_name(output_path.stem+'.'+uuid4().hex+'.partial.mp4')
    report_path=output_path.with_suffix('.render.json')
    report_temp=report_path.with_name(report_path.name+'.'+uuid4().hex+'.partial')
    completed=0
    flat_shots=[shot for row in plans for shot in row]
    shot_cursor=0
    boundaries=[row['end_frame'] for row in report_segments]
    title_frames=min(round(TITLE_SECONDS*fps),position)
    title_overlay=_make_caption_overlay(size,title,'')
    source_decode_events=[]
    encoding_started=time.perf_counter()
    try:
        with closing(imageio.get_writer(str(temp_path),format='FFMPEG',mode='I',fps=fps,
                codec='libx264',pixelformat='yuv420p',macro_block_size=None,quality=None,
                ffmpeg_params=['-crf','20','-preset','fast','-movflags','+faststart'])) as writer:
            for segment,shots,schedule in zip(segments,plans,schedules):
                local_frame,page_index=0,0
                for shot in shots:
                    # 新分镜/镜头从正确字幕页开始，避免边界出现上一页文字。
                    while local_frame>=schedule[page_index][0]:
                        page_index+=1
                    view_asset=_view_asset(shot)
                    overlay=_make_caption_overlay(size,'',schedule[page_index][1],view_asset)
                    caption_box=_caption_box(size,schedule[page_index][1],view_asset)[2]
                    enter=shot_cursor>0 and _shot_key(flat_shots[shot_cursor-1])!=_shot_key(shot)
                    leave=shot_cursor+1<len(flat_shots) and _shot_key(flat_shots[shot_cursor+1])!=_shot_key(shot)
                    shot_decode_events=[]
                    frames=(_shot_image_frames(shot,size) if shot.asset.media_type=='image' else
                        _video_frames(shot.asset,size,shot.frames,fps,shot.video_start_seconds,
                                      diagnostics,shot_decode_events))
                    global_start=completed
                    with closing(frames):
                        for shot_index,raw in enumerate(frames):
                            strength=_transition_strength(shot_index,shot.frames,fps,enter,leave)
                            if strength<1:
                                raw=np.asarray(Image.blend(Image.new('RGB',size,_CURRENT_STYLE.get().backdrop),
                                    Image.fromarray(raw),strength))
                            while local_frame>=schedule[page_index][0]:
                                page_index+=1
                                overlay=_make_caption_overlay(size,'',schedule[page_index][1],view_asset)
                                caption_box=_caption_box(size,schedule[page_index][1],view_asset)[2]
                            result=_draw_caption(raw,overlay)
                            if completed<title_frames:
                                remaining=title_frames-completed
                                fade_frames=max(1,round(.35*fps))
                                alpha=min(1.0,remaining/fade_frames)
                                active_title=title_overlay
                                if alpha<1:
                                    active_title=title_overlay.copy()
                                    active_title.putalpha(title_overlay.getchannel('A').point(lambda x:round(x*alpha)))
                                result=_draw_caption(result,active_title)
                            result=_decorate_frame(result,size,completed,position,boundaries,
                                                   shot.view=='detail',caption_box)
                            writer.append_data(result)
                            completed+=1
                            local_frame+=1
                            if on_progress and completed%fps==0:
                                on_progress(min(.94,completed/position*.94),f'正在合成分镜 {segment.get("no","")}……')
                    for event in shot_decode_events:
                        if len(source_decode_events)>=MAX_AUDIT_EVENTS:
                            break
                        source_decode_events.append({**event,'no':segment.get('no'),
                            'asset_id':shot.asset.asset_id,'file_path':str(shot.asset.path),
                            'global_frame':global_start+event['local_frame'],
                            'seconds':round((global_start+event['local_frame'])/fps,3)})
                    shot_cursor+=1
        if completed!=position:
            raise ValueError('编码帧数与规划时间线不一致，未发布成片')
        encoding_finished=time.perf_counter()
        if on_progress:
            on_progress(.95,'正在检查输出文件……')
        with closing(imageio.get_reader(str(temp_path),format='FFMPEG')) as reader:
            meta=reader.get_meta_data()
            reader.get_next_data()
            reader.get_data(position-1)
            if tuple(meta.get('size',()))!=size or abs(_number(meta.get('duration'))-position/fps)>max(.12,2/fps):
                raise ValueError('输出画幅或时长校验失败，未发布成片')
        audit=_FrameAudit(plans,report_segments,size,fps)
        validation=_verify_full_decode(temp_path,position,fps,audit=audit,on_progress=on_progress)
        validation.update(geometry_validation)
        for path,identity in input_identities.items():
            _assert_file_identity(path,identity)
        reuse=_reuse_audit(plans,report_segments,fps)
        visual_audit=validation['visual_audit']
        for event in visual_audit['events']:
            if event['severity']=='warning':
                name={'suspected_black_picture':'疑似近黑画面','suspected_video_freeze':'疑似视频停帧'}.get(event['kind'],event['kind'])
                _warn(diagnostics,f'分镜 {event["no"]}，{event["start_seconds"]:.2f}～{event["end_seconds"]:.2f} 秒：{name}，请结合原素材核对。')
        for entry in reuse['assets']:
            if entry['frequent_reuse']:
                _warn(diagnostics,f'同一素材覆盖 {len(entry["segment_numbers"])} 个分镜、约 {entry["fraction"]:.0%} 时长；按允许复用规则继续生成。')
        material_checks=[]
        for asset in assets:
            bounds=_foreground_bounds(asset,size)
            scale=max((bounds[2]-bounds[0])/asset.width,(bounds[3]-bounds[1])/asset.height)
            material_checks.append({'asset_id':asset.asset_id,'file_path':str(asset.path),
                'media_type':asset.media_type,'width':asset.width,'height':asset.height,
                'content_kind':asset.content_kind,'preserve_full':asset.preserve_full,
                'quality':asset.quality,'quality_notes':_quality_notes(asset),
                'focus_box':list(asset.focus_box) if asset.focus_box is not None else None,
                'target_upscale_factor':round(scale,2),
                'severe_upscale_suspected':scale>2,'used':any(asset.path==s.asset.path for plan in plans for s in plan)})
        finished=time.perf_counter()
        metrics=dict(_CURRENT_METRICS.get() or {})
        report={'renderer_version':'2.5','platform':platform,'style':_CURRENT_STYLE.get().name,
            'aspect_ratio':aspect_ratio,'size':list(size),'fps':fps,
            'total_frames':position,'duration_seconds':position/fps,'title_seconds':title_frames/fps,
            'validation':validation,
            'source_decode_events':source_decode_events,'reuse_check':reuse,
            'performance':{'preflight_seconds':round(preflight_finished-started,4),
                'planning_seconds':round(encoding_started-preflight_finished,4),
                'encoding_seconds':round(encoding_finished-encoding_started,4),
                'validation_seconds':round(finished-encoding_finished,4),
                'total_seconds':round(finished-started,4),'cache_activity':metrics,
                'cache_state':{'preflight':_PROBE_CACHE.info(),'images':_IMAGE_CACHE.info()},
                'cache_lifetime':'process_only_stat_identity_invalidation',
                'memory_limits':'cache_limits_not_total_process_memory_limit_no_full_video_frame_cache'},
            'segments':report_segments,'substitutions':notices,'warnings':list(dict.fromkeys(diagnostics)),
            'material_checks':material_checks,
            'preflight_method':'images_fully_decoded_videos_sampled_up_to_5_positions',
            'quality_limits':'像素指标只作疑似问题提示；素材类别来自上游文字，不识别主体、文字或真实地点。',
            'text_cleaning_notice':'仅画面显示清理不支持的装饰符号；输入JSON不改写。',
            'caption_preferred_seconds':PREFERRED_CAPTION_SECONDS,
            'transition_method':'current_scene_dip_no_previous_picture_under_new_caption',
            'detail_policy':'optional_upstream_focus_box_only_overview_kept',
            'style_basis':'project_presets_not_platform_official_rules',
            'semantic_recognition':False,'audio':'silent'}
        # 视频成功后再替换旧文件；失败时保留原成片。
        report_temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        temp_path.replace(output_path)
        try:
            report_temp.replace(report_path)
        except OSError:
            LOGGER.exception('视频已成功，合成记录写入失败：%s',report_path)
        LOGGER.info('视频合成完成：%s；记录：%s',output_path,report_path)
    finally:
        for path in (temp_path,report_temp):
            if path.exists():
                path.unlink()
    if on_progress:
        on_progress(1.0,'视频已保存，可以预览和下载')
    return output_path,[a.asset_id for a in primary],notices


def generate_platform_video(research,adaptations,platform,*,assets_base_dir,output_path,on_progress=None):
    """保留原入口；后台启用TTS后自动配音，否则保持原静音流程。"""
    if platform not in {'douyin','kuaishou','xiaohongshu'}:
        raise ValueError('不支持的视频平台')
    if not isinstance(research,dict) or not isinstance(adaptations,dict):
        raise ValueError('内部资料和平台结果必须是字典')
    platforms=adaptations.get('platforms')
    data=platforms.get(platform) if isinstance(platforms,dict) else None
    if not isinstance(data,dict):
        raise ValueError('平台适配结果缺少所选平台')
    _validate_segments(data.get('segments'))
    if type(data.get('total_duration')) is not int or sum(s['duration'] for s in data['segments'])!=data['total_duration']:
        raise ValueError('平台脚本总时长与分镜时长之和不一致')
    from tts_agent import tts_enabled, generate_narrated_video
    if tts_enabled():
        return generate_narrated_video(research,data,platform,assets_base_dir=assets_base_dir,
            output_path=output_path,on_progress=on_progress)
    return render_video(title=data.get('title'),segments=data['segments'],
        media_assets=research.get('media_assets',[]),aspect_ratio=data.get('aspect_ratio'),
        output_path=output_path,assets_base_dir=assets_base_dir,on_progress=on_progress,platform=platform)


def generate_video_from_files(research_path,adaptations_path,platform,output_path,on_progress=None):
    research_path=Path(research_path).resolve()
    research=json.loads(research_path.read_text(encoding='utf-8-sig'))
    adaptations=json.loads(Path(adaptations_path).read_text(encoding='utf-8-sig'))
    return generate_platform_video(research,adaptations,platform,
        assets_base_dir=research_path.parent,output_path=output_path,on_progress=on_progress)
