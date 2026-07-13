#!/usr/bin/env python3
"""
xchina.co video downloader -> Telegram channel
Railway deployment: self-looping, fetches one page every 6 hours
State files persisted in /data (Railway volume)
"""
import requests
from bs4 import BeautifulSoup
import os, re, time, json, sys, subprocess, tempfile, asyncio, base64, gzip, logging
from telethon import TelegramClient
import urllib3
from PIL import Image
import io
urllib3.disable_warnings()

# ==================== Logging ====================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("xchina")

# Raise Pillow decompression bomb limit to avoid rejecting large cover images
Image.MAX_IMAGE_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "200000000"))

# ==================== Config ====================
CF_COOKIE       = os.getenv("CF_COOKIE", "")
BASE_URL        = "https://xchina.co"
SERIES_URL      = "https://xchina.co/videos/series-63824a975d8ae/{page}.html"
FIRST_URL       = "https://xchina.co/videos/series-63824a975d8ae.html"
DATA_DIR        = os.getenv("DATA_DIR", "/data")
SEEN_FILE       = os.path.join(DATA_DIR, "seen_xchina_video.json")
PAGE_FILE       = os.path.join(DATA_DIR, "next_video_page.txt")
SESSION_FILE    = os.path.join(DATA_DIR, "xchina_video.session")
START_PAGE      = 1
FETCH_PAGES     = 1
TG_INTERVAL     = int(os.getenv("TG_INTERVAL", "10"))
LOOP_INTERVAL   = int(os.getenv("LOOP_INTERVAL", "21600"))
FFMPEG_TIMEOUT  = int(os.getenv("FFMPEG_TIMEOUT", "300"))
MAX_SEEN_ENTRIES = int(os.getenv("MAX_SEEN_ENTRIES", "50000"))
API_ID          = int(os.getenv("TG_API_ID", "0"))
API_HASH        = os.getenv("TG_API_HASH", "")
PHONE           = os.getenv("TG_PHONE", "")
CHAT_ID         = int(os.getenv("TG_CHAT_ID", "0"))
TG_SESSION_B64  = os.getenv("TG_SESSION", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

# SSL verification: enabled by default; set VERIFY_SSL=0 to disable
if os.getenv("VERIFY_SSL", "1") == "0":
    logger.warning("SSL verification disabled")
    SESSION.verify = False
else:
    SESSION.verify = True


def inject_cookies(cookie_str):
    if not cookie_str:
        logger.warning("CF_COOKIE not set")
        return
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            SESSION.cookies.set(k.strip(), v.strip(), domain="xchina.co")
            SESSION.cookies.set(k.strip(), v.strip(), domain="video.xchina.download")
    logger.info("Cookies injected")


inject_cookies(CF_COOKIE)

# ==================== State files ====================
def load_page():
    if not os.path.exists(PAGE_FILE):
        return START_PAGE
    try:
        return max(int(open(PAGE_FILE).read().strip()), 1)
    except:
        return START_PAGE


def save_page(page):
    with open(PAGE_FILE, "w") as f:
        f.write(str(page))


def load_seen():
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        data = json.load(open(SEEN_FILE, encoding="utf-8"))
        seen = set(data)
        # Trim to max entries if exceeded
        if len(seen) > MAX_SEEN_ENTRIES:
            logger.warning(f"seen set has {len(seen)} entries, trimming to {MAX_SEEN_ENTRIES}")
            seen = set(list(seen)[-MAX_SEEN_ENTRIES:])
        return seen
    except:
        return set()


def save_seen(seen):
    # Enforce maximum size before saving
    if len(seen) > MAX_SEEN_ENTRIES:
        logger.warning(f"Trimming seen set from {len(seen)} to {MAX_SEEN_ENTRIES}")
        seen = set(list(seen)[-MAX_SEEN_ENTRIES:])
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, ensure_ascii=False)


# ==================== Session ====================
def restore_session():
    """Restore Telegram session from TG_SESSION env var. Returns True if restored."""
    if not TG_SESSION_B64:
        return False
    try:
        raw = base64.b64decode(TG_SESSION_B64)
        # Try gzip decompression (gzip magic: 1f 8b)
        if raw[:2] == b'\x1f\x8b':
            raw = gzip.decompress(raw)
            logger.info("TG_SESSION decompressed from gzip")
        # Delete old session file (and journal) to avoid SQLite lock issues
        for f in [SESSION_FILE, SESSION_FILE + "-journal", SESSION_FILE + "-wal", SESSION_FILE + "-shm"]:
            if os.path.exists(f):
                try:
                    os.unlink(f)
                    logger.debug(f"Deleted old session file: {f}")
                except:
                    pass
        # Only write if content changed (avoid unnecessary disk I/O)
        with open(SESSION_FILE, "wb") as f:
            f.write(raw)
        logger.info("Session restored from TG_SESSION")
        return True
    except Exception as e:
        logger.warning(f"Session restore failed: {e}")
        return False


# ==================== HTTP requests ====================
def safe_get(url, retries=2, timeout=15):
    for i in range(retries):
        try:
            logger.debug(f"  GET {url[:60]}...")
            sys.stdout.flush()
            r = SESSION.get(url, timeout=timeout)
            logger.debug(f"  status={r.status_code}, size={len(r.text)}")
            sys.stdout.flush()
            if r.status_code == 200:
                # Cloudflare challenge detection: check for common indicators
                text_lower = r.text.lower()
                is_cf = ("cloudflare" in text_lower or
                         "cf-" in text_lower or
                         "challenge-platform" in text_lower)
                if len(r.text) < 20000 and is_cf:
                    logger.warning(f"  Cloudflare challenge detected ({len(r.text)} bytes)")
                    return None
                return r
            elif r.status_code == 403:
                logger.warning(f"  403: {url}")
                return None
            elif r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 30))
                logger.warning(f"  Rate limited (429), waiting {wait}s before retry")
                time.sleep(wait)
                # Retry immediately after waiting; do not count as a consumed attempt
                try:
                    logger.debug(f"  Retrying GET after 429 {url[:60]}...")
                    r2 = SESSION.get(url, timeout=timeout)
                    if r2.status_code == 200:
                        text_lower2 = r2.text.lower()
                        is_cf2 = ("cloudflare" in text_lower2 or
                                  "cf-" in text_lower2 or
                                  "challenge-platform" in text_lower2)
                        if len(r2.text) < 20000 and is_cf2:
                            logger.warning(f"  Cloudflare challenge after 429 wait")
                            return None
                        return r2
                    logger.warning(f"  After 429 wait: HTTP {r2.status_code}")
                except Exception as e2:
                    logger.warning(f"  After 429 wait: {type(e2).__name__}: {e2}")
                return None
            else:
                logger.warning(f"  HTTP {r.status_code}")
                time.sleep(2)
        except requests.exceptions.ConnectTimeout as e:
            logger.warning(f"  Connect timeout ({i+1}/{retries}): {url[:40]}...")
            sys.stdout.flush()
            time.sleep(2)
        except requests.exceptions.ReadTimeout as e:
            logger.warning(f"  Read timeout ({i+1}/{retries}): {url[:40]}...")
            sys.stdout.flush()
            time.sleep(2)
        except Exception as e:
            logger.warning(f"  Request error ({i+1}/{retries}): {type(e).__name__}: {e}")
            sys.stdout.flush()
            time.sleep(2)
    return None


def fix_url(url):
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE_URL + url
    if not url.startswith("http"):
        return BASE_URL + "/" + url
    return url


def get_page_url(page):
    return FIRST_URL if page == 1 else SERIES_URL.format(page=page)


# ==================== Video list parsing ====================
def get_videos_from_list(page):
    url = get_page_url(page)
    logger.info(f"Listing page {page}: {url}")
    sys.stdout.flush()
    r = safe_get(url)
    if not r:
        raise RuntimeError(f"Cloudflare challenge or request failed (page {page}), please update CF_COOKIE")
    soup = BeautifulSoup(r.text, "html.parser")
    has_video_list = bool(soup.select_one("div.list.video-list"))
    items = soup.select("div.item.video")
    logger.info(f"  has_video_list={has_video_list}, item_count={len(items)}")
    sys.stdout.flush()
    videos = []
    seen_ids = set()
    for item in soup.select("div.list.video-list div.item.video"):
        a_tag = item.find("a", href=re.compile(r'/video/id-[a-f0-9]+\.html'))
        if not a_tag:
            continue
        detail_url = a_tag["href"]
        if not detail_url.startswith("http"):
            detail_url = BASE_URL + detail_url
        m = re.search(r'/video/id-([a-f0-9]+)\.html', detail_url)
        if not m:
            continue
        vid_id = m.group(1)
        if vid_id in seen_ids:
            continue
        seen_ids.add(vid_id)
        cover = ""
        img_div = item.select_one("div.img[style]")
        if img_div:
            mc = re.search(r"url\(['\"]?(https?://[^'\"\s]+)['\"]?\)", img_div.get("style",""))
            if mc:
                cover = mc.group(1)
        actor = ""
        model_div = item.select_one(".model-container")
        if model_div:
            actor = model_div.get_text(strip=True)
        title_from_list = a_tag.get("title", "") or a_tag.get_text(strip=True)
        platform = ""
        tags_div = item.select_one("div.tags")
        if tags_div:
            for d in tags_div.find_all("div", recursive=False):
                if not d.get("class") and not d.find("i"):
                    t = d.get_text(strip=True)
                    if t:
                        platform = t
                        break
        videos.append({
            "vid_id": vid_id,
            "url": detail_url,
            "cover": cover,
            "婕斿憳": actor,
            "骞冲彴": platform,
            "鏍囬": title_from_list,
        })
    logger.info(f"  Found {len(videos)} videos")
    return videos


# ==================== Detail page parsing ====================
def get_m3u8_url(video_url):
    r = safe_get(video_url)
    if not r:
        return None
    # Primary: match video.xchina.download domain m3u8
    m = re.search(
        r"src:\s*['\"]"
        r"(https://video\.xchina\.download/m3u8/[^'\"]+\.m3u8[^'\"]*)"
        r"['\"]",
        r.text
    )
    if m:
        return m.group(1)
    # Fallback: only match xchina-related domain m3u8 links
    m2 = re.search(r"(https://[^\s'\"<>]*xchina[^\s'\"<>]*\.m3u8[^\s'\"<>]*)", r.text)
    return m2.group(1) if m2 else None


def get_preview_image_url(video_url):
    r = safe_get(video_url)
    if not r:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    container = soup.select_one("div.screenshot-container")
    if container:
        imgs = container.find_all("img")
        if imgs:
            src = imgs[0].get("src") or imgs[0].get("data-src")
            if src:
                return fix_url(src)
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return fix_url(og["content"])
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return fix_url(tw["content"])
    return None


# ==================== Image & video processing ====================
def download_and_convert_thumbnail(url, referer, max_size_kb=200, max_dim=640):
    try:
        logger.info(f"  Downloading preview: {url[:80]}...")
        r = SESSION.get(url, headers={"Referer": referer}, timeout=30)
        r.raise_for_status()
        if len(r.content) < 2000:
            logger.warning("  Image too small, may be invalid")
            return None
        img = Image.open(io.BytesIO(r.content))
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = bg
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        # Use new Resampling.LANCZOS, with fallback to old LANCZOS and BICUBIC
        resample_filter = getattr(Image.Resampling, 'LANCZOS', getattr(Image, 'LANCZOS', Image.BICUBIC))
        img.thumbnail((max_dim, max_dim), resample_filter)
        thumb_io = io.BytesIO()
        quality = 85
        while quality >= 30:
            thumb_io.seek(0)
            thumb_io.truncate()
            img.save(thumb_io, format='JPEG', quality=quality, optimize=True)
            if thumb_io.tell() <= max_size_kb * 1024:
                break
            quality -= 10
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.jpg')
        tmp.write(thumb_io.getvalue())
        tmp.close()
        logger.info(f"  Thumbnail ready: {os.path.getsize(tmp.name)//1024}KB")
        return tmp.name
    except Image.DecompressionBombError:
        logger.error("  Image too large (decompression bomb limit), skipped")
        return None
    except Exception as e:
        logger.error(f"  Thumbnail processing failed: {e}")
        return None


def download_m3u8_to_mp4(m3u8_url, referer):
    """Download m3u8 video using Python requests for ts segments (keeps cookies) + ffmpeg for merging."""
    import concurrent.futures
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    tmp.close()
    out_path = tmp.name
    tmp_dir = None
    tmp_dir = tempfile.mkdtemp(prefix="m3u8_")
    try:
        logger.info(f"  Downloading m3u8 segments (requests+ffmpeg)...")
        sys.stdout.flush()
        # Step 1: Fetch m3u8 playlist
        r = SESSION.get(m3u8_url, headers={"Referer": referer}, timeout=30)
        r.raise_for_status()
        playlist = r.text
        # Step 2: Parse and filter .ts segment URLs
        from urllib.parse import urljoin
        segments = []
        for line in playlist.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                seg_url = urljoin(m3u8_url, line)
                segments.append(seg_url)
        if not segments:
            logger.warning("  No .ts segments found in m3u8")
            return None
        logger.info(f"  Found {len(segments)} segments, downloading in parallel...")
        # Step 3: Download segments in parallel (8 threads)
        seg_files = []
        def download_seg(idx_url):
            idx, url = idx_url
            fname = os.path.join(tmp_dir, f"seg_{idx:05d}.ts")
            try:
                sr = SESSION.get(url, headers={"Referer": referer}, timeout=60)
                sr.raise_for_status()
                if len(sr.content) < 100:
                    raise Exception(f"Segment too small: {len(sr.content)} bytes")
                with open(fname, 'wb') as f:
                    f.write(sr.content)
                return (idx, fname)
            except Exception as e:
                logger.warning(f"  Segment {idx} failed: {e}")
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(download_seg, enumerate(segments)))
        seg_files = [r for r in results if r is not None]
        seg_files.sort(key=lambda x: x[0])
        if len(seg_files) < len(segments) * 0.8:
            logger.warning(f"  Too many segments failed: {len(seg_files)}/{len(segments)}")
            return None
        # Step 4: Binary concatenate TS segments to temp file
        ts_path = os.path.join(tmp_dir, "concat.ts")
        with open(ts_path, "wb") as outf:
            for _, fname in seg_files:
                with open(fname, "rb") as inf:
                    outf.write(inf.read())
        ts_size = os.path.getsize(ts_path) / 1024 / 1024
        logger.info(f"  TS downloaded: {ts_size:.1f}MB ({len(seg_files)} segments)")
        # Step 5: Remux TS to proper MP4 (so Telegram can stream inline)
        cmd = [
            "ffmpeg", "-y", "-i", ts_path,
            "-c", "copy", "-movflags", "+faststart",
            "-f", "mp4", out_path
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=FFMPEG_TIMEOUT)
        except subprocess.TimeoutExpired:
            logger.warning(f"  ffmpeg remux timeout ({FFMPEG_TIMEOUT}s)")
            return None
        except subprocess.CalledProcessError as e:
            logger.warning(f"  ffmpeg remux failed: {e.stderr.decode()[:200] if e.stderr else 'unknown'}")
            return None
        size_mb = os.path.getsize(out_path) / 1024 / 1024
        logger.info(f"  MP4 ready: {size_mb:.1f}MB")
        return out_path
    except Exception as e:
        logger.error(f"  Download error: {e}")
        return None
    finally:
        # Cleanup temp dir
        for f in os.listdir(tmp_dir):
            try:
                os.unlink(os.path.join(tmp_dir, f))
            except:
                pass
        try:
            os.rmdir(tmp_dir)
        except:
            pass


# ==================== Telegram sending ====================

def build_caption(info):
    title = info.get("标题", "Unknown")
    platform = info.get("平台", "")
    actor = info.get("演员", "")
    lines = [f"标题：{title}"]
    if platform:
        lines.append(f"平台：#{platform}")
    if actor:
        lines.append(f"演员：#{actor}")
    return "\n".join(lines)

async def send_video_with_thumb(client, video_path, thumb_path, caption):
    try:
        logger.info("  Sending video to channel...")
        await client.send_file(
            CHAT_ID,
            video_path,
            caption=caption,
            thumb=thumb_path,
            supports_streaming=True,
            force_document=False
        )
        logger.info("  Video sent successfully")
        return True
    except Exception as e:
        logger.error(f"  Send failed: {e}")
        return False

# ==================== Session restore helper ====================
_session_restored_at_least_once = False


def ensure_session():
    """Restore session if TG_SESSION env var is set and session file missing/stale."""
    global _session_restored_at_least_once
    if not TG_SESSION_B64:
        return
    # Always try to restore on first call; afterwards only if session file is missing
    if not _session_restored_at_least_once or not os.path.exists(SESSION_FILE):
        if restore_session():
            _session_restored_at_least_once = True


# ==================== Single run ====================
async def run_once():
    ensure_session()
    seen = load_seen()
    current_page = load_page()
    logger.info(f"Starting from page {current_page}, fetching {FETCH_PAGES} page(s)")
    pages = list(range(current_page, current_page + FETCH_PAGES))
    all_videos = []
    for page in pages:
        try:
            vids = get_videos_from_list(page)
        except RuntimeError as e:
            logger.error(f"{e}")
            logger.info("State saved, will retry this page next run")
            return False
        # Reverse so newest videos (from higher-numbered pages) are processed first
        vids.reverse()
        all_videos.extend(vids)
        time.sleep(1)
    seen_run = set()
    unique = []
    for v in all_videos:
        if v["vid_id"] not in seen and v["vid_id"] not in seen_run:
            unique.append(v)
            seen_run.add(v["vid_id"])
    logger.info(f"New videos: {len(unique)}")
    if not unique:
        logger.info("No new videos, advancing to next page")
        save_page(current_page + FETCH_PAGES)
        return True
    logger.info("  Connecting to Telegram...")
    sys.stdout.flush()
    client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
    try:
        # When session is valid, phone is not needed; pass None instead of empty string
        phone_to_use = PHONE if PHONE else None
        await asyncio.wait_for(client.start(phone=phone_to_use), timeout=30)
        logger.info("Telegram client logged in")
    except asyncio.TimeoutError:
        logger.warning("Telegram login timeout (30s), will retry with backoff")
        await client.disconnect()
        return False
    except Exception as e:
        logger.error(f"Telegram login failed: {e}, will retry with backoff")
        # Delete session file so it can be re-restored from env var next run
        for sf in [SESSION_FILE, SESSION_FILE + "-journal", SESSION_FILE + "-wal", SESSION_FILE + "-shm"]:
            try:
                os.unlink(sf)
            except:
                pass
        await client.disconnect()
        return False
    sys.stdout.flush()
    try:
        failed_videos = []
        for idx, video in enumerate(unique):
            logger.info(f"[{idx+1}/{len(unique)}] {video['url']}")
            m3u8 = get_m3u8_url(video["url"])
            if not m3u8:
                logger.warning("  No m3u8 found, will retry next run")
                failed_videos.append(video["vid_id"])
                continue
            video_path = download_m3u8_to_mp4(m3u8, video["url"])
            if not video_path:
                logger.warning("  Download failed, will retry next run")
                failed_videos.append(video["vid_id"])
                continue
            thumb_path = None
            img_url = get_preview_image_url(video["url"])
            if not img_url and video.get("cover"):
                img_url = video["cover"]
                logger.info(f"  Using list page cover: {img_url}")
            if img_url:
                thumb_path = download_and_convert_thumbnail(img_url, video["url"])
            else:
                logger.warning("  No preview image found, sending without thumbnail")
            caption = build_caption(video)
            ok = await send_video_with_thumb(client, video_path, thumb_path, caption)
            for p in [video_path, thumb_path]:
                if p and os.path.exists(p):
                    os.unlink(p)
            if ok:
                seen.add(video["vid_id"])
                save_seen(seen)
            else:
                logger.warning("  Send failed, will retry next run")
                failed_videos.append(video["vid_id"])
            await asyncio.sleep(TG_INTERVAL)
        if failed_videos:
            logger.warning(f"  {len(failed_videos)} video(s) failed, will retry next run")
    finally:
        await client.disconnect()
    save_page(current_page + FETCH_PAGES)
    logger.info(f"Done, next run starts from page {current_page + FETCH_PAGES}")
    return True


# ==================== Entrypoint ====================
def main():
    if not CF_COOKIE:
        logger.error("CF_COOKIE not set")
        sys.exit(1)
    if not API_ID or not API_HASH or not CHAT_ID:
        logger.error("Missing Telegram config")
        sys.exit(1)
    if not PHONE and not TG_SESSION_B64:
        logger.error("TG_PHONE or TG_SESSION is required")
        sys.exit(1)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(PAGE_FILE):
        with open(PAGE_FILE, "w") as f:
            f.write(str(START_PAGE))
        logger.info(f"{PAGE_FILE} initialized to {START_PAGE}")
    if not os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "w") as f:
            json.dump([], f)
        logger.info(f"{SEEN_FILE} initialized to empty")
    while True:
        logger.info("=" * 50)
        logger.info(f"{time.strftime('%Y-%m-%d %H:%M:%S')} -- Starting new run")
        logger.info("=" * 50)
        try:
            ok = asyncio.run(run_once(), debug=False)
        except Exception as e:
            logger.error(f"Run exception: {e}", exc_info=True)
            ok = False
        if ok:
            next_run = time.strftime('%Y-%m-%d %H:%M:%S',
                                     time.localtime(time.time() + LOOP_INTERVAL))
            logger.info(f"Waiting {LOOP_INTERVAL}s, next run: {next_run}")
        else:
            backoff = min(LOOP_INTERVAL // 2, 1800)
            retry_time = time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(time.time() + backoff))
            logger.info(f"Run not fully successful, retrying in {backoff}s ({retry_time})")
            time.sleep(backoff)
            continue
        time.sleep(LOOP_INTERVAL)


if __name__ == "__main__":
    main()
