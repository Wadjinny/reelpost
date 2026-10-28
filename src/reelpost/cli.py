"""Command line for publishing Instagram, Facebook, and YouTube Reels."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from reelpost.accounts import choose_page, enrich_instagram, list_pages, public_page, warn_tasks
from reelpost.auth import (
    build_login_url,
    exchange_user_token,
    open_browser,
    parse_oauth_redirect,
    wait_for_code,
)
from reelpost.caption import compose_caption, parse_tags
from reelpost.config import (
    clear_youtube_auth,
    delete_config,
    load_dotenv,
    load_settings,
    save_settings,
    update_saved_page,
    update_youtube_auth,
)
from reelpost.config import Settings
from reelpost.constants import (
    DEFAULT_SCOPES,
    FB_COVER_MAX_BYTES,
    MAX_COLLABORATORS,
    REDIRECT_URI,
    TITLE_LIMIT,
    YT_COVER_MAX_BYTES,
    YT_REDIRECT_PORT,
    YT_REDIRECT_URI,
)
from reelpost.cover_host import public_cover
from reelpost.errors import ReelpostError
from reelpost.facebook import publish_facebook, upload_thumbnail
from reelpost.graph import GraphClient, normalize_version
from reelpost.instagram import publish_instagram, read_quota
from reelpost.media import (
    CoverAsset,
    cover_from_url,
    cover_limit,
    cover_warning,
    format_bytes,
    format_seconds,
    inspect_video,
    limits_for,
    prepare_cover,
)
from reelpost.youtube import (
    access_is_fresh,
    build_youtube_login_url,
    exchange_youtube_code,
    fetch_channel,
    publish_youtube,
    refresh_youtube_token,
    set_youtube_thumbnail,
    token_expiry,
    youtube_description,
    youtube_tags,
    youtube_title,
)
from reelpost.schedule import parse_schedule

EPILOG = """examples:
  reelpost auth login
  reelpost auth youtube
  reelpost accounts
  reelpost publish clip.mp4 --description "Morning on the canal" --tags berlin,travel --thumbnail cover.jpg --yes
  reelpost publish clip.mp4 --to youtube --privacy unlisted --thumbnail cover.jpg --yes
  reelpost publish clip.mp4 --to facebook,youtube --schedule 2026-10-29T09:00+01:00 --thumbnail cover.jpg --yes
"""


@dataclass
class Job:
    video_path: Path
    targets: list[str]
    caption: str
    tags: list[str]
    title: str | None
    privacy: str
    collaborators: list[str]
    place_id: str | None
    schedule_at: datetime | None
    share_to_feed: bool
    ai_generated: bool
    thumb_offset_ms: int | None
    thumbnail_url: str | None
    tunnel: bool
    cover: CoverAsset | None
    warnings: list[str]
    errors: list[str]
    info: object

    def close(self) -> None:
        if self.cover is not None:
            self.cover.close()
            self.cover = None


def console_main() -> None:
    sys.exit(main())


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ReelpostError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("cancelled", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--graph-version", help="Graph API version, for example v26.0")
    parser = argparse.ArgumentParser(
        prog="reelpost",
        description="Publish a Reel to Instagram, a Facebook Page, and YouTube Shorts, with one cover, description, and tags.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    commands = parser.add_subparsers(dest="command", required=True)

    auth = commands.add_parser("auth", help="Log in to Meta or YouTube")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_commands.add_parser("login", parents=[common], help="Sign in with Facebook and choose a Page")
    login.add_argument("--app-id", help="Meta app id. Defaults to REELPOST_APP_ID.")
    login.add_argument("--app-secret", help="Meta app secret. Prefer REELPOST_APP_SECRET.")
    login.add_argument("--config-id", help="Facebook Login for Business configuration id")
    login.add_argument("--page", help="Page id or exact Page name when you manage more than one")
    login.add_argument("--paste", action="store_true", help="Paste the redirect URL instead of listening on localhost")
    login.add_argument("--timeout", type=float, default=180, help="Seconds to wait for the browser login")
    login.set_defaults(func=cmd_login)

    youtube_login = auth_commands.add_parser("youtube", parents=[common], help="Sign in with Google and store a YouTube channel")
    youtube_login.add_argument("--client-id", help="Google OAuth client id. Defaults to REELPOST_YOUTUBE_CLIENT_ID.")
    youtube_login.add_argument("--client-secret", help="Google OAuth client secret. Prefer REELPOST_YOUTUBE_CLIENT_SECRET.")
    youtube_login.add_argument("--paste", action="store_true", help="Paste the redirect URL instead of listening on localhost")
    youtube_login.add_argument("--timeout", type=float, default=180, help="Seconds to wait for the browser login")
    youtube_login.set_defaults(func=cmd_auth_youtube)

    status = auth_commands.add_parser("status", parents=[common], help="Show the saved accounts without printing tokens")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    logout = auth_commands.add_parser("logout", help="Delete the saved login. This removes Meta and YouTube.")
    logout.add_argument("--youtube", action="store_true", help="Remove only the YouTube login")
    logout.set_defaults(func=cmd_logout)

    accounts = commands.add_parser("accounts", parents=[common], help="List Pages and linked Instagram accounts")
    accounts.add_argument("--page", help="Show one Page by id or exact name")
    accounts.add_argument("--json", action="store_true")
    accounts.set_defaults(func=cmd_accounts)

    check = commands.add_parser("check", parents=[common], help="Check a video and cover locally")
    _add_media_arguments(check)
    check.set_defaults(func=cmd_check)

    publish = commands.add_parser("publish", parents=[common], help="Upload the Reel")
    _add_media_arguments(publish)
    publish.add_argument("--yes", action="store_true", help="Publish without a confirmation prompt")
    publish.add_argument("--dry-run", action="store_true", help="Validate and print the plan, then stop")
    publish.add_argument("--timeout", type=float, default=600, help="Seconds to wait for each processing step")
    publish.set_defaults(func=cmd_publish)

    thumbnail = commands.add_parser("thumbnail", parents=[common], help="Set the cover on a Reel that is already uploaded")
    thumbnail.add_argument("video_id", help="Facebook video id, or an 11-character YouTube video id")
    thumbnail.add_argument("--to", choices=["facebook", "youtube"], default="facebook", help="Where the video was published. Default: facebook.")
    thumbnail.add_argument("--thumbnail", required=True, help="Local JPEG, PNG, or WebP cover")
    thumbnail.add_argument("--page", help="Page id or exact Page name, for Facebook")
    thumbnail.add_argument("--yes", action="store_true")
    thumbnail.set_defaults(func=cmd_thumbnail)
    return parser


def _add_media_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("video", type=Path, help="Local .mp4 or .mov file")
    parser.add_argument("--description", "-d", help="Caption for Instagram and Facebook, and the YouTube description")
    parser.add_argument("--description-file", type=Path, help="Read the description from a text file")
    parser.add_argument("--tags", action="append", default=[], help="Hashtags, separated by commas or spaces. Repeatable.")
    parser.add_argument("--thumbnail", "-t", help="Local JPEG, PNG, or WebP cover")
    parser.add_argument("--thumbnail-url", help="Public https JPEG. Instagram can fetch this directly.")
    parser.add_argument("--thumb-offset-ms", type=int, help="Instagram cover frame, in milliseconds, when you have no image")
    parser.add_argument("--title", help="Title for YouTube and Facebook. YouTube uses the first caption line when this is omitted.")
    parser.add_argument(
        "--to",
        action="append",
        help="facebook, instagram, youtube, both, or all. Repeatable. Default: all three.",
    )
    parser.add_argument("--page", help="Page id or exact Page name")
    parser.add_argument("--reels-tab-only", action="store_true", help="Do not also share the Instagram Reel to the profile feed")
    parser.add_argument("--collaborators", action="append", default=[], help="Up to 3 public Instagram usernames")
    parser.add_argument("--place-id", help="Numeric Facebook Page id of a location to tag")
    parser.add_argument(
        "--schedule",
        help="Facebook and YouTube publish time, ISO 8601. A time with no zone uses this machine's local zone. Instagram cannot be scheduled.",
    )
    parser.add_argument(
        "--privacy",
        choices=["public", "unlisted", "private"],
        default="public",
        help="YouTube privacy. Default: public. A scheduled Short stays private until that time, then becomes public.",
    )
    parser.add_argument("--ai-generated", action="store_true", help="Mark the Reel as AI-generated")
    parser.add_argument("--json", action="store_true")


def cmd_login(args: argparse.Namespace) -> int:
    settings = _settings(args)
    app_id = args.app_id or settings.app_id
    app_secret = args.app_secret or _env("REELPOST_APP_SECRET")
    config_id = args.config_id or settings.config_id
    if not app_id or not app_secret:
        raise ReelpostError(
            "Login needs the Meta app id and secret. Set REELPOST_APP_ID and REELPOST_APP_SECRET, "
            "or pass --app-id and --app-secret."
        )
    if args.app_secret:
        _log("warning: --app-secret can end up in shell history. Prefer REELPOST_APP_SECRET.")
    import secrets

    state = secrets.token_urlsafe(24)
    url = build_login_url(
        app_id=app_id,
        redirect_uri=REDIRECT_URI,
        state=state,
        version=settings.graph_version,
        config_id=config_id,
        scopes=DEFAULT_SCOPES,
    )
    print(url, file=sys.stderr)
    if args.paste:
        _log("Approve access in the browser, then paste the full redirect URL.")
        pasted = input("Redirect URL: ").strip()
        code = parse_oauth_redirect(pasted, state)
    else:
        open_browser(url)
        _log(f"Waiting for Facebook login on {REDIRECT_URI}")
        code = wait_for_code(state, timeout=args.timeout)
    session = requests.Session()
    token, expires_at = exchange_user_token(
        session,
        version=settings.graph_version,
        app_id=app_id,
        app_secret=app_secret,
        code=code,
        redirect_uri=REDIRECT_URI,
    )
    pages = list_pages(GraphClient(token, settings.graph_version, session=session))
    page = choose_page(pages, args.page, interactive=sys.stdin.isatty())
    if not page.get("ig_user_id"):
        _log(f"warning: {page.get('name') or page['id']} has no linked Instagram professional account.")
    if not page.get("access_token"):
        raise ReelpostError("Meta did not return a Page access token. Grant pages_show_list and log in again.")
    path = save_settings(
        Settings(
            graph_version=settings.graph_version,
            app_id=app_id,
            config_id=config_id,
            user_access_token=token,
            user_token_expires_at=expires_at,
            page_id=page["id"],
            page_name=page.get("name"),
            page_access_token=page["access_token"],
            ig_user_id=page.get("ig_user_id"),
            ig_username=page.get("ig_username"),
            youtube_client_id=settings.youtube_client_id,
            youtube_refresh_token=settings.youtube_refresh_token,
            youtube_access_token=settings.youtube_access_token,
            youtube_access_expires_at=settings.youtube_access_expires_at,
            youtube_channel_id=settings.youtube_channel_id,
            youtube_channel_title=settings.youtube_channel_title,
        )
    )
    ig = f" and @{page['ig_username']}" if page.get("ig_username") else ""
    print(f"Saved {page.get('name') or page['id']} ({page['id']}){ig}")
    _log(f"config: {path}")
    return 0


def cmd_auth_youtube(args: argparse.Namespace) -> int:
    settings = _settings(args)
    client_id = args.client_id or settings.youtube_client_id or _env("REELPOST_YOUTUBE_CLIENT_ID")
    client_secret = args.client_secret or _env("REELPOST_YOUTUBE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ReelpostError(
            "YouTube login needs a Google OAuth client id and secret. "
            "Set REELPOST_YOUTUBE_CLIENT_ID and REELPOST_YOUTUBE_CLIENT_SECRET, "
            "or pass --client-id and --client-secret."
        )
    if args.client_secret:
        _log("warning: --client-secret can end up in shell history. Prefer REELPOST_YOUTUBE_CLIENT_SECRET.")
    import secrets

    state = secrets.token_urlsafe(24)
    url = build_youtube_login_url(client_id, state)
    _log(url)
    session = requests.Session()
    if args.paste:
        pasted = input("Paste the redirect URL: ").strip()
        code = parse_oauth_redirect(pasted, state, service="Google")
    else:
        open_browser(url)
        code = wait_for_code(
            state,
            timeout=args.timeout,
            port=YT_REDIRECT_PORT,
            service="YouTube",
            redirect_uri=YT_REDIRECT_URI,
        )
    payload = exchange_youtube_code(session, client_id, client_secret, code)
    refresh = payload.get("refresh_token")
    if not refresh:
        raise ReelpostError(
            "Google did not return a refresh token. Remove reelpost from the Google account's "
            "third-party access and run `reelpost auth youtube` again."
        )
    token = payload["access_token"]
    channel_id, channel_title = fetch_channel(session, token)
    if not channel_id:
        _log("warning: could not read the YouTube channel name. The login was saved anyway.")
    settings.youtube_client_id = client_id
    settings.youtube_refresh_token = refresh
    settings.youtube_access_token = token
    settings.youtube_access_expires_at = token_expiry(payload.get("expires_in"))
    settings.youtube_channel_id = channel_id
    settings.youtube_channel_title = channel_title
    path = save_settings(settings)
    label = channel_title or channel_id or "the channel"
    print(f"Saved YouTube channel {label}")
    _log(f"config: {path}")
    _log("The client secret was not saved. Keep REELPOST_YOUTUBE_CLIENT_SECRET set so the login can refresh.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _settings(args)
    meta = bool(settings.user_access_token or settings.page_access_token)
    youtube = _youtube_public(settings)
    payload = {
        "logged_in": meta or youtube["logged_in"],
        "app_id": settings.app_id,
        "graph_version": settings.graph_version,
        "user_token": bool(settings.user_access_token),
        "user_token_expires_at": settings.user_token_expires_at,
        "page_id": settings.page_id,
        "page_name": settings.page_name,
        "page_token": bool(settings.page_access_token),
        "instagram_id": settings.ig_user_id,
        "instagram_username": settings.ig_username,
        "youtube": youtube,
    }
    if args.json:
        _emit(payload)
        return 0
    if not meta and not youtube["logged_in"]:
        print("Not logged in.")
        return 0
    if meta:
        print(f"App            {settings.app_id or '-'}")
        print(f"Graph          {settings.graph_version}")
        print(f"User token     {'saved' if settings.user_access_token else '-'}")
        if settings.user_token_expires_at:
            print(f"Expires        {settings.user_token_expires_at}")
        print(f"Page           {settings.page_name or '-'} ({settings.page_id or '-'})")
        print(f"Page token     {'saved' if settings.page_access_token else '-'}")
        if settings.ig_username:
            print(f"Instagram      @{settings.ig_username} ({settings.ig_user_id})")
        elif settings.ig_user_id:
            print(f"Instagram      {settings.ig_user_id}")
        else:
            print("Instagram      not linked")
    if youtube["logged_in"]:
        title = settings.youtube_channel_title or "-"
        channel_id = settings.youtube_channel_id or "-"
        print(f"YouTube        {title} ({channel_id})")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    if args.youtube:
        if clear_youtube_auth():
            print("Removed the YouTube login.")
        else:
            print("YouTube is already logged out.")
        return 0
    if delete_config():
        print("Removed the saved login.")
    else:
        print("Already logged out.")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    settings = _settings(args)
    youtube = _youtube_public(settings)
    meta = bool(settings.user_access_token or (settings.page_access_token and settings.page_id))
    if not meta and not youtube["logged_in"]:
        raise ReelpostError("Not logged in. Run `reelpost auth login` or `reelpost auth youtube`.")
    public: list[dict] = []
    if settings.user_access_token:
        pages = list_pages(GraphClient(settings.user_access_token, settings.graph_version))
        if args.page:
            pages = [choose_page(pages, args.page, interactive=False)]
        public = [public_page(page) for page in pages]
    elif settings.page_access_token and settings.page_id:
        page = _saved_page(settings)
        assert page is not None
        public = [public_page(enrich_instagram(GraphClient(page["access_token"], settings.graph_version), page))]
    if args.json:
        _emit({"pages": public, "youtube": youtube if youtube["logged_in"] else None})
        return 0
    if not public and not youtube["logged_in"]:
        print("No Pages were returned.")
        return 0
    for page in public:
        print(page["name"] or "Untitled")
        print(f"  page       {page['id']}")
        if page["instagram_username"]:
            print(f"  instagram  @{page['instagram_username']} ({page['instagram_id']})")
        elif page["instagram_id"]:
            print(f"  instagram  {page['instagram_id']}")
        else:
            print("  instagram  not linked")
    if youtube["logged_in"]:
        print(settings.youtube_channel_title or "YouTube")
        print(f"  channel    {settings.youtube_channel_id or '-'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    job = assemble(args, online=False)
    try:
        _print_warnings(job.warnings)
        if job.errors:
            raise ReelpostError("\n".join(f"- {error}" for error in job.errors))
        if args.json:
            _emit(_plan_payload(job, settings=None, dry_run=True))
        else:
            _print_plan(job, settings=None, dry_run=False)
            print("Ready.")
        return 0
    finally:
        job.close()


def cmd_publish(args: argparse.Namespace) -> int:
    job = assemble(args, online=not args.dry_run)
    try:
        _print_warnings(job.warnings)
        if job.errors:
            raise ReelpostError("Nothing was published.\n" + "\n".join(f"- {error}" for error in job.errors))
        settings = _settings(args)
        if args.dry_run:
            if args.json:
                _emit(_plan_payload(job, settings, dry_run=True))
            else:
                _print_plan(job, settings, dry_run=True)
            return 0
        if not args.yes and not sys.stdin.isatty():
            raise ReelpostError(
                "Refusing to publish without --yes when stdin is not a terminal. Nothing was published."
            )
        needs_meta = "facebook" in job.targets or "instagram" in job.targets
        page = None
        client = None
        if needs_meta:
            _warn_expiry(settings)
            page = resolve_page(settings, args.page, job.targets, interactive=sys.stdin.isatty())
            task_warning = warn_tasks(page)
            if task_warning:
                _log(f"warning: {task_warning}")
            client = GraphClient(page["access_token"], settings.graph_version)
            if "instagram" in job.targets:
                _check_quota(client, page["ig_user_id"])
        session = requests.Session()
        youtube_token = ensure_youtube_token(settings, session) if "youtube" in job.targets else None
        if not args.yes:
            _print_plan(job, settings, dry_run=False, page=page, file=sys.stderr)
            _confirm()
        results = _upload(job, page, client, youtube_token=youtube_token, session=session, timeout=args.timeout)
        if args.json:
            _emit(results)
        else:
            _print_results(results)
        return 1 if any("error" in item for item in results.values()) else 0
    finally:
        job.close()


def cmd_thumbnail(args: argparse.Namespace) -> int:
    if args.to == "youtube":
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", args.video_id):
            raise ReelpostError("YouTube video ids are 11 characters: letters, numbers, _ or -.")
    elif not re.fullmatch(r"\d+", args.video_id):
        raise ReelpostError("video_id must be the numeric Facebook video id.")
    if not args.yes and not sys.stdin.isatty():
        raise ReelpostError("Refusing to set a cover without --yes when stdin is not a terminal.")
    settings = _settings(args)
    limit = YT_COVER_MAX_BYTES if args.to == "youtube" else FB_COVER_MAX_BYTES
    asset = prepare_cover(Path(args.thumbnail), limit)
    try:
        if args.to == "youtube":
            session = requests.Session()
            token = ensure_youtube_token(settings, session)
            if not args.yes:
                label = settings.youtube_channel_title or settings.youtube_channel_id or "YouTube"
                _log(f"Set the cover on YouTube video {args.video_id} for {label}?")
                _confirm()
            set_youtube_thumbnail(session, token, args.video_id, asset.path)
            print(f"Cover updated for {args.video_id}")
            print(f"https://www.youtube.com/shorts/{args.video_id}")
            return 0
        page = resolve_page(settings, args.page, ["facebook"], interactive=sys.stdin.isatty())
        if not args.yes:
            _log(f"Set the cover on Facebook video {args.video_id} for {page.get('name') or page['id']}?")
            _confirm()
        upload_thumbnail(
            GraphClient(page["access_token"], settings.graph_version),
            args.video_id,
            asset.path,
        )
    finally:
        asset.close()
    print(f"Cover updated for {args.video_id}")
    print(f"https://www.facebook.com/reel/{args.video_id}")
    return 0


def assemble(args: argparse.Namespace, *, online: bool) -> Job:
    targets = parse_targets(args.to)
    description = _description(args)
    tags = parse_tags(args.tags or [])
    caption = compose_caption(description, tags)
    collaborators = _collaborators(args.collaborators or [])
    place_id = _place(args.place_id)
    schedule_at = parse_schedule(args.schedule) if args.schedule else None
    offset = _offset(args.thumb_offset_ms)
    if args.thumbnail and args.thumbnail_url:
        raise ReelpostError("Pass either --thumbnail or --thumbnail-url, not both.")
    if (args.thumbnail or args.thumbnail_url) and offset is not None:
        raise ReelpostError("Pass either a thumbnail or --thumb-offset-ms, not both.")
    if schedule_at is not None and "instagram" in targets:
        raise ReelpostError(
            "Instagram cannot be scheduled. Use --to facebook, --to youtube, or both of those."
        )
    if schedule_at is not None and "youtube" in targets and args.privacy == "unlisted":
        raise ReelpostError(
            "A scheduled YouTube Short is private until the chosen time, then becomes public. "
            "Drop --privacy unlisted, or drop --schedule."
        )
    info = inspect_video(Path(args.video))
    errors, warnings = limits_for(info, targets)
    title = args.title
    if "youtube" in targets:
        title, title_warning = youtube_title(args.title, caption, info.path.name)
        if title_warning:
            warnings.append(title_warning)
        youtube_description(caption)
        _kept, tag_warning = youtube_tags(tags)
        if tag_warning:
            warnings.append(tag_warning)
    elif args.title and len(args.title) > TITLE_LIMIT:
        raise ReelpostError(f"--title is {len(args.title)} characters. The limit is {TITLE_LIMIT}.")
    if not caption:
        warnings.append("No description or tags were provided.")
    if schedule_at is not None and "youtube" in targets:
        warnings.append("The YouTube Short stays private until the scheduled time, then becomes public.")
    if args.privacy != "public" and "youtube" not in targets:
        warnings.append("--privacy is only sent to YouTube.")
    if args.title and "facebook" not in targets and "youtube" not in targets:
        warnings.append("--title is only sent to Facebook and YouTube.")
    if collaborators and "instagram" not in targets:
        warnings.append("--collaborators is only sent to Instagram.")
    if offset is not None and "instagram" not in targets:
        warnings.append("--thumb-offset-ms is only sent to Instagram.")
    if offset is not None and "facebook" in targets:
        warnings.append("Facebook cannot use a frame offset. It will choose its own cover unless you pass a thumbnail.")

    cover: CoverAsset | None = None
    thumbnail_url = None
    tunnel = False
    try:
        cover, thumbnail_url, tunnel, cover_warnings = _cover(args, targets, online=online)
        warnings.extend(cover_warnings)
    except Exception:
        if cover is not None:
            cover.close()
        raise
    return Job(
        video_path=info.path,
        targets=targets,
        caption=caption,
        tags=tags,
        title=title,
        privacy=args.privacy,
        collaborators=collaborators,
        place_id=place_id,
        schedule_at=schedule_at,
        share_to_feed=not args.reels_tab_only,
        ai_generated=args.ai_generated,
        thumb_offset_ms=offset,
        thumbnail_url=thumbnail_url,
        tunnel=tunnel,
        cover=cover,
        warnings=warnings,
        errors=errors,
        info=info,
    )


def parse_targets(values: list[str] | None) -> list[str]:
    if not values:
        return ["facebook", "instagram", "youtube"]
    selected: list[str] = []
    aliases = {
        "facebook": "facebook",
        "fb": "facebook",
        "instagram": "instagram",
        "ig": "instagram",
        "youtube": "youtube",
        "yt": "youtube",
        "shorts": "youtube",
    }
    for value in values:
        for part in value.split(","):
            name = part.strip().lower()
            if not name:
                continue
            if name == "both":
                group = ("facebook", "instagram")
            elif name == "all":
                group = ("facebook", "instagram", "youtube")
            else:
                canonical = aliases.get(name)
                if canonical is None:
                    raise ReelpostError(
                        f"Unknown target {part.strip()!r}. Use facebook, instagram, youtube, both, or all."
                    )
                group = (canonical,)
            for item in group:
                if item not in selected:
                    selected.append(item)
    if not selected:
        raise ReelpostError("Choose at least one target: facebook, instagram, youtube, both, or all.")
    return selected


def resolve_page(settings: Settings, page_query: str | None, targets: list[str], *, interactive: bool) -> dict:
    need_ig = "instagram" in targets
    saved = _saved_page(settings)
    if saved is not None and _page_matches(saved, page_query):
        client = GraphClient(saved["access_token"], settings.graph_version)
        if need_ig and not saved.get("ig_user_id"):
            enrich_instagram(client, saved)
        _require_instagram(saved, need_ig)
        return saved
    if not settings.user_access_token:
        raise ReelpostError(
            "Not logged in. Run `reelpost auth login`, or set REELPOST_PAGE_TOKEN and REELPOST_PAGE_ID."
        )
    pages = list_pages(GraphClient(settings.user_access_token, settings.graph_version))
    page = choose_page(pages, page_query, interactive=interactive)
    if need_ig and not page.get("ig_user_id") and page.get("access_token"):
        enrich_instagram(GraphClient(page["access_token"], settings.graph_version), page)
    _require_instagram(page, need_ig)
    if not page.get("access_token"):
        raise ReelpostError("The Page response did not include an access token. Log in again.")
    update_saved_page(page)
    return page


def _upload(
    job: Job,
    page: dict | None,
    client: GraphClient | None,
    *,
    youtube_token: str | None,
    session,
    timeout: float,
) -> dict:
    results: dict[str, dict] = {}
    if "facebook" in job.targets:
        try:
            if page is None or client is None:
                raise ReelpostError("Missing Facebook Page.")
            results["facebook"] = publish_facebook(
                client,
                page["id"],
                job.video_path,
                job.caption,
                title=job.title,
                thumbnail=None if job.cover is None else job.cover.path,
                place_id=job.place_id,
                schedule_at=job.schedule_at,
                ai_generated=job.ai_generated,
                timeout=timeout,
                log=_log,
                on_bytes=_Bytes("facebook upload"),
            )
        except ReelpostError as exc:
            results["facebook"] = {"error": str(exc)}
            _log(f"facebook failed: {exc}")
    if "instagram" in job.targets:
        try:
            if page is None or client is None:
                raise ReelpostError("Missing Facebook Page.")
            with _cover_context(job) as cover_url:
                if cover_url:
                    _log(f"instagram cover: {cover_url}")
                results["instagram"] = publish_instagram(
                    client,
                    page["ig_user_id"],
                    job.video_path,
                    job.caption,
                    cover_url=cover_url,
                    thumb_offset_ms=job.thumb_offset_ms,
                    share_to_feed=job.share_to_feed,
                    collaborators=job.collaborators or None,
                    location_id=job.place_id,
                    ai_generated=job.ai_generated,
                    timeout=timeout,
                    log=_log,
                    on_bytes=_Bytes("instagram upload"),
                )
        except ReelpostError as exc:
            results["instagram"] = {"error": str(exc)}
            _log(f"instagram failed: {exc}")
    if "youtube" in job.targets:
        try:
            if not youtube_token:
                raise ReelpostError("YouTube is not logged in. Run `reelpost auth youtube`.")
            results["youtube"] = publish_youtube(
                session,
                youtube_token,
                job.video_path,
                job.caption,
                title=job.title or "Short",
                tags=job.tags,
                thumbnail=None if job.cover is None else job.cover.path,
                privacy=job.privacy,
                schedule_at=job.schedule_at,
                ai_generated=job.ai_generated,
                log=_log,
                on_bytes=_Bytes("youtube upload"),
            )
        except ReelpostError as exc:
            results["youtube"] = {"error": str(exc)}
            _log(f"youtube failed: {exc}")
    return results


def _cover_context(job: Job):
    if job.tunnel:
        if job.cover is None:
            raise ReelpostError("Missing thumbnail file for Instagram.")
        _log("The Instagram cover will be available at a temporary public URL until processing finishes.")
        return public_cover(job.cover.path)
    return nullcontext(job.thumbnail_url)


def _cover(args, targets: list[str], *, online: bool):
    max_bytes = cover_limit(targets)
    warnings: list[str] = []
    if args.thumbnail:
        asset = prepare_cover(Path(args.thumbnail), max_bytes)
        warning = cover_warning(asset.width, asset.height)
        if warning:
            warnings.append(warning)
        return asset, None, "instagram" in targets, warnings
    if args.thumbnail_url:
        if not str(args.thumbnail_url).lower().startswith("https://"):
            raise ReelpostError("--thumbnail-url must start with https://.")
        if not online:
            return None, args.thumbnail_url, False, warnings
        asset = cover_from_url(args.thumbnail_url, max_bytes)
        warning = cover_warning(asset.width, asset.height)
        if warning:
            warnings.append(warning)
        if "instagram" in targets and not asset.reencoded:
            needs_file = "facebook" in targets or "youtube" in targets
            if not needs_file:
                asset.close()
                return None, args.thumbnail_url, False, warnings
            return asset, args.thumbnail_url, False, warnings
        if "instagram" in targets:
            warnings.append(
                "The thumbnail URL was converted to JPEG. Instagram will fetch the converted file "
                "from a temporary public URL."
            )
            return asset, None, True, warnings
        return asset, None, False, warnings
    return None, None, False, warnings


def _check_quota(client: GraphClient, ig_user_id: str) -> None:
    quota = read_quota(client, ig_user_id)
    if quota is None:
        _log("warning: could not read the Instagram publishing quota. Continuing.")
        return
    usage, total = quota
    _log(f"instagram publishing quota {usage}/{total} in the current 24-hour window")
    if total and usage >= total:
        raise ReelpostError(
            f"Instagram publishing quota is exhausted ({usage}/{total} in 24 hours). Nothing was published."
        )


def _saved_page(settings: Settings) -> dict | None:
    if not (settings.page_access_token and settings.page_id):
        return None
    return {
        "id": settings.page_id,
        "name": settings.page_name or settings.page_id,
        "access_token": settings.page_access_token,
        "ig_user_id": settings.ig_user_id,
        "ig_username": settings.ig_username,
        "tasks": [],
    }


def _page_matches(page: dict, query: str | None) -> bool:
    if not query:
        return True
    return query == page["id"] or query.casefold() == (page.get("name") or "").casefold()


def _require_instagram(page: dict, need_ig: bool) -> None:
    if need_ig and not page.get("ig_user_id"):
        raise ReelpostError(
            f"Page {page.get('name') or page['id']} has no linked Instagram professional account. "
            "Link a Business or Creator account in the Page settings, or publish with --to facebook."
        )


def _description(args: argparse.Namespace) -> str:
    if args.description and args.description_file:
        raise ReelpostError("Pass --description or --description-file, not both.")
    if args.description_file:
        path = Path(args.description_file)
        if not path.is_file():
            raise ReelpostError(f"Description file not found: {path}")
        return path.read_text(encoding="utf-8").lstrip("\ufeff")
    return args.description or ""


def _collaborators(values: list[str]) -> list[str]:
    names: list[str] = []
    for value in values:
        for part in re.split(r"[\s,]+", value.strip()):
            name = part[1:] if part.startswith("@") else part
            if not name:
                continue
            if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", name):
                raise ReelpostError(f"Invalid Instagram username {part!r}.")
            if name not in names:
                names.append(name)
    if len(names) > MAX_COLLABORATORS:
        raise ReelpostError(f"Instagram allows at most {MAX_COLLABORATORS} collaborators.")
    return names


def _place(value: str | None) -> str | None:
    if not value:
        return None
    if not re.fullmatch(r"\d+", value):
        raise ReelpostError("--place-id must be the numeric id of a Facebook Page that has a location.")
    return value


def _offset(value: int | None) -> int | None:
    if value is None:
        return None
    if value < 0:
        raise ReelpostError("--thumb-offset-ms cannot be negative.")
    if value > 15 * 60 * 1000:
        raise ReelpostError("--thumb-offset-ms must be within 15 minutes.")
    return value


def _settings(args: argparse.Namespace) -> Settings:
    settings = load_settings()
    if getattr(args, "graph_version", None):
        settings.graph_version = normalize_version(args.graph_version)
    return settings


def _warn_expiry(settings: Settings) -> None:
    raw = settings.user_token_expires_at
    if not raw:
        return
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if expires < now:
        _log("warning: the user token is expired. Publishing may fail until you run reelpost auth login.")
    elif expires - now < timedelta(days=7):
        _log("warning: the user token expires within 7 days. Run reelpost auth login to refresh it.")


def _confirm() -> None:
    print("Publish now? [y/N] ", file=sys.stderr, end="", flush=True)
    answer = sys.stdin.readline().strip().lower()
    if answer not in {"y", "yes"}:
        raise ReelpostError("Cancelled. Nothing was published.")


def _plan_payload(job: Job, settings: Settings | None, *, dry_run: bool) -> dict:
    info = job.info
    return {
        "dry_run": dry_run,
        "targets": job.targets,
        "video": {
            "path": str(job.video_path),
            "bytes": info.size_bytes,
            "duration_seconds": info.duration_seconds,
            "width": info.width,
            "height": info.height,
            "faststart": info.faststart,
        },
        "caption": job.caption,
        "title": job.title,
        "privacy": job.privacy if "youtube" in job.targets else None,
        "youtube_description": youtube_description(job.caption) if "youtube" in job.targets else None,
        "youtube_channel": None if settings is None else settings.youtube_channel_title,
        "thumbnail": None if job.cover is None else str(job.cover.path),
        "thumbnail_url": job.thumbnail_url,
        "instagram_cover": _instagram_cover_note(job),
        "schedule": None if job.schedule_at is None else job.schedule_at.isoformat(),
        "page_name": None if settings is None else settings.page_name,
        "instagram_username": None if settings is None else settings.ig_username,
        "warnings": job.warnings,
        "errors": job.errors,
    }


def _print_plan(job: Job, settings: Settings | None, *, dry_run: bool, page: dict | None = None, file=None) -> None:
    info = job.info
    out = file or sys.stdout
    if dry_run:
        print("Dry run. Nothing will be uploaded.", file=out)
        print(file=out)
    bits = [format_bytes(info.size_bytes)]
    if info.duration_seconds is not None:
        bits.append(format_seconds(info.duration_seconds))
    if info.width and info.height:
        bits.append(f"{info.width}×{info.height}")
    if info.faststart is True:
        bits.append("moov at front")
    print(f"Targets: {', '.join(job.targets)}", file=out)
    print(f"Video: {job.video_path}", file=out)
    print(f"  {', '.join(bits)}", file=out)
    print("Caption:", file=out)
    print(job.caption or "(empty)", file=out)
    if job.title:
        print(f"Title: {job.title}", file=out)
    if "youtube" in job.targets:
        print(f"YouTube privacy: {job.privacy}", file=out)
        description = youtube_description(job.caption)
        if description != job.caption:
            print("YouTube description:", file=out)
            print(description, file=out)
        if settings is not None and settings.youtube_channel_title:
            print(f"YouTube: {settings.youtube_channel_title}", file=out)
    if job.schedule_at is not None:
        print(f"Schedule: {job.schedule_at.isoformat()}", file=out)
    cover = _instagram_cover_note(job)
    if job.cover is not None:
        print(f"Cover file: {job.cover.path} ({job.cover.width}×{job.cover.height})", file=out)
    if cover:
        print(f"Instagram cover: {cover}", file=out)
    if page is not None:
        ig = f" @{page['ig_username']}" if page.get("ig_username") else ""
        print(f"Account: {page.get('name') or page['id']}{ig}", file=out)
    elif settings is not None and (settings.page_name or settings.ig_username):
        ig = f" @{settings.ig_username}" if settings.ig_username else ""
        print(f"Saved account: {settings.page_name or '-'}{ig}", file=out)


def _instagram_cover_note(job: Job) -> str | None:
    if "instagram" not in job.targets:
        return None
    if job.tunnel:
        return "temporary public URL created at publish time"
    if job.thumbnail_url:
        return job.thumbnail_url
    if job.thumb_offset_ms is not None:
        return f"frame at {job.thumb_offset_ms} ms"
    return None


def _print_results(results: dict) -> None:
    for name in ("facebook", "instagram", "youtube"):
        if name not in results:
            continue
        item = results[name]
        print(name.capitalize())
        if "error" in item:
            print(f"  error      {item['error']}")
            continue
        if name == "facebook":
            print(f"  video      {item.get('video_id')}")
            if item.get("post_id"):
                print(f"  post       {item['post_id']}")
            print(f"  url        {item.get('permalink')}")
            print(f"  cover      {'set' if item.get('thumbnail') else 'default'}")
            if item.get("scheduled"):
                print("  state      scheduled")
        elif name == "youtube":
            print(f"  video      {item.get('video_id')}")
            print(f"  url        {item.get('url')}")
            print(f"  cover      {'set' if item.get('thumbnail') else 'default'}")
            print(f"  privacy    {item.get('privacy')}")
            if item.get("scheduled"):
                print(f"  state      scheduled for {item.get('publish_at')}")
        else:
            print(f"  media      {item.get('media_id')}")
            if item.get("permalink"):
                print(f"  url        {item['permalink']}")
            if item.get("confirmed") is False:
                print("  status     returned a media id; confirmation timed out")


def _print_warnings(warnings: list[str]) -> None:
    for warning in warnings:
        _log(f"warning: {warning}")


def _emit(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


def _log(message: str) -> None:
    print(f"reelpost: {message}", file=sys.stderr)


def ensure_youtube_token(settings: Settings, session) -> str:
    if access_is_fresh(settings.youtube_access_token, settings.youtube_access_expires_at):
        return settings.youtube_access_token or ""
    refresh = settings.youtube_refresh_token
    client_id = settings.youtube_client_id or _env("REELPOST_YOUTUBE_CLIENT_ID")
    secret = _env("REELPOST_YOUTUBE_CLIENT_SECRET")
    if not refresh:
        raise ReelpostError(
            "YouTube is not logged in. Run `reelpost auth youtube`, or publish with --to facebook,instagram."
        )
    if not client_id or not secret:
        raise ReelpostError(
            "Refreshing the YouTube login needs REELPOST_YOUTUBE_CLIENT_ID and REELPOST_YOUTUBE_CLIENT_SECRET."
        )
    payload = refresh_youtube_token(session, client_id, secret, refresh)
    token = payload["access_token"]
    expiry = token_expiry(payload.get("expires_in"))
    settings.youtube_access_token = token
    settings.youtube_access_expires_at = expiry
    fields = {"youtube_access_token": token, "youtube_access_expires_at": expiry}
    if payload.get("refresh_token"):
        settings.youtube_refresh_token = payload["refresh_token"]
        fields["youtube_refresh_token"] = payload["refresh_token"]
    update_youtube_auth(fields)
    return token


def _youtube_public(settings: Settings) -> dict:
    logged_in = bool(
        settings.youtube_refresh_token or settings.youtube_access_token or settings.youtube_channel_id
    )
    return {
        "logged_in": logged_in,
        "channel_id": settings.youtube_channel_id,
        "channel_title": settings.youtube_channel_title,
        "refresh_token": bool(settings.youtube_refresh_token),
    }


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


class _Bytes:
    def __init__(self, label: str):
        self.label = label
        self.last = -1

    def __call__(self, sent: int, total: int) -> None:
        percent = 100 if total <= 0 else min(100, int(sent * 100 / total))
        bucket = percent // 10
        if bucket != self.last:
            self.last = bucket
            _log(f"{self.label} {percent}%")
