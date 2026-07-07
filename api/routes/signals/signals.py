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

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
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
from api.routes.dependencies import get_current_user, get_current_user_optional, admin_required

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["signals"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

LIKE_CHOPS    = 2
COMMENT_CHOPS = 5

VALID_STATUSES = {"draft", "published", "archived"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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

    return {
        "id":              signal.id,
        "title":           signal.title,
        "slug":            signal.slug,
        "excerpt":         signal.excerpt,
        "content":         signal.content,
        "cover_image_url": signal.cover_image_url,
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
    page:     int           = Query(default=1, ge=1),
    limit:    int           = Query(default=10, ge=1, le=50),
    category: Optional[str] = Query(default=None),
    tag:      Optional[str] = Query(default=None),
    featured: Optional[bool]= Query(default=None),
    search:   Optional[str] = Query(default=None),
    db:       Session       = Depends(get_db),
):
    """
    Return a paginated list of published Signal posts.

    Supports filtering by category, tag, featured flag, and a keyword
    search across title and excerpt. Results are ordered by pinned status
    (pinned posts appear first), then by published_at descending.
    """
    query = db.query(Signal).filter(Signal.status == "published")

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


@router.get("/{slug}")
async def get_signal(
    slug: str,
    db:   Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_current_user_optional),
):
    """
    Return a single published Signal by its URL slug.
    Increments the view counter on every successful request.

    Uses optional auth (get_current_user_optional) so:
      - a logged-in visitor gets an accurate `has_liked` on their own likes
      - a logged-out visitor still gets the full post, just with
        `has_liked: false` always (there's no session to check against)
    """
    signal = db.query(Signal).filter(
        Signal.slug   == slug,
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

    if payload.title is not None:
        base_slug    = _generate_slug(payload.title)
        signal.slug  = _unique_slug(base_slug, db, exclude_id=signal_id)
        signal.title = payload.title

    if payload.content is not None:
        signal.content    = payload.content
        signal.read_time  = _estimate_read_time(payload.content)

    if payload.excerpt         is not None: signal.excerpt         = payload.excerpt
    if payload.cover_image_url is not None: signal.cover_image_url = payload.cover_image_url
    if payload.category        is not None: signal.category        = payload.category
    if payload.tags            is not None: signal.tags            = payload.tags
    if payload.is_featured     is not None: signal.is_featured     = payload.is_featured
    if payload.is_pinned       is not None: signal.is_pinned       = payload.is_pinned

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
    current_user: User    = Depends(get_current_user),
    db:           Session = Depends(get_db),
):
    """
    Archive (soft-delete) a Signal post by setting its status to "archived".

    - Authors may archive their own posts.
    - Admins may archive any post.
    - The record and all engagement data are preserved; the post simply
      disappears from the public list endpoint.
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
    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    # Fetch only top-level comments; replies are loaded via relationship
    query = db.query(SignalComment).filter(
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
    _require_moderator(current_user)

    query = db.query(Signal)

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
    current_user: User    = Depends(admin_required),
    db:           Session = Depends(get_db),
):
    """
    Toggle the is_featured flag on any Signal.
    Admin only — used to surface curated content on the homepage.
    """
    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    signal.is_featured = not signal.is_featured
    db.commit()

    state = "featured" if signal.is_featured else "unfeatured"
    return {
        "status":      "success",
        "is_featured": signal.is_featured,
        "message":     f"Signal has been {state}.",
    }


@router.patch("/manage/{signal_id}/pin")
async def toggle_pinned(
    signal_id:    int,
    current_user: User    = Depends(admin_required),
    db:           Session = Depends(get_db),
):
    """
    Toggle the is_pinned flag on any Signal.
    Admin only — pinned posts appear above the feed regardless of date.
    """
    signal = db.query(Signal).filter(Signal.id == signal_id).first()
    if not signal:
        raise HTTPException(status_code=404, detail="Signal not found.")

    signal.is_pinned = not signal.is_pinned
    db.commit()

    state = "pinned" if signal.is_pinned else "unpinned"
    return {
        "status":    "success",
        "is_pinned": signal.is_pinned,
        "message":   f"Signal has been {state}.",
    }