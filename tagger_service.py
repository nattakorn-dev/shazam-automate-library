import asyncio
import os
import shutil
import re
import logging
import time
import random
import requests
from aiohttp import web
from datetime import datetime
from shazamio import Shazam
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.easyid3 import EasyID3
import mutagen.id3 as id3
from mutagen.wave import WAVE
from mutagen.mp4 import MP4

# Rate limit settings (seconds between Shazam API calls)
SHAZAM_DELAY = float(os.getenv('SHAZAM_DELAY', '1.5'))
SHAZAM_RETRIES = int(os.getenv('SHAZAM_RETRIES', '3'))

# ปิด Log ของ Mutagen เพื่อความสะอาดของหน้าจอ
logging.getLogger("mutagen").setLevel(logging.ERROR)

# --- Configuration ---
WATCH_DIR = "/music/watch"
TAG_DIR = "/music/library"
UNMANAGE_DIR = "/music/unmanage"
INTERVAL = 300

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
    return web.json_response({"status": "ok", "service": "shazam-tagger", "time": datetime.utcnow().isoformat() + "Z"})

async def start_health_server():
    app = web.Application()
    app.add_routes([web.get('/health', health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', 5000)
    await site.start()
    print('✅ Health endpoint listening on http://0.0.0.0:5000/health')

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
    clean = re.sub(r'[\\/*?:"<>|]', "", str(text))
    return clean.strip()

def safe_truncate_path(artist, album, title, ext, base_dir="/music/tag", max_filename=200):
    """ Truncate artist/album/title to keep full path under filesystem limits. """
    artist_safe = clean_filename(artist)[:100]
    album_safe = clean_filename(album)[:100]
    title_safe = clean_filename(title)[:max_filename]
    
    filename = f"{title_safe}{ext}"
    if len(filename.encode('utf-8')) > 250:
        title_safe = clean_filename(title)[:150]
        filename = f"{title_safe}{ext}"
        if len(filename.encode('utf-8')) > 250:
            title_safe = clean_filename(title)[:50]
            filename = f"{title_safe}{ext}"
    
    full_path = os.path.join(base_dir, artist_safe, album_safe, filename)
    if len(full_path.encode('utf-8')) > 4000:
        album_safe = clean_filename(album)[:50]
        artist_safe = clean_filename(artist)[:50]
        full_path = os.path.join(base_dir, artist_safe, album_safe, filename)
    
    return artist_safe, album_safe, filename

def _download_cover(url):
    """ ดาวน์โหลดข้อมูลรูปภาพปกเพลงจาก URL """
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print(f"⚠️ Failed to download cover art from {url}: {e}")
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
        elif ext in ('.m4a', '.mp4'):
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
    except Exception as e:
        print(f"⚠️ Artwork embedding failed for {os.path.basename(file_path)}: {e}")
    return False

def move_with_dedup(source_file, artist, album, title, target_base, cover_bytes=None):
    """ ย้ายไฟล์ไปยัง Folder Artist/Album พร้อมเช็คไฟล์ซ้ำที่คุณภาพต่ำกว่า """
    ext = os.path.splitext(source_file)[1].lower()
    artist_folder, album_folder, file_name = safe_truncate_path(artist, album, title, ext, target_base)

    target_dir = os.path.join(target_base, artist_folder, album_folder)
    os.makedirs(target_dir, exist_ok=True)
    target_path = os.path.join(target_dir, file_name)

    if os.path.exists(target_path):
        print(f"🔍 Comparing quality for duplicate: {file_name}...")
        curr_bitrate, curr_size = get_audio_quality(target_path)
        new_bitrate, new_size = get_audio_quality(source_file)

        if (new_bitrate > curr_bitrate) or (new_bitrate == curr_bitrate and new_size > curr_size):
            print(f"♻️ Replacing: {file_name} ({new_bitrate//1000}k > {curr_bitrate//1000}k)")
            os.remove(target_path)
            shutil.move(source_file, target_path)
        else:
            print(f"⏭️ Lower quality already exists. Skipping: {file_name}")
            os.remove(source_file) 
    else:
        print(f"🚚 Moving file: {source_file} -> {target_path}")
        shutil.move(source_file, target_path)
    
    if cover_bytes:
        cover_path = os.path.join(target_dir, "cover.jpg")
        if not os.path.exists(cover_path):
            try:
                with open(cover_path, 'wb') as f: f.write(cover_bytes)
                print(f"🎨 Saved album art cover.jpg in: {target_dir}")
            except Exception as e:
                print(f"⚠️ Failed to save cover.jpg in {target_dir}: {e}")
    return target_path

def safe_write_tags(file_path, artist, album, title, genre=None, date=None):
    """ เขียน Tag (UTF-8) ลงในไฟล์เสียงอย่างปลอดภัย รองรับหลายนามสกุล """
    ext = os.path.splitext(file_path)[1].lower()
    
    try:
        try:
            audio_clean = File(file_path, easy=True)
            if audio_clean is not None:
                audio_clean.delete()
                audio_clean.save()
        except Exception as e:
            print(f"⚠️ Could not delete old tags for {os.path.basename(file_path)}: {e}")
            
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
        print(f"⚠️ Easy tagging failed for {os.path.basename(file_path)}, trying specific format: {e}")

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
        elif ext in ('.m4a', '.mp4'):
            audio = MP4(file_path)
            audio['\xa9ART'] = [artist]
            audio['\xa9nam'] = [title]
            audio['\xa9alb'] = [album]
            if genre and is_valid_tag(genre): audio['\xa9gen'] = [genre]
            if date and is_valid_tag(date): audio['\xa9day'] = [date]
            audio.save()
            return True
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
        print(f"❌ Failed all tagging fallback methods for {os.path.basename(file_path)}: {e}")
        return False

async def call_shazam_with_retries(shazam_client, path, limiter):
    backoff = SHAZAM_DELAY
    for attempt in range(1, SHAZAM_RETRIES + 1):
        try:
            if attempt > 1: await asyncio.sleep(backoff)
            await limiter.wait()
            return await shazam_client.recognize(path)
        except Exception as e:
            errstr = str(e).lower()
            if '429' in errstr or 'too many' in errstr or 'rate' in errstr or 'timeout' in errstr:
                print(f"⚠️ Shazam API temporary error (attempt {attempt}): {e} - backing off {backoff}s")
                backoff *= 2
                continue
            else:
                print(f"⚠️ Shazam API error: {e}")
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
            print(f"🔒 Locked & Claimed: {filename_only} -> {os.path.basename(locked_path)}")
        except Exception:
            return

        try:
            print(f"📁 Analyzing structure for: {os.path.basename(locked_path)}")
            try:
                test_audio = File(locked_path)
                if test_audio is None: raise ValueError("Invalid structure")
            except Exception:
                print(f"❌ Corrupted Header: {filename_only} -> Moving to Unmanage")
                target_un = os.path.join(UNMANAGE_DIR, filename_only)
                os.makedirs(UNMANAGE_DIR, exist_ok=True)
                shutil.move(locked_path, target_un)
                return

            print(f"🎵 Querying Shazam API for: {os.path.basename(locked_path)}...")
            out = await call_shazam_with_retries(shazam, locked_path, limiter)
            artist, title, album, genre, date = None, None, None, None, None
            cover_bytes = None

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
                        
                print(f"🔍 Shazam Match: {artist} - {title}")
                cover_url = track.get('images', {}).get('coverarthq')
                if cover_url:
                    cover_bytes = await asyncio.to_thread(_download_cover, cover_url)

            # 2. กรณี Shazam ไม่เจอ -> ตรวจสอบและ "ซ่อมภาษาไทย" จาก Tag เดิมในไฟล์
            else:
                print(f"❓ Shazam did not match {os.path.basename(locked_path)}. Falling back to internal tags...")
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
                            print(f"🛠️ File Tag RECOVERED successfully: {artist} - {title}")
                        else:
                            print(f"⚠️ Tag Unreadable/Missing even after repair attempt: {filename_only}")
                except Exception as tag_err:
                    print(f"⚠️ Error reading internal tags: {tag_err}")

            # 3. จัดการย้ายและบันทึกผล
            if artist and title and is_valid_tag(artist) and is_valid_tag(title):
                # ตรวจสอบชื่อ Album เผื่อกรณีเป็นค่าว่างหรือพัง
                final_album = album if is_valid_tag(album) else title
                
                print(f"✍️ Writing clean tags to: {os.path.basename(locked_path)}")
                success = safe_write_tags(locked_path, artist, final_album, title, genre, date)
                
                if cover_bytes: embed_artwork(locked_path, cover_bytes)

                print(f"✨ Success Processing: {artist} - {title}")
                move_with_dedup(locked_path, artist, final_album, title, TAG_DIR, cover_bytes)
            else:
                # ถ้าซ่อมไม่สำเร็จ หรือไม่มีข้อมูลจริง ๆ ส่งไป Unmanage
                print(f"❓ Unmanageable (No tag or recovery failed): {filename_only} -> Moving to Unmanage")
                target_un = os.path.join(UNMANAGE_DIR, filename_only)
                os.makedirs(UNMANAGE_DIR, exist_ok=True)
                shutil.move(locked_path, target_un)

        except Exception as e:
            print(f"⚠️ Error processing {filename_only}: {e}")
            if os.path.exists(locked_path):
                try: os.rename(locked_path, file_path)
                except Exception: pass

async def tag_music():
    shazam = Shazam()
    limiter = AsyncRateLimiter(SHAZAM_DELAY)
    semaphore = asyncio.Semaphore(3)
    print(f"--- Starting internal HTTP health endpoint on port 5000 ---")
    asyncio.create_task(start_health_server())
    print(f"--- [{datetime.now().strftime('%H:%M:%S')}] Service Running (Advanced Thai Auto-Repair Mode) ---")

    while True:
        try:
            all_files = []
            for root, dirs, files in os.walk(WATCH_DIR):
                for file in files:
                    if file.lower().endswith(('.mp3', '.m4a', '.flac', '.wav')) and '.processing.' not in file.lower():
                        all_files.append(os.path.join(root, file))

            if not all_files:
                await asyncio.sleep(60)
                continue

            print(f"📂 Found {len(all_files)} file(s) to process...")
            random.shuffle(all_files)
            
            tasks = [process_file(file_path, shazam, limiter, semaphore) for file_path in all_files]
            await asyncio.gather(*tasks)

        except Exception as e:
            print(f"⚠️ Global Loop Error: {e}")
        
        await asyncio.sleep(INTERVAL)

if __name__ == "__main__":
    for d in [WATCH_DIR, TAG_DIR, UNMANAGE_DIR]: os.makedirs(d, exist_ok=True)
    asyncio.run(tag_music())