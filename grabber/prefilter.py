"""Whitelist keyword gate applied to RSS items before the LLM classifier.

Deliberately wide: a false positive just costs one LLM call, a false negative
loses a post candidate forever. Substring matching covers both English stems
(encod -> encoding/encoder) and Russian stems (кодек -> кодеков/кодеки).
Telegram sources bypass this filter entirely (hand-curated niche channels).
"""

KEYWORDS: tuple[str, ...] = (
    # video/image codecs & formats
    "av1", "av2", "avc", "hevc", "h264", "h.264", "h265", "h.265", "h266", "h.266",
    "vvc", "vp8", "vp9", "evc", "lcevc", "codec", "dav1d", "libaom", "aomedia",
    "svt-av1", "rav1e", "x264", "x265", "vvenc", "mp4", "mkv", "webm", "mov",
    "jpeg", "jpg", "jxl", "webp", "avif", "heif", "heic", "png", "gif", "lottie",
    "dolby", "atmos", "prores", "image", "изображен",
    # audio
    "aac", "opus", "mp3", "flac", "vorbis", "xhe", "audio", "аудио", "звук",
    "voice", "голос", "speech",
    # streaming protocols & delivery
    "hls", "dash", "cmaf", "moq", "quic", "webrtc", "rtmp", "rtsp", "rtp", "rtcp",
    "srt", "whip", "whep", "sip", "cdn", "abr", "cmcd", "cmsd", "scte", "ott",
    "vod", "iptv", "manifest", "playlist", "плейлист", "сегмент", "segment",
    "low latency", "low-latency", "steering", "multicast", "unicast", "ingest",
    "sdp", "stun", "turn server", "sfu", "simulcast", "svc", "jitter", "fec",
    "packet loss", "bandwidth", "битрейт", "bitrate", "задержк", "latency",
    # browser / web platform media
    "mse", "eme", "webcodecs", "webtransport", "webgpu", "wasm", "webassembly",
    "media source", "mediasource", "picture-in-picture", "picture in picture",
    "chrome", "chromium", "safari", "webkit", "firefox", "браузер", "browser",
    # DRM & security
    "drm", "widevine", "playready", "fairplay", "encryption key", "piracy",
    "пиратс", "защита контента",
    # tools & players
    "ffmpeg", "libav", "gstreamer", "obs", "shaka", "exoplayer", "avplayer",
    "media3", "hls.js", "dash.js", "video.js", "videojs", "rx-player", "gpac",
    "mp4box", "vlc", "videolan", "mpv", "demux", "remux", "muxer", "muxing",
    "player", "плеер", "playback", "воспроизведен", "encod", "decod", "transcod",
    "кодир", "декодир", "транскод",
    # quality / QoE / perception
    "vmaf", "psnr", "ssim", "qoe", "per-title", "качеств", "quality", "hdr",
    "sdr", "10-bit", "4k", "8k", "uhd", "fps", "frame rate", "framerate",
    "frame", "кадр", "resolution", "разрешен", "upscal", "downscal", "апскейл",
    "перцепт", "восприят", "зрен", "пиксел", "pixel", "дисплей", "display",
    "экран", "screen", "монитор", "герц", "hz",
    # platforms & companies
    "netflix", "youtube", "twitch", "vimeo", "hulu", "disney", "dazn", "roku",
    "mux", "bitmovin", "wowza", "theo", "kaltura", "brightcove", "akamai",
    "fastly", "cloudfront", "elemental", "ateme", "harmonic", "v-nova",
    "кинопоиск", "rutube", "рутуб", "okko", "иви", "ivi.ru", "wink", "premier",
    "kion", "kinescope", "flussonic", "видеоплатформ",
    # events & community
    "demuxed", "videotech", "видеотех", "nab ", "nab show", "ibc", "fosdem",
    "mile high video", "streaming media", "rtc.on", "rtc@scale", "wwdc",
    "meetup", "митап", "конференц", "conference", "вебинар", "webinar",
    "доклад", "подкаст", "podcast", "call for papers", "cfp", "hackathon",
    "хакатон",
    # generic video/streaming words (RU + EN) — the wide net
    "video", "видео", "stream", "стрим", "трансля", "broadcast", "вещан",
    "эфир", "телевиден", "телеканал", "кинотеатр", "мультимедиа", "multimedia",
    "media", "медиа", "субтитр", "subtitle", "caption", "буфер", "buffer",
    "камер", "camera", "монтаж", "рендер", "render", "звонк", "call quality",
    "видеосвязь", "сжат", "compress", "смотрен", "просмотр", "зрител", "viewer",
    "watch", "live",
)


def match(title: str, text: str) -> str | None:
    """Return the first matching keyword, or None if the item has no video signal."""
    haystack = f"{title}\n{text}".lower()
    for kw in KEYWORDS:
        if kw in haystack:
            return kw
    return None
