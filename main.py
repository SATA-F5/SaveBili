import re
import os
import time
import json
import asyncio
import tempfile
import aiohttp
import sys
import traceback
import zipfile
import tarfile
import shutil
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
import astrbot.api.message_components as Comp

from .plugin_api import PluginAPI

sys.stdout.reconfigure(encoding='utf-8')

gk_art = r"""//       ________ __    _____    __                  __   _        
//      / ____/ //_/   / ___/   / /_   __  __   ____/ /  (_)  ____ 
//     / / __/ ,<      \__ \   / __/  / / / /  / __  /  / /  / __ \
//    / /_/ / /| |    ___/ /  / /_   / /_/ /  / /_/ /  / /  / /_/ /
//    \____/_/ |_|   /____/   \__/   \__,_/   \__,_/  /_/   \____/ 
"""
print(gk_art)


@register("saveany_bilibili_downloader", "Shou_Lu",
          "使用 saveany 解析并下载 B 站内容，支持视频/番剧/专栏/收藏夹/合集/动态，多 Cookie 管理",
          "1.2.2")
class SaveAnyBilibiliDownloader(Star):

    # GitHub 加速镜像候选列表
    # 排序策略：快速代理在前，老牌稳定代理居中，小众代理垫底
    FFMPEG_MIRROR_CANDIDATES = [
        "https://raw.ihtw.moe/",
        "https://gh.zwy.one/",
        "https://gh.llkk.cc/",
        "https://ghfast.top/",
        "https://gh.h233.eu.org/",
        "https://gh-proxy.com/",
        "https://ghproxy.net/",
        "https://gh.xxooo.cf/",
        "https://ghfile.geekertao.top/",
        "https://ghproxy.cxkpro.top/",
        "https://git.yylx.win/",
        "https://cdn.crashmc.com/",
        "https://githubproxy.cc/",
    ]

    # 超时策略：任何一步超时立即切换到下一个镜像，不再苦等
    FFMPEG_CONNECT_TIMEOUT = 8       # TCP 连接超时（秒）
    FFMPEG_READ_TIMEOUT = 15         # 两次读取之间的间隔超时（秒）
    FFMPEG_TOTAL_TIMEOUT = 90        # 单个镜像的总时长上限（秒）

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.base_dir = Path(__file__).parent
        self.data_dir = self._get_data_dir(context)
        os.makedirs(self.data_dir, exist_ok=True)

        self.download_dir = "./downloads"
        self.max_size_mb = 2000.0
        self.bot_qq = ""
        self.quality = 80
        self.ffmpeg_path = None
        # FFmpeg 下载状态，供 WebUI 轮询
        self.ffmpeg_status = {"status": "idle", "message": "尚未检测 FFmpeg"}

        if config:
            self.download_dir = str(config.get("download_dir", self.download_dir))
            self.max_size_mb = float(config.get("max_size_mb", self.max_size_mb))
            self.bot_qq = str(config.get("bot_qq", self.bot_qq)).strip()
            self.quality = int(config.get("quality", self.quality))

        self._load_plugin_config()
        self.cookies = []
        self.bilibili_cookie = ""
        self._load_cookies()

        self.session = None
        self.plugin_api = PluginAPI(self)
        self._tasks = set()

    def _get_data_dir(self, context: Context) -> Path:
        for attr in ["get_plugin_data_dir", "get_data_dir"]:
            if hasattr(context, attr):
                try:
                    dir_path = getattr(context, attr)()
                    if dir_path:
                        return Path(dir_path) / "saveany_bilibili_downloader"
                except Exception:
                    pass
        env_dir = os.environ.get("ASTRBOT_DATA_DIR")
        if env_dir:
            return Path(env_dir) / "plugin_data" / "saveany_bilibili_downloader"
        return Path.home() / ".astrbot" / "data" / "plugin_data" / "saveany_bilibili_downloader"

    def _load_plugin_config(self):
        cfg_file = self.data_dir / "plugin_config.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                self.download_dir = str(data.get("download_dir", self.download_dir))
                self.max_size_mb = float(data.get("max_size_mb", self.max_size_mb))
                self.bot_qq = str(data.get("bot_qq", self.bot_qq)).strip()
                self.quality = int(data.get("quality", self.quality))
            except Exception as e:
                logger.error(f"加载插件本地配置失败: {e}")

    def _save_plugin_config(self):
        cfg_file = self.data_dir / "plugin_config.json"
        data = {
            "download_dir": self.download_dir,
            "max_size_mb": self.max_size_mb,
            "bot_qq": self.bot_qq,
            "quality": self.quality,
        }
        cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        if cookie_file.exists():
            try:
                self.cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
            except Exception:
                self.cookies = []
        self._select_best_cookie()

    def _save_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        cookie_file.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        self._select_best_cookie()

    def _select_best_cookie(self):
        enabled = [c for c in self.cookies if c.get("enabled", True)]
        enabled.sort(key=lambda x: x.get("priority", 100))
        self.bilibili_cookie = enabled[0]["cookie"] if enabled else ""

    def _check_ffmpeg_local(self):
        """本地快速检查 FFmpeg"""
        system_ffmpeg = shutil.which("ffmpeg")
        if system_ffmpeg:
            self.ffmpeg_path = system_ffmpeg
            self.ffmpeg_status = {"status": "ready", "message": "已检测到系统自带的 FFmpeg"}
            return

        bin_dir = self.data_dir / "bin"
        bin_dir.mkdir(exist_ok=True)
        ffmpeg_exe = bin_dir / "ffmpeg.exe" if os.name == 'nt' else bin_dir / "ffmpeg"

        if ffmpeg_exe.exists():
            self.ffmpeg_path = str(ffmpeg_exe)
            self.ffmpeg_status = {"status": "ready", "message": "已检测到插件目录下的 FFmpeg"}
            if os.name != 'nt':
                os.chmod(self.ffmpeg_path, 0o755)
        else:
            self.ffmpeg_status = {"status": "idle", "message": "未检测到 FFmpeg，可点击右侧按钮一键下载"}

    async def initialize(self):
        timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=15)
        self.session = aiohttp.ClientSession(timeout=timeout)
        os.makedirs(self.download_dir, exist_ok=True)
        self.plugin_api.register(self.context)
        logger.info("B站下载插件已启动 (1.2.2 快速切换镜像版)")
        # 初始化时只进行本地检查，绝对不联网
        self._check_ffmpeg_local()

    # ================= WebUI 触发下载 FFmpeg（多镜像快速切换） =================
    async def trigger_ffmpeg_download(self):
        """由 Web API 调用，触发后台下载任务"""
        if self.ffmpeg_status["status"] == "downloading":
            return

        self.ffmpeg_status = {"status": "downloading", "message": "正在下载 FFmpeg，请耐心等待..."}

        async def download_task():
            try:
                bin_dir = self.data_dir / "bin"
                bin_dir.mkdir(exist_ok=True)

                if os.name == 'nt':
                    github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
                    temp_file = self.data_dir / "ffmpeg_temp.zip"
                    target_name = "ffmpeg.exe"
                    extract_func = self._extract_zip
                else:
                    github_url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
                    temp_file = self.data_dir / "ffmpeg_temp.tar.xz"
                    target_name = "ffmpeg"
                    extract_func = self._extract_tar

                total_mirrors = len(self.FFMPEG_MIRROR_CANDIDATES)
                last_error = None

                for idx, mirror in enumerate(self.FFMPEG_MIRROR_CANDIDATES, start=1):
                    full_url = mirror + github_url
                    self.ffmpeg_status["message"] = f"[{idx}/{total_mirrors}] 正在通过 {mirror} 下载..."
                    logger.info(f"[{idx}/{total_mirrors}] 尝试从镜像下载 FFmpeg: {full_url}")
                    start_time = time.time()
                    try:
                        # 单镜像限时，总超时 90 秒，超时立即切下一个
                        await asyncio.wait_for(
                            self._download_file_async(full_url, temp_file),
                            timeout=self.FFMPEG_TOTAL_TIMEOUT,
                        )
                        elapsed = time.time() - start_time
                        logger.info(f"镜像下载成功: {mirror} (耗时 {elapsed:.1f} 秒)")
                        break
                    except asyncio.TimeoutError:
                        elapsed = time.time() - start_time
                        last_error = f"超时 ({elapsed:.1f}s > {self.FFMPEG_TOTAL_TIMEOUT}s)"
                        logger.warning(f"镜像 {mirror} 超时 ({elapsed:.1f}s)，立即切换到下一个...")
                    except Exception as e:
                        elapsed = time.time() - start_time
                        last_error = str(e)
                        logger.warning(f"镜像 {mirror} 下载失败 ({elapsed:.1f}s): {e}，切换到下一个...")

                    # 清理残留临时文件，避免污染下一个镜像的下载
                    if temp_file.exists():
                        try:
                            temp_file.unlink()
                        except Exception:
                            pass
                else:
                    # 所有镜像都失败
                    raise Exception(f"所有 {total_mirrors} 个镜像均下载失败，最后一个错误: {last_error}")

                # 解压（放到线程池，避免阻塞事件循环）
                self.ffmpeg_status["message"] = "下载完成，正在解压..."
                await asyncio.to_thread(extract_func, temp_file, bin_dir, target_name)

                # 重新检查
                self._check_ffmpeg_local()
                if self.ffmpeg_status["status"] != "ready":
                    raise Exception("解压完成但未能找到可执行文件")

            except Exception as e:
                logger.error(f"FFmpeg 下载失败: {e}")
                self.ffmpeg_status = {"status": "error", "message": f"下载失败: {str(e)}"}

        asyncio.create_task(download_task())

    async def _download_file_async(self, url, dest):
        """
        下载单个文件。
        超时策略：连接 8 秒、读间隔 15 秒、总时长由外层 asyncio.wait_for 控制。
        任一超时抛出异常，让外层快速切换镜像。
        """
        timeout = aiohttp.ClientTimeout(
            total=None,  # 由外层 wait_for 控制
            connect=self.FFMPEG_CONNECT_TIMEOUT,
            sock_read=self.FFMPEG_READ_TIMEOUT,
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "*/*",
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status}")
                with open(dest, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)

    def _extract_zip(self, temp_file, dest_dir, target_name):
        with zipfile.ZipFile(temp_file, 'r') as z:
            for filename in z.namelist():
                if filename.endswith(target_name):
                    with z.open(filename) as source, open(dest_dir / target_name, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    break
        os.remove(temp_file)

    def _extract_tar(self, temp_file, dest_dir, target_name):
        with tarfile.open(temp_file, "r:xz") as tar:
            for member in tar.getmembers():
                if member.name.endswith(target_name) and member.isfile():
                    member.name = os.path.basename(member.name)
                    tar.extract(member, path=dest_dir)
                    break
        os.remove(temp_file)

    # ---------- 命令入口 ----------
    @filter.command("saveany")
    async def saveany(self, event: AstrMessageEvent, url: str):
        if not url:
            await event.send(event.plain_result("请提供一个 B 站链接或 BV 号。"))
            return
        await self.download_and_send(event, url)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        message = event.message_str.strip()
        if message.startswith('/saveany'):
            return
        url_or_bvid = self.extract_bvid_or_url(message)
        if not url_or_bvid:
            return
        if 'bilibili.com' in url_or_bvid or 'b23.tv' in url_or_bvid:
            event.stop_event()
            await self.download_and_send(event, url_or_bvid)
            return
        if await self._is_mentioned(event):
            event.stop_event()
            await self.download_and_send(event, url_or_bvid)

    async def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        self_id = getattr(event.message_obj, 'self_id', self.bot_qq)
        if not self_id:
            return False
        for comp in event.get_messages():
            if isinstance(comp, Comp.At) and str(comp.qq) == str(self_id):
                return True
        return False

    def extract_bvid_or_url(self, text: str) -> str:
        match = re.search(r'https?://[^\s]*(bilibili\.com|b23\.tv)[^\s]*', text)
        if match:
            return match.group(0)
        m = re.search(r'BV[0-9A-Za-z]{10}', text)
        if m:
            return m.group(0)
        m = re.search(r'av(\d+)', text, re.I)
        if m:
            return f"av{m.group(1)}"
        return None

    def detect_input_type(self, text: str) -> dict:
        text = text.strip()
        if re.match(r'^(BV[0-9A-Za-z]{10})$', text):
            return {"type": "video", "id": text}
        if re.match(r'^av(\d+)$', text, re.I):
            return {"type": "video", "id": text}
        if re.match(r'^(cv\d+)$', text, re.I):
            return {"type": "article", "id": text}
        if re.match(r'^(ep|ss)(\d+)$', text, re.I):
            return {"type": "bangumi", "id": text}
        if re.match(r'^(\d+)$', text) and len(text) > 10:
            return {"type": "opus", "id": text}
        try:
            parsed = urlparse(text)
            path = parsed.path
            query = parse_qs(parsed.query)
        except Exception:
            return {"type": "unknown", "id": ""}
        for pattern, t in [(r'/bangumi/play/(ep|ss)(\d+)', "bangumi"), (r'/video/(BV[0-9A-Za-z]{10}|av\d+)', "video"), (r'/read/(cv\d+)', "article"), (r'/opus/(\d+)', "opus")]:
            m = re.search(pattern, path)
            if m:
                return {"type": t, "id": m.group(1) if t != 'bangumi' else f"{m.group(1)}{m.group(2)}"}
        if 'collectiondetail' in path and 'sid' in query:
            return {"type": "collection", "id": query['sid'][0], "mid": query.get('mid', [''])[0]}
        if 'favlist' in path and 'fid' in query:
            return {"type": "favlist", "id": query['fid'][0], "mid": query.get('mid', [''])[0]}
        m = re.search(r'BV[0-9A-Za-z]{10}', text)
        return {"type": "video", "id": m.group(0)} if m else {"type": "unknown", "id": ""}

    def _bili_headers(self, referer: str = "https://www.bilibili.com/") -> dict:
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", "Referer": referer}
        if self.bilibili_cookie:
            h["Cookie"] = self.bilibili_cookie
        return h

    def _format_desc(self, desc: str, max_len: int = 120) -> str:
        desc = re.sub(r'\s+', ' ', (desc or "").strip())
        return (desc[:max_len] + "...") if len(desc) > max_len else (desc or "（无简介）")

    def build_video_notify(self, info: dict) -> str:
        stat = info.get("stat", {}) or {}
        return f"正在下载：\n{info.get('title', '未知')}\n\nUP主：{info.get('owner', '未知')}\n简介：{self._format_desc(info.get('desc'))}\n目前播放量：{stat.get('view', 0):,}"

    def build_bangumi_notify(self, info: dict, ep_title: str, index: str) -> str:
        return f"正在下载番剧：\n{info.get('title', '未知')}\n集数：{index} / 共 {len(info.get('episodes', []))} 集\n\n本集标题：{ep_title or '（无标题）'}\n简介：{self._format_desc(info.get('desc'))}"

    async def _fetch_cover_to_temp(self, url: str) -> str:
        if not url: return ""
        url = "https:" + url if url.startswith("//") else url
        try:
            suffix = ".jpg"
            for ext in (".png", ".gif", ".webp", ".jpeg"):
                if ext in url.lower(): suffix = ext; break
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    if resp.status == 200:
                        tmp.write(await resp.read())
                        return True
                    return False
            success = await asyncio.wait_for(fetch(), timeout=10.0)
            tmp.close()
            return tmp.name if success else ""
        except Exception as e:
            logger.warning(f"封面下载失败: {e}")
            return ""

    async def download_and_send(self, event: AstrMessageEvent, url_or_bvid: str):
        info = self.detect_input_type(url_or_bvid)
        if info["type"] == "unknown":
            await event.send(event.plain_result("无法识别的链接类型。"))
            return
        type_name = {"video": "视频", "bangumi": "番剧", "article": "专栏", "opus": "动态", "favlist": "收藏夹", "collection": "合集"}.get(info["type"], "内容")
        await event.send(event.plain_result(f"已接收您的{type_name}请求，正在后台解析和下载，请稍候..."))
        task = asyncio.create_task(self._background_download(event, info))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _background_download(self, event: AstrMessageEvent, info: dict):
        try:
            t = info["type"]
            logger.info(f"后台任务开始处理: type={t}, id={info.get('id')}")
            handlers = {
                "video": self._handle_video, "bangumi": self._handle_bangumi,
                "article": self._handle_article, "opus": self._handle_opus,
                "favlist": lambda e, i: self._handle_list(e, "favlist", i),
                "collection": lambda e, i: self._handle_list(e, "collection", i, info.get("mid", ""))
            }
            await handlers[t](event, info["id"])
        except Exception as e:
            logger.error(f"后台下载任务出错: {traceback.format_exc()}")
            await event.send(event.plain_result(f"下载过程中发生未捕获错误: {str(e)}"))

    async def _handle_video(self, event, bvid):
        info = await self.get_bilibili_video_info(bvid)
        if not info:
            await event.send(event.plain_result("获取视频信息失败，请查看控制台日志。")); return
        cover = await self._fetch_cover_to_temp(info.get("pic")) if info.get("pic") else ""
        await event.send(event.chain_result([Comp.Image.fromFileSystem(path=cover), Comp.Plain(self.build_video_notify(info))]) if cover else event.plain_result(self.build_video_notify(info)))
        url = await self.get_download_url(bvid, info.get("aid"), info.get("cid"))
        if not url:
            await event.send(event.plain_result("获取下载地址失败，请查看控制台日志。")); return
        path = await self.download_file(url, info["title"], None)
        if path:
            await event.send(event.chain_result([Comp.Video.fromFileSystem(path=path)]))
        else:
            await event.send(event.plain_result("下载失败，可能文件过大或网络问题。"))

    async def _handle_bangumi(self, event, ep_or_ss):
        bangumi = await self.get_bangumi_info(ep_or_ss)
        if not bangumi:
            await event.send(event.plain_result("番剧信息获取失败（超时或无数据），请查看控制台日志。")); return
        episodes = bangumi.get("episodes", [])
        if ep_or_ss.startswith("ep"):
            episodes = [e for e in episodes if e.get("ep_id") == int(ep_or_ss[2:])]
        if not episodes:
            await event.send(event.plain_result("未找到可下载的剧集。")); return
        if ep_or_ss.startswith("ss") and len(episodes) > 1:
            await event.send(event.plain_result(f"检测到整季共 {len(episodes)} 集，仅下载第 1 集。"))
            episodes = episodes[:1]
        for ep in episodes:
            cover = await self._fetch_cover_to_temp(bangumi.get("cover")) if bangumi.get("cover") else ""
            await event.send(event.chain_result([Comp.Image.fromFileSystem(path=cover), Comp.Plain(self.build_bangumi_notify(bangumi, ep.get('title'), ep.get('index')))]) if cover else event.plain_result(self.build_bangumi_notify(bangumi, ep.get('title'), ep.get('index'))))

            streams = await self.get_bangumi_playurl(ep["ep_id"], ep["cid"])
            if not streams or not streams.get("video_url"):
                await event.send(event.plain_result(f"第 {ep.get('index')} 集解析失败，请查看控制台输出的原始响应进行排查。")); continue

            title = f"{bangumi['title']} - {ep.get('index')} {ep.get('title')}".strip()
            path = await self.download_file(streams["video_url"], title, streams.get("audio_url"))
            if path:
                await event.send(event.chain_result([Comp.Video.fromFileSystem(path=path)]))
            else:
                await event.send(event.plain_result(f"第 {ep.get('index')} 集下载失败。"))

    async def get_bilibili_video_info(self, bvid: str) -> dict:
        api_url = f"https://api.bilibili.com/x/web-interface/view?{'aid' if bvid.lower().startswith('av') else 'bvid'}={bvid[2:] if bvid.lower().startswith('av') else bvid}"
        try:
            async def fetch():
                async with self.session.get(api_url, headers=self._bili_headers()) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"视频 API 返回错误: {data.get('message')}")
                return None
            vd = data.get("data") or data.get("result")
            if not vd:
                logger.error(f"视频 API 缺少 data 字段，原始响应: {raw_text}")
                return None
            return {"bvid": vd.get("bvid"), "aid": vd.get("aid"), "cid": vd.get("cid"), "title": vd.get("title"), "desc": vd.get("desc"), "pic": vd.get("pic"), "owner": vd.get("owner", {}).get("name"), "stat": vd.get("stat", {})}
        except Exception as e:
            logger.error(f"视频解析异常: {traceback.format_exc()}")
            return None

    async def get_bangumi_info(self, ep_or_ss: str) -> dict:
        url = f"https://api.bilibili.com/pgc/view/web/season?{'ep_id' if ep_or_ss.startswith('ep') else 'season_id'}={ep_or_ss[2:]}"
        referer = f"https://www.bilibili.com/bangumi/play/{ep_or_ss}"
        headers = self._bili_headers(referer=referer)
        try:
            async def fetch():
                async with self.session.get(url, headers=headers) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"番剧 API 返回错误: code={data.get('code')}, message={data.get('message')}")
                return None
            d = data.get("data") or data.get("result")
            if not d:
                logger.error(f"番剧 API 缺少 data 字段，原始响应: {raw_text}")
                return None
            raw_eps = d.get("episodes") or []
            for section in d.get("section", []) or []:
                raw_eps.extend(section.get("episodes") or [])
            episodes = []
            for ep in raw_eps:
                episodes.append({
                    "ep_id": ep.get("id"), "cid": ep.get("cid"), "aid": ep.get("aid", 0),
                    "title": ep.get("share_copy") or ep.get("long_title") or ep.get("title", ""),
                    "index": ep.get("title", ""),
                })
            return {"title": d.get("title", ""), "cover": d.get("cover", ""), "desc": d.get("evaluate", ""), "episodes": episodes}
        except Exception as e:
            logger.error(f"番剧解析异常: {traceback.format_exc()}")
            return None

    async def get_bangumi_playurl(self, ep_id: int, cid: int) -> dict:
        url = f"https://api.bilibili.com/pgc/player/web/playurl?ep_id={ep_id}&cid={cid}&qn={self.quality}&fnval=4048&fourk=1&otype=json"
        headers = self._bili_headers(referer=f"https://www.bilibili.com/bangumi/play/ep{ep_id}")
        try:
            async def fetch():
                async with self.session.get(url, headers=headers) as resp:
                    return resp.status, await resp.text()
            status, raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") != 0:
                logger.error(f"番剧 playurl 返回错误: code={data.get('code')}, message={data.get('message')}")
                return None
            d = data.get("data") or data.get("result")
            if not d:
                logger.error(f"番剧 playurl 缺少 data 或 result 字段，原始响应: {raw_text}")
                return None

            result = {"video_url": None, "audio_url": None}
            if d.get("dash"):
                videos = d["dash"].get("video", [])
                audios = d["dash"].get("audio", [])
                if videos:
                    videos.sort(key=lambda v: v.get("id", 0), reverse=True)
                    result["video_url"] = videos[0].get("baseUrl")
                if audios:
                    audios.sort(key=lambda a: a.get("bandwidth", 0), reverse=True)
                    result["audio_url"] = audios[0].get("baseUrl")
                return result if result["video_url"] else None

            if d.get("durl"):
                result["video_url"] = d["durl"][0].get("url")
                return result
            return None
        except Exception as e:
            logger.error(f"番剧 playurl 请求异常: {traceback.format_exc()}")
            return None

    async def get_download_url(self, bvid: str, aid: int, cid: int) -> str:
        url = f"https://api.bilibili.com/x/player/playurl?avid={aid}&cid={cid}&qn={self.quality}&otype=json&platform=html5&fnver=0&fnval=1"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            if data.get("code") == 0:
                d = data.get("data") or data.get("result")
                if d and d.get("durl"):
                    return d["durl"][0]["url"]
        except Exception: pass
        for api in ["https://api.injahow.cn/bparse/", "https://jx.jsonplayer.com/player/"]:
            try:
                async def fetch_parse():
                    async with self.session.get(f"{api}?bv={bvid}&q={self.quality}") as resp:
                        return await resp.text()
                raw_text = await asyncio.wait_for(fetch_parse(), timeout=10.0)
                data = json.loads(raw_text)
                if data.get("url"): return data["url"]
                if data.get("data", {}).get("url"): return data["data"]["url"]
            except Exception: continue
        return None

    async def download_file(self, download_url: str, title: str, audio_url: str = None) -> str:
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:100]
        timestamp = int(time.time())

        if not audio_url:
            file_path = os.path.join(self.download_dir, f"{safe_title}_{timestamp}.mp4")
            return await self._download_single_file(download_url, file_path)

        video_temp = os.path.join(self.download_dir, f".{safe_title}_{timestamp}_v.mp4")
        audio_temp = os.path.join(self.download_dir, f".{safe_title}_{timestamp}_a.mp4")
        final_path = os.path.join(self.download_dir, f"{safe_title}_{timestamp}.mp4")

        try:
            logger.info(f"开始下载视频流: {title}")
            if not await self._download_single_file(download_url, video_temp):
                return None

            logger.info(f"开始下载音频流: {title}")
            if not await self._download_single_file(audio_url, audio_temp):
                return None

            logger.info(f"开始合并音视频: {title}")
            if not self.ffmpeg_path:
                logger.error("未配置 FFmpeg，无法合并音视频。请前往插件 WebUI 点击按钮下载。")
                return None

            cmd = [self.ffmpeg_path, "-y", "-i", video_temp, "-i", audio_temp, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0", final_path]

            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode('utf-8', errors='ignore')[-500:]
                logger.error(f"FFmpeg 合并失败: {error_msg}")
                return None

            logger.info(f"音视频合并成功: {final_path}")
            return final_path
        except Exception as e:
            logger.error(f"合并过程异常: {traceback.format_exc()}")
            return None
        finally:
            for temp_file in [video_temp, audio_temp]:
                if os.path.exists(temp_file):
                    try: os.remove(temp_file)
                    except: pass

    async def _download_single_file(self, url: str, file_path: str) -> bool:
        try:
            timeout = aiohttp.ClientTimeout(total=1800, connect=15, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as download_session:
                async with download_session.get(url, headers=self._bili_headers()) as resp:
                    if resp.status != 200:
                        logger.error(f"文件下载 HTTP 错误: {resp.status}")
                        return False
                    with open(file_path, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            f.write(chunk)
            return True
        except Exception as e:
            logger.error(f"文件下载异常: {traceback.format_exc()}")
            return False

    async def _handle_article(self, event, cv_id):
        article_id = cv_id[2:] if cv_id.lower().startswith("cv") else cv_id
        url = f"https://api.bilibili.com/x/article/view?id={article_id}"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            d = data.get("data") or data.get("result")
            if data.get("code") != 0 or not d:
                logger.error(f"专栏 API 错误: code={data.get('code')}, raw={raw_text}")
                await event.send(event.plain_result("专栏获取失败，请查看控制台日志。"))
                return
            await event.send(event.plain_result(f"正在获取专栏：\n{d.get('title')}\n作者：{d.get('author', {}).get('name')}\n摘要：{self._format_desc(d.get('summary'))}"))
            text = self._html_to_text(d.get("content", ""))
            txt_path = os.path.join(self.download_dir, f"{re.sub(r'[\\\\/*?:\"<>|]', '_', d.get('title', 'article'))[:80]}.txt")
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write(f"{d.get('title')}\n作者：{d.get('author', {}).get('name')}\n链接：https://www.bilibili.com/read/cv{article_id}\n\n{text}")
            await event.send(event.plain_result(f"专栏正文已保存至：{txt_path}"))
        except Exception as e:
            logger.error(f"专栏处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("专栏处理失败，请查看控制台日志。"))

    async def _handle_opus(self, event, dynamic_id):
        url = f"https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?timezone_offset=-480&id={dynamic_id}"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            item = data.get("data", {}).get("item") if data.get("code") == 0 else None
            if not item:
                logger.error(f"动态 API 错误: raw={raw_text}")
                await event.send(event.plain_result("动态获取失败，请查看控制台日志。"))
                return
            major = item.get("modules", {}).get("module_dynamic", {}).get("major") or {}
            archive = major.get("archive")
            if archive and archive.get("bvid"):
                await event.send(event.plain_result("检测到视频动态，正在下载视频..."))
                await self._handle_video(event, archive["bvid"]); return
            text = item.get("modules", {}).get("module_dynamic", {}).get("desc", {}).get("text", "")
            await event.send(event.plain_result(f"动态内容：\n{text or '（无文字）'}"))
            for it in (major.get("draw", {}) or {}).get("items", []):
                img = await self._fetch_cover_to_temp(it.get("src"))
                if img: await event.send(event.chain_result([Comp.Image.fromFileSystem(path=img)]))
        except Exception as e:
            logger.error(f"动态处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("动态处理失败，请查看控制台日志。"))

    async def _handle_list(self, event, kind: str, list_id: str, mid: str = ""):
        if kind == "favlist":
            url = f"https://api.bilibili.com/x/v3/fav/resource/list?media_id={list_id}&pn=1&ps=20&platform=web"
        else:
            if not mid: await event.send(event.plain_result("缺少 mid 参数。")); return
            url = f"https://api.bilibili.com/x/polymer/web-space/seasons_archives_list?mid={mid}&season_id={list_id}&page_num=1&page_size=30"
        try:
            async def fetch():
                async with self.session.get(url, headers=self._bili_headers()) as resp:
                    return await resp.text()
            raw_text = await asyncio.wait_for(fetch(), timeout=10.0)
            data = json.loads(raw_text)
            d = data.get("data") or data.get("result")
            if data.get("code") != 0 or not d:
                logger.error(f"列表 API 错误: raw={raw_text}")
                await event.send(event.plain_result("列表解析失败，请查看控制台日志。"))
                return
            items = [m for m in (d.get("medias") or d.get("archives") or []) if m.get("bvid")]
            await event.send(event.plain_result(f"共获取到 {len(items)} 个视频。\n如需下载，请单独发送 BV 号。"))
        except Exception as e:
            logger.error(f"列表处理异常: {traceback.format_exc()}")
            await event.send(event.plain_result("列表处理失败，请查看控制台日志。"))

    def _html_to_text(self, html: str) -> str:
        if not html: return ""
        html = re.sub(r'<[^>]+>', '', re.sub(r'<img[^>]*>', '[图片]', re.sub(r'</h[1-6]>', '\n\n', re.sub(r'<br\s*/?>', '\n', re.sub(r'</p>', '\n\n', html, flags=re.I), flags=re.I), flags=re.I), flags=re.I))
        return re.sub(r'\n{3,}', '\n\n', html.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'")).strip()

    async def terminate(self):
        if self.session and not self.session.closed: await self.session.close()
        logger.info("B站下载插件已停止")