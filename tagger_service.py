import asyncio
import os
import shutil
import re
import logging
import signal
import time
import random
import requests
from logging.handlers import RotatingFileHandler
from aiohttp import web
from datetime import datetime
from shazamio import Shazam
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
import mutagen.id3 as id3
from mutagen.wave import WAVE
from mutagen.mp4 import MP4

# Note: .env loading intentionally omitted (use environment variables)

# Rate limit settings (seconds between Shazam API calls)
SHAZAM_DELAY = float(os.getenv('SHAZAM_DELAY', '3'))
SHAZAM_RETRIES = int(os.getenv('SHAZAM_RETRIES', '3'))
SHAZAM_TIMEOUT = float(os.getenv('SHAZAM_TIMEOUT', '30'))

# Service configuration
WATCH_DIR = os.getenv('WATCH_DIR', '/music/watch')
TAG_DIR = os.getenv('TAG_DIR', '/music/library')
UNMANAGE_DIR = os.getenv('UNMANAGE_DIR', '/music/unmanage')
INTERVAL = int(os.getenv('INTERVAL', '300'))
LOG_FILE = os.getenv('LOG_FILE', '/logs/tagger_service.log')
MAX_WORKERS = int(os.getenv('MAX_WORKERS', '3'))
HTTP_PORT = int(os.getenv('HTTP_PORT', '5000'))

# ปิด Log ของ Mutagen เพื่อความสะอาดของหน้าจอ
logging.getLogger('mutagen').setLevel(logging.ERROR)
logger = logging.getLogger('shazam_tagger')


def setup_logging():
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%Y-%m-%d %H:%M:%S')

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_dir = os.path.dirname(LOG_FILE)
    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            file_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding='utf-8')
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except Exception as exc:
            logger.warning('Unable to configure file logger %s: %s', LOG_FILE, exc)


shutdown_event = asyncio.Event()


def _shutdown_signal_handler(sig):
    logger.info('Received shutdown signal: %s', sig.name if hasattr(sig, 'name') else sig)
    shutdown_event.set()


def configure_signal_handlers(loop):
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: _shutdown_signal_handler(s))
        except NotImplementedError:
            logger.warning('Signal handlers not supported on this platform: %s', sig)


class AsyncRateLimiter:
    """ ระบบจัดคิวหน่วงเวลาแบบ Async เพื่อไม่ให้ส่งคำขอไปหาสะสมพร้อมกันเกินกำหนด (Rate Limit Protection) """
    def __init__(self, delay):
        self.delay = delay
        self.lock = asyncio.Lock()
        self.last_call = 0.0

    async def wait(self):
        async with self.lock:
            now = time.monotonic()
            elapsed = now - self.last_call
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self.last_call = time.monotonic()

def repair_thai_encoding(text):
    """ แก้ปัญหาภาษาต่างดาวขั้นสูง รองรับทั้ง Latin-1->CP874 และ UTF-8 in Latin-1 """
    if not text: return ""
    text = str(text).strip()
    
    # วิธีที่ 1: แก้ไขเคสลืมกล่อง UTF-8 (เช่น à¸žà¸‡à¸©à¹Œà¸ªà¸´à¸—à¸˜à¸´à¹Œ)
    try:
        repaired = text.encode('latin-1').decode('utf-8')
        # ตรวจสอบว่ามีพยัญชนะหรือสระไทยหลักๆ อยู่หลังจากซ่อมหรือไม่
        if any(c in repaired for c in "กขคตงจชนยรลวมสอาิีุููเแโำะา้๊๋็"):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    # วิธีที่ 2: แก้ไขเคสเก่า Windows-874 ผิดรหัส (สคริปต์เดิม)
    try:
        repaired = text.encode('latin-1').decode('cp874')
        if any(c in repaired for c in "กขคตงจชนยรลวมสอาิีุููเแโำะา้๊๋็"):
            return repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass

    return text

def is_valid_tag(text):
    """ ตรวจสอบว่า Tag ที่ได้มาใช้งานได้จริงหรือไม่ (ไม่ว่าง ไม่ประหลาด และไม่มีเศษอักษรต่างดาวค้าง) """
    if not text: return False
    text = str(text).strip()
    if len(text) < 1: return False
    if re.fullmatch(r'[?\. ]+', text): return False
    
    # ดักจับถ้าหากยังมีตัวหนังสือเศษต่างดาวภาษาไทยที่ซ่อมไม่สำเร็จค้างอยู่
    if any(char in text for char in ['à', '¸', '¹', 'º', 'à¸', 'à¹', '„']):
        return False
        
    return True

async def health(request):
    return web.json_response({
        "status": "ok",
        "service": "shazam-tagger",
        "time": datetime.utcnow().isoformat() + "Z",
        "watch_dir": WATCH_DIR,
        "tag_dir": TAG_DIR,
        "unmanage_dir": UNMANAGE_DIR,
    })

async def start_health_server():
    app = web.Application()
    app.add_routes([web.get('/health', health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', HTTP_PORT)
    await site.start()
    logger.info('Health endpoint listening on http://0.0.0.0:%s/health', HTTP_PORT)
    return runner


async def stop_health_server(runner):
    if runner is None:
        return
    try:
        await runner.cleanup()
        logger.info('Health endpoint stopped')
    except Exception as exc:
        logger.warning('Unable to stop health endpoint cleanly: %s', exc)

def get_audio_quality(file_path):
    """ ดึงค่า Bitrate และขนาดไฟล์เพื่อใช้เทียบคุณภาพ """
    try:
        audio = File(file_path)
        if audio is None: return 0, 0
        bitrate = getattr(audio.info, 'bitrate', 0)
        filesize = os.path.getsize(file_path)
        return bitrate, filesize
    except Exception:
        return 0, 0

def clean_filename(text):
    """ ล้างอักขระที่ระบบปฏิบัติการไม่รองรับในการตั้งชื่อไฟล์ """
    if not text: return "Unknown"
    clean = re.sub(r'[\x00-\x1f\x7f\\/*?:"<>|]', "", str(text))
    return clean.strip()


def sanitize_tag_value(text):
    """Remove embedded nulls and control characters from tag values."""
    if not text:
        return ""
    return re.sub(r'[\x00-\x1f\x7f]+', "", str(text)).strip()


def safe_truncate_path(artist, album, title, ext, base_dir="/music/tag", max_filename=200):
    """ Truncate artist/album/title to keep full path under filesystem limits. """
    def truncate_utf8(value, max_bytes):
        value = clean_filename(value)
        encoded = value.encode('utf-8')
        if len(encoded) <= max_bytes:
            return value
        low, high = 0, len(value)
        while low < high:
            mid = (low + high + 1) // 2
            if len(value[:mid].encode('utf-8')) <= max_bytes:
                low = mid
            else:
                high = mid - 1
        return value[:low].rstrip()

    artist_safe = truncate_utf8(artist, 200)
    album_safe = truncate_utf8(album, 200)
    title_safe = truncate_utf8(title, max_filename)

    filename = f"{title_safe}{ext}"
    if len(filename.encode('utf-8')) > 250:
        title_safe = truncate_utf8(title, 220 - len(ext.encode('utf-8')))
        filename = f"{title_safe}{ext}"
        if len(filename.encode('utf-8')) > 250:
            title_safe = truncate_utf8(title, 180 - len(ext.encode('utf-8')))
            filename = f"{title_safe}{ext}"
        if len(filename.encode('utf-8')) > 250:
            title_safe = truncate_utf8(title, 120 - len(ext.encode('utf-8')))
            filename = f"{title_safe}{ext}"

    full_path = os.path.join(base_dir, artist_safe, album_safe, filename)
    if len(full_path.encode('utf-8')) > 4000:
        album_safe = truncate_utf8(album, 120)
        artist_safe = truncate_utf8(artist, 120)
        full_path = os.path.join(base_dir, artist_safe, album_safe, filename)
    
    return artist_safe, album_safe, filename

def _download_cover(url):
    """ ดาวน์โหลดข้อมูลรูปภาพปกเพลงจาก URL """
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        logger.warning('Failed to download cover art from %s: %s', url, e)
    return None

def embed_artwork(file_path, image_bytes):
    """ ฝังรูปปกอัลบั้มลงในไฟล์เสียงตามประเภทของฟอร์แมตไฟล์ """
    ext = os.path.splitext(file_path)[1].lower()
    try:
        if ext == '.mp3':
            try:
                audio = id3.ID3(file_path)
            except id3.ID3NoHeaderError:
                audio = id3.ID3()
            audio.add(id3.APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=image_bytes))
            audio.save(file_path)
            return True
        elif ext in ('.m4a', '.mp4', '.m4'):
            from mutagen.mp4 import MP4, MP4Cover
            audio = MP4(file_path)
            audio['covr'] = [MP4Cover(image_bytes, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            return True
        elif ext == '.flac':
            from mutagen.flac import FLAC, Picture
            audio = FLAC(file_path)
            pic = Picture()
            pic.data = image_bytes
            pic.type = 3
            pic.mime = 'image/jpeg'
            pic.desc = 'Cover'
            audio.add_picture(pic)
            audio.save()
            return True
        elif ext == '.wav':
            audio = WAVE(file_path)
            if audio.tags is None: audio.add_tags()
            audio.tags.add(id3.APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=image_bytes))
            audio.save()
            return True
        elif ext == '.ogg':
            try:
                from mutagen.oggvorbis import OggVorbis
                from mutagen.flac import Picture
                import base64
                pic = Picture()
                pic.data = image_bytes
                pic.type = 3
                pic.mime = 'image/jpeg'
                pic.desc = 'Cover'
                encoded = base64.b64encode(pic.write()).decode('ascii')
                audio = OggVorbis(file_path)
                audio['metadata_block_picture'] = [encoded]
                audio.save()
                return True
            except Exception as e:
                logger.warning('OGG artwork embedding failed for %s: %s', os.path.basename(file_path), e)
        elif ext in ('.wma', '.asf'):
            try:
                from mutagen.asf import ASF, Picture as ASF_Picture
                audio = ASF(file_path)
                pic = ASF_Picture()
                pic.data = image_bytes
                pic.mime_type = 'image/jpeg'
                pic.type = 3
                # Append picture to WM/Picture field (mutagen handles encoding)
                pics = audio.tags.get('WM/Picture', []) if audio.tags is not None else []
                pics.append(pic)
                if audio.tags is None:
                    audio.tags = {}
                audio.tags['WM/Picture'] = pics
                audio.save()
                return True
            except Exception as e:
                logger.warning('WMA/ASF artwork embedding failed for %s: %s', os.path.basename(file_path), e)
    except Exception as e:
        logger.warning('Artwork embedding failed for %s: %s', os.path.basename(file_path), e)
    return False

def normalize_name_for_matching(name):
    """
    Normalize ชื่อโดยตัดช่องว่างทั้งหมดและ lowercase
    เพื่อให้ Modern Dog กับ ModernDog ถือเป็นชื่อเดียวกัน
    """
    if not name:
        return ""
    return re.sub(r'\s+', '', str(name)).strip().lower()


def find_existing_folder_case_insensitive(parent_dir, folder_name):
    """
    ค้นหา Folder artist/album ที่มีชื่อเดียวกัน 
    โดยไม่สนใจตัวพิมพ์ใหญ่-เล็กและช่องว่าง (SMB Compatible - Case-Insensitive)
    """
    try:
        if not os.path.exists(parent_dir):
            return None
        
        name_expected = normalize_name_for_matching(folder_name)
        for folder in os.listdir(parent_dir):
            folder_path = os.path.join(parent_dir, folder)
            if os.path.isdir(folder_path):
                if normalize_name_for_matching(folder) == name_expected:
                    return folder_path
    except Exception:
        pass
    return None

def find_existing_file_case_insensitive(target_dir, title_safe):
    """
    ค้นหาไฟล์ที่มีชื่อเดียวกัน (โดยไม่สนใจนามสกุล) ในแบบ Case-Insensitive 
    สำหรับการเข้ากันได้กับ SMB Protocol
    """
    try:
        if not os.path.exists(target_dir):
            return None
        
        expected_base = normalize_name_for_matching(title_safe)
        for file in os.listdir(target_dir):
            file_base = os.path.splitext(file)[0]
            if normalize_name_for_matching(file_base) == expected_base:
                full_path = os.path.join(target_dir, file)
                if os.path.isfile(full_path):
                    return full_path
    except Exception:
        pass
    return None

def move_with_dedup(source_file, artist, album, title, target_base, cover_bytes=None):
    """ ย้ายไฟล์ไปยัง Folder Artist/Album พร้อมเช็คไฟล์ซ้ำที่คุณภาพต่ำกว่า 
    (SMB Compatible - Case-Insensitive สำหรับ Folder & File) """
    ext = os.path.splitext(source_file)[1].lower()
    artist_folder, album_folder, file_name = safe_truncate_path(artist, album, title, ext, target_base)

    # ค้นหา artist folder แบบ case-insensitive
    artist_path = find_existing_folder_case_insensitive(target_base, artist_folder)
    if not artist_path:
        artist_path = os.path.join(target_base, artist_folder)
    
    # ค้นหา album folder แบบ case-insensitive
    album_path = find_existing_folder_case_insensitive(artist_path, album_folder)
    if not album_path:
        album_path = os.path.join(artist_path, album_folder)
    
    target_dir = album_path
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, file_name)
    
    # ค้นหาไฟล์ที่มีชื่อเดียวกัน (ไม่สนใจนามสกุล)
    file_base = os.path.splitext(file_name)[0]
    existing_file = find_existing_file_case_insensitive(target_dir, file_base)

    if existing_file:
        logger.info('Comparing quality for duplicate: %s', file_name)
        curr_bitrate, curr_size = get_audio_quality(existing_file)
        new_bitrate, new_size = get_audio_quality(source_file)

        if (new_bitrate > curr_bitrate) or (new_bitrate == curr_bitrate and new_size > curr_size):
            existing_filename = os.path.basename(existing_file)
            logger.info('Replacing existing file %s with higher quality %s (%dk > %dk)', existing_filename, os.path.basename(source_file), new_bitrate//1000, curr_bitrate//1000)
            try:
                os.remove(existing_file)
            except Exception as exc:
                logger.warning('Unable to remove existing file %s: %s', existing_file, exc)
            shutil.move(source_file, target_path)
        else:
            logger.info('Lower quality already exists. Removing incoming file: %s', source_file)
            try:
                os.remove(source_file)
            except Exception as exc:
                logger.warning('Unable to remove lower quality source file %s: %s', source_file, exc)
    else:
        logger.info('Moving file: %s -> %s', source_file, target_path)
        shutil.move(source_file, target_path)
    
    if cover_bytes:
        cover_path = os.path.join(target_dir, 'cover.jpg')
        if not os.path.exists(cover_path):
            try:
                with open(cover_path, 'wb') as f:
                    f.write(cover_bytes)
                logger.info('Saved album art cover.jpg in: %s', target_dir)
            except Exception as e:
                logger.warning('Failed to save cover.jpg in %s: %s', target_dir, e)
    return target_path

def safe_write_tags(file_path, artist, album, title, genre=None, date=None):
    """ เขียน Tag (UTF-8) ลงในไฟล์เสียงอย่างปลอดภัย รองรับหลายนามสกุล """
    artist = sanitize_tag_value(artist)
    album = sanitize_tag_value(album)
    title = sanitize_tag_value(title)
    genre = sanitize_tag_value(genre)
    date = sanitize_tag_value(date)
    ext = os.path.splitext(file_path)[1].lower()
    
    try:
        try:
            audio_clean = File(file_path, easy=True)
            if audio_clean is not None:
                audio_clean.delete()
                audio_clean.save()
        except Exception as e:
            logger.warning('Could not delete old tags for %s: %s', os.path.basename(file_path), e)
            
        audio = File(file_path, easy=True)
        if audio is not None:
            audio['artist'] = artist
            audio['title'] = title
            audio['album'] = album
            if genre and is_valid_tag(genre): audio['genre'] = genre
            if date and is_valid_tag(date): audio['date'] = date
            audio.save()
            return True
    except Exception as e:
        logger.warning('Easy tagging failed for %s, trying specific format: %s', os.path.basename(file_path), e)

    try:
        if ext == '.mp3':
            try:
                tags = id3.ID3(file_path)
            except id3.ID3NoHeaderError:
                tags = id3.ID3()
            tags['TPE1'] = id3.TPE1(encoding=3, text=artist)
            tags['TIT2'] = id3.TIT2(encoding=3, text=title)
            tags['TALB'] = id3.TALB(encoding=3, text=album)
            if genre and is_valid_tag(genre): tags['TCON'] = id3.TCON(encoding=3, text=genre)
            if date and is_valid_tag(date): tags['TDRC'] = id3.TDRC(encoding=3, text=date)
            tags.save(file_path)
            return True
        elif ext in ('.m4a', '.mp4', '.m4'):
            audio = MP4(file_path)
            audio['\xa9ART'] = [artist]
            audio['\xa9nam'] = [title]
            audio['\xa9alb'] = [album]
            if genre and is_valid_tag(genre): audio['\xa9gen'] = [genre]
            if date and is_valid_tag(date): audio['\xa9day'] = [date]
            audio.save()
            return True
        elif ext == '.ogg':
            try:
                from mutagen.oggvorbis import OggVorbis
                audio = OggVorbis(file_path)
                audio['artist'] = [artist]
                audio['title'] = [title]
                audio['album'] = [album]
                if genre and is_valid_tag(genre): audio['genre'] = [genre]
                if date and is_valid_tag(date): audio['date'] = [date]
                audio.save()
                return True
            except Exception:
                pass
        elif ext == '.wav':
            try:
                audio = WAVE(file_path)
                if audio.tags is None: audio.add_tags()
                tags = audio.tags
            except Exception:
                tags = id3.ID3(file_path)
            tags['TPE1'] = id3.TPE1(encoding=3, text=artist)
            tags['TIT2'] = id3.TIT2(encoding=3, text=title)
            tags['TALB'] = id3.TALB(encoding=3, text=album)
            if genre and is_valid_tag(genre): tags['TCON'] = id3.TCON(encoding=3, text=genre)
            if date and is_valid_tag(date): tags['TDRC'] = id3.TDRC(encoding=3, text=date)
            tags.save(file_path)
            return True
        else:
            audio = File(file_path)
            if audio is not None:
                audio['artist'] = [artist]
                audio['title'] = [title]
                audio['album'] = [album]
                if genre and is_valid_tag(genre): audio['genre'] = [genre]
                if date and is_valid_tag(date): audio['date'] = [date]
                audio.save()
                return True
            else:
                raise ValueError("Unsupported format")
    except Exception as e:
        logger.warning('Failed all tagging fallback methods for %s: %s', os.path.basename(file_path), e)
        return False

async def call_shazam_with_retries(shazam_client, path, limiter):
    backoff = SHAZAM_DELAY
    for attempt in range(1, SHAZAM_RETRIES + 1):
        try:
            if attempt > 1:
                await asyncio.sleep(backoff)
            await limiter.wait()
            return await asyncio.wait_for(shazam_client.recognize(path), timeout=SHAZAM_TIMEOUT)
        except asyncio.TimeoutError as e:
            errstr = 'timeout'
            logger.warning('Shazam API timeout (attempt %s/%s): %s', attempt, SHAZAM_RETRIES, e)
            is_retryable = attempt < SHAZAM_RETRIES
        except Exception as e:
            errstr = str(e).lower()
            # Retryable errors: rate limit, timeout, JSON decode errors, connection errors
            is_retryable = any(keyword in errstr for keyword in [
                '429', 'too many', 'rate', 'timeout', 
                'failed to decode', 'json', 'connection',
                'reset by peer', 'broken pipe'
            ])
            
            if is_retryable and attempt < SHAZAM_RETRIES:
                logger.warning('Shazam API temporary error (attempt %s/%s): %s - backing off %ss', 
                              attempt, SHAZAM_RETRIES, e, backoff)
                backoff *= 2
                continue
            else:
                logger.warning('Shazam API error (attempt %s/%s): %s', attempt, SHAZAM_RETRIES, e)
                if attempt == SHAZAM_RETRIES:
                    logger.warning('Max retries reached for: %s', os.path.basename(path))
                return {}
    return {}

async def process_file(file_path, shazam, limiter, semaphore):
    filename_only = os.path.basename(file_path)
    async with semaphore:
        if not os.path.exists(file_path): return

        base, ext = os.path.splitext(file_path)
        locked_path = f"{base}.processing{ext}"
        try:
            os.rename(file_path, locked_path)
            logger.info('Locked & Claimed: %s -> %s', filename_only, os.path.basename(locked_path))
        except Exception as exc:
            logger.warning('Unable to claim %s: %s', file_path, exc)
            return

        try:
            logger.info('Analyzing structure for: %s', os.path.basename(locked_path))
            try:
                test_audio = File(locked_path)
                if test_audio is None: raise ValueError('Invalid structure')
            except Exception:
                logger.warning('Corrupted Header: %s -> Moving to Unmanage', filename_only)
                target_un = os.path.join(UNMANAGE_DIR, filename_only)
                os.makedirs(UNMANAGE_DIR, exist_ok=True)
                shutil.move(locked_path, target_un)
                return

            logger.info('Querying Shazam API for: %s', os.path.basename(locked_path))
            out = await call_shazam_with_retries(shazam, locked_path, limiter)
            artist, title, album, genre, date = None, None, None, None, None
            cover_bytes = None
            target_dir = TAG_DIR

            if 'track' in out:
                track = out['track']
                artist = track.get('subtitle')
                title = track.get('title')
                album = title
                
                genres = track.get('genres', {})
                if isinstance(genres, dict): genre = genres.get('primary')
                
                if 'sections' in track:
                    for s in track['sections']:
                        if s.get('type') == 'SONG':
                            for m in s.get('metadata', []):
                                m_title = m.get('title')
                                m_text = m.get('text')
                                if m_title == 'Album': album = m_text
                                elif m_title in ('Released', 'Released Date'): date = m_text
                if date:
                    match_year = re.search(r'\b(19\d\d|20\d\d)\b', str(date))
                    if match_year: date = match_year.group(1)
                        
                logger.info('Shazam Match: %s - %s', artist, title)
                cover_url = track.get('images', {}).get('coverarthq')
                if cover_url:
                    cover_bytes = await asyncio.to_thread(_download_cover, cover_url)

            # 2. กรณี Shazam ไม่เจอ -> ตรวจสอบและ "ซ่อมภาษาไทย" จาก Tag เดิมในไฟล์
            else:
                logger.info('Shazam did not match %s. Falling back to internal tags', os.path.basename(locked_path))
                try:
                    audio_orig = File(locked_path, easy=True)
                    if audio_orig:
                        raw_artist = audio_orig.get('artist', [''])[0]
                        raw_title = audio_orig.get('title', [''])[0]
                        
                        # รันกระบวนการซ่อมรหัสภาษาขั้นสูง (Advanced Mojibake Fix)
                        fixed_artist = repair_thai_encoding(raw_artist)
                        fixed_title = repair_thai_encoding(raw_title)

                        if is_valid_tag(fixed_artist) and is_valid_tag(fixed_title):
                            artist = fixed_artist
                            title = fixed_title
                            album = repair_thai_encoding(audio_orig.get('album', [title])[0])
                            genre = repair_thai_encoding(audio_orig.get('genre', [''])[0])
                            date = repair_thai_encoding(audio_orig.get('date', [''])[0])
                            target_dir = os.path.join(TAG_DIR, '_tag')
                            logger.info('File Tag RECOVERED successfully: %s - %s', artist, title)
                        else:
                            logger.warning('Tag unreadable or missing after repair attempt: %s', filename_only)
                except Exception as tag_err:
                    logger.warning('Error reading internal tags: %s', tag_err)

            # 3. จัดการย้ายและบันทึกผล
            if artist and title and is_valid_tag(artist) and is_valid_tag(title):
                # ตรวจสอบชื่อ Album เผื่อกรณีเป็นค่าว่างหรือพัง
                final_album = album if is_valid_tag(album) else title
                artist = sanitize_tag_value(artist)
                title = sanitize_tag_value(title)
                final_album = sanitize_tag_value(final_album)
                genre = sanitize_tag_value(genre)
                date = sanitize_tag_value(date)

                logger.info('Writing clean tags to: %s', os.path.basename(locked_path))
                success = safe_write_tags(locked_path, artist, final_album, title, genre, date)
                
                if cover_bytes:
                    embed_artwork(locked_path, cover_bytes)

                logger.info('Success Processing: %s - %s', artist, title)
                move_with_dedup(locked_path, artist, final_album, title, target_dir, cover_bytes)
            else:
                # ถ้าซ่อมไม่สำเร็จ หรือไม่มีข้อมูลจริง ๆ ส่งไป Unmanage
                logger.warning('Unmanageable (No tag or recovery failed): %s -> Moving to Unmanage', filename_only)
                target_un = os.path.join(UNMANAGE_DIR, filename_only)
                os.makedirs(UNMANAGE_DIR, exist_ok=True)
                shutil.move(locked_path, target_un)

        except Exception as e:
            logger.error('Error processing %s: %s', filename_only, e)
            if os.path.exists(locked_path):
                try:
                    os.rename(locked_path, file_path)
                except Exception as exc:
                    logger.warning('Unable to restore in-progress file %s: %s', locked_path, exc)

async def tag_music():
    shazam = Shazam()
    limiter = AsyncRateLimiter(SHAZAM_DELAY)
    semaphore = asyncio.Semaphore(MAX_WORKERS)
    logger.info('Starting internal HTTP health endpoint on port %s', HTTP_PORT)
    health_runner = await start_health_server()
    logger.info('Service Running (Advanced Thai Auto-Repair Mode)')

    try:
        while not shutdown_event.is_set():
            try:
                all_files = []
                for root, dirs, files in os.walk(WATCH_DIR):
                    for file in files:
                                if file.lower().endswith(('.mp3', '.m4a', '.m4', '.flac', '.wav', '.aif', '.aiff', '.dsf', '.ogg', '.wma')) and '.processing.' not in file.lower():
                                    all_files.append(os.path.join(root, file))

                if not all_files:
                    await asyncio.wait_for(shutdown_event.wait(), timeout=60)
                    continue

                logger.info('Found %d file(s) to process...', len(all_files))
                random.shuffle(all_files)
                tasks = [process_file(file_path, shazam, limiter, semaphore) for file_path in all_files]
                await asyncio.gather(*tasks)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error('Global loop error: %s', e)

            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=INTERVAL)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
    finally:
        await stop_health_server(health_runner)
        logger.info('Shutdown complete')

if __name__ == "__main__":
    setup_logging()
    for d in [WATCH_DIR, TAG_DIR, UNMANAGE_DIR]:
        os.makedirs(d, exist_ok=True)
    loop = asyncio.get_event_loop()
    configure_signal_handlers(loop)
    try:
        loop.run_until_complete(tag_music())
    except KeyboardInterrupt:
        logger.info('Keyboard interrupt received, shutting down...')
    finally:
        loop.close()