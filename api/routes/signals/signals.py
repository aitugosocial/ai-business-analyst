"""
api/routes/signals/signals.py

Signal (blog) endpoints for the Lavoo platform.

Authoring is restricted to moderators and admins.
Any authenticated user may like or comment.
Published Signals are publicly readable without authentication.

Chop rewards:
  - Like a Signal    →  +2 chops (reversed on unlike)
  - Comment on Signal →  +5 chops (reversed on comment deletion)
"""

import re
import unicodedata
import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc, func, or_, cast, String

from database.pg_connections import get_db
from database.pg_models import (
    User,
    Signal,
    SignalLike,
    SignalComment,
    SignalCreate,
    SignalUpdate,
    SignalCommentCreate,
    SignalCommentUpdate,
    UserRole,
)
from api.routes.dependencies import get_current_user, get_current_user_optional

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIKE_CHOPS    = 2
COMMENT_CHOPS = 5

VALID_STATUSES = {"draft", "published", "archived"}

# The featured grid on the /blog homepage (the big card + 2 mini cards)
# shows exactly this many posts. Enforced everywhere is_featured can be
# set, not just the dedicated toggle endpoint below, so the invariant
# can't be bypassed via a direct call to update_signal().
MAX_FEATURED = 3

# The hero slot at the top of /blog shows whichever single post is
# pinned — at most one, ever. Same enforcement approach as MAX_FEATURED
# above, sharing the same underlying helper (_apply_capped_flag_change).
MAX_PINNED = 1

# Server-side backstop for cover_image_data. The frontend auto-compresses
# uploads to ~150KB raw before base64-encoding (see
# lib/utils/imageCompression.ts), and base64 inflates that by ~33% — so a
# correctly-compressed image lands around 200KB of text here. This cap is
# set generously above that (not tight to 200KB) specifically so it never
# false-rejects a legitimately compressed image; it exists purely to catch
# someone bypassing the UI and hitting the API directly with an
# uncompressed multi-megabyte image, which is the actual DB-cost risk this
# whole feature is protecting against.
MAX_COVER_IMAGE_B64_CHARS = 400_000


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_cover_image_size(cover_image_data: Optional[str]) -> None:
    """
    Raise HTTP 400 if a base64 cover image exceeds the server-side backstop.
    See MAX_COVER_IMAGE_B64_CHARS above for why this limit is set where it
    is — this is a safety net against bypassing the frontend's
    auto-compression, not the primary size-shaping mechanism.
    """
    if cover_image_data and len(cover_image_data) > MAX_COVER_IMAGE_B64_CHARS:
        raise HTTPException(
            status_code=400,
            detail="This cover image is too large. Please choose a smaller image "
                   "— images are automatically optimized when uploaded through the editor.",
        )


def _generate_slug(title: str) -> str:
    """
    Convert a post title to a lowercase, URL-safe slug.
    Example: "Hello World! (2026)" → "hello-world-2026"
    """
    value = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s-]", "", value).strip().lower()
    value = re.sub(r"[-\s]+", "-", value)
    return value


def _unique_slug(base_slug: str, db: Session, exclude_id: int = None) -> str:
    """
    Append an incrementing suffix to base_slug until the slug is unique in
    the database. The exclude_id allows a signal to retain its own slug
    during an update without triggering a false conflict.
    """
    slug    = base_slug
    counter = 1
    while True:
        query = db.query(Signal).filter(Signal.slug == slug)
        if exclude_id:
            query = query.filter(Signal.id != exclude_id)
        if not query.first():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1


def _estimate_read_time(content: str) -> str:
    """
    Estimate reading time from word count at 200 wpm.
    Returns a human-readable string such as "4 min read".
    """
    word_count = len(content.split())
    minutes    = max(1, round(word_count / 200))
    return f"{minutes} min read"


def _require_moderator(current_user: User) -> User:
    """
    Raise HTTP 403 unless the user holds the moderator role or is an admin.
    Call at the top of any write endpoint that authors Signals.
    """
    is_moderator = current_user.role == UserRole.MODERATOR.value
    if not is_moderator and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only moderators can author Signal posts.",
        )
    return current_user


def _apply_capped_flag_change(
    signal: Signal,
    field: str,  # "is_featured" or "is_pinned" — validated against this exact set below
    want_value: bool,
    db: Session,
    max_count: int,
    noun: str,  # participle form for messages, e.g. "featured" / "pinned"
    verb: str,  # base verb form for messages, e.g. "feature" / "pin"
    replace_id: Optional[int] = None,
) -> None:
    """
    Shared cap-enforcement for both is_featured (max MAX_FEATURED) and
    is_pinned (max MAX_PINNED — effectively a single "hero slot"). Mutates
    `signal` (and possibly a replaced signal) in place; caller is
    responsible for db.commit(). Does not check the acting user's
    permissions — callers must already have authorized the request.

    - Turning the flag off is always allowed.
    - Turning it on succeeds immediately if fewer than `max_count` other
      posts currently have it set.
    - Turning it on while already at the cap raises HTTP 409 with the list
      of posts currently holding the flag (id/title/slug), UNLESS
      `replace_id` names one of them — in which case that post has the
      flag cleared and this one gets it, atomically, in the same
      transaction.

    `field` is restricted to a known-safe set rather than accepting any
    string, so a typo here fails loudly at call time instead of silently
    no-op'ing via getattr on a nonexistent attribute. `noun`/`verb` are
    passed separately rather than derived from one another (e.g. stripping
    "featured" down to "feature") because that kind of string surgery is
    exactly the sort of thing that quietly produces bad grammar in one of
    the two branches below.
    """
    if field not in ("is_featured", "is_pinned"):
        raise ValueError(f"_apply_capped_flag_change: unsupported field {field!r}")

    current_value = signal.is_featured if field == "is_featured" else signal.is_pinned
    if current_value == want_value:
        return  # already in the desired state — nothing to do

    if not want_value:
        if field == "is_featured":
            signal.is_featured = False
        else:
            signal.is_pinned = False
        return

    query = db.query(Signal).filter(Signal.id != signal.id)
    query = query.filter(Signal.is_featured == True) if field == "is_featured" else query.filter(Signal.is_pinned == True)  # noqa: E712
    holders = query.order_by(desc(Signal.published_at)).all()

    if len(holders) < max_count:
        if field == "is_featured":
            signal.is_featured = True
        else:
            signal.is_pinned = True
        return

    if replace_id is not None:
        target = next((s for s in holders if s.id == replace_id), None)
        if not target:
            raise HTTPException(
                status_code=400,
                detail=f"The post you selected to replace is not currently {noun}.",
            )
        if field == "is_featured":
            target.is_featured = False
            signal.is_featured = True
        else:
            target.is_pinned = False
            signal.is_pinned = True
        return

    cap_phrase = (
        f"Only {max_count} post can be {noun} at once."
        if max_count == 1
        else f"You can {verb} at most {max_count} posts at once."
    )
    raise HTTPException(
        status_code=409,
        detail={
            "status":  "limit_reached",
            "message": f"{cap_phrase} Choose one below to replace, or remove the {noun} status from a post first.",
            "conflicting_posts": [
                {"id": s.id, "title": s.title, "slug": s.slug} for s in holders
            ],
        },
    )


def _format_signal(signal: Signal, current_user_id: int = None, db: Session = None) -> dict:
    """
    Serialise a Signal ORM object to the standard API response shape.
    Resolves has_liked when a current_user_id is supplied.
    """
    has_liked = False
    if current_user_id and db:
        has_liked = db.query(SignalLike).filter(
            SignalLike.signal_id == signal.id,
            SignalLike.user_id   == current_user_id,
        ).first() is not None

    canonical_url = f"https://lavoo.io/l/thesignal/{signal.id}/{signal.slug}"

    return {
        "id":              signal.id,
        "title":           signal.title,
        "slug":            signal.slug,
        "canonical_url":   canonical_url,
        "excerpt":         signal.excerpt,
        "content":         signal.content,
        "cover_image_url": signal.cover_image_url,
        "cover_image_data": signal.cover_image_data,
        "category":        signal.category,
        "tags":            signal.tags,
        "status":          signal.status,
        "read_time":       signal.read_time,
        "is_featured":     signal.is_featured,
        "is_pinned":       signal.is_pinned,
        "view_count":      signal.view_count,
        "like_count":      signal.like_count,
        "comment_count":   signal.comment_count,
        "published_at":    signal.published_at.isoformat() if signal.published_at else None,
        "created_at":      signal.created_at.isoformat() if signal.created_at else None,
        "updated_at":      signal.updated_at.isoformat() if signal.updated_at else None,
        "has_liked":       has_liked,
        "author": {
            "id":         signal.author.id,
            "name":       signal.author.name,
            "avatar_url": signal.author.avatar_url,
            "role":       signal.author.role,
        },
    }


def _format_comment(comment: SignalComment, include_replies: bool = True) -> dict:
    """
    Serialise a SignalComment ORM object.
    Soft-deleted comments expose is_deleted=True with content cleared —
    the record is preserved so threaded replies are not orphaned.
    """
    replies = []
    if include_replies and comment.replies:
        replies = [
            _format_comment(reply, include_replies=False)
            for reply in comment.replies
            if not reply.is_deleted
        ]

    return {
        "id":                comment.id,
        "signal_id":         comment.signal_id,
        "user_id":           comment.user_id,
        "parent_comment_id": comment.parent_comment_id,
        "content":           comment.content,
        "is_edited":         comment.is_edited,
        "edited_at":         comment.edited_at.isoformat() if comment.edited_at else None,
        "is_deleted":        comment.is_deleted,
        "created_at":        comment.created_at.isoformat() if comment.created_at else None,
        "replies":           replies,
        "author": {
            "id":         comment.user.id,
            "name":       comment.user.name,
            "avatar_url": comment.user.avatar_url,
            "role":       comment.user.role,
        },
    }


# ---------------------------------------------------------------------------
# Read endpoints  (publicly accessible)
# ---------------------------------------------------------------------------

@router.get("")
async def list_signals(
    response: Response,
    page:     int           = Query(default=1, ge=1),
    limit:    int           = Query(default=10, ge=1, le=50),
    category: Optional[str] = Query(default=None),
    tag:      Optional[str] = Query(default=None),
    featured: Optional[bool]= Query(default=None),
    pinned:   Optional[bool]= Query(default=None),
    search:   Optional[str] = Query(default=None),
    db:       Session       = Depends(get_db),
):
    """
    Return a paginated list of published Signal posts.

    Supports filtering by category, tag, featured flag, and a keyword
    search across title and excerpt. Results are ordered by pinned status
    (pinned posts appear first), then by published_at descending.
    """
    # This list changes on every publish, feature/unfeature, and like, and
    # is read by a plain browser fetch() with no cache-busting query
    # param — without an explicit no-store, either the browser's own HTTP
    # cache or an intermediate proxy/CDN can silently serve a stale copy,
    # which only a hard refresh (bypassing HTTP cache) would reveal.
    response.headers["Cache-Control"] = "no-store"

    query = db.query(Signal).options(joinedload(Signal.author)).filter(Signal.status == "published")

    if category:
        query = query.filter(Signal.category.ilike(f"%{category}%"))

    if tag:
        # `tags` is a plain JSON column (not JSONB), so there is no native
        # `@>` containment operator to rely on here. Casting to text and
        # matching the quoted tag substring works without needing a JSONB
        # migration. Matches an exact tag value, e.g. ["AI", "automation"].
        query = query.filter(cast(Signal.tags, String).ilike(f'%"{tag}"%'))

    if featured is not None:
        query = query.filter(Signal.is_featured == featured)

    if pinned is not None:
        query = query.filter(Signal.is_pinned == pinned)

    if search:
        term = f"%{search}%"
        query = query.filter(
            or_(
                Signal.title.ilike(term),
                Signal.excerpt.ilike(term),
            )
        )

    total = query.count()

    signals = (
        query
        .order_by(desc(Signal.is_pinned), desc(Signal.published_at))
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "signals":     [_format_signal(s) for s in signals],
        "total":       total,
        "page":        page,
        "limit":       limit,
        "total_pages": (total + limit - 1) // limit,
    }


@router.get("/{identifier}")
async def get_signal(
    identifier: str,
    response: Response,
    db:   Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Return a single published Signal by its ID or URL slug.
    Increments the view counter on every successful request.

    Uses optional auth (get_current_user_optional) so:
      - a logged-in visitor gets an accurate `has_liked` on their own likes
      - a logged-out visitor still gets the full post, just with
        `has_liked: false` always (there's no session to check against)
    """
    response.headers["Cache-Control"] = "no-store"

    if identifier.isdigit():
        signal = db.query(Signal).filter(
            Signal.id == int(identifier),
            Signal.status == "published",
        ).first()
    else:
        signal = db.query(Signal).filter(
            Signal.slug == identifier,
            Signal.status == "published",
        ).first()

    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    # Increment view count without a full model refresh
    signal.view_count = (signal.view_count or 0) + 1
    db.commit()
    db.refresh(signal)

    return _format_signal(
        signal,
        current_user_id=current_user.id if current_user else None,
        db=db,
    )


# ---------------------------------------------------------------------------
# Authoring endpoints  (moderator / admin only)
# ---------------------------------------------------------------------------

@router.post("", status_code=201)
async def create_signal(
    payload:      SignalCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Create a new Signal post.

    - Restricted to users with role == "moderator" or is_admin == True.
    - Slug is auto-generated from the title and guaranteed unique.
    - Read time is estimated from content length.
    - published_at is set automatically when status is "published".
    """
    _require_moderator(current_user)

    if payload.status and payload.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}.",
        )

    _check_cover_image_size(payload.cover_image_data)

    base_slug = _generate_slug(payload.title)
    slug      = _unique_slug(base_slug, db)

    published_at = None
    if payload.status == "published":
        published_at = datetime.now(timezone.utc)

    signal = Signal(
        author_id       = current_user.id,
        title           = payload.title,
        slug            = slug,
        excerpt         = payload.excerpt,
        content         = payload.content,
        cover_image_url = payload.cover_image_url,
        cover_image_data = payload.cover_image_data,
        category        = payload.category,
        tags            = payload.tags,
        status          = payload.status or "draft",
        published_at    = published_at,
        read_time       = _estimate_read_time(payload.content),
        is_featured     = payload.is_featured or False,
        is_pinned       = payload.is_pinned or False,
    )

    db.add(signal)
    db.commit()
    db.refresh(signal)

    logger.info(f"Signal created: id={signal.id} slug='{signal.slug}' author={current_user.email}")
    return _format_signal(signal, current_user_id=current_user.id, db=db)


@router.patch("/{signal_id}")
async def update_signal(
    signal_id:    int,
    payload:      SignalUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Update an existing Signal post.

    - Restricted to the post's author or an admin.
    - Only supplied (non-None) fields are applied — others are left unchanged.
    - Slug is regenerated if the title changes, preserving uniqueness.
    - published_at is set when status changes to "published" for the first time.
    """
    _require_moderator(current_user)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    # Authors may only edit their own posts; admins may edit any
    if signal.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You can only edit your own Signal posts.",
        )

    if payload.status and payload.status not in VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Must be one of: {', '.join(VALID_STATUSES)}.",
        )

    _check_cover_image_size(payload.cover_image_data)

    if payload.title is not None:
        base_slug    = _generate_slug(payload.title)
        signal.slug  = _unique_slug(base_slug, db, exclude_id=signal_id)
        signal.title = payload.title

    if payload.content is not None:
        signal.content    = payload.content
        signal.read_time  = _estimate_read_time(payload.content)

    if payload.excerpt         is not None: signal.excerpt         = payload.excerpt
    if payload.cover_image_url is not None: signal.cover_image_url = payload.cover_image_url
    if payload.cover_image_data is not None: signal.cover_image_data = payload.cover_image_data
    if payload.category        is not None: signal.category        = payload.category
    if payload.tags            is not None: signal.tags            = payload.tags
    if payload.is_featured is not None:
        # Enforced here too (not just the dedicated /feature endpoint) so
        # the MAX_FEATURED cap can't be bypassed via a direct edit-form
        # save. No replace_id support on this path by design — conflicts
        # are resolved through the dedicated toggle endpoint's UI, which
        # can show the moderator which posts are currently featured.
        _apply_capped_flag_change(signal, "is_featured", payload.is_featured, db, MAX_FEATURED, noun="featured", verb="feature", replace_id=None)
    if payload.is_pinned is not None:
        # Same reasoning as is_featured above — the hero slot is a
        # single-post cap now, not a free toggle, so this has to go
        # through the same enforcement rather than setting the column
        # directly.
        _apply_capped_flag_change(signal, "is_pinned", payload.is_pinned, db, MAX_PINNED, noun="pinned", verb="pin", replace_id=None)

    if payload.status is not None:
        # Set published_at the first time a post goes live
        if payload.status == "published" and signal.status != "published":
            signal.published_at = datetime.now(timezone.utc)
        signal.status = payload.status

    db.commit()
    db.refresh(signal)

    logger.info(f"Signal updated: id={signal.id} by user={current_user.email}")
    return _format_signal(signal, current_user_id=current_user.id, db=db)


@router.delete("/{signal_id}", status_code=200)
async def delete_signal(
    signal_id:    int,
    permanent:    bool = Query(default=True),
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Delete or archive a Signal post.
    When permanent=True (default), removes the record and its associations completely from the database.
    """
    _require_moderator(current_user)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    if signal.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You can only delete your own Signal posts.",
        )

    if permanent:
        db.query(SignalLike).filter(SignalLike.signal_id == signal_id).delete(synchronize_session=False)
        db.query(SignalComment).filter(SignalComment.signal_id == signal_id).delete(synchronize_session=False)
        db.delete(signal)
        db.commit()
        logger.info(f"Signal permanently deleted: id={signal_id} by user={current_user.email}")
        return {"status": "success", "message": "Signal has been permanently deleted."}
    else:
        signal.status = "archived"
        db.commit()
        logger.info(f"Signal archived: id={signal.id} by user={current_user.email}")
        return {"status": "success", "message": "Signal has been archived."}


# ---------------------------------------------------------------------------
# Like endpoints  (authenticated users)
# ---------------------------------------------------------------------------

@router.post("/{signal_id}/like")
async def toggle_like(
    signal_id:    int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Toggle the current user's like on a Signal post.

    - First call  → creates a SignalLike, awards +2 chops to the user,
                     increments signal.like_count.
    - Second call → removes the SignalLike, deducts 2 chops,
                     decrements signal.like_count.

    Returns the new like state and the user's updated total_chops.
    """
    signal = db.query(Signal).filter(
        Signal.id     == signal_id,
        Signal.status == "published",
    ).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    existing_like = db.query(SignalLike).filter(
        SignalLike.signal_id == signal_id,
        SignalLike.user_id   == current_user.id,
    ).first()

    if existing_like:
        # ── Unlike ────────────────────────────────────────────────────────────
        chops_to_deduct = existing_like.chops_awarded

        db.delete(existing_like)

        signal.like_count              = max(0, (signal.like_count or 0) - 1)
        current_user.total_chops       = max(0, (current_user.total_chops or 0) - chops_to_deduct)
        current_user.signal_like_chops = max(0, (current_user.signal_like_chops or 0) - chops_to_deduct)

        db.commit()

        logger.info(
            f"Signal unliked: signal={signal_id} user={current_user.id} "
            f"chops_deducted={chops_to_deduct}"
        )
        return {
            "status":       "success",
            "liked":        False,
            "like_count":   signal.like_count,
            "total_chops":  current_user.total_chops,
            "message":      "Like removed.",
        }

    else:
        # ── Like ──────────────────────────────────────────────────────────────
        new_like = SignalLike(
            user_id       = current_user.id,
            signal_id     = signal_id,
            chops_awarded = LIKE_CHOPS,
        )
        db.add(new_like)

        signal.like_count              = (signal.like_count or 0) + 1
        current_user.total_chops       = (current_user.total_chops or 0) + LIKE_CHOPS
        current_user.signal_like_chops = (current_user.signal_like_chops or 0) + LIKE_CHOPS

        db.commit()

        logger.info(
            f"Signal liked: signal={signal_id} user={current_user.id} "
            f"chops_awarded={LIKE_CHOPS}"
        )
        return {
            "status":       "success",
            "liked":        True,
            "like_count":   signal.like_count,
            "total_chops":  current_user.total_chops,
            "message":      f"Signal liked! +{LIKE_CHOPS} chops awarded.",
        }


# ---------------------------------------------------------------------------
# Comment endpoints  (authenticated users)
# ---------------------------------------------------------------------------

@router.get("/{signal_id}/comments")
async def list_comments(
    signal_id: int,
    response:  Response,
    page:      int = Query(default=1, ge=1),
    limit:     int = Query(default=20, ge=1, le=100),
    db:        Session = Depends(get_db),
):
    """
    Return a paginated list of top-level comments for a Signal post,
    each carrying its first-level replies inline.

    Soft-deleted comments are included so thread structure is preserved,
    but their content is replaced with "[deleted]" by the formatter.
    """
    response.headers["Cache-Control"] = "no-store"

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    # Fetch only top-level comments; replies are loaded via relationship.
    # joinedload both `user` (this comment's author) and `replies.user`
    # (each reply's author) up front — without this, SQLAlchemy lazy-loads
    # the author separately for every comment AND every reply individually,
    # turning a single request into 1 + 2*N queries on a busy thread.
    query = db.query(SignalComment).options(
        joinedload(SignalComment.user),
        joinedload(SignalComment.replies).joinedload(SignalComment.user),
    ).filter(
        SignalComment.signal_id         == signal_id,
        SignalComment.parent_comment_id == None,   # noqa: E711 — SQLAlchemy requires `==`
    )

    total    = query.count()
    comments = (
        query
        .order_by(SignalComment.created_at.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "comments":    [_format_comment(c) for c in comments],
        "total":       total,
        "page":        page,
        "limit":       limit,
        "total_pages": (total + limit - 1) // limit,
    }


@router.post("/{signal_id}/comments", status_code=201)
async def create_comment(
    signal_id:    int,
    payload:      SignalCommentCreate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Post a comment on a Signal.

    - Any authenticated user may comment.
    - Supports one level of threading via parent_comment_id.
    - Awards +5 chops to the commenter on creation.
    - Increments signal.comment_count.
    """
    signal = db.query(Signal).filter(
        Signal.id     == signal_id,
        Signal.status == "published",
    ).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    if not payload.content or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Comment content cannot be empty.")

    # Validate parent comment belongs to the same signal
    if payload.parent_comment_id is not None:
        parent = db.query(SignalComment).filter(
            SignalComment.id        == payload.parent_comment_id,
            SignalComment.signal_id == signal_id,
            SignalComment.is_deleted == False,   # noqa: E712
        ).first()
        if not parent:
            raise HTTPException(
                status_code=404,
                detail="Parent comment not found on this Signal.",
            )

    comment = SignalComment(
        signal_id         = signal_id,
        user_id           = current_user.id,
        parent_comment_id = payload.parent_comment_id,
        content           = payload.content.strip(),
        chops_awarded     = COMMENT_CHOPS,
    )
    db.add(comment)

    # Award chops and update counters
    signal.comment_count               = (signal.comment_count or 0) + 1
    current_user.total_chops           = (current_user.total_chops or 0) + COMMENT_CHOPS
    current_user.signal_comment_chops  = (current_user.signal_comment_chops or 0) + COMMENT_CHOPS

    db.commit()
    db.refresh(comment)

    logger.info(
        f"Comment created: id={comment.id} signal={signal_id} "
        f"user={current_user.id} chops_awarded={COMMENT_CHOPS}"
    )
    return {
        **_format_comment(comment),
        "total_chops": current_user.total_chops,
        "message":     f"Comment posted! +{COMMENT_CHOPS} chops awarded.",
    }


@router.patch("/{signal_id}/comments/{comment_id}")
async def update_comment(
    signal_id:    int,
    comment_id:   int,
    payload:      SignalCommentUpdate,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Edit the content of an existing comment.

    - Only the comment's author may edit it.
    - is_edited and edited_at are updated automatically.
    - Deleted comments cannot be edited.
    """
    comment = db.query(SignalComment).filter(
        SignalComment.id        == comment_id,
        SignalComment.signal_id == signal_id,
    ).first()

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found.")

    if comment.is_deleted:
        raise HTTPException(status_code=400, detail="Cannot edit a deleted comment.")

    if comment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only edit your own comments.")

    if not payload.content or not payload.content.strip():
        raise HTTPException(status_code=400, detail="Comment content cannot be empty.")

    comment.content   = payload.content.strip()
    comment.is_edited = True
    comment.edited_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(comment)

    return _format_comment(comment)


@router.delete("/{signal_id}/comments/{comment_id}", status_code=200)
async def delete_comment(
    signal_id:    int,
    comment_id:   int,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Soft-delete a comment.

    - Authors may delete their own comments.
    - Moderators and admins may delete any comment.
    - The record is preserved (is_deleted=True, content="[deleted]") so that
      threaded replies are not orphaned.
    - Chops awarded at comment-time are deducted from the author's balance.
    - signal.comment_count is decremented.
    """
    comment = db.query(SignalComment).filter(
        SignalComment.id        == comment_id,
        SignalComment.signal_id == signal_id,
    ).first()

    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found.")

    if comment.is_deleted:
        raise HTTPException(status_code=400, detail="Comment is already deleted.")

    is_own_comment = comment.user_id == current_user.id
    is_moderator   = current_user.role == UserRole.MODERATOR.value
    if not is_own_comment and not is_moderator and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to delete this comment.",
        )

    # Retrieve the comment author to deduct chops (may differ from current_user)
    comment_author = db.query(User).filter(User.id == comment.user_id).first()
    chops_to_deduct = comment.chops_awarded or COMMENT_CHOPS

    # Soft-delete: preserve thread shape, clear sensitive content
    comment.is_deleted = True
    comment.content    = "[deleted]"

    # Deduct chops from the original commenter
    if comment_author:
        comment_author.total_chops           = max(0, (comment_author.total_chops or 0) - chops_to_deduct)
        comment_author.signal_comment_chops  = max(0, (comment_author.signal_comment_chops or 0) - chops_to_deduct)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if signal:
        signal.comment_count = max(0, (signal.comment_count or 0) - 1)

    db.commit()

    logger.info(
        f"Comment soft-deleted: id={comment_id} signal={signal_id} "
        f"deleted_by={current_user.id} chops_deducted={chops_to_deduct}"
    )
    return {
        "status":  "success",
        "message": "Comment deleted.",
    }


# ---------------------------------------------------------------------------
# Moderator / admin management endpoints
# ---------------------------------------------------------------------------

@router.get("/manage/all")
async def list_all_signals_for_moderator(
    response: Response,
    page:   int           = Query(default=1, ge=1),
    limit:  int           = Query(default=10, ge=1, le=50),
    status: Optional[str] = Query(default=None),
    current_user: User    = Depends(get_current_user),
    db:     Session       = Depends(get_db),
):
    """
    Return all Signals regardless of status — for the moderator dashboard.

    A moderator sees only their own posts across all statuses.
    An admin sees every post across all statuses.
    """
    response.headers["Cache-Control"] = "no-store"

    _require_moderator(current_user)

    query = db.query(Signal).options(joinedload(Signal.author))

    if not current_user.is_admin:
        # Moderators see only their own signals
        query = query.filter(Signal.author_id == current_user.id)

    if status and status in VALID_STATUSES:
        query = query.filter(Signal.status == status)

    total   = query.count()
    signals = (
        query
        .order_by(desc(Signal.created_at))
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )

    return {
        "signals":     [_format_signal(s, current_user_id=current_user.id, db=db) for s in signals],
        "total":       total,
        "page":        page,
        "limit":       limit,
        "total_pages": (total + limit - 1) // limit,
    }


@router.get("/manage/{signal_id}")
async def get_signal_for_edit(
    signal_id:    int,
    response:     Response,
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Return a single Signal by id, regardless of status — for the editor.

    Unlike the public GET /{slug} endpoint, this works for drafts and
    archived posts (which have no meaningful public route) and is scoped
    to the requester:
      - Authors may fetch their own post in any status.
      - Admins may fetch any post.
      - Moderators requesting another author's post get 403, matching the
        same ownership rule enforced on update/delete.

    Registered before /manage/{signal_id}/feature and /pin below is fine —
    FastAPI dispatches by path template, and those routes have an extra
    path segment, so there's no ambiguity with this one.
    """
    response.headers["Cache-Control"] = "no-store"

    _require_moderator(current_user)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    if signal.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You can only edit your own Signal posts.",
        )

    return _format_signal(signal, current_user_id=current_user.id, db=db)


@router.patch("/manage/{signal_id}/feature")
async def toggle_featured(
    signal_id:    int,
    replace_id:   Optional[int] = Query(
        default=None,
        description="If the featured cap is already reached, the id of a "
                    "currently-featured post to unfeature in the same "
                    "request as this one is featured.",
    ),
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Toggle the is_featured flag on a Signal.

    - Moderators may feature/unfeature their own posts; admins may do so
      for any post — matching the ownership rule on update/delete.
    - Unfeaturing always succeeds.
    - Featuring while MAX_FEATURED posts are already featured returns
      HTTP 409 with the current featured list, unless `replace_id` is
      supplied to swap one out atomically.
    """
    _require_moderator(current_user)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    if signal.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You can only feature your own Signal posts.",
        )

    want_featured = not signal.is_featured
    _apply_capped_flag_change(
        signal, "is_featured", want_featured, db,
        max_count=MAX_FEATURED, noun="featured", verb="feature", replace_id=replace_id,
    )
    db.commit()
    db.refresh(signal)

    state = "featured" if signal.is_featured else "unfeatured"
    logger.info(f"Signal {state}: id={signal.id} by user={current_user.email}")
    return {
        "status":      "success",
        "is_featured": signal.is_featured,
        "message":     f"Signal has been {state}.",
    }


@router.patch("/manage/{signal_id}/pin")
async def toggle_pinned(
    signal_id:    int,
    replace_id:   Optional[int] = Query(
        default=None,
        description="If a post is already pinned, its id — to unpin it in "
                    "the same request as this one is pinned.",
    ),
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Toggle the is_pinned flag on a Signal — the single "hero slot" shown
    at the top of the /blog homepage.

    - Moderators may pin/unpin their own posts; admins may do so for any
      post — matching the ownership rule on update/delete. (Previously
      admin-only; opened up to moderators as part of this feature.)
    - Unpinning always succeeds.
    - Pinning while a post is already pinned (MAX_PINNED = 1, so this is
      effectively "any time another post holds the slot") returns HTTP 409
      with that post's info, unless `replace_id` confirms swapping it out.
    """
    _require_moderator(current_user)

    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    if signal.author_id != current_user.id and not current_user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="You can only pin your own Signal posts.",
        )

    want_pinned = not signal.is_pinned
    _apply_capped_flag_change(
        signal, "is_pinned", want_pinned, db,
        max_count=MAX_PINNED, noun="pinned", verb="pin", replace_id=replace_id,
    )
    db.commit()
    db.refresh(signal)

    state = "pinned" if signal.is_pinned else "unpinned"
    logger.info(f"Signal {state}: id={signal.id} by user={current_user.email}")
    return {
        "status":    "success",
        "is_pinned": signal.is_pinned,
        "message":   f"Signal has been {state}.",
    }