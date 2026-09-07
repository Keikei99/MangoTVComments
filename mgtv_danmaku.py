#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
芒果TV(mgtv.com) 弹幕抓取工具

用法:
    python3 mgtv_danmaku.py "https://www.mgtv.com/b/815824/24328855.html"
    python3 mgtv_danmaku.py "<url>" --out data --delay 0.3

原理:
    1. 从播放页 URL 解析 cid(合集id) 和 vid(视频id)
    2. 调用 pcweb.api.mgtv.com/video/info 拿到标题与时长
    3. 按每 60 秒一个分片轮询 galaxy.bz.mgtv.com/rdbarrage 拉取弹幕
    4. 按弹幕 ids 去重后导出 CSV / JSON
"""

import argparse
import csv
import json
import os
import re
import sys
import time

import requests

INFO_API = "https://pcweb.api.mgtv.com/video/info"
BARRAGE_API = "https://galaxy.bz.mgtv.com/rdbarrage"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.mgtv.com/",
    "Origin": "https://www.mgtv.com",
}


def parse_url(url):
    """从 https://www.mgtv.com/b/815824/24328855.html 解析出 (cid, vid)"""
    m = re.search(r"/b/(\d+)/(\d+)", url)
    if not m:
        raise ValueError(f"无法从 URL 中解析 cid/vid: {url}")
    return m.group(1), m.group(2)


def hhmmss_to_seconds(text):
    """'59:14' 或 '1:02:33' -> 秒数"""
    parts = [int(p) for p in text.strip().split(":")]
    total = 0
    for p in parts:
        total = total * 60 + p
    return total


def get_video_info(session, cid, vid):
    r = session.get(INFO_API, params={"cid": cid, "vid": vid}, timeout=15)
    r.raise_for_status()
    info = r.json()["data"]["info"]
    return {
        "cid": cid,
        "vid": vid,
        "title": info.get("videoName", ""),
        "clip_name": info.get("clipName", ""),
        "duration_text": info.get("time", ""),
        "duration": hhmmss_to_seconds(info.get("time", "0")),
    }


def fetch_segment(session, cid, vid, time_ms, retries=3):
    """拉取某个时间点(毫秒)所在的 60 秒弹幕分片"""
    params = {"version": "3.0.0", "vid": vid, "cid": cid, "time": time_ms}
    last_err = None
    for attempt in range(retries):
        try:
            r = session.get(BARRAGE_API, params=params, timeout=15)
            r.raise_for_status()
            payload = r.json()
            if payload.get("status") != 0:
                raise RuntimeError(f"接口返回异常: {payload.get('msg')}")
            return payload.get("data") or {}
        except Exception as e:  # 网络抖动/限流时退避重试
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"time={time_ms} 拉取失败: {last_err}")


def ms_to_timestamp(ms):
    """毫秒 -> HH:MM:SS.mmm，方便对照视频进度"""
    ms = int(ms)
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, msec = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{msec:03d}"


def crawl(url, out_dir=".", delay=0.3):
    session = requests.Session()
    session.headers.update(HEADERS)

    cid, vid = parse_url(url)
    meta = get_video_info(session, cid, vid)
    print(f"标题: {meta['clip_name']} / {meta['title']}")
    print(f"cid={cid} vid={vid} 时长={meta['duration_text']} ({meta['duration']}s)")

    duration_ms = meta["duration"] * 1000
    seen = set()
    rows = []
    t = 0
    empty_streak = 0

    while t <= duration_ms + 60000:
        data = fetch_segment(session, cid, vid, t)
        items = data.get("items") or []
        new = 0
        for it in items:
            did = str(it.get("ids"))
            if did in seen:
                continue
            seen.add(did)
            rows.append(
                {
                    "id": did,
                    "uid": it.get("uid"),
                    "time_ms": it.get("time"),
                    "timestamp": ms_to_timestamp(it.get("time", 0)),
                    "content": (it.get("content") or "").replace("\n", " ").strip(),
                    "up": it.get("up", 0),
                    "type": it.get("type", 0),
                }
            )
            new += 1

        empty_streak = empty_streak + 1 if not items else 0
        print(
            f"  [{ms_to_timestamp(t)}] 本片段 {len(items):>4} 条 (新增 {new:>4})"
            f" | 累计 {len(rows)}",
            flush=True,
        )

        # 优先跟随接口给出的 next 指针，避免漏段
        nxt = data.get("next")
        t = nxt if isinstance(nxt, int) and nxt > t else t + 60000

        # 结尾连续多个空片段则提前结束
        if empty_streak >= 5 and t > duration_ms:
            break
        time.sleep(delay)

    rows.sort(key=lambda x: (x["time_ms"] or 0))
    return meta, rows


def save(meta, rows, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[\\/:*?\"<>|]", "_", meta["title"] or meta["vid"])[:80]
    base = os.path.join(out_dir, f"{meta['vid']}_{safe}")

    csv_path = base + ".csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f, fieldnames=["id", "uid", "time_ms", "timestamp", "content", "up", "type"]
        )
        w.writeheader()
        w.writerows(rows)

    json_path = base + ".json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "count": len(rows), "danmaku": rows},
                  f, ensure_ascii=False, indent=2)

    return csv_path, json_path


def main():
    ap = argparse.ArgumentParser(description="芒果TV弹幕抓取")
    ap.add_argument("url", help="芒果TV播放页链接")
    ap.add_argument("--out", default="data", help="输出目录 (默认: data)")
    ap.add_argument("--delay", type=float, default=0.3, help="请求间隔秒数 (默认: 0.3)")
    args = ap.parse_args()

    meta, rows = crawl(args.url, args.out, args.delay)
    csv_path, json_path = save(meta, rows, args.out)

    print(f"\n完成: 共 {len(rows)} 条弹幕")
    print(f"  CSV : {csv_path}")
    print(f"  JSON: {json_path}")


if __name__ == "__main__":
    sys.exit(main())
