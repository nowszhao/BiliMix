"""
播客搜索与 RSS 解析服务模块
封装 iTunes Search API 搜索接口和 RSS Feed 解析功能。
"""
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def search_podcasts_itunes(query: str, limit: int = 25) -> dict:
    """通过 iTunes Search API 搜索播客，返回播客列表

    iTunes Search API 是 Apple 官方免费 API，无需 API Key。
    """
    try:
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({
            "term": query,
            "media": "podcast",
            "limit": limit,
        })
        req = urllib.request.Request(url, headers={
            "User-Agent": "BiliMix-PodcastHelper/1.0",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))

        items = result.get("results", [])
        podcasts = []
        for item in items:
            feed_url = item.get("feedUrl", "")
            if not feed_url:
                continue
            podcasts.append({
                "title": item.get("collectionName", ""),
                "author": item.get("artistName", ""),
                "description": (item.get("collectionCensoredName")
                                or item.get("primaryGenreName")
                                or "")[:200],
                "image": item.get("artworkUrl600", ""),
                "url": feed_url,  # RSS Feed URL
            })
        return {"podcasts": podcasts, "count": len(podcasts)}
    except Exception as e:
        return {"error": f"搜索失败: {str(e)}"}


def _format_duration(raw: str) -> str:
    """
    将 RSS 中的时长值统一格式化为 HH:MM:SS。

    支持输入格式：
    - "HH:MM:SS" → 原样返回
    - "MM:SS"   → 补零为 "M:SS" 或 "0:MM:SS"
    - 纯数字秒  → 转为 "HH:MM:SS"
    - 空字符串  → 返回空字符串
    """
    if not raw:
        return ""

    # 已是 HH:MM:SS 或 MM:SS 格式
    if re.match(r'^\d+:\d{2}(:\d{2})?$', raw):
        parts = raw.split(":")
        if len(parts) == 2:  # MM:SS
            m, s = int(parts[0]), int(parts[1])
            h, m = divmod(m, 60)
            if h > 0:
                return f"{h}:{m:02d}:{s:02d}"
            return f"{m}:{s:02d}"
        return raw  # 已是 HH:MM:SS

    # 纯数字（秒）
    try:
        total = int(raw)
    except (ValueError, TypeError):
        return raw  # 无法解析，原样返回

    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    if m > 0:
        return f"{m}:{s:02d}"
    return f"0:{s:02d}"


def parse_rss_feed(feed_url: str, max_episodes: int = 30) -> dict:
    """解析 RSS Feed URL，提取播客信息和单集列表"""
    try:
        req = urllib.request.Request(feed_url, headers={
            "User-Agent": "BiliMix-PodcastHelper/1.0"
        })
        with urllib.request.urlopen(req, timeout=20) as resp:
            content = resp.read()
        root = ET.fromstring(content)
        channel = root.find("channel")
        if channel is None:
            return {"error": "无效的 RSS Feed：未找到 channel 元素"}

        # 播客基本信息
        podcast_info = {
            "title": (channel.findtext("title") or "").strip(),
            "description": (channel.findtext("description") or "").strip()[:300],
            "author": "",
            "image": "",
            "link": (channel.findtext("link") or "").strip(),
        }

        # 尝试获取作者（itunes:author）
        for ns_prefix in [
            "{http://www.itunes.com/dtds/podcast-1.0.dtd}",
            "{http://www.itunes.com/dtds/podcast-1.0.dtd/}",
        ]:
            author_el = channel.find(f"{ns_prefix}author")
            if author_el is not None and author_el.text:
                podcast_info["author"] = author_el.text.strip()
                break

        # 尝试获取封面图
        itunes_ns = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
        img_el = channel.find(f"{itunes_ns}image")
        if img_el is not None:
            podcast_info["image"] = img_el.get("href", "")
        if not podcast_info["image"]:
            img_el2 = channel.find("image")
            if img_el2 is not None:
                url_el = img_el2.find("url")
                if url_el is not None and url_el.text:
                    podcast_info["image"] = url_el.text.strip()

        # 单集列表
        episodes = []
        items = channel.findall("item")
        for item in items[:max_episodes]:
            enclosure = item.find("enclosure")
            audio_url = ""
            audio_type = ""
            if enclosure is not None:
                audio_url = enclosure.get("url", "")
                audio_type = enclosure.get("type", "")

            # 时长：尝试多个位置/格式提取
            duration_str = ""
            # 尝试 itunes:duration（带命名空间）
            for ns_prefix in [
                "{http://www.itunes.com/dtds/podcast-1.0.dtd}",
            ]:
                dur_el = item.find(f"{ns_prefix}duration")
                if dur_el is not None and dur_el.text:
                    duration_str = dur_el.text.strip()
                    break
            # 回退：尝试不带命名空间的 duration 元素
            if not duration_str:
                dur_el = item.find("duration")
                if dur_el is not None and dur_el.text:
                    duration_str = dur_el.text.strip()
            # 回退：尝试从 enclosure 的 length 估算（无时长时的保底）
            if not duration_str:
                dur_el = item.find("enclosure")
                if dur_el is not None and dur_el.get("length"):
                    # 粗略按 ~64kbps 估算秒数
                    try:
                        length = int(dur_el.get("length", "0"))
                        if length > 0:
                            duration_str = str(length // 8000)
                    except (ValueError, TypeError):
                        pass
            # 统一格式化为 HH:MM:SS
            duration_str = _format_duration(duration_str)

            pub_date = (item.findtext("pubDate") or "").strip()

            episodes.append({
                "title": (item.findtext("title") or "").strip(),
                "description": (item.findtext("description") or "").strip()[:200],
                "enclosureUrl": audio_url,
                "enclosureType": audio_type,
                "duration": duration_str,
                "datePublished": pub_date,
            })

        return {
            "podcast": podcast_info,
            "episodes": episodes,
            "count": len(episodes),
        }
    except ET.ParseError as e:
        return {"error": f"RSS 解析失败: {str(e)}"}
    except Exception as e:
        return {"error": f"获取 RSS 失败: {str(e)}"}
