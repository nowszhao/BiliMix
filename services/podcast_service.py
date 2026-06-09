"""
播客搜索与 RSS 解析服务模块
封装 iTunes Search API 搜索接口和 RSS Feed 解析功能。
"""
import json
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

            # 时长
            duration_str = ""
            for ns_prefix in [
                "{http://www.itunes.com/dtds/podcast-1.0.dtd}",
            ]:
                dur_el = item.find(f"{ns_prefix}duration")
                if dur_el is not None and dur_el.text:
                    duration_str = dur_el.text.strip()
                    break

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
