"""Limits and defaults taken from Meta's Reels publishing docs."""

DEFAULT_GRAPH_VERSION = "v26.0"
REDIRECT_HOST = "127.0.0.1"
REDIRECT_PORT = 53682
REDIRECT_PATH = "/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"

# Facebook Login for Business. Every scope must be added on the app and accepted
# at login, or Meta grants none of them.
DEFAULT_SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "pages_manage_engagement",
    "pages_read_user_content",
    "instagram_basic",
    "instagram_content_publish",
    "business_management",
)

IG_MAX_BYTES = 300 * 1024 * 1024
IG_MIN_SECONDS = 3
IG_MAX_SECONDS = 15 * 60
FB_MIN_SECONDS = 3
FB_MAX_SECONDS = 90
FB_MIN_SHORT_SIDE = 540
FB_MIN_LONG_SIDE = 960
IG_COVER_MAX_BYTES = 8 * 1024 * 1024
FB_COVER_MAX_BYTES = 10 * 1024 * 1024
CAPTION_LIMIT = 2200
MAX_HASHTAGS = 30
MAX_COLLABORATORS = 3
TITLE_LIMIT = 255
SCHEDULE_MIN = 10 * 60
SCHEDULE_MAX_DAYS = 182

# YouTube Shorts are vertical or square videos up to 3 minutes, uploaded with the Data API.
YT_REDIRECT_PORT = 53683
YT_REDIRECT_URI = f"http://localhost:{YT_REDIRECT_PORT}/callback"
YT_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YT_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YT_SCOPES = (YT_UPLOAD_SCOPE, YT_READONLY_SCOPE)
YT_MIN_SECONDS = 1
YT_MAX_SECONDS = 3 * 60
YT_TITLE_LIMIT = 100
YT_DESCRIPTION_BYTES = 5000
YT_TAGS_LIMIT = 500
YT_COVER_MAX_BYTES = 2 * 1024 * 1024
YT_CATEGORY_ID = "22"
