import re
import os
import time
import json
import aiohttp
import sys
from pathlib import Path

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

@register("saveany_bilibili_downloader", "Shou_Lu", "使用 saveany 解析并下载 B 站视频，支持多 Cookie 管理", "1.6.0")
class SaveAnyBilibiliDownloader(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.base_dir = Path(__file__).parent

        # 获取持久化数据目录（关键修改）
        self.data_dir = self._get_data_dir(context)
        os.makedirs(self.data_dir, exist_ok=True)

        # 默认配置
        self.download_dir = "./downloads"
        self.max_size_mb = 200.0
        self.bot_qq = ""
        self.quality = 80

        # 从 AstrBot 配置读取
        if config:
            self.download_dir = str(config.get("download_dir", self.download_dir))
            self.max_size_mb = float(config.get("max_size_mb", self.max_size_mb))
            self.bot_qq = str(config.get("bot_qq", self.bot_qq)).strip()
            self.quality = int(config.get("quality", self.quality))

        # 加载插件本地配置覆盖（数据目录已持久化）
        self._load_plugin_config()

        # Cookie 列表（数据目录已持久化）
        self.cookies = []
        self.bilibili_cookie = ""
        self._load_cookies()

        self.session = None
        self.plugin_api = PluginAPI(self)

    def _get_data_dir(self, context: Context) -> Path:
        """获取插件持久化数据目录（优先使用 AstrBot 官方方法）"""
        # 1. 尝试 context.get_plugin_data_dir()（或类似方法）
        for attr in ["get_plugin_data_dir", "get_data_dir"]:
            if hasattr(context, attr):
                try:
                    dir_path = getattr(context, attr)()
                    if dir_path:
                        return Path(dir_path) / "saveany_bilibili_downloader"
                except Exception:
                    pass

        # 2. 尝试从环境变量获取 AstrBot 数据根目录
        env_dir = os.environ.get("ASTRBOT_DATA_DIR")
        if env_dir:
            return Path(env_dir) / "plugin_data" / "saveany_bilibili_downloader"

        # 3. 回退到用户主目录下的标准路径
        fallback = Path.home() / ".astrbot" / "data" / "plugin_data" / "saveany_bilibili_downloader"
        logger.warning(f"无法获取 AstrBot 官方数据目录，使用回退路径: {fallback}")
        return fallback

    def _load_plugin_config(self):
        cfg_file = self.data_dir / "plugin_config.json"
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                if "download_dir" in data:
                    self.download_dir = str(data["download_dir"])
                if "max_size_mb" in data:
                    self.max_size_mb = float(data["max_size_mb"])
                if "bot_qq" in data:
                    self.bot_qq = str(data["bot_qq"]).strip()
                if "quality" in data:
                    self.quality = int(data["quality"])
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
        try:
            cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"保存插件本地配置失败: {e}")

    def _load_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        if cookie_file.exists():
            try:
                self.cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
            except Exception as e:
                logger.error(f"加载 Cookie 列表失败: {e}")
                self.cookies = []
        self._select_best_cookie()

    def _save_cookies(self):
        cookie_file = self.data_dir / "cookies.json"
        try:
            cookie_file.write_text(json.dumps(self.cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            self._select_best_cookie()
        except Exception as e:
            logger.error(f"保存 Cookie 列表失败: {e}")

    def _select_best_cookie(self):
        enabled = [c for c in self.cookies if c.get("enabled", True)]
        enabled.sort(key=lambda x: x.get("priority", 100))
        self.bilibili_cookie = enabled[0]["cookie"] if enabled else ""
        if self.bilibili_cookie:
            logger.info("已选择优先级最高的 Cookie 用于下载")

    async def initialize(self):
        self.session = aiohttp.ClientSession()
        os.makedirs(self.download_dir, exist_ok=True)
        self.plugin_api.register(self.context)
        logger.info("B站下载插件已启动")

    # ---------- 以下部分不变 ----------
    @filter.command("saveany")
    async def saveany(self, event: AstrMessageEvent, url: str):
        if not url:
            yield event.plain_result("请提供一个 B 站视频链接或 BV 号，例如：/saveany BV1xx411c7mD")
            return
        async for result in self.download_and_send(event, url):
            yield result

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_mention(self, event: AstrMessageEvent):
        if not await self._is_mentioned(event):
            return
        message = event.message_str.strip()
        if message.startswith('/saveany'):
            return
        url_or_bvid = self.extract_bvid_or_url(message)
        if not url_or_bvid:
            return
        event.stop_event()
        async for result in self.download_and_send(event, url_or_bvid):
            yield result

    async def _is_mentioned(self, event: AstrMessageEvent) -> bool:
        self_id = None
        try:
            self_id = event.message_obj.self_id
        except AttributeError:
            self_id = None
        if not self_id and self.bot_qq:
            self_id = self.bot_qq
        if not self_id:
            logger.warning("无法获取机器人自身 ID，@ 触发功能禁用。请在插件配置中填写 bot_qq。")
            return False
        for comp in event.get_messages():
            if isinstance(comp, Comp.At):
                if str(comp.qq) == str(self_id):
                    return True
        return False

    def extract_bvid_or_url(self, text: str) -> str:
        bvid = self.extract_bvid(text)
        if bvid:
            return bvid
        match = re.search(r'https?://[^\s]+', text)
        if match:
            return match.group(0)
        return None

    def extract_bvid(self, url: str) -> str:
        patterns = [r'BV[0-9A-Za-z]+', r'bvid=([0-9A-Za-z]+)']
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1) if 'bvid=' in pattern else match.group(0)
        return None

    async def download_and_send(self, event: AstrMessageEvent, url_or_bvid: str):
        bvid = self.extract_bvid(url_or_bvid)
        if not bvid:
            yield event.plain_result("无法识别 B 站视频 BV 号，请检查链接。")
            return
        try:
            video_info = await self.get_bilibili_video_info(bvid)
            if not video_info:
                yield event.plain_result("获取视频信息失败，可能视频不存在。")
                return
            download_url = await self.get_download_url(bvid, video_info.get("aid"), video_info.get("cid"))
            if not download_url:
                yield event.plain_result("获取下载地址失败，请稍后重试或尝试其他链接。")
                return
            yield event.plain_result(f"开始下载：{video_info['title']}\n请稍候...")
            file_path = await self.download_file(download_url, video_info['title'])
            if not file_path:
                yield event.plain_result("下载失败，可能是文件过大或网络问题。")
                return
            video = Comp.Video.fromFileSystem(path=file_path)
            yield event.chain_result([video])
        except Exception as e:
            logger.error(f"处理下载请求出错: {e}")
            yield event.plain_result(f"处理失败：{str(e)}")

    async def get_bilibili_video_info(self, bvid: str) -> dict:
        api_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        headers = {"User-Agent": "Mozilla/5.0"}
        if self.bilibili_cookie:
            headers["Cookie"] = self.bilibili_cookie
        try:
            async with self.session.get(api_url, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                if data["code"] != 0:
                    return None
                vd = data["data"]
                return {
                    "bvid": bvid,
                    "aid": vd["aid"],
                    "cid": vd["cid"],
                    "title": vd["title"],
                    "desc": vd.get("desc", ""),
                    "pic": vd.get("pic", ""),
                    "owner": vd.get("owner", {}).get("name", ""),
                    "face": vd.get("owner", {}).get("face", ""),
                }
        except Exception as e:
            logger.error(f"请求 B 站 API 异常: {e}")
            return None

    async def get_download_url(self, bvid: str, aid: int, cid: int) -> str:
        official_url = (
            f"https://api.bilibili.com/x/player/playurl?"
            f"avid={aid}&cid={cid}&qn={self.quality}&otype=json&platform=html5&fnver=0&fnval=1"
        )
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        if self.bilibili_cookie:
            headers["Cookie"] = self.bilibili_cookie
        try:
            async with self.session.get(official_url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data["code"] == 0 and data["data"].get("durl"):
                        return data["data"]["durl"][0]["url"]
        except Exception:
            pass
        parse_apis = [
            "https://api.injahow.cn/bparse/",
            "https://jx.jsonplayer.com/player/",
            "https://jx.bozrc.com:4433/player/",
            "https://jx.parwix.com:4433/player/",
        ]
        for api in parse_apis:
            try:
                async with self.session.get(f"{api}?bv={bvid}&q={self.quality}", timeout=15) as resp:
                    if resp.status != 200:
                        continue
                    data = await resp.json()
                    if "url" in data and data["url"]:
                        return data["url"]
                    if "data" in data and data["data"].get("url"):
                        return data["data"]["url"]
            except Exception as e:
                logger.warning(f"解析接口 {api} 失败: {e}")
                continue
        return None

    async def download_file(self, download_url: str, title: str) -> str:
        safe_title = re.sub(r'[\\/*?:"<>|]', '_', title)[:100]
        timestamp = int(time.time())
        file_path = os.path.join(self.download_dir, f"{safe_title}_{timestamp}.mp4")
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        if self.bilibili_cookie:
            headers["Cookie"] = self.bilibili_cookie
        try:
            async with self.session.get(download_url, headers=headers, timeout=60) as resp:
                if resp.status != 200:
                    return None
                content_length = resp.headers.get("Content-Length")
                if content_length:
                    size_mb = int(content_length) / (1024 * 1024)
                    if size_mb > self.max_size_mb:
                        return None
                with open(file_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(1024 * 1024):
                        f.write(chunk)
                logger.info(f"视频已下载: {file_path}")
                return file_path
        except Exception as e:
            logger.error(f"下载文件异常: {e}")
            return None

    async def terminate(self):
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("B站下载插件已停止")