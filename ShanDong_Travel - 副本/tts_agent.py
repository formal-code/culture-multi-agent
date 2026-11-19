"""百炼中文配音。密钥仅从环境读取；未开启TTS时不会导入SDK或访问网络。"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import wave

import imageio_ffmpeg

LOGGER=logging.getLogger(__name__)
SAMPLE_RATE=24000
CHANNELS=1
SAMPLE_WIDTH=2
PAUSE_SECONDS=.12
TAIL_SECONDS=.25
MAX_TEMPO=1.12
PAUSE_POLICY_VERSION='semantic_slack_v1'
# 均为程序额外插入的静音，不包含服务音频本身的停顿。
PAUSE_LIMIT_SECONDS={'continuation':PAUSE_SECONDS,'sentence':.40,
                     'question':.55,'observation':1.60}
PAUSE_WEIGHTS={'continuation':0,'sentence':1,'question':2,'observation':4}
ENDING_RESERVE_SECONDS=.60
DEFAULT_MODEL='qwen-audio-3.0-tts-plus'
DEFAULT_VOICE='longanlingxin'
DEFAULT_ENDPOINT='wss://ws-5bzdd4psgp3q9wue.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference'


class TTSError(RuntimeError):
    """可以显示在页面的受控错误，不携带密钥、内部路径或原始服务回复。"""


def tts_enabled():
    value=os.getenv('TRAVEL_TTS_ENABLED','0').strip().lower()
    if value not in {'0','1','false','true'}:
        raise TTSError('配音开关配置无效，请维护者检查后台配置。')
    return value in {'1','true'}


@dataclass(frozen=True)
class TTSConfig:
    model: str=DEFAULT_MODEL
    voice: str=DEFAULT_VOICE
    endpoint: str=DEFAULT_ENDPOINT
    timeout_seconds: int=90
    attempts: int=2
    diagnostic_path: Path | None=None

    @classmethod
    def from_env(cls):
        return cls(model=os.getenv('TRAVEL_TTS_MODEL',DEFAULT_MODEL).strip(),
            voice=os.getenv('TRAVEL_TTS_VOICE',DEFAULT_VOICE).strip(),
            endpoint=os.getenv('TRAVEL_TTS_WS_URL',DEFAULT_ENDPOINT).strip())

    def validate(self):
        if not self.model or not self.voice or not self.endpoint.startswith('wss://'):
            raise TTSError('配音模型、音色或服务地址配置不完整，请维护者检查。')
        if not (1<=self.attempts<=3 and 1<=self.timeout_seconds<=300):
            raise TTSError('配音超时或重试次数配置无效。')


def _run_ffmpeg(arguments,timeout=180):
    command=[imageio_ffmpeg.get_ffmpeg_exe(),'-hide_banner','-nostdin','-y','-v','error',*map(str,arguments)]
    try:
        result=subprocess.run(command,capture_output=True,timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise TTSError('音频处理暂未完成，请重试；已完成视频和配音缓存仍保留。') from exc
    if result.returncode:
        # 只写脱敏错误类型，不把接口、密钥或原始输入送入前端。
        LOGGER.error('FFmpeg音频处理失败，退出码=%s',result.returncode)
        raise TTSError('音频无法解码或合并，请维护者检查音频格式后重试。')


def _wav_samples(path):
    try:
        with wave.open(str(path),'rb') as stream:
            if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())!=(CHANNELS,SAMPLE_WIDTH,SAMPLE_RATE):
                raise ValueError('unexpected PCM format')
            count=stream.getnframes()
            if count<1:
                raise ValueError('invalid duration')
            remaining=count*SAMPLE_WIDTH
            while remaining:
                chunk=stream.readframes(min(8192,remaining//SAMPLE_WIDTH))
                if not chunk:
                    raise ValueError('truncated PCM')
                remaining-=len(chunk)
            return count
    except (OSError,EOFError,wave.Error,ValueError) as exc:
        raise TTSError('配音文件不完整或格式错误，请重新生成受影响的配音。') from exc


def _sha_file(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def _read_cache(path,meta_path,key):
    try:
        meta=json.loads(meta_path.read_text(encoding='utf-8'))
        if meta.get('key')!=key or meta.get('sha256')!=_sha_file(path):
            return None
        count=_wav_samples(path)
        return count if count==meta.get('samples') else None
    except (OSError,ValueError,TTSError):
        return None


def _safe_error(value,text=''):
    """先脱敏再截短；仅导出诊断字段，不导出请求体、完整头或环境变量。"""
    message=str(value or '')
    for name,secret in os.environ.items():
        if secret and any(token in name.upper() for token in ('API_KEY','APIKEY','TOKEN','SECRET','PASSWORD')):
            message=message.replace(secret,'[REDACTED]')
    if text:
        message=message.replace(text,'[TEXT]')
    message=re.sub(r'(?i)\b(?:sk-|secret-key-)[A-Za-z0-9_-]+','[REDACTED]',message)
    message=re.sub(r'(?i)Bearer\s+[^\s\"\',}]+','Bearer [REDACTED]',message)
    message=re.sub(r'(?i)((?:api[-_]?key|access[-_]?token|secret|password|authorization|set-cookie|cookie)[\"\']?\s*[:=]\s*)[^\r\n,}]+',r'\1[REDACTED]',message)
    message=re.sub(r'(https?|wss?)://[^\s/@:]+:[^\s/@]+@',r'\1://[REDACTED]@',message)
    return re.sub(r'\s+',' ',message).strip()[:1200]


def _diagnostic(exc,synthesizer=None,text='',sdk_version='',sdk_logs=()):
    response=getattr(synthesizer,'last_response',None)
    header=response.get('header',{}) if isinstance(response,dict) else {}
    if not isinstance(header,dict):header={}
    detail={
        'error_type':type(exc).__name__,
        'message':_safe_error(getattr(exc,'message',None) or str(exc),text),
        'code':_safe_error(header.get('error_code') or header.get('error_name') or
                           getattr(exc,'code',None) or getattr(exc,'name',None),text),
        'status_code':_safe_error(getattr(exc,'http_code',None) or getattr(exc,'status_code',None)),
        'request_id':_safe_error(header.get('task_id') or getattr(exc,'request_id',None)),
        'sdk_version':_safe_error(sdk_version),
        'sdk_errors':[_safe_error(line,text) for line in list(sdk_logs)[-3:]],
    }
    service_message=header.get('error_message')
    if service_message:
        detail['service_message']=_safe_error(service_message,text)
    return detail


def _sdk_worker(payload):
    """每个SDK调用在独立子进程，防止全局配置串扰，并确保可强制超时退出。"""
    try:
        import dashscope
        from dashscope.audio.tts_v2 import SpeechSynthesizer
    except ImportError:
        return {'status':'sdk_missing'}
    if not os.getenv('DASHSCOPE_API_KEY','').strip():
        return {'status':'key_missing'}
    sdk_version=''
    try:
        from dashscope.version import __version__
        sdk_version=__version__
    except ImportError:
        pass
    synthesizer=None
    sdk_logs=[]
    class ErrorCollector(logging.Handler):
        def emit(self,record):
            # SDK连接失败有时只在日志给出401/代理等原因，主线程只抛TimeoutError。
            sdk_logs.append(_safe_error(record.getMessage(),payload['text']))
            del sdk_logs[:-3]
    collector=ErrorCollector(level=logging.ERROR)
    sdk_logger=logging.getLogger('dashscope')
    sdk_logger.addHandler(collector)
    try:
        dashscope.api_key=os.environ['DASHSCOPE_API_KEY']
        dashscope.base_websocket_api_url=payload['endpoint']
        # 沿用用户提供的接口，不自行替换模型、音色、地域或工作空间。
        synthesizer=SpeechSynthesizer(model=payload['model'],voice=payload['voice'])
        audio=synthesizer.call(payload['text'])
        if not isinstance(audio,(bytes,bytearray)) or not audio or len(audio)>32*1024*1024:
            return {'status':'empty_audio'}
        Path(payload['output']).write_bytes(audio)
        return {'status':'ok'}
    except Exception as exc:
        return {'status':'provider_error','error_type':type(exc).__name__,
                'diagnostic':_diagnostic(exc,synthesizer,payload['text'],sdk_version,sdk_logs)}
    finally:
        sdk_logger.removeHandler(collector)
        if synthesizer is not None:
            try:
                close=getattr(synthesizer,'close',None)
                if close:close()
            except Exception:
                pass


def _call_provider(text,path,config):
    if not os.getenv('DASHSCOPE_API_KEY','').strip():
        raise TTSError('尚未配置配音密钥。请维护者在启动页面的同一终端配置 DASHSCOPE_API_KEY。')
    payload={'model':config.model,'voice':config.voice,'endpoint':config.endpoint,
             'text':text,'output':str(path)}
    last_status='provider_error'
    last_detail={}
    def save_diagnostic(attempt,status,detail):
        if config.diagnostic_path is None:return
        record={'model':config.model,'voice':config.voice,'endpoint':config.endpoint,
            'attempt':attempt,'max_attempts':config.attempts,'status':status,
            'details':detail,'contains_full_request':False,'api_key_included':False}
        # 再做一遍整份脱敏，不保存SDK原始stdout/stderr或环境变量。
        for field in ('model','voice','endpoint'):
            record[field]=_safe_error(record[field],text)
        try:
            Path(config.diagnostic_path).write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        except OSError:
            LOGGER.warning('诊断文件未能保存；请查看终端中的脱敏原因。')
    for attempt in range(config.attempts):
        try:
            result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker'],
                input=json.dumps(payload,ensure_ascii=False),capture_output=True,text=True,
                encoding='utf-8',errors='replace',timeout=config.timeout_seconds,
                env={**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUTF8':'1'},
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            # SDK可能输出额外日志，受控结果用固定前缀区分，不把其余输出转发。
            lines=[line[len('TTS_RESULT:'):] for line in result.stdout.splitlines() if line.startswith('TTS_RESULT:')]
            response=json.loads(lines[-1]) if lines else {}
            last_status=response.get('status','provider_error')
            detail=response.get('diagnostic',{})
            last_detail={k:_safe_error(val,text) for k,val in detail.items() if k in {
                'error_type','message','code','status_code','request_id','sdk_version','service_message'}} if isinstance(detail,dict) else {}
            if isinstance(detail,dict) and isinstance(detail.get('sdk_errors'),list):
                last_detail['sdk_errors']=[_safe_error(line,text) for line in detail['sdk_errors'][-3:]]
            if not lines:
                last_status='worker_error'
                last_detail['worker_error']=_safe_error(result.stderr[-2000:],text)
                last_detail['exit_code']=result.returncode
            save_diagnostic(attempt+1,last_status,last_detail)
            if result.returncode==0 and last_status=='ok' and path.is_file():
                return
            if last_status in {'sdk_missing','key_missing'}:
                break
        except subprocess.TimeoutExpired:
            last_status='timeout'
            last_detail={'message':f'单次配音调用超过 {config.timeout_seconds} 秒，已终止子进程。'}
            save_diagnostic(attempt+1,last_status,last_detail)
        except (OSError,ValueError):
            last_status='worker_error'
            last_detail={'message':'配音子进程启动失败或未返回有效JSON。'}
            save_diagnostic(attempt+1,last_status,last_detail)
        LOGGER.warning('配音调用未完成，状态=%s，尝试=%s/%s',last_status,attempt+1,config.attempts)
        if last_detail:
            LOGGER.warning('脱敏诊断：%s',json.dumps(last_detail,ensure_ascii=False))
        if attempt+1<config.attempts:
            time.sleep(.7)
    if last_status=='sdk_missing':
        raise TTSError('尚未安装百炼语音 SDK，请维护者安装新版 requirements.txt。')
    if last_status=='key_missing':
        raise TTSError('未配置 DASHSCOPE_API_KEY，请维护者检查后台环境。')
    diagnostic_hint='请查看 tts_diagnostic.json 中的脱敏服务原因。' if config.diagnostic_path else '具体服务原因已记录在后台终端。'
    raise TTSError('配音未完成：'+diagnostic_hint+'成功音频缓存保持不变。')


def cached_speech(text,cache_dir,config,provider=None):
    config.validate()
    cache_dir=Path(cache_dir)
    cache_dir.mkdir(parents=True,exist_ok=True)
    params={'schema':'bailian-tts-pcm-1','model':config.model,'voice':config.voice,
            'endpoint':config.endpoint,'text':text,'sample_rate':SAMPLE_RATE,
            'channels':CHANNELS,'sample_width':SAMPLE_WIDTH,'provider_speech_rate':'default'}
    key=hashlib.sha256(json.dumps(params,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()
    path=cache_dir/f'{key}.wav'
    meta_path=cache_dir/f'{key}.json'
    count=_read_cache(path,meta_path,key)
    if count is not None:
        return {'path':path,'samples':count,'key':key,'cache_hit':True}
    with tempfile.TemporaryDirectory(prefix='tts_build_',dir=cache_dir) as temporary:
        raw=Path(temporary)/'source.mp3'
        pcm=Path(temporary)/'normalized.wav'
        (provider or _call_provider)(text,raw,config)
        if not raw.is_file() or raw.stat().st_size<1:
            raise TTSError('配音服务未返回可用音频，未生成新成片。')
        _run_ffmpeg(['-i',raw,'-vn','-ac',CHANNELS,'-ar',SAMPLE_RATE,'-c:a','pcm_s16le',pcm])
        count=_wav_samples(pcm)
        meta={**params,'key':key,'samples':count,'sha256':_sha_file(pcm)}
        temp_meta=Path(temporary)/'metadata.json'
        temp_meta.write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding='utf-8')
        pcm.replace(path)
        temp_meta.replace(meta_path)
    return {'path':path,'samples':count,'key':key,'cache_hit':False}


def _write_silence(stream,count):
    while count>0:
        amount=min(8192,count)
        stream.writeframesraw(b'\x00'*(amount*SAMPLE_WIDTH))
        count-=amount


def _append_wav(target,path):
    with wave.open(str(path),'rb') as source:
        while True:
            block=source.readframes(8192)
            if not block:
                break
            target.writeframesraw(block)


def _pause_boundary(texts,index):
    """只判断已有配音页的末尾；不拆音频，不做画面识别或改写。"""
    ending=texts[index].rstrip().rstrip('"\'”’）)]】').rstrip()
    if not ending or ending[-1] not in '。！？!?；;，,：:.':
        return 'continuation'
    punctuation=ending[-1]
    # 向前看少量相邻页，兼容一句话因字幕宽度拆成多页。
    context=''.join(texts[max(0,index-3):index])+ending
    body=context.rstrip('。！？!?；;，,：:.')
    sentence=re.split(r'[。！？!?；;\n]|(?<!\d)\.|\.(?!\d)',body)[-1].strip(' "\'”’（()【】')
    clause=re.split(r'[，,：:]',sentence)[-1].strip() if punctuation in '，,：:' else sentence
    # 设问、否定指令以及尚未给出观察对象的提示不安排长停顿。
    rhetorical=bool(re.search(r'(?:你知道吗|大家知道吗|知道为什么吗|你可知道|你知道为什么|你听说过吗|为什么|为何|是不是很|难道|岂不是|何尝|why\b|did you know\b|do you know\b)',clause,re.I))
    negated=bool(re.search(r'(?:不要|不用|无需|别|不必|不能|不需要|不建议)(?:再|去|急着|仔细|认真|试着|一起|继续|让孩子|让宝贝|只|光)*[找看数猜想观比辨指]|\b(?:do not|don.t|never)\s+(?:look|find|spot|count|guess|observe|compare)\b',clause,re.I))
    unfinished=bool(re.search(r'(?:找一找|找找|数一数|数数|猜一猜|猜猜|想一想|想想|观察一下|仔细看看|认真看看|看看|看一看|以及|还有|或者|然后|比如|包括|和|与|的|是|把|让|向|到|先|再)$',clause)) or punctuation in '：:'
    directive=bool(re.search(r'(?:找一找|找找|(?:先|再|请|试着|一起)找(?:出|到)?|数一数|数数|猜一猜|猜猜|想一想|想想|观察一下|仔细看(?:看)?|认真看(?:看)?|看看|看一看|对比一下|比较一下|辨认一下|认一认|指一指).+',clause))
    visual_question=punctuation in '？?' and bool(re.search(
        r'(?:什么颜色|哪(?:一)?种颜色|几种色[彩调]|几种颜色|你(?:们)?(?:能|有没有|已经)?(?:找[到着]|看[到见]|发现|注意到|认出)|[找看]到了吗|找出来了吗|(?:旁边|上面|图中|画面|海报).*(?:写着|写了|有).*(?:什么|哪|几)|(?:画面|照片|图片|眼前|图中).*(?:几[个只朵棵条艘处片座块]|多少|哪|什么不同))',clause))
    english=bool(re.search(r'\b(?:look at|take a look at|find|spot|count|guess|observe|compare)\b\s+.+',clause,re.I))
    english_question=punctuation in '？?' and bool(re.search(r'\b(?:what colo[u]?r|how many .+ (?:can|do) you see|can you (?:see|find|spot)|what do you see)\b',clause,re.I))
    if not (rhetorical or negated or unfinished) and (directive or visual_question or english or english_question):
        return 'observation'
    if punctuation in '？?':
        return 'question'
    if punctuation in '。！!；;.':
        return 'sentence'
    return 'continuation'


def _weighted_pause_samples(capacities,weights,budget):
    """以整数采样点分配，优先观察边界；总量与每项上限均严格守恒。"""
    allocated=[0]*len(capacities)
    remaining=min(max(0,budget),sum(capacities))
    while remaining:
        active=[i for i,cap in enumerate(capacities) if cap>allocated[i] and weights[i]>0]
        if not active:
            break
        weight_sum=sum(weights[i] for i in active)
        portions={i:min(capacities[i]-allocated[i],remaining*weights[i]//weight_sum) for i in active}
        used=sum(portions.values())
        if not used:
            order=sorted(active,key=lambda i:(-(remaining*weights[i]%weight_sum),-weights[i],i))
            for i in order[:remaining]:
                portions[i]=1
            used=sum(portions.values())
        for i,amount in portions.items():
            allocated[i]+=amount
        remaining-=used
    return allocated


def _plan_segment_pauses(units,target_samples):
    """只移动已有静音；不靠增加停顿延长分镜，也不动原始音频。"""
    base=round(PAUSE_SECONDS*SAMPLE_RATE)
    gaps=[base]*max(0,len(units)-1)
    baseline_tail=target_samples-sum(u['samples'] for u in units)-sum(gaps)
    if baseline_tail<0:
        raise TTSError('配音时间线越界，未截断语音；请维护者检查。')
    texts=[u['text'] for u in units]
    kinds=[_pause_boundary(texts,i) for i in range(len(texts))]
    reserve_seconds=(PAUSE_LIMIT_SECONDS['observation'] if kinds and kinds[-1]=='observation'
                     else ENDING_RESERVE_SECONDS)
    reserve=min(baseline_tail,round(max(TAIL_SECONDS,reserve_seconds)*SAMPLE_RATE))
    capacities=[max(0,round(PAUSE_LIMIT_SECONDS[kind]*SAMPLE_RATE)-base) for kind in kinds[:-1]]
    extra=_weighted_pause_samples(capacities,[PAUSE_WEIGHTS[k] for k in kinds[:-1]],
                                   baseline_tail-reserve)
    gaps=[gap+addition for gap,addition in zip(gaps,extra)]
    moved=sum(extra)
    tail=baseline_tail-moved
    return {'gaps':gaps,'tail':tail,'kinds':kinds,'report':{
        'policy_version':PAUSE_POLICY_VERSION,
        'status':'redistributed' if moved else ('unchanged_no_speech' if not units else 'unchanged'),
        'baseline_tail_samples':baseline_tail,'baseline_tail_seconds':round(baseline_tail/SAMPLE_RATE,6),
        'final_tail_samples':tail,'final_tail_seconds':round(tail/SAMPLE_RATE,6),
        'redistributed_samples':moved,'redistributed_seconds':round(moved/SAMPLE_RATE,6),
        'reserved_tail_samples':reserve,'boundary_kinds':kinds,'internal_gap_samples':gaps,
        'limited_by_boundary_caps':bool(units and tail>reserve)}}


def prepare_narration(segments,size,fps,work_dir,cache_dir,config,caption_pages,caption_schedule,
                      on_progress=None,provider=None):
    """实际音频长度驱动时间线；无服务时间戳，字幕只做到短句边界对齐。"""
    adjusted=deepcopy(segments)
    schedules=[]
    reports=[]
    work_dir=Path(work_dir)
    work_dir.mkdir(parents=True,exist_ok=True)
    audio_path=work_dir/'narration.wav'
    cursor=0
    pause=round(PAUSE_SECONDS*SAMPLE_RATE)
    total_requests=0
    cache_hits=0
    has_speech=False
    with wave.open(str(audio_path),'wb') as track:
        track.setnchannels(CHANNELS);track.setsampwidth(SAMPLE_WIDTH);track.setframerate(SAMPLE_RATE)
        for index,(original,segment) in enumerate(zip(segments,adjusted)):
            no=original.get('no',index+1)
            vo=str(original.get('vo') or '').strip()
            text_pages=caption_pages(vo,size) if vo else []
            units=[]
            for unit_index,text in enumerate(text_pages):
                if not text.strip():
                    continue
                if on_progress:
                    on_progress(.04+.25*(index+(unit_index/max(1,len(text_pages))))/len(segments),
                        f'正在生成分镜 {no} 的配音（短句 {unit_index+1}/{len(text_pages)}）……')
                item=cached_speech(text,cache_dir,config,provider)
                total_requests+=int(not item['cache_hit'])
                cache_hits+=int(item['cache_hit'])
                units.append({**item,'text':text})
            original_seconds=original['duration']
            natural_samples=sum(u['samples'] for u in units)+pause*max(0,len(units)-1)
            available=max(1,round((original_seconds-TAIL_SECONDS)*SAMPLE_RATE)-pause*max(0,len(units)-1))
            speech_samples=sum(u['samples'] for u in units)
            tempo=min(MAX_TEMPO,max(1.,speech_samples/available)) if units else 1.
            if tempo>1.0001:
                for unit_index,unit in enumerate(units):
                    paced=work_dir/f'paced_{index}_{unit_index}.wav'
                    _run_ffmpeg(['-i',unit['path'],'-filter:a',f'atempo={tempo:.8f}',
                        '-ar',SAMPLE_RATE,'-ac',CHANNELS,'-c:a','pcm_s16le',paced])
                    unit['path']=paced
                    unit['samples']=_wav_samples(paced)
            voiced_samples=sum(u['samples'] for u in units)+pause*max(0,len(units)-1)
            seconds=max(original_seconds,math.ceil((voiced_samples+round(TAIL_SECONDS*SAMPLE_RATE))/SAMPLE_RATE)) if units else original_seconds
            segment['duration']=seconds
            count=seconds*fps
            target_samples=seconds*SAMPLE_RATE
            pause_plan=_plan_segment_pauses(units,target_samples)
            local=0
            pages=[]
            unit_reports=[]
            for unit_index,unit in enumerate(units):
                start=local
                _append_wav(track,unit['path'])
                local+=unit['samples']
                end=local
                pause_after=(pause_plan['gaps'][unit_index] if unit_index+1<len(units)
                             else pause_plan['tail'])
                if unit_index+1<len(units):
                    _write_silence(track,pause_after);local+=pause_after
                page_end=min(count,math.ceil(local/SAMPLE_RATE*fps)) if unit_index+1<len(units) else count
                if pages and page_end<=pages[-1][0]:
                    raise TTSError('短句语音过短，无法分配字幕帧，未生成新成片。')
                pages.append((page_end,unit['text']))
                unit_reports.append({'text':unit['text'],'cache_key':unit['key'],'cache_hit':unit['cache_hit'],
                    'start_sample':cursor+start,'end_sample':cursor+end,
                    'start_seconds':round((cursor+start)/SAMPLE_RATE,4),
                    'end_seconds':round((cursor+end)/SAMPLE_RATE,4),
                    'pause_after_samples':pause_after,'pause_after_seconds':round(pause_after/SAMPLE_RATE,6),
                    'pause_kind':pause_plan['kinds'][unit_index]})
            if local>target_samples:
                raise TTSError('配音时间线越界，未截断语音；请维护者检查。')
            _write_silence(track,target_samples-local)
            if not pages:
                pages=caption_schedule(original,size,count,fps)
            schedules.append(pages)
            reports.append({'no':no,'original_duration_seconds':original_seconds,
                'actual_duration_seconds':seconds,'extended':seconds>original_seconds,
                'natural_audio_seconds':round(natural_samples/SAMPLE_RATE,4),
                'tempo_factor':round(tempo,6),'start_sample':cursor,'end_sample':cursor+target_samples,
                'vo_empty':not bool(vo),'pause_allocation':pause_plan['report'],'units':unit_reports})
            cursor+=target_samples
            has_speech=has_speech or bool(units)
    if _wav_samples(audio_path)!=cursor:
        raise TTSError('配音轨长度校验失败，未生成新成片。')
    return {'segments':adjusted,'caption_schedules':schedules,'audio_path':audio_path,
        'has_speech':has_speech,'sample_count':cursor,'duration_seconds':cursor/SAMPLE_RATE,
        'report':{'provider':'dashscope.audio.tts_v2.SpeechSynthesizer','model':config.model,
            'voice':config.voice,'sample_rate':SAMPLE_RATE,'channels':CHANNELS,
            'alignment':'measured_short_phrase_audio_not_word_timestamps',
            'speed_policy':'provider_default_then_pitch_preserving_atempo_max_1.12',
            'pause_seconds':PAUSE_SECONDS,'tail_seconds':TAIL_SECONDS,
            'pause_policy':{'version':PAUSE_POLICY_VERSION,
                'strategy':'redistribute_existing_tail_at_existing_phrase_boundaries',
                'inserted_pause_limits_seconds':dict(PAUSE_LIMIT_SECONDS),
                'ordinary_ending_reserve_seconds':ENDING_RESERVE_SECONDS,
                'observation_ending_reserve_seconds':PAUSE_LIMIT_SECONDS['observation'],
                'includes_provider_internal_silence':False},
            'provider_calls':total_requests,'cache_hits':cache_hits,'segments':reports}}


def _publish_pair(staged_video,staged_report,output,work):
    """发布中途出错时恢复旧成片及旧报告。"""
    report=output.with_suffix('.render.json')
    destinations=[(staged_video,output),(staged_report,report)]
    backups=[]
    published=[]
    try:
        for index,(_,destination) in enumerate(destinations):
            if destination.exists():
                backup=work/f'previous_{index}{destination.suffix}'
                destination.replace(backup)
                backups.append((backup,destination))
        for source,destination in destinations:
            source.replace(destination)
            published.append(destination)
    except Exception:
        for destination in published:
            destination.unlink(missing_ok=True)
        for backup,destination in backups:
            backup.replace(destination)
        raise


def generate_narrated_video(research,data,platform,*,assets_base_dir,output_path,on_progress=None):
    import video_generator as renderer
    output=Path(output_path).resolve()
    if output.suffix.lower()!='.mp4':
        raise TTSError('输出视频必须是 MP4。')
    if data.get('aspect_ratio') not in renderer.PLATFORM_SIZE:
        raise TTSError('平台画幅不受支持。')
    renderer._validate_segments(data.get('segments'))
    config=TTSConfig.from_env();config.validate()
    # 在付费请求前检查素材，不能因素材缺失先消耗所有配音调用。
    assets=renderer._resolve_assets(research.get('media_assets',[]),assets_base_dir)
    if any(a.path==output for a in assets):
        raise TTSError('输出路径不能覆盖输入素材。')
    fps=20
    started=time.perf_counter()
    output.parent.mkdir(parents=True,exist_ok=True)
    cache_dir=Path(os.getenv('TRAVEL_TTS_CACHE_DIR',str(Path(__file__).parent/'tts_cache'))).resolve()
    if on_progress:on_progress(0.,'正在准备分镜配音……')
    with tempfile.TemporaryDirectory(prefix='narrated_',dir=output.parent) as temporary:
        work=Path(temporary)
        token=renderer._CURRENT_STYLE.set(renderer.STYLES[platform])
        try:
            voice=prepare_narration(data['segments'],renderer.PLATFORM_SIZE[data['aspect_ratio']],fps,
                work,cache_dir,config,renderer._caption_pages,renderer._caption_schedule,on_progress)
        finally:
            renderer._CURRENT_STYLE.reset(token)
        prepared=time.perf_counter()
        silent=work/'visual.mp4'
        visual_progress=(lambda value,text:on_progress(.30+.60*value,text)) if on_progress else None
        _,ids,notices=renderer.render_video(title=data['title'],segments=voice['segments'],
            media_assets=research.get('media_assets',[]),aspect_ratio=data['aspect_ratio'],
            output_path=silent,assets_base_dir=assets_base_dir,fps=fps,platform=platform,
            on_progress=visual_progress,caption_schedules=voice['caption_schedules'])
        final=work/'final.mp4'
        if on_progress:on_progress(.91,'正在合并配音并检查音画时长……')
        if voice['has_speech']:
            _run_ffmpeg(['-i',silent,'-i',voice['audio_path'],'-map','0:v:0','-map','1:a:0',
                '-c:v','copy','-c:a','aac','-b:a','128k','-ar',SAMPLE_RATE,'-ac',CHANNELS,
                '-movflags','+faststart',final],timeout=max(180,voice['duration_seconds']*2))
            checked=work/'decoded_soundtrack.wav'
            _run_ffmpeg(['-xerror','-i',final,'-map','0:a:0','-vn','-ac',CHANNELS,'-ar',SAMPLE_RATE,
                '-c:a','pcm_s16le',checked],timeout=max(180,voice['duration_seconds']*2))
            actual_samples=_wav_samples(checked)
            if abs(actual_samples-voice['sample_count'])>SAMPLE_RATE*.12:
                raise TTSError('合并后的配音时长校验失败，未发布新成片。')
        else:
            silent.replace(final)
            actual_samples=0
        final_validation=renderer._verify_full_decode(final,sum(s['duration'] for s in voice['segments'])*fps,fps)
        report_path=silent.with_suffix('.render.json')
        report=json.loads(report_path.read_text(encoding='utf-8'))
        report['audio']='tts' if voice['has_speech'] else 'silent_no_vo'
        report['tts']=voice['report']
        report['tts']['original_total_duration_seconds']=data['total_duration']
        report['tts']['actual_total_duration_seconds']=voice['duration_seconds']
        report['tts']['final_decoded_audio_samples']=actual_samples
        report['validation']['final_mux_full_decode_verified']=final_validation['full_decode_verified']
        report['validation']['audio_duration_verified']=True if voice['has_speech'] else None
        report['performance']['tts_prepare_seconds']=round(prepared-started,4)
        report['performance']['total_with_tts_seconds']=round(time.perf_counter()-started,4)
        report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        _publish_pair(final,report_path,output,work)
    if on_progress:on_progress(1.,'视频已保存，可以预览和下载')
    return output,ids,notices


def main():
    parser=argparse.ArgumentParser(description='百炼短口播试听，仅生成一条短语音，不执行整段视频。')
    parser.add_argument('--sample',action='store_true')
    parser.add_argument('--diagnose',action='store_true',help='短口播只尝试一次，并保存脱敏诊断文件')
    parser.add_argument('--output',default='tts_sample.mp3')
    parser.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.worker:
        payload=json.loads(sys.stdin.read())
        result=_sdk_worker(payload)
        print('TTS_RESULT:'+json.dumps(result,ensure_ascii=False),flush=True)
        return 0 if result['status']=='ok' else 1
    if not args.sample:
        parser.print_help();return 0
    config=TTSConfig.from_env()
    if args.diagnose:
        config=replace(config,attempts=1,diagnostic_path=Path('tts_diagnostic.json').resolve())
        print(f'单次诊断模式：模型={config.model}；音色={config.voice}',flush=True)
    text='跟着我们走进山东，看看湖光古城，发现传统手艺的美。'
    output=Path(args.output).resolve()
    output.parent.mkdir(parents=True,exist_ok=True)
    cache=Path(os.getenv('TRAVEL_TTS_CACHE_DIR',str(Path(__file__).parent/'tts_cache')))
    try:
        item=cached_speech(text,cache,config)
        _run_ffmpeg(['-i',item['path'],'-c:a','libmp3lame','-b:a','128k',output])
        print(f'试听已保存：{output}\n模型：{config.model}\n音色：{config.voice}\n时长：{item["samples"]/SAMPLE_RATE:.2f} 秒')
        print('请先试听。确认声音合适后，再启用完整视频配音。')
        return 0
    except TTSError as exc:
        print(str(exc),file=sys.stderr);return 1


if __name__=='__main__':
    raise SystemExit(main())