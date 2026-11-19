# 山东文旅内容工作台

本版用户页面只需要输入创作要求，不提供 JSON/图片上传、磁盘路径输入、模型密钥设置或内部数据展示。
后台处理 A 的资料、B 的候选脚本和 C 的筛选结果，用户选择一个平台后生成静音字幕视频。

## 文件与放置位置

将以下 8 个文件解压到同一个 PyCharm 项目目录：

- streamlit_app.py：页面，包含真实阶段状态、两个独立结果区和三平台视频窗口。
- app_config.py：仅供开发者修改的后台路径与运行模式。
- team_interfaces.py：A/B/C 预留函数；查找“预留接口”注释。
- workflow.py：将提示词依次传给 A、B、C，再调用现有一体化 agent。
- integrated_agent.py：原一体化 agent，继续使用百炼模型接口。
- video_generator.py：读取内部数据匹配图片/视频，输出静音字幕 MP4。
- requirements.txt：安装依赖。
- README.md：本说明。

如果原一体化 agent 在你的项目中叫 main.py，请保留原文件备份，并使用本包提供的 integrated_agent.py。
页面按这个文件名导入，不要覆盖为 main.py。

## 安装与启动（PyCharm Terminal / PowerShell）

先确认项目已选好 Python 解释器。建议 Python 3.10 或更新版本。

    python -m pip install -r requirements.txt

只有实际调用平台适配模型时需要百炼密钥。与启动页面使用同一个 Terminal：

    $env:DASHSCOPE_API_KEY="你的百炼API Key"
    python -m streamlit run streamlit_app.py

浏览器打开命令显示的本地网址。不要通过普通的 python streamlit_app.py 启动页面。
如果密钥仅配置在 PyCharm 某个 Python 运行配置中，Terminal 启动的 Streamlit 不会自动继承那项配置。
不在前端输入密钥，不要提交真实密钥。

## 正式接口模式（默认）

app_config.py 默认 PIPELINE_MODE 为 agents。
页面可以启动并查看全部占位区域。点击“开始创作”后会调用 A；
A/B/C 还未接入时会停在对应步骤，明确提示暂未接入，不伪造进度与结果。

依次修改 team_interfaces.py 中的三个函数体：

1. collect_research(prompt, work_dir)
   输入用户完整提示词和工作目录。返回 ResearchPackage(data=资料字典, base_dir=素材包根目录)。
   A 的结果继续包含 schema_version、reference_videos，并新增 media_assets。
   返回前必须完成实际素材文件准备；路径相对于 base_dir。

2. plan_scripts(prompt, research)
   输入提示词与 A 的完整资料字典，返回 B 的候选脚本字典（含 candidates 数组）。

3. evaluate_scripts(prompt, research, candidates)
   输入提示词、资料与候选脚本，返回 C 的原约定字典；
   topic / candidates / candidate_id / script 等结构不变，candidates 恰好保留一条。

如果团队模块交付的是 JSON 文件路径，在接口函数内读取成字典再返回；
可以使用 workflow.read_json（或 json.loads + UTF-8-SIG）。
不需要让用户手动上传任何文件，也不要将文件路径暴露到页面。

工作流：
提示词 -> A -> B -> C -> 平台适配 -> 用户选择平台 -> 视频合成 -> 页面预览和下载

视频读取 A 的完整资料字典与平台适配结果。
C 的 evaluated_script 先传给一体化 agent 得到各平台分镜；视频合成不会用一份原脚本冒充三个平台版本。
原一体化 agent 不需要新增素材处理；A 的 media_assets 在后台单独保留，直接传入视频 agent。

## A/B/C 尚未接入时的后台联调

这个模式只是开发者联调已有 JSON，不会根据提示词真实搜集资料。
页面会明确显示“本地联调模式”，普通用户仍无文件上传操作。

内部目录：

    项目目录/
    ├─ streamlit_app.py
    ├─ integrated_agent.py
    ├─ video_generator.py
    ├─ app_config.py
    ├─ workflow.py
    ├─ team_interfaces.py
    ├─ Json/
    │  ├─ research.json
    │  ├─ evaluated_script.json
    │  ├─ platform_adaptations.json  （可选，避免再次调用模型）
    │  └─ assets/
    │     ├─ lake.jpg
    │     └─ craft.mp4
    └─ generated_videos/

如需改内部路径，只修改 app_config.py，页面无目录设置。
启动联调：

    $env:TRAVEL_PIPELINE_MODE="local"
    python -m streamlit run streamlit_app.py

若无已有 platform_adaptations.json，仍需 DASHSCOPE_API_KEY 来生成适配版本。
有已有适配结果时会检查主题、筛选编号与分镜，防止混用不对应的文件。
本地模式跳过 B、读取 C 的结果，不声称已在线策划或测评。

正式接入后恢复：

    $env:TRAVEL_PIPELINE_MODE="agents"
    python -m streamlit run streamlit_app.py

## 视频 agent 内部调用

传字典（推荐由后台流程调用）：

    from video_generator import generate_platform_video

    path, selected_ids, notices = generate_platform_video(
        research=research_data,
        adaptations=platform_adaptations,
        platform="douyin",
        assets_base_dir=research_package_dir,
        output_path="generated_videos/douyin.mp4",
        on_progress=lambda fraction, message: print(message),
    )

也支持后台文件入口 generate_video_from_files(research_path, adaptations_path, platform, output_path)；
素材路径自动相对于 research_path 所在目录解析，无前端上传依赖。

匹配使用分镜 visual / vo / subtitle 和素材 title / description / keywords。
这是关键词匹配，不会分析真实画面内容；A 的素材描述和关键词应准确。
分镜没有匹配时优先复用前面已选素材；第一段无匹配时使用素材包首个可读素材，不因未匹配停止合成。
损坏或缺失的素材会在检查时跳过。若全部素材均不可读，明确报错；没有真实素材无法生成真实画面。
具体素材 ID 与技术诊断只留在内部结果/日志，用户页面仅提示部分画面被替换。

图片按分镜时长显示；视频从头截取，短于分镜则循环，统一帧率并移除原声。
输出 H.264 MP4：抖音/快手 1080x1920，小红书 1080x1440，20 fps。
画面叠加标题与分镜 subtitle，不把 visual 拍摄指令写到成片里。
vo 保留在分镜展示与内部数据中，为后续 TTS 使用；当前成片静音、不加背景音乐。
生成失败不会覆盖其他平台的已完成视频。视频按独立任务保存到 generated_videos/<任务编号>/。

Windows 默认使用系统中文字体。若部署到其他系统，请提供中文字体并修改 video_generator._font 的路径。
imageio-ffmpeg 安装包提供合成所需 FFmpeg。

## 成片理解与分析

默认在成片生成后自动分析：先展示视频和下载按钮，再显示分析进度，完成后直接在视频下方展示总体判断、六维结论和报告下载。“测评与热度”页同步汇总各平台的结果；查看结果、切换页面不会再次调用模型。若分析失败，视频和成功的结论仍保留，可点击“重试分析”。当前实现两步：读取实际画面和独立音轨，再输出六维依据报告；不会预测播放量、爆款概率，也不会编辑成片。每段结果与最终报告保存到 `video_reviews/<任务编号>/`，可下载 Markdown 和 JSON。

测评模型读取项目根目录的 `c_agent.env`。至少配置 `DATAOPENAI_API_KEY`；视频模型默认为 `qwen3.8-omni-flash`，需要时可用 `OMNI_MODEL` 覆盖。指定 env 文件的值优先于 IDE/终端遗留环境变量。密钥只留在本机，不要提交。

也可在终端单独运行：

    python -m evaluation_agent.video_review report "成片.mp4" --env-file c_agent.env --output video_reviews --platform douyin

第一版支持最长 300 秒；分段长度可通过 `VIDEO_REVIEW_CHUNK_SECONDS` 设为 5–30 秒。处理成功的画面、音频和分析维度会缓存并复用。若音轨在程序分段处出现半句话，报告会结合相邻段核对，不将分段痕迹直接认作原片故障。

## 后续扩展位置

- 热度预测：`streamlit_app.show_evaluation` 目前已包含成片理解和分析报告；播放量、爆款概率与真实观众数据建模尚未实现。
- TTS：语音生成入口在 `tts_agent.py`，视频合成在 `video_generator.py`。
- 更换团队调用：team_interfaces.py，不在前端放 A/B/C 实现。

## 本版验证

已用测试图片和短视频实际检查：
- 内部 A/B/C 数据交接、阶段事件与未接入提示；
- 无上传控件/目录输入框的页面渲染；
- 关键词匹配、缺失/损坏素材跳过、首段无匹配、后续复用；
- 短视频循环、实际 MP4 时长、两种 1080p 比例和进度回调；
- 页面分别生成两个平台视频并保留预览与下载。

已用模拟客户端验证错误恢复、证据校验、缓存、静音和中文路径；另以百炼 `qwen3.8-omni-flash` 对一条60秒成片完成真实画面、音轨及六维报告端到端验证。该次输入没有任务简报或近期来源，所以报告相应标注为推断/待核实；接口成功不代表模型观察逐帧无误。A/B 的真实服务仍需接入后联调。
