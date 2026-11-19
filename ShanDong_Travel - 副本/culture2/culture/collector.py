"""有预算的官网文章探索、素材下载和模型审核。"""
import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from bs4 import BeautifulSoup

import config
from culture.extract import extract_article, find_links, official_page_url, article_matches_targets, pagination_branch
from culture.intent import finalize_intent
from culture.facts import populate_fact_links
from culture.model import ModelClient, analyze_request, review_asset
from culture.ranking import SiteQueue, article_score, page_key
from culture.types import Article, Budget, BudgetExceeded, Candidate, CollectionError, Media, ModelUnavailable
from culture.web import WebClient


def _json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _asset_id(url):
    return "asset_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _extension(media, response):
    ext = Path(urlsplit(response.url).path).suffix.lower()
    if ext in ({".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov", ".m3u8"}):
        return ext
    mime = response.content_type.split(";", 1)[0].strip()
    ext = mimetypes.guess_extension(mime) if mime else None
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif"} if media.media_type == "image" else {".mp4", ".webm", ".mov", ".m3u8"}
    return ext if ext in allowed else (".jpg" if media.media_type == "image" else ".mp4")


def _check_media(path, media_type):
    if not path.is_file() or not path.stat().st_size:
        raise CollectionError("下载文件为空")
    if media_type == "image":
        try:
            from PIL import Image
            with Image.open(path) as image:
                image.verify()
                if image.width < 160 or image.height < 100:
                    raise CollectionError("图片过小，可能是图标或占位图")
        except ImportError:
            pass
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("下载结果不是可读取图片") from exc
    elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        raise CollectionError("视频地址实际返回图片")


def _article_from_browser(page, site, plan, depth):
    # Browser response HTML is consumed as a static snapshot by the same safe parser.
    html = page.content()
    return find_links(html, page.url, site, plan, depth)


def _collection_counts(approved, budget):
    videos = sum(item["media_type"] == "video" for item in approved)
    return {"minimum_counts_enabled": False,
            "approved_counts": {"total": len(approved), "image": len(approved) - videos, "video": videos},
            "download_bytes": budget.download_bytes,
            "limits": {"pages": budget.max_pages, "candidates": budget.max_candidates,
                       "model_calls": budget.max_calls, "requests": budget.max_requests,
                       "seconds": budget.max_seconds, "download_bytes": budget.max_download_bytes}}


def _collection_progress(approved):
    videos = sum(item["media_type"] == "video" for item in approved)
    return f"通过 {len(approved)} 份（图片 {len(approved) - videos}，视频 {videos}）"


def collect(raw_request, *, progress=print):
    budget = Budget(max_pages=config.MAX_PAGES, max_candidates=config.MAX_CANDIDATES,
                    max_calls=config.MAX_MODEL_CALLS, max_requests=config.MAX_REQUESTS,
                    max_seconds=config.MAX_SECONDS, max_download_bytes=config.MAX_DOWNLOAD_BYTES)
    model = ModelClient(budget)
    web = WebClient(budget, progress=progress)
    try:
        plan = analyze_request(model, raw_request)
        progress("需求解析：聊城；对象 " + ("、".join(plan["targets"]) or "（未指定具体对象）"))
        if plan.get("themes"):
            progress("需求主题：" + "、".join(plan["themes"]))
            progress("主题检索词：" + "、".join(plan.get("theme_keywords", [])))
        progress(f"范围：{len(config.OFFICIAL_SITES)} 个站点；不设总素材、图片或视频的最低数量，不因已通过数量提前停止。")
        progress(f"额度：候选最多 {budget.max_candidates} 项（含下载失败及审核未通过）；网页 {budget.max_pages} 页；模型 {budget.max_calls} 次（含需求解析），可能计费；{budget.max_seconds // 60} 分钟；媒体下载总量 {budget.max_download_bytes / 1024**3:g} GiB")
        return _collect_plan(plan, budget, model, web, progress)
    finally:
        model.close()
        web.close()


def _collect_plan(plan, budget, model, web, progress):
    plan = finalize_intent(plan)
    sites = config.OFFICIAL_SITES
    site_by_id = {s["id"]: s for s in sites}
    queue = SiteQueue(site_by_id, batch=config.SITE_BATCH)
    failures = []
    visited = []
    approved = []
    attempted = set()
    articles_read = 0
    video_candidates = 0
    known_article_links = set()
    empty_pages_by_branch = {}
    paused_branches = set()
    # 首页不提取首页缩略图，只用于发现站内文章和栏目。
    for site in sites:
        queue.add(Candidate(site["url"], site["name"] + "首页", site["id"], "home", 1000, 0, "官网入口"))
        for entry in site.get("entry_urls", []):
            url = official_page_url(site["url"], entry, site)
            if url:
                queue.add(Candidate(url, site["name"] + "文旅/视频栏目入口", site["id"],
                                    "column", 4, 1, "配置中已知的官网栏目入口"))
    work_root = config.OUTPUT_DIR / "work"
    if work_root.is_symlink():
        raise CollectionError("work 是符号链接，停止写入")
    if work_root.exists():
        shutil.rmtree(work_root)
    stage = work_root / ("current_" + uuid4().hex[:8])
    download_root = stage / "downloads"
    download_root.mkdir(parents=True)
    run_started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    page_limit = min(max(1, config.INITIAL_PAGES), budget.max_pages)

    try:
        while True:
            budget.check()
            if budget.candidates >= budget.max_candidates:
                raise BudgetExceeded(f"达到候选素材总额度 {budget.max_candidates}")
            if budget.pages >= budget.max_pages:
                raise BudgetExceeded(f"达到网页总额度 {budget.max_pages}")
            candidate = queue.pop()
            if candidate is None:
                break
            if candidate.kind == "pagination" and candidate.branch in paused_branches:
                continue
            if budget.pages >= page_limit:
                page_limit = min(budget.max_pages, page_limit + max(1, config.INITIAL_PAGES))
                progress(f"[自动扩展] {_collection_progress(approved)}，继续浏览相关文章、栏目和真实分页；累计最多 {page_limit} 页")
            site = site_by_id[candidate.site_id]
            progress(f"[浏览 {budget.pages + 1}/{budget.max_pages}] {site['name']}：{candidate.title[:70]}")
            try:
                budget.take("pages")
                final_url, html = web.html(candidate.url, hosts=set(site["hosts"]))
                links = find_links(html, final_url, site, plan, candidate.depth, page_kind=candidate.kind)
                new_article_links = {page_key(link.url) for link in links
                                     if link.kind == "article"} - known_article_links
                known_article_links.update(new_article_links)
                if candidate.kind in {"column", "search", "pagination"}:
                    branch = candidate.branch or pagination_branch(final_url)
                    empty_pages_by_branch[branch] = (0 if new_article_links else
                                                     empty_pages_by_branch.get(branch, 0) + 1)
                    if empty_pages_by_branch[branch] >= config.MAX_EMPTY_PAGINATION_PAGES:
                        paused_branches.add(branch)
                        progress(f"  [停止该栏翻页] 连续 {empty_pages_by_branch[branch]} 页未发现新的相关文章，继续探索其他入口")
                        failures.append({"url": final_url, "reason": "该栏目连续多页没有新的相关文章，停止翻页"})
                for link in links:
                    if link.kind != "pagination" or link.branch not in paused_branches:
                        queue.add(link)
                visited.append({"url": final_url, "title": candidate.title, "site": site["name"],
                                "status": "read", "kind": candidate.kind, "discovered_links": len(links),
                                "new_article_links": len(new_article_links),
                                "pagination_links": sum(link.kind == "pagination" for link in links)})
                if candidate.kind != "article":
                    progress(f"  发现 {len(links)} 个相关文章/栏目/搜索/翻页入口（含 {visited[-1]['pagination_links']} 个翻页链接）；按相关度择优")
                    continue

                article = extract_article(html, final_url)
                if not article.is_article:
                    failures.append({"url": final_url, "title": candidate.title, "reason": "未识别到独立文章正文"})
                    progress("  [页面跳过] 未识别到独立文章正文")
                    continue
                articles_read += 1
                score, reason = article_score(article.title or candidate.title, plan)
                visited[-1]["relevance_score"] = score
                visited[-1]["relevance_reason"] = reason
                if not article_matches_targets(article, plan):
                    failures.append({"url": final_url, "title": article.title, "reason": "正文与图注不匹配指定对象或主题，跳过下载和模型审核"})
                    progress("  [需求过滤] 正文与图注不匹配指定对象或主题，跳过素材下载及模型审核")
                    continue
                if score <= 0 and not plan.get("targets"):
                    failures.append({"url": final_url, "title": article.title, "reason": "文章标题相关度不足"})
                    continue
                media_items = [m for m in article.media if plan["media_type"] == "all" or m.media_type == plan["media_type"]]
                video_candidates += sum(m.media_type == "video" for m in media_items)
                media_items.sort(key=lambda media: media.media_type != "video")
                for media in media_items:
                    if budget.candidates >= budget.max_candidates:
                        break
                    if media.url.lower().split("?", 1)[0].endswith(".m3u8"):
                        failures.append({"url": media.url, "article_url": article.url,
                                        "reason": "HLS 播放清单不是可直接交付的视频文件"})
                        continue
                    if media.url in attempted:
                        continue
                    attempted.add(media.url)
                    budget.take("candidates")
                    aid = _asset_id(media.url)
                    temp = download_root / (aid + ".part")
                    try:
                        response = web.download(media.url, temp, media.media_type, article.url)
                        suffix = _extension(media, response)
                        final_file = download_root / (aid + suffix)
                        temp.replace(final_file)
                        _check_media(final_file, media.media_type)
                        progress(f"[素材筛选] {media.media_type} {aid}")
                        review = review_asset(model, media.media_type, final_file, article, media, plan)
                        progress("  [" + review["decision"] + "] " + review["description"])
                        record = {"id": aid, "media_type": media.media_type, "download": final_file,
                                  "url": media.url, "article": article, "media": media, "review": review}
                        requirement_ok = (not plan.get("targets") or review.get("requirement_status") == "match")
                        if review["decision"] == "keep" and requirement_ok and review.get("requirement_status") != "mismatch":
                            approved.append(record)
                        else:
                            if review["decision"] in {"review", "keep"}:
                                review["decision"] = "review"
                                progress("  [待核实不保留] " + review.get("reason", "未通过本次审核"))
                            failures.append({"id": aid, "url": media.url, "article_url": article.url,
                                "title": article.title, "decision": review["decision"],
                                "reason": review.get("reason", "未达到本轮需求匹配标准")})
                            final_file.unlink(missing_ok=True)
                    except ModelUnavailable:
                        raise
                    except BudgetExceeded:
                        raise
                    except CollectionError as exc:
                        temp.unlink(missing_ok=True)
                        failures.append({"id": aid, "url": media.url, "article_url": article.url, "reason": str(exc)})
                        progress("  [候选未交付] " + str(exc))
                    except Exception as exc:
                        temp.unlink(missing_ok=True)
                        error = f"素材处理失败：{type(exc).__name__}: {exc}"
                        failures.append({"id": aid, "url": media.url, "article_url": article.url,
                                         "reason": error})
                        progress("  [候选未交付] " + error)
                progress(f"  {_collection_progress(approved)}；候选 {budget.candidates}/{budget.max_candidates}；已打开 {budget.pages} 页；模型 {budget.calls} 次")
            except ModelUnavailable:
                raise
            except BudgetExceeded:
                raise
            except CollectionError as exc:
                failures.append({"url": candidate.url, "title": candidate.title, "reason": str(exc)})
                progress("  [页面跳过] " + str(exc))
            except Exception as exc:
                failures.append({"url": candidate.url, "title": candidate.title,
                                 "reason": f"页面处理失败：{type(exc).__name__}: {exc}"})

        if plan["media_type"] in {"video", "all"} and articles_read and not video_candidates:
            failures.append({"reason": "本轮成功解析的文章中未提取到视频地址；不代表网站没有视频"})
        status = "采集完成"
        reason = "当前发现的相关文章/栏目队列已探索完毕；" + _collection_progress(approved)
        if failures:
            from collections import Counter
            counts = Counter(row["reason"] for row in failures)
            progress("[失败汇总] " + "；".join(f"{key}（{count} 项）" for key, count in counts.most_common(3)))
        report = {"status": status, "reason": reason, "plan": plan, "pages_read": budget.pages,
                  "candidates_attempted": budget.candidates, "model_calls": budget.calls,
                  "requests": budget.requests, "elapsed_seconds": round(time.monotonic() - budget.started, 1),
                  "started_at": run_started, "visited_pages": visited, "failures": failures,
                  "selected_asset_ids": [x["id"] for x in approved]}
        report.update(_collection_counts(approved, budget))
        _publish(approved, plan, report, stage, progress)
        return report
    except ModelUnavailable as exc:
        report = {"status": "模型停止", "reason": str(exc), "plan": plan, "pages_read": budget.pages,
                  "candidates_attempted": budget.candidates, "model_calls": budget.calls,
                  "requests": budget.requests, "failures": failures, "visited_pages": visited,
                  "started_at": run_started,
                  "elapsed_seconds": round(time.monotonic() - budget.started, 1),
                  "selected_asset_ids": [x["id"] for x in approved]}
        report.update(_collection_counts(approved, budget))
        report["reason"] += "；" + _collection_progress(approved)
        # No further model work: publish only records already reviewed in this run.
        _publish(approved, plan, report, stage, progress)
        raise
    except BudgetExceeded as exc:
        failures.append({"reason": str(exc)})
        report = {"status": "预算停止", "reason": str(exc), "plan": plan, "pages_read": budget.pages,
                  "candidates_attempted": budget.candidates, "model_calls": budget.calls,
                  "requests": budget.requests, "elapsed_seconds": round(time.monotonic() - budget.started, 1),
                  "started_at": run_started, "visited_pages": visited, "failures": failures,
                  "selected_asset_ids": [x["id"] for x in approved]}
        report.update(_collection_counts(approved, budget))
        report["reason"] += "；" + _collection_progress(approved)
        _publish(approved, plan, report, stage, progress)
        return report
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def remove_legacy_review(output_dir):
    """Remove only the retired sibling review folder; never touch shared approved assets."""
    root = Path(output_dir).resolve()
    target = root / "review"
    if target.is_symlink() or target.is_junction():
        raise CollectionError("旧 review 是目录链接，未自动删除")
    if target.exists():
        if not target.is_dir() or target.resolve().parent != root:
            raise CollectionError("旧 review 路径异常，未自动删除")
        shutil.rmtree(target)


def _publish(approved, plan, report, stage, progress):
    out = config.OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    remove_legacy_review(out)
    if not approved:
        # Preserve the last usable package when this run found nothing to replace it with.
        _json(out / "collection_report.json", report)
        progress("未找到合格素材；上次 assets 和素材清单保持不变，失败原因已写入 collection_report.json")
        return
    assets_stage = stage / "assets"
    assets_stage.mkdir(exist_ok=True)
    source_rows, research_rows = {}, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for item in approved:
        aid, article, review = item["id"], item["article"], item["review"]
        dest = assets_stage / item["download"].name
        shutil.copy2(item["download"], dest)
        source_id = "source_" + hashlib.sha256(article.url.encode()).hexdigest()[:12]
        source_rows[source_id] = {"id": source_id, "type": "official_article", "title": article.title,
            "publisher": next((s["name"] for s in config.OFFICIAL_SITES if urlsplit(article.url).hostname in s["hosts"]), "官网"),
            "url": article.url, "published_at": article.published_at, "accessed_at": now,
            "evidence": item["media"].caption or article.text[:1200] or article.title,
            "article_text": article.text,
            "media_locator": item["media"].locator}
        if item["media"].media_type == "video":
            desc = review["description"]
            basis = "source_article_text_video_not_visually_verified"
            uncertainties = list(dict.fromkeys([*review.get("uncertainties", []), "未核实实际视频画面、声音和剪辑内容"]))
        else:
            desc = review["description"]
            basis = "direct_image_model_review"
            uncertainties = review.get("uncertainties", [])
            if review.get("requirement_status") != "match":
                uncertainties = list(dict.fromkeys([*uncertainties, "图片与指定地点/对象未能由来源确认"]))
        relative = "assets/" + dest.name
        research_rows.append({"id": aid, "media_type": item["media"].media_type, "file_path": relative,
            "title": article.title, "description": desc, "description_basis": basis,
            "source_url": article.url, "source_ids": [source_id], "fact_refs": [],
            "media_caption": item["media"].caption,
            "request_match": {"status": review.get("requirement_status", "uncertain"),
                              "evidence": review.get("requirement_evidence", "")},
            "license_status": "unknown", "license": "待确认；未检查授权范围",
            "keywords": review.get("tags", []), "uncertainties": uncertainties})
    fact_rows = populate_fact_links(research_rows, list(source_rows.values()), plan)
    package = {
        "research.json": {"schema_version": "1.0", "city": plan["city"], "request": plan["raw_request"],
                          "assets": research_rows},
        "fact.json": {"schema_version": "1.0", "city": plan["city"], "facts": fact_rows},
        "sources.json": {"schema_version": "1.0", "city": plan["city"], "sources": list(source_rows.values())},
        "collection_report.json": report,
    }
    readme = ("聊城文旅素材交付\n\n"
        "assets/：本轮模型筛选通过并下载的素材。\n"
        "仅保留通过审核的素材；待核实和不通过的文件不保留，原因记录在 collection_report.json。\n"
        "research.json：素材、文件位置、一句话概括、需求匹配和核实状态。\n"
        "fact.json：从来源正文、标题或图注摘录的相关陈述，evidence_type标明位置；status 为 source_text_unverified 表示尚未独立核实。\n"
        "sources.json：文章出处与素材关联信息。\n"
        "collection_report.json：需求、访问额度、搜索记录、失败原因、通过数量及停止原因。\n\n"
        "图片描述来自视觉模型；视频描述仅根据文章文字，实际视频画面未核实。" )
    new_assets = out / ".assets_new"
    old_assets = out / ".assets_previous"
    for path in (new_assets, old_assets):
        if path.is_symlink():
            raise CollectionError("交付目录是符号链接，已停止发布")
        if path.exists():
            shutil.rmtree(path)
    assets_stage.rename(new_assets)
    moved_old = False
    published = False
    try:
        target = out / "assets"
        if target.exists():
            target.rename(old_assets)
            moved_old = True
        new_assets.rename(target)
        published = True
        for name, value in package.items():
            temp = out / ("." + name + ".new")
            _json(temp, value)
            temp.replace(out / name)
        (out / "README.md.new").write_text(readme, encoding="utf-8")
        (out / "README.md.new").replace(out / "README.md")
    except Exception:
        if published and (out / "assets").exists():
            shutil.rmtree(out / "assets")
        if moved_old and old_assets.exists():
            old_assets.rename(out / "assets")
        raise
    finally:
        if old_assets.exists():
            shutil.rmtree(old_assets)
    progress(f"本轮 {report['status']}：交付 {len(approved)} 项；共享目录已覆盖：{out}")

