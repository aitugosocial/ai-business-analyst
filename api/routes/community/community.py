"""
Community Feature — Channels, Discussions, Events, Leaderboard, Saved Items
"""
import logging
import re
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from database.pg_connections import get_db
from database.pg_models import (
    User,
    CommunityChannel, ChannelMember,
    CommunityDiscussion, DiscussionReply, DiscussionLike,
    CommunityEvent, EventRegistration,
    CommunityActivity, SavedItem,
    UserSettings, BusinessAnalysis,
    UserNotification,
)
from api.routes.auth.login import get_current_user
from api.routes.user.missions import _flatten_roadmap_tasks
from api.cache import get_cached, set_cached, delete_cached

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/community", tags=["community"])


def _log_activity(db: Session, user_id: int, action_type: str, target_id: Optional[int] = None, target_type: Optional[str] = None, target_name: Optional[str] = None):
    try:
        db.add(CommunityActivity(user_id=user_id, action_type=action_type, target_id=target_id, target_type=target_type, target_name=target_name))
        db.commit()
    except Exception:
        db.rollback()


def _channel_dict(ch: CommunityChannel, joined_ids: Optional[set] = None) -> dict:
    is_member = ch.id in joined_ids if joined_ids is not None else False
    return {
        "id": ch.id, "name": ch.name, "slug": ch.slug, "description": ch.description,
        "category": ch.category, "member_count": ch.member_count,
        "members": ch.member_count,  # frontend alias
        "active": True,
        "isJoined": is_member, "is_member": is_member,
        "post_count": ch.post_count,
        "icon": ch.icon, "is_public": ch.is_public,
        "created_at": ch.created_at.isoformat() if ch.created_at else None,
    }


class GiftChopsRequest(BaseModel):
    amount: int


_AUTHOR_GRADIENTS = [
    "from-orange-400 to-rose-400",
    "from-blue-400 to-cyan-500",
    "from-green-400 to-teal-500",
    "from-violet-400 to-purple-600",
    "from-amber-400 to-orange-500",
    "from-pink-400 to-rose-500",
]

# All valid topic slugs that can be stored as post_type
_TOPIC_SLUGS = {
    'what-worked', 'tool-recommendations', 'collaboration-offers',
    'questions', 'shared-wins', 'resources', 'reflections',
}


def _discussion_dict(d: CommunityDiscussion, liked_ids: Optional[set] = None, include_quoted: bool = True) -> dict:
    has_liked = d.id in liked_ids if liked_ids is not None else False
    post_type_val = getattr(d, 'post_type', None) or 'discussion'

    # Return topic slug as a plain string so the frontend can filter by it directly.
    # Generic 'discussion' posts fall back to the actual backend channel object.
    if post_type_val == 'reflection':
        channel_display: any = 'reflections'
    elif post_type_val in _TOPIC_SLUGS:
        channel_display = post_type_val
    else:
        channel_display = {"id": d.channel.id, "name": d.channel.name, "slug": d.channel.slug} if d.channel else None

    author_obj = None
    if d.user:
        name = d.user.name or "Member"
        author_obj = {
            "id": d.user.id,
            "name": name,
            "initials": name[:2].upper(),
            "gradient": _AUTHOR_GRADIENTS[d.user.id % len(_AUTHOR_GRADIENTS)],
            "role": getattr(d.user, 'role', '') or '',
            "total_chops": d.user.total_chops or 0,
        }

    quoted_dict = None
    if include_quoted and getattr(d, 'quoted_discussion', None) is not None:
        try:
            quoted_dict = _discussion_dict(d.quoted_discussion, liked_ids=liked_ids, include_quoted=False)
        except Exception:
            quoted_dict = None

    spice_cnt = getattr(d, 'spice_count', 0) or 0

    return {
        "id": d.id, "channel_id": d.channel_id, "title": d.title, "content": d.content,
        "tags": d.tags or [], "like_count": d.like_count, "reply_count": d.reply_count,
        "likes": d.like_count, "replies": d.reply_count,
        "pinned": d.is_pinned, "is_pinned": d.is_pinned,
        "hot": d.like_count >= 10,
        "view_count": d.view_count, "has_liked": has_liked, "liked_by_user": has_liked,
        "chops_gifted": d.chops_gifted or 0,
        "spice_count": spice_cnt, "spiced": spice_cnt, "spices": spice_cnt,
        "quoted_discussion_id": getattr(d, 'quoted_discussion_id', None),
        "quoted_discussion": quoted_dict,
        "type": post_type_val,
        "author": author_obj,
        "channel": channel_display,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


EVENT_TYPE_MAP = {"live": "Live", "workshop": "Workshop", "webinar": "Webinar"}

def _event_dict(ev: CommunityEvent, registered_ids: Optional[set] = None) -> dict:
    is_registered = ev.id in registered_ids if registered_ids is not None else False
    formatted_date = ev.scheduled_at.strftime("%b %d, %Y") if ev.scheduled_at else None
    formatted_time = ev.scheduled_at.strftime("%I:%M %p") if ev.scheduled_at else None
    frontend_type = EVENT_TYPE_MAP.get((ev.event_type or "").lower(), ev.event_type or "Live")
    return {
        "id": ev.id, "title": ev.title, "description": ev.description,
        "event_type": ev.event_type, "type": frontend_type,
        "date": formatted_date, "time": formatted_time,
        "attendees": ev.attendee_count, "attendee_count": ev.attendee_count,
        "registered": is_registered, "is_registered": is_registered,
        "scheduled_at": ev.scheduled_at.isoformat() if ev.scheduled_at else None,
        "duration_minutes": ev.duration_minutes, "max_attendees": ev.max_attendees,
        "host_name": ev.host_name, "meeting_link": ev.meeting_link,
        "created_at": ev.created_at.isoformat() if ev.created_at else None,
    }


# ─── Content moderation ─────────────────────────────────────────────────────

_DEROGATORY_TERMS = {
    "nigger", "nigga", "faggot", "fag", "retard", "retarded", "cunt",
    "kike", "spic", "chink", "gook", "wetback", "tranny", "dyke",
    "whore", "slut", "bitch", "bastard", "asshole", "motherfucker",
    "fuck you", "go fuck", "piece of shit", "piece of crap",
}

def _contains_derogatory_content(text: str) -> bool:
    lowered = text.lower()
    for term in _DEROGATORY_TERMS:
        pattern = r'\b' + re.escape(term) + r'\b'
        if re.search(pattern, lowered):
            return True
    return False


# ─── Pydantic Models ────────────────────────────────────────────────────────

class CreateDiscussionRequest(BaseModel):
    title: str
    content: str
    channel_id: int
    tags: Optional[List[str]] = None
    type: Optional[str] = "discussion"  # "discussion" | "reflection" | "spiced"


class SpiceDiscussionRequest(BaseModel):
    content: str


class CreateReplyRequest(BaseModel):
    content: str
    parent_reply_id: Optional[int] = None


class LikeReplyRequest(BaseModel):
    action: str  # "like" | "unlike"


class SaveItemRequest(BaseModel):
    item_id: int
    item_type: str


# ─── Stats ──────────────────────────────────────────────────────────────────

@router.get("/stats")
async def get_community_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        total_channels = db.query(CommunityChannel).count()
        total_discussions = db.query(CommunityDiscussion).count()
        total_events = db.query(CommunityEvent).filter_by(is_published=True).count()
        total_members = db.query(User).count()
        user_channels = db.query(ChannelMember).filter_by(user_id=current_user.id).count()

        return {"success": True, "data": {
            "total_channels": total_channels,
            "total_discussions": total_discussions,
            "total_events": total_events,
            "total_members": total_members,
            "my_channels": user_channels,
        }}
    except Exception as e:
        logger.error(f"Community stats error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch community stats")


# ─── Channels ───────────────────────────────────────────────────────────────

@router.get("/channels/my")
async def get_my_channels(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        memberships = db.query(ChannelMember).filter_by(user_id=current_user.id).all()
        joined = {m.channel_id for m in memberships}
        channels = [_channel_dict(m.channel, joined) for m in memberships if m.channel]
        return {"success": True, "data": channels}
    except Exception as e:
        logger.error(f"Get my channels error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch your channels")


@router.get("/channels/{channel_name}")
async def get_channel_by_name(
    channel_name: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ch = db.query(CommunityChannel).filter_by(slug=channel_name).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    joined = {m.channel_id for m in db.query(ChannelMember.channel_id).filter_by(user_id=current_user.id).all()}
    return {"success": True, "data": _channel_dict(ch, joined)}


@router.get("/channels")
async def get_channels(
    category: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        valid_slugs = list(_TOPIC_SLUGS)
        q = db.query(CommunityChannel).filter(CommunityChannel.slug.in_(valid_slugs))
        if category and category.lower() != "all":
            q = q.filter(CommunityChannel.category == category)
        channels = q.order_by(CommunityChannel.member_count.desc()).all()
        # Batch-fetch memberships to avoid N+1
        joined = {m.channel_id for m in db.query(ChannelMember.channel_id).filter_by(user_id=current_user.id).all()}
        return {"success": True, "data": [_channel_dict(ch, joined) for ch in channels]}
    except Exception as e:
        logger.error(f"Get channels error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch channels")


@router.post("/channels/{channel_id}/join")
async def join_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ch = db.query(CommunityChannel).filter_by(id=channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    existing = db.query(ChannelMember).filter_by(user_id=current_user.id, channel_id=channel_id).first()
    if existing:
        return {"success": True, "message": "Already a member"}

    db.add(ChannelMember(user_id=current_user.id, channel_id=channel_id))
    ch.member_count = (ch.member_count or 0) + 1
    db.commit()
    _log_activity(db, current_user.id, "joined_channel", channel_id, "channel", ch.name)
    return {"success": True, "message": f"Joined {ch.name}"}


@router.post("/channels/{channel_id}/leave")
async def leave_channel(
    channel_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    membership = db.query(ChannelMember).filter_by(user_id=current_user.id, channel_id=channel_id).first()
    if not membership:
        raise HTTPException(status_code=404, detail="Not a member")

    ch = db.query(CommunityChannel).filter_by(id=channel_id).first()
    db.delete(membership)
    if ch and ch.member_count > 0:
        ch.member_count -= 1
    db.commit()
    return {"success": True, "message": "Left channel"}


# ─── Discussions ─────────────────────────────────────────────────────────────

def _summarize_mission_title(text: str, max_chars: int = 80) -> str:
    """Word-bounded summary of a mission's full task text for the reflection
    post card's title slot — caps length like the old `[:80]` slice did, but
    never cuts a word in half."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    words = text.split()
    out, length = [], 0
    for w in words:
        add = len(w) + (1 if out else 0)
        if length + add > max_chars - 1:  # leave room for the ellipsis
            break
        out.append(w)
        length += add
    return (' '.join(out) + '…') if out else text[:max_chars - 1] + '…'


@router.get("/discussions")
async def get_discussions(
    channel_id: Optional[int] = Query(None),
    limit: int = Query(20, le=100),
    offset: int = Query(0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        cache_key = f"community:discussions:user:{current_user.id}:ch:{channel_id}:lim:{limit}:off:{offset}"
        cached = await get_cached(cache_key)
        if cached is not None:
            return cached

        q = db.query(CommunityDiscussion)
        if channel_id:
            q = q.filter_by(channel_id=channel_id)
        discussions = q.order_by(CommunityDiscussion.is_pinned.desc(), CommunityDiscussion.created_at.desc()).offset(offset).limit(limit).all()
        # Batch-fetch likes to avoid N+1
        discussion_ids = [d.id for d in discussions]
        liked = {l.discussion_id for l in db.query(DiscussionLike.discussion_id).filter(
            DiscussionLike.user_id == current_user.id,
            DiscussionLike.discussion_id.in_(discussion_ids)
        ).all()} if discussion_ids else set()
        result = [_discussion_dict(d, liked) for d in discussions]

        # Include mission roadmap comments from users who opted in.
        # Comments live in analysis.user_progress['roadmap_comments'] (a dict of task_id → [comment, ...]).
        try:
            opted_in_user_ids = db.query(UserSettings.user_id).filter(
                UserSettings.show_mission_comments_in_community == True
            ).all()
            opted_in_ids = [row[0] for row in opted_in_user_ids]
            if opted_in_ids:
                analyses = (
                    db.query(BusinessAnalysis, User)
                    .join(User, BusinessAnalysis.user_id == User.id)
                    .filter(BusinessAnalysis.user_id.in_(opted_in_ids))
                    .all()
                )
                any_summaries_changed = False
                for analysis, author in analyses:
                    up = analysis.user_progress or {}
                    roadmap_comments = up.get('roadmap_comments', {}) if isinstance(up, dict) else {}
                    if not isinstance(roadmap_comments, dict):
                        continue
                    # Map each task's frontend_id → its mission text, so the
                    # reflection post title is the mission it was submitted
                    # under, not the reflection body itself.
                    mission_titles = {
                        t['frontend_id']: t['text'] for t in _flatten_roadmap_tasks(analysis)
                    }
                    # Full mission text is often too long for the post card's
                    # title (wraps/overflows). Word-capped summaries are
                    # computed once per task and persisted in their own column
                    # (business_analyses.roadmap_task_summaries) so they're
                    # reused on every later fetch instead of being
                    # re-summarized — and re-truncated — on every request.
                    task_summaries = analysis.roadmap_task_summaries
                    if not isinstance(task_summaries, dict):
                        task_summaries = {}
                    analysis_summaries_changed = False
                    for task_id, comments in roadmap_comments.items():
                        if not isinstance(comments, list):
                            continue
                        for comment in comments:
                            text = comment.get('text', '').strip()
                            if not text:
                                continue
                            created_at = comment.get('createdAt')
                            comment_id = comment.get('id', f"rc_{analysis.id}_{task_id}")
                            mission_title = task_summaries.get(task_id)
                            if mission_title is None:
                                full_title = mission_titles.get(task_id)
                                if full_title:
                                    mission_title = _summarize_mission_title(full_title)
                                    task_summaries[task_id] = mission_title
                                    any_summaries_changed = True
                                    analysis_summaries_changed = True
                            result.append({
                                "id": f"rc_{comment_id}",
                                "type": "reflection",
                                "title": mission_title if mission_title else text[:80],
                                "content": text,
                                "excerpt": text[:160],
                                "tags": [],
                                "like_count": 0, "reply_count": 0,
                                "likes": 0, "replies": 0,
                                "pinned": False, "is_pinned": False,
                                "hot": False,
                                "view_count": 0,
                                "has_liked": False, "liked_by_user": False,
                                "chops_gifted": 0,
                                "author": {
                                    "id": author.id,
                                    "name": author.name or "Member",
                                    "initials": (author.name or "M")[:2].upper(),
                                    "gradient": "from-orange-400 to-rose-400",
                                    "role": "",
                                    "total_chops": author.total_chops or 0,
                                },
                                "channel": "reflections",
                                "created_at": created_at,
                                "timeAgo": created_at,
                                "updated_at": None,
                            })
                    if analysis_summaries_changed:
                        analysis.roadmap_task_summaries = task_summaries
                if any_summaries_changed:
                    db.commit()
        except Exception as reflection_err:
            logger.warning(f"Mission reflections fetch error: {reflection_err}")

        response = {"success": True, "data": result}
        await set_cached(cache_key, response, ttl_seconds=15)
        return response
    except Exception as e:
        logger.error(f"Get discussions error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch discussions")


@router.get("/discussions/{discussion_id}")
async def get_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")
    d.view_count = (d.view_count or 0) + 1
    db.commit()

    def _serialise_reply(r) -> dict:
        return {
            "id": r.id,
            "content": r.content,
            "like_count": r.like_count,
            "parent_reply_id": r.parent_reply_id,
            "author": {
                "id": r.user.id,
                "name": r.user.name,
                "total_chops": r.user.total_chops or 0,
            } if r.user else None,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "sub_replies": [_serialise_reply(sr) for sr in (r.sub_replies or [])],
        }

    # Only return top-level replies; sub_replies are nested inside each
    top_level = [r for r in d.replies if r.parent_reply_id is None]
    replies = [_serialise_reply(r) for r in top_level]

    liked = {d.id} if db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=d.id).first() else set()
    data = _discussion_dict(d, liked)
    data["replies"] = replies
    return {"success": True, "data": data}


@router.post("/discussions")
async def create_discussion(
    body: CreateDiscussionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if _contains_derogatory_content(body.title) or _contains_derogatory_content(body.content):
        raise HTTPException(
            status_code=422,
            detail="Your post contains language that isn't allowed in the Lavoo Build Room. Please review your content and try again."
        )

    ch = db.query(CommunityChannel).filter_by(id=body.channel_id).first()
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")

    _all_valid_types = {'discussion', 'reflection'} | _TOPIC_SLUGS
    raw_type = (body.type or 'discussion').strip().lower()
    post_type = raw_type if raw_type in _all_valid_types else 'discussion'
    d = CommunityDiscussion(
        channel_id=body.channel_id, user_id=current_user.id,
        title=body.title.strip(), content=body.content.strip(),
        tags=body.tags or [], post_type=post_type,
    )
    db.add(d)
    ch.post_count = (ch.post_count or 0) + 1
    current_user.total_chops = (current_user.total_chops or 0) + 10
    db.commit()
    db.refresh(d)
    _log_activity(db, current_user.id, "posted", d.id, "discussion", d.title)
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:{body.channel_id}:lim:20:off:0")
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:None:lim:20:off:0")
    return {"success": True, "data": _discussion_dict(d, set())}


@router.delete("/discussions/{discussion_id}")
async def delete_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Post not found")
    if d.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own posts")
    ch = db.query(CommunityChannel).filter_by(id=d.channel_id).first()
    if ch and ch.post_count > 0:
        ch.post_count -= 1
    db.delete(d)
    db.commit()
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:{d.channel_id}:lim:20:off:0")
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:None:lim:20:off:0")
    return {"success": True}


@router.post("/discussions/{discussion_id}/pin")
async def pin_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Post not found")
    # Anyone can pin/unpin any post
    d.is_pinned = not d.is_pinned
    db.commit()
    return {"success": True, "pinned": d.is_pinned}


@router.post("/discussions/{discussion_id}/like")
async def like_discussion(
    discussion_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    existing = db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=discussion_id).first()
    if existing:
        # Unlike
        db.delete(existing)
        d.like_count = max(0, (d.like_count or 0) - 1)
        db.commit()
        return {"success": True, "liked": False, "like_count": d.like_count}

    db.add(DiscussionLike(user_id=current_user.id, discussion_id=discussion_id))
    d.like_count = (d.like_count or 0) + 1
    db.commit()

    # Notify post owner (skip when liking own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_cooked",
                title=f"{current_user.name or 'Someone'} cooked your post",
                message=f"{current_user.name or 'Someone'} liked your post '{d.title[:60]}'",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Cooked notification creation failed: {notif_err}")

    return {"success": True, "liked": True, "like_count": d.like_count}


@router.post("/discussions/{discussion_id}/gift_chops")
async def gift_chops_to_discussion(
    discussion_id: int,
    body: GiftChopsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if (current_user.total_chops or 0) < body.amount:
        raise HTTPException(status_code=400, detail="Insufficient chops")

    current_user.total_chops = (current_user.total_chops or 0) - body.amount

    if d.user_id and d.user_id != current_user.id:
        author = db.query(User).filter_by(id=d.user_id).first()
        if author:
            author.total_chops = (author.total_chops or 0) + body.amount

    d.chops_gifted = (d.chops_gifted or 0) + body.amount
    db.commit()

    # Notify post owner (skip when gifting self)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_chops",
                title=f"{current_user.name or 'Someone'} gifted you {body.amount} Chops!",
                message=f"You received {body.amount} Chops for your post '{d.title[:60]}'",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Gift chops notification creation failed: {notif_err}")

    return {"success": True, "chops_gifted": d.chops_gifted, "remaining_chops": current_user.total_chops}


@router.post("/discussions/{discussion_id}/spice")
async def spice_discussion(
    discussion_id: int,
    body: SpiceDiscussionRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    content_text = (body.content or "").strip()
    if not content_text:
        raise HTTPException(status_code=400, detail="Spice insight content cannot be empty")

    if _contains_derogatory_content(content_text):
        raise HTTPException(
            status_code=422,
            detail="Your spice contains language that isn't allowed in the Lavoo Build Room. Please review your content and try again."
        )

    # Increment spice count on original discussion
    d.spice_count = (d.spice_count or 0) + 1

    # Create new discussion referencing original post as a quoted post
    spiced_post = CommunityDiscussion(
        channel_id=d.channel_id,
        user_id=current_user.id,
        title=f"Spiced: {d.title[:80]}",
        content=content_text,
        tags=d.tags or [],
        post_type="spiced",
        quoted_discussion_id=d.id,
    )
    db.add(spiced_post)

    # Award user chops for spicing / contributing insight
    current_user.total_chops = (current_user.total_chops or 0) + 10
    db.commit()
    db.refresh(spiced_post)

    # Notify original post owner (unless spicing own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_spice",
                title=f"{current_user.name or 'Someone'} spiced your post",
                message=content_text[:120],
                link=f"/dashboard/community/post/{d.id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Spice notification creation failed: {notif_err}")

    _log_activity(db, current_user.id, "spiced", d.id, "discussion", d.title)
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:{d.channel_id}:lim:20:off:0")
    await delete_cached(f"community:discussions:user:{current_user.id}:ch:None:lim:20:off:0")

    liked = {spiced_post.id} if db.query(DiscussionLike).filter_by(user_id=current_user.id, discussion_id=spiced_post.id).first() else set()
    return {
        "success": True,
        "data": _discussion_dict(spiced_post, liked),
        "original_spice_count": d.spice_count,
    }


@router.post("/discussions/{discussion_id}/replies")
async def reply_to_discussion(
    discussion_id: int,
    body: CreateReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if not d:
        raise HTTPException(status_code=404, detail="Discussion not found")

    # Validate parent_reply_id belongs to the same discussion
    parent_reply_id = body.parent_reply_id
    if parent_reply_id is not None:
        parent = db.query(DiscussionReply).filter_by(id=parent_reply_id, discussion_id=discussion_id).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent reply not found")

    reply = DiscussionReply(
        discussion_id=discussion_id,
        user_id=current_user.id,
        content=body.content.strip(),
        parent_reply_id=parent_reply_id,
    )
    db.add(reply)
    # Count all replies (including nested) so comment badge is accurate
    d.reply_count = (d.reply_count or 0) + 1
    current_user.total_chops = (current_user.total_chops or 0) + 5
    db.commit()
    db.refresh(reply)

    # Notify post owner (skip when replying to own post)
    try:
        if d.user_id and d.user_id != current_user.id:
            notif = UserNotification(
                user_id=d.user_id,
                type="community_reply",
                title=f"{current_user.name or 'Someone'} replied to your post",
                message=body.content.strip()[:120],
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Notification creation failed: {notif_err}")

    return {"success": True, "data": {
        "id": reply.id, "content": reply.content, "like_count": 0,
        "parent_reply_id": reply.parent_reply_id,
        "author": {
            "id": current_user.id,
            "name": current_user.name,
            "total_chops": current_user.total_chops or 0,
        },
        "created_at": reply.created_at.isoformat() if reply.created_at else None,
        "sub_replies": [],
    }}


@router.post("/discussions/{discussion_id}/replies/{reply_id}/like")
async def like_reply(
    discussion_id: int,
    reply_id: int,
    body: LikeReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reply = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")
    if body.action == "like":
        reply.like_count = (reply.like_count or 0) + 1
    else:
        reply.like_count = max(0, (reply.like_count or 0) - 1)
    db.commit()
    return {"success": True, "like_count": reply.like_count}


@router.post("/discussions/{discussion_id}/replies/{reply_id}/gift_chops")
async def gift_chops_to_reply(
    discussion_id: int,
    reply_id: int,
    body: GiftChopsRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reply = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not reply:
        raise HTTPException(status_code=404, detail="Reply not found")
    if reply.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="Cannot gift chops to yourself")
    if body.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    if (current_user.total_chops or 0) < body.amount:
        raise HTTPException(status_code=400, detail="Insufficient chops")
    current_user.total_chops = (current_user.total_chops or 0) - body.amount
    author = db.query(User).filter_by(id=reply.user_id).first()
    if author:
        author.total_chops = (author.total_chops or 0) + body.amount
    db.commit()

    # Notify reply owner (skip when gifting self)
    try:
        if reply.user_id and reply.user_id != current_user.id:
            notif = UserNotification(
                user_id=reply.user_id,
                type="community_chops",
                title=f"{current_user.name or 'Someone'} gifted you {body.amount} Chops!",
                message=f"You received {body.amount} Chops for your reply",
                link=f"/dashboard/community/post/{discussion_id}",
                is_read=False,
            )
            db.add(notif)
            db.commit()
    except Exception as notif_err:
        logger.warning(f"Reply gift chops notification creation failed: {notif_err}")

    return {"success": True, "remaining_chops": current_user.total_chops}


@router.delete("/discussions/{discussion_id}/replies/{reply_id}")
async def delete_reply(
    discussion_id: int,
    reply_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    r = db.query(DiscussionReply).filter_by(id=reply_id, discussion_id=discussion_id).first()
    if not r:
        raise HTTPException(status_code=404, detail="Reply not found")
    if r.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own replies")
    d = db.query(CommunityDiscussion).filter_by(id=discussion_id).first()
    if d and (d.reply_count or 0) > 0:
        d.reply_count = d.reply_count - 1
    db.delete(r)
    db.commit()
    return {"success": True}


# ─── Events ──────────────────────────────────────────────────────────────────

@router.get("/events/my")
async def get_my_events(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    regs = db.query(EventRegistration).filter_by(user_id=current_user.id).all()
    registered = {r.event_id for r in regs}
    events = [_event_dict(r.event, registered) for r in regs if r.event]
    return {"success": True, "data": events}


@router.get("/events")
async def get_events(
    type: Optional[str] = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    try:
        q = db.query(CommunityEvent).filter_by(is_published=True)
        if type and type.lower() != "all":
            q = q.filter(CommunityEvent.event_type == type)
        events = q.order_by(CommunityEvent.scheduled_at.asc()).all()
        # Batch-fetch registrations to avoid N+1
        event_ids = [ev.id for ev in events]
        registered = {r.event_id for r in db.query(EventRegistration.event_id).filter(
            EventRegistration.user_id == current_user.id,
            EventRegistration.event_id.in_(event_ids)
        ).all()} if event_ids else set()
        return {"success": True, "data": [_event_dict(ev, registered) for ev in events]}
    except Exception as e:
        logger.error(f"Get events error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch events")


@router.post("/events/{event_id}/register")
async def register_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    ev = db.query(CommunityEvent).filter_by(id=event_id, is_published=True).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found")

    if ev.max_attendees and ev.attendee_count >= ev.max_attendees:
        raise HTTPException(status_code=400, detail="Event is fully booked")

    existing = db.query(EventRegistration).filter_by(user_id=current_user.id, event_id=event_id).first()
    if existing:
        return {"success": True, "message": "Already registered"}

    db.add(EventRegistration(user_id=current_user.id, event_id=event_id))
    ev.attendee_count = (ev.attendee_count or 0) + 1
    db.commit()
    _log_activity(db, current_user.id, "registered_event", event_id, "event", ev.title)
    return {"success": True, "message": f"Registered for {ev.title}"}


@router.post("/events/{event_id}/unregister")
async def unregister_event(
    event_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    reg = db.query(EventRegistration).filter_by(user_id=current_user.id, event_id=event_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Not registered")

    ev = db.query(CommunityEvent).filter_by(id=event_id).first()
    db.delete(reg)
    if ev and ev.attendee_count > 0:
        ev.attendee_count -= 1
    db.commit()
    return {"success": True, "message": "Unregistered from event"}


# ─── User Activity, Profile, Leaderboard ────────────────────────────────────

@router.get("/activity")
async def get_my_activity(
    limit: int = Query(20, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    activities = db.query(CommunityActivity).filter_by(user_id=current_user.id)\
        .order_by(CommunityActivity.created_at.desc()).limit(limit).all()
    return {"success": True, "data": [{
        "id": a.id, "action_type": a.action_type, "target_id": a.target_id,
        "target_type": a.target_type, "target_name": a.target_name,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in activities]}


@router.get("/profile")
async def get_my_community_profile(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    channels_joined = db.query(ChannelMember).filter_by(user_id=current_user.id).count()
    posts_count = db.query(CommunityDiscussion).filter_by(user_id=current_user.id).count()
    events_count = db.query(EventRegistration).filter_by(user_id=current_user.id).count()
    replies_count = db.query(DiscussionReply).filter_by(user_id=current_user.id).count()

    return {"success": True, "data": {
        "user_id": current_user.id,
        "name": current_user.name,
        "email": current_user.email,
        "total_chops": current_user.total_chops or 0,
        "channels_joined": channels_joined,
        "posts": posts_count,
        "replies": replies_count,
        "events_registered": events_count,
    }}


@router.get("/leaderboard")
async def get_leaderboard(
    limit: int = Query(10, le=50),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    top_users = db.query(User).filter(User.is_active == True)\
        .order_by(User.total_chops.desc(), User.created_at.asc()).limit(limit).all()

    return {"success": True, "data": [{
        "rank": idx + 1,
        "user_id": u.id,
        "name": u.name,
        "total_chops": u.total_chops or 0,
        "referral_count": u.referral_count or 0,
        "joined_at": u.created_at.isoformat() if u.created_at else None,
    } for idx, u in enumerate(top_users)]}


# ─── Saved Items ─────────────────────────────────────────────────────────────

@router.get("/saved")
async def get_saved_items(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    items = db.query(SavedItem).filter_by(user_id=current_user.id)\
        .order_by(SavedItem.created_at.desc()).all()
    return {"success": True, "data": [{
        "id": item.id, "item_id": item.item_id, "item_type": item.item_type,
        "created_at": item.created_at.isoformat() if item.created_at else None,
    } for item in items]}


@router.post("/saved")
async def save_item(
    body: SaveItemRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    existing = db.query(SavedItem).filter_by(user_id=current_user.id, item_id=body.item_id, item_type=body.item_type).first()
    if existing:
        return {"success": True, "message": "Already saved", "id": existing.id}

    saved = SavedItem(user_id=current_user.id, item_id=body.item_id, item_type=body.item_type)
    db.add(saved)
    db.commit()
    db.refresh(saved)
    return {"success": True, "data": {"id": saved.id, "item_id": saved.item_id, "item_type": saved.item_type}}


@router.delete("/saved/{item_id}")
async def unsave_item(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    saved = db.query(SavedItem).filter_by(id=item_id, user_id=current_user.id).first()
    if not saved:
        raise HTTPException(status_code=404, detail="Saved item not found")
    db.delete(saved)
    db.commit()
    return {"success": True, "message": "Item unsaved"}


@router.get("/users/{user_id}/profile")
async def get_user_profile(
    user_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user = db.query(User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    post_count = db.query(CommunityDiscussion).filter_by(user_id=user_id).count()
    return {"success": True, "data": {
        "id": user.id,
        "name": user.name or "Member",
        "email": user.email if user_id == current_user.id else None,
        "bio": getattr(user, "bio", None),
        "total_chops": user.total_chops or 0,
        "post_count": post_count,
        "joined_at": user.created_at.isoformat() if getattr(user, "created_at", None) else None,
    }}
