"""启动：python -m streamlit run streamlit_app.py。团队接口在 team_interfaces.py。"""
from __future__ import annotations
from html import escape
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from queue import Empty, Queue
from uuid import uuid4
import streamlit as st
import app_config as config
from team_interfaces import IntegrationPending
from video_generator import generate_platform_video
from tts_agent import TTSError, tts_enabled
from workflow import STAGES, WorkflowState, run_content_workflow, run_video_review

NAMES = {'douyin': '抖音', 'kuaishou': '快手', 'xiaohongshu': '小红书'}
STAGE_NAMES = dict(zip(STAGES, ['资料搜集', '脚本策划', '测评筛选', '多平台适配', '视频制作']))
LOGGER = logging.getLogger(__name__)
st.set_page_config(page_title='山东文旅内容工作台', page_icon='📍', layout='wide')
st.markdown('''<style>
.stApp{background:#f6f7f4;color:#1c302a}.block-container{max-width:1180px;padding-top:2.4rem}
h1,h2,h3{color:#193b32}.eyebrow{color:#557467;font-size:.76rem;letter-spacing:.12em}
.stage{min-height:112px;background:white;padding:14px;border:1px solid #dde4dc;border-radius:8px}
.stage-running{border-color:#27745f;background:#eef6f1}.stage-error{border-color:#b94e47}
.stage-label{font-size:.88rem;font-weight:600;margin-bottom:8px}.stage-detail{font-size:.77rem;color:#63736b}
</style>''', unsafe_allow_html=True)


def show_segments(segments):
    for index, segment in enumerate(segments):
        with st.expander(f"分镜 {segment.get('no', index + 1)} · {segment.get('duration', 0)} 秒", expanded=index == 0):
            for label, key in [('旁白', 'vo'), ('字幕', 'subtitle'), ('画面', 'visual')]:
                st.markdown(f'**{label}**')
                st.write(segment.get(key, ''))
    # 不展示路径、素材 ID、fact_ids 等内部字段。


def show_review_evidence(evidence):
    """Render observations as readable text, with their approximate times."""
    if 'start_seconds' in evidence:
        st.caption(f"约 {evidence['start_seconds']:g}—{evidence['end_seconds']:g} 秒")
    for scene in evidence.get('scenes', []):
        st.write(f"约 {scene['start']:g}—{scene['end']:g} 秒：{scene['description']}")
        if scene.get('on_screen_text'):
            st.caption('画面文字：' + ' / '.join(scene['on_screen_text']))
    detail = evidence.get('text') or evidence.get('title')
    if isinstance(detail, str) and detail:
        st.write(detail)
    if evidence.get('sound_description'):
        st.write(evidence['sound_description'])
    for note in evidence.get('uncertainty', []):
        st.caption('识别限制：' + note)
    url = evidence.get('url')
    if isinstance(url, str) and url.startswith(('https://', 'http://')):
        st.link_button('查看来源', url)


def show_video_analysis(state, platform, *, location):
    """The same saved analysis appears below the video and in the evaluation tab."""
    from evaluation_agent.video_review.models import DIMENSIONS
    video = state.videos[platform]
    review = state.video_reviews.get(platform, {})
    if review.get('video_path') != video['path']:
        review = {}
    result = review.get('result')
    st.markdown('### 成片分析')
    if result and result.get('status') == 'complete':
        st.success('视频理解和分析报告已完成')
    else:
        button = '重试分析' if review else '分析此成片'
        if st.button(button, key=f'review_{location}_{state.job_id}_{platform}'):
            execute_video_review(state, platform)
            st.rerun()
    if not result:
        st.info(review.get('error') or '尚未完成分析；视频可直接播放和下载。')
        return

    coverage = result.get('coverage', {})
    st.caption(f"画面片段 {coverage.get('visual_chunks_read', 0)}/{coverage.get('expected_chunks', 0)}；"
               f"音频片段 {coverage.get('audio_chunks_read', 0)}/{coverage.get('audio_chunks_expected', 0)}。"
               '片段读取成功不代表识别完全准确。')
    if result.get('api_error'):
        st.warning(result['api_error']['message'])
    if result['status'] != 'complete':
        st.warning('分析尚未全部完成，下方保留已成功的结论，可以重试。')
    if result.get('overall'):
        st.markdown('**总体判断**')
        st.write(result['overall'])
    for key, finding in result.get('dimensions', {}).items():
        st.markdown('**' + DIMENSIONS.get(key, key) + '**')
        st.write(finding['conclusion'])
        st.caption('判断性质：' + finding['nature'])
        if finding.get('uncertainty'):
            st.caption('不确定性：' + finding['uncertainty'])
        with st.expander('查看依据 · ' + DIMENSIONS.get(key, key)):
            for evidence_id in finding['evidence_ids']:
                show_review_evidence(result['evidence'][evidence_id])
    for title, key in [('主要优势', 'strengths'), ('限制与未完成项', 'limitations')]:
        if result.get(key):
            with st.expander(title):
                for item in result[key]:
                    st.write('• ' + item)
    with st.expander('画面与声音识别记录'):
        for evidence in result['evidence'].values():
            if evidence['kind'] in {'video_observation', 'audio_observation'}:
                show_review_evidence(evidence)
    directory = Path(review['directory'])
    for filename, label, mime in [('analysis_report.md', '下载文字报告', 'text/markdown'),
                                  ('analysis_report.json', '下载结构化报告', 'application/json')]:
        path = directory / filename
        if path.is_file():
            st.download_button(label, path.read_bytes(), file_name=f'{platform}_{filename}', mime=mime,
                               key=f'{location}_{filename}_{state.job_id}_{platform}')


def show_evaluation(state):
    st.subheader('测评与热度')
    st.caption('生成视频后，分析会自动展示在视频下方，这里同步汇总各平台的结果。播放量与爆款概率预测尚未启用。')
    if not state or not state.videos:
        st.info('请先生成某个平台的视频，完成后将在这里展示对应成片的分析。')
        return
    for platform in state.videos:
        with st.container(border=True):
            st.markdown('### ' + NAMES[platform] + '成片')
            show_video_analysis(state, platform, location='evaluation')


def execute_video_review(state, platform):
    """Wait with visible progress; worker callbacks never invoke Streamlit."""
    messages = Queue()
    started = time.monotonic()
    with st.status('正在理解成片，已生成的视频仍然保留……', expanded=True) as status:
        detail = st.empty()
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_video_review, state, platform, messages.put)
                latest = '准备画面与原始音轨'
                while not future.done():
                    try:
                        while True:
                            latest = messages.get_nowait()
                    except Empty:
                        pass
                    detail.write(f'{latest} · 已等待 {int(time.monotonic() - started)} 秒')
                    wait([future], timeout=1)
                result = future.result()
            if result['status'] == 'complete':
                status.update(label='成片理解与分析报告已生成；识别结果仍有模型局限', state='complete')
            else:
                message = result.get('api_error', {}).get('message') or '分析未全部完成，成功部分已保存，可在测评窗口重试。'
                detail.warning(message)
                status.update(label='分析部分完成或未完成；视频仍可下载', state='error')
        except Exception:
            LOGGER.exception('成片分析未完成，任务=%s，平台=%s', state.job_id, platform)
            detail.warning(state.video_reviews.get(platform, {}).get('error', '分析未完成，原视频仍然保留。'))
            status.update(label='分析未完成，原视频仍然保留', state='error')


def show_platform(platform, state):
    name = NAMES[platform]
    if not state or not state.adaptations:
        st.info(f'完成平台适配后，这里将展示{name}文案与分镜。')
        st.button(f'生成{name}视频', disabled=True, key=f'pending_{platform}')
        return
    data = state.adaptations['platforms'][platform]
    st.subheader(data['title'])
    st.write(data['body'])
    st.write('　'.join(data['hashtags']))
    st.caption(f"画幅 {data['aspect_ratio']} · 脚本参考时长 {data['total_duration']} 秒")
    st.caption('将自动生成配音并对齐画面与字幕。' if tts_enabled() else '静音字幕视频')
    show_segments(data['segments'])
    slot = st.empty()
    if st.button(f'生成{name}视频', type='primary', key=f'video_{state.job_id}_{platform}'):
        state.stages['video'] = ('running', f'正在制作{name}视频……')
        render_stages(state)
        meter = slot.progress(0.0, text='正在检查后台素材……')
        try:
            output = config.GENERATED_DIR / state.job_id / f'{platform}_{uuid4().hex[:8]}.mp4'
            path, _ids, notices = generate_platform_video(
                state.research, state.adaptations, platform,
                assets_base_dir=state.assets_base_dir, output_path=output,
                on_progress=lambda value, text: meter.progress(value, text=text))
            report=json.loads(path.with_suffix('.render.json').read_text(encoding='utf-8'))
            state.videos[platform] = {'path': str(path), 'substituted': bool(notices),
                'duration_seconds':report['duration_seconds'],'audio':report.get('audio','silent')}
            state.video_reviews.pop(platform, None)
            state.videos[platform]['review_pending'] = config.VIDEO_REVIEW_AUTO
            for notice in notices:
                LOGGER.info('素材替代：%s', notice)
            state.stages['video'] = ('complete', f'{name}视频已完成，可继续制作其他平台')
            st.success(f'{name}视频已生成')
        except TTSError as exc:
            LOGGER.warning('配音未完成，任务=%s，平台=%s：%s',state.job_id,platform,exc)
            meter.empty()
            state.stages['video'] = ('error', f'{name}配音或合成未完成，可重试')
            st.error(str(exc))
        except Exception:
            LOGGER.exception('视频制作失败，任务=%s，平台=%s', state.job_id, platform)
            meter.empty()
            state.stages['video'] = ('error', f'{name}视频暂未生成，可重试')
            st.error('视频暂时无法生成。请维护者检查后台素材与依赖后重试；脚本和其他平台已完成的视频仍然保留。')
        render_stages(state)
    video = state.videos.get(platform)
    if video and Path(video['path']).is_file():
        seconds=video.get('duration_seconds')
        if seconds is not None:
            mode='含中文配音' if video.get('audio')=='tts' else '静音字幕视频'
            st.caption(f'成片实际时长 {seconds:g} 秒 · {mode}')
        if video['substituted']:
            st.caption('部分分镜已使用相似或重复画面替代。')
        video_bytes = Path(video['path']).read_bytes()
        st.video(video_bytes, format='video/mp4')
        st.download_button(f'下载{name}视频', data=video_bytes, file_name=f'{platform}.mp4',
            mime='video/mp4', key=f'download_{state.job_id}_{platform}')
        if video.pop('review_pending', False):
            execute_video_review(state, platform)
        show_video_analysis(state, platform, location='video')


st.markdown('<div class="eyebrow">SHANDONG · CULTURE & TRAVEL</div>', unsafe_allow_html=True)
st.title('山东文旅内容工作台')
st.write('描述你的创作想法，从一段要求开始，制作适合不同平台的文旅内容。')
if config.PIPELINE_MODE == "local":
    if config.USE_REAL_C_IN_LOCAL:
        st.warning(
            "联调模式：资料与候选脚本使用后台预置数据；"
            "测评与平台适配会真实执行。"
            "本次输入会作为测评要求，不会重新搜集资料或策划候选脚本。"
        )
    else:
        st.warning(
            "本地联调模式：使用后台预置资料与脚本，"
            "不会根据本次输入重新搜集或策划。"
        )
with st.form('creation_form'):
    prompt = st.text_area('想做一期什么内容？', height=140,
        placeholder='例如：制作一条介绍聊城东昌湖和光岳楼的文旅短视频，面向大众，语言自然易懂，时长约60秒。')
    submitted = st.form_submit_button('开始创作', type='primary', use_container_width=True)
st.subheader('创作进度')
columns = st.columns(5)
slots = {stage: col.empty() for stage, col in zip(STAGES, columns)}
progress_slot = st.empty()


def render_stages(state):
    symbols = {'pending': '○', 'running': '●', 'complete': '✓', 'waiting': '◷', 'skipped': '—', 'error': '!'}
    for stage in STAGES:
        status, message = state.stages[stage] if state else ('pending', '等待开始')
        slots[stage].markdown(
            f'<div class="stage stage-{status}"><div class="stage-label">{symbols[status]} {STAGE_NAMES[stage]}</div>'
            f'<div class="stage-detail">{escape(message)}</div></div>', unsafe_allow_html=True)
    done = sum(status == 'complete' for status, _ in state.stages.values()) if state else 0
    progress_slot.progress(done / len(STAGES), text=f'已完成 {done} / {len(STAGES)} 个阶段')


state = st.session_state.get('workflow')
render_stages(state)
if submitted:
    if not prompt.strip():
        st.warning('请先描述你的创作要求。')
    else:
        state = WorkflowState(prompt=prompt.strip())
        st.session_state['workflow'] = state
        render_stages(state)
        try:
            # 【前端连接后台】实际团队调用在 workflow.py，A/B/C 接口在 team_interfaces.py。
            run_content_workflow(state, lambda *_event: render_stages(state))
            st.success('脚本与三个平台版本已就绪，请选择平台制作视频。')
        except IntegrationPending as exc:
            st.info(str(exc))
        except Exception:
            LOGGER.exception('内容工作流失败，任务=%s', state.job_id)
            st.error('当前创作步骤暂未完成，请稍后重试或联系维护者。已完成的结果会保留。')
st.divider()
script_tab, evaluation_tab = st.tabs(['脚本与分镜', '测评与热度'])
with script_tab:
    st.subheader('筛选后的脚本')
    if state and state.evaluated:
        script = state.evaluated['candidates'][0]['script']
        st.markdown('#### ' + script['title'])
        if script.get('hook'):
            st.write('开场钩子：' + script['hook'])
        st.caption(f"总时长 {script['total_duration']} 秒 · {len(script['segments'])} 个分镜")
        show_segments(script['segments'])
    else:
        st.info('脚本筛选完成后，标题、开场钩子和分镜会显示在这里。')
    if state and state.research:
        with st.expander('参考资料'):
            references = state.research.get('reference_videos', [])
            if not references:
                st.caption('暂无参考视频记录')
            for item in references:
                st.write(item.get('title', '参考资料'))
                st.caption(item.get('topic', ''))
                url = item.get('url', '')
                if isinstance(url, str) and url.startswith(('https://', 'http://')):
                    st.link_button('查看来源', url)
    st.divider()
    st.subheader('多平台适配与视频')
    tabs = st.tabs(list(NAMES.values()))
    for tab, platform in zip(tabs, NAMES):
        with tab:
            show_platform(platform, state)
with evaluation_tab:
    show_evaluation(state)
