"""
Dynamic Missions System
Missions are dynamically generated from user's business analyses
Each action plan becomes a trackable mission
"""
import logging
from typing import List, Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
import json

def _ensure_dict(value, field_name: str = "field") -> dict:
    """Safely coerce any JSON column value to a dict.

    PostgreSQL JSON columns sometimes return raw JSON strings instead of parsed
    dicts when data was written by older code paths or direct SQL inserts.
    This guard handles: None, empty string, valid JSON string, and non-dict types.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        if not value.strip():
            return {}
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
            logger.warning(f"[Missions] {field_name} parsed to non-dict type {type(parsed).__name__}, using {{}}")
            return {}
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[Missions] {field_name} is an unparseable string: {e!r} | value[:120]: {str(value)[:120]}")
            return {}
    logger.warning(f"[Missions] {field_name} unexpected type {type(value).__name__}, using {{}}")
    return {}


def _ensure_list(value, field_name: str = "field") -> list:
    """Safely coerce any JSON column value to a list."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        if not value.strip():
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
            logger.warning(f"[Missions] {field_name} parsed to non-list type {type(parsed).__name__}, wrapping")
            return [parsed] if parsed else []
        except (json.JSONDecodeError, ValueError) as e:
            logger.error(f"[Missions] {field_name} is an unparseable string: {e!r}")
            return []
    if isinstance(value, dict):
        return [value]
    return []


def _parse_action_plans(analysis):
    if not analysis or getattr(analysis, 'action_plans', None) is None:
        return []
    plans = getattr(analysis, 'action_plans', [])
    plans = _ensure_list(plans, "action_plans")
    # Ensure every element is a dict — sometimes individual elements are JSON strings
    result = []
    for i, item in enumerate(plans):
        if isinstance(item, dict):
            result.append(item)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
                result.append(parsed if isinstance(parsed, dict) else {})
            except (json.JSONDecodeError, ValueError):
                logger.error(f"[Missions] action_plans[{i}] is an unparseable string, skipping")
                result.append({})
        else:
            logger.warning(f"[Missions] action_plans[{i}] unexpected type {type(item).__name__}, skipping")
    return result

from pydantic import BaseModel

from database.pg_connections import get_db
from database.pg_models import User, BusinessAnalysis
from api.routes.auth.login import get_current_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/missions", tags=["missions"])


class MissionStepComplete(BaseModel):
    reflection: Optional[str] = None


class MissionResponse(BaseModel):
    id: int
    analysis_id: int
    business_goal: str
    title: str
    description: str
    progress: float
    total_steps: int
    completed_steps: int
    points_reward: int
    status: str
    created_at: str
    steps: List[dict]


@router.get("")
async def get_user_missions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all missions for the current user.
    Missions are generated from their business analyses.
    Each action plan in an analysis becomes a mission step.
    """
    try:
        # Get all user's analyses
        analyses = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.user_id == current_user.id
        ).order_by(BusinessAnalysis.created_at.desc()).all()

        missions = []
        # Track which strategic priorities we've already turned into a mission.
        # analyses is ordered newest-first so we keep the latest run per goal.
        seen_priorities: set = set()

        for analysis in analyses:
            # Parse once — used three times in the original code (N+1 call smell)
            action_plans = _parse_action_plans(analysis)
            if not action_plans:
                continue

            # Deduplicate: skip missions whose strategic_priority / goal we already have.
            # Use the first 80 chars of goal as a normalised key so near-identical
            # re-runs of the same analysis don't appear as separate missions.
            priority_key = (analysis.strategic_priority or (analysis.business_goal or '')[:80]).strip().lower()
            if priority_key and priority_key in seen_priorities:
                continue
            if priority_key:
                seen_priorities.add(priority_key)

            # Get user progress from analysis metadata
            user_progress = _ensure_dict(analysis.user_progress, f"user_progress[{analysis.id}]")
            completed_steps = _ensure_list(user_progress.get('completed_actions', []), "completed_actions")
            reflections = _ensure_dict(user_progress.get('reflections', {}), "reflections")

            # Convert action plans to mission steps
            steps = []
            for idx, action in enumerate(action_plans, 1):
                step_id = f"{analysis.id}_action_{idx}"
                is_completed = step_id in completed_steps

                what_to_do = _ensure_list(
                    action.get('what_to_do') or action.get('what_to_do_steps'),
                    f"action[{idx}].what_to_do"
                )
                steps.append({
                    'id': step_id,
                    'day': idx,
                    'label': action.get('title') or action.get('action_title') or f'Action {idx}',
                    'description': what_to_do[0] if what_to_do else action.get('description', ''),
                    'done': is_completed,
                    'active': not is_completed and (idx == len(completed_steps) + 1),
                    'points': 20,
                    'effort': action.get('effort', 'MEDIUM'),
                    'reflection': reflections.get(step_id)
                })

            # Calculate progress
            total_steps = len(steps)
            completed_count = len(completed_steps)
            progress_percentage = (completed_count / total_steps * 100) if total_steps > 0 else 0

            # Determine status
            if completed_count == 0:
                mission_status = "not_started"
            elif completed_count == total_steps:
                mission_status = "completed"
            else:
                mission_status = "active"

            missions.append({
                'id': analysis.id,
                'analysis_id': analysis.id,
                'business_goal': analysis.business_goal,
                'title': analysis.strategic_priority or f"Mission: {analysis.business_goal[:50]}",
                'description': f"Complete {total_steps} action steps",
                'progress': round(progress_percentage, 1),
                'total_steps': total_steps,
                'completed_steps': completed_count,
                'points_reward': total_steps * 20,
                'status': mission_status,
                'created_at': analysis.created_at.isoformat() if analysis.created_at else None,
                'steps': steps,
                'mission_config': _ensure_dict(user_progress.get('mission_config'), "mission_config") or None,
            })

        return {
            "success": True,
            "data": missions
        }

    except Exception as e:
        logger.error(f"[Missions] Error in get_user_missions: {type(e).__name__}: {e}", exc_info=True)
        logger.error(f"Error fetching missions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch missions"
        )


@router.get("/active")
async def get_active_missions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get only active (incomplete) missions"""
    try:
        analyses = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.user_id == current_user.id
        ).order_by(BusinessAnalysis.created_at.desc()).all()

        active_missions = []
        seen_active: set = set()

        for analysis in analyses:
            action_plans = _parse_action_plans(analysis)
            if not action_plans:
                continue

            priority_key = (analysis.strategic_priority or (analysis.business_goal or '')[:80]).strip().lower()
            if priority_key and priority_key in seen_active:
                continue
            if priority_key:
                seen_active.add(priority_key)

            user_progress = _ensure_dict(analysis.user_progress, f"user_progress[{analysis.id}]")
            completed_steps = _ensure_list(user_progress.get('completed_actions', []), "completed_actions")
            total_steps = len(action_plans)

            # Only include if not fully completed
            if len(completed_steps) < total_steps:
                active_missions.append({
                    'id': analysis.id,
                    'analysis_id': analysis.id,
                    'business_goal': analysis.business_goal or "Business Goal",
                    'title': analysis.strategic_priority or (analysis.business_goal[:50] if analysis.business_goal else "Business Goal"),
                    'progress': round(len(completed_steps) / total_steps * 100, 1) if total_steps > 0 else 0,
                    'total_steps': total_steps,
                    'completed_steps': len(completed_steps),
                    'next_action': _parse_action_plans(analysis)[len(completed_steps)].get('title', '') if len(completed_steps) < len(_parse_action_plans(analysis)) else None
                })

        return {
            "success": True,
            "data": active_missions
        }

    except Exception as e:
        logger.error(f"Error fetching active missions: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch active missions"
        )


@router.get("/constraints/active")
async def get_active_constraints(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all active constraints from user's analyses.
    These are bottlenecks and constraints that haven't been resolved yet.
    """
    try:
        analyses = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.user_id == current_user.id
        ).order_by(BusinessAnalysis.created_at.desc()).all()

        active_constraints = []

        for analysis in analyses:
            user_progress = _ensure_dict(analysis.user_progress, f"user_progress[{analysis.id}]")
            resolved_constraints = _ensure_list(user_progress.get('resolved_constraints', []), "resolved_constraints")

            # Add primary bottleneck if exists and not resolved
            if analysis.primary_bottleneck:
                constraint_id = f"{analysis.id}_primary"
                if constraint_id not in resolved_constraints:
                    active_constraints.append({
                        'id': constraint_id,
                        'analysis_id': analysis.id,
                        'type': 'primary_bottleneck',
                        'title': analysis.primary_bottleneck.get('title', 'Primary Bottleneck'),
                        'description': analysis.primary_bottleneck.get('description', ''),
                        'impact': analysis.primary_bottleneck.get('impact', 'high'),
                        'consequence': analysis.primary_bottleneck.get('consequence', ''),
                        'created_at': analysis.created_at.isoformat() if analysis.created_at else None
                    })

            # Add secondary constraints if not resolved
            if analysis.secondary_constraints:
                for idx, constraint in enumerate(analysis.secondary_constraints, 1):
                    constraint_id = f"{analysis.id}_constraint_{idx}"
                    if constraint_id not in resolved_constraints:
                        active_constraints.append({
                            'id': constraint_id,
                            'analysis_id': analysis.id,
                            'type': 'secondary_constraint',
                            'number': idx,
                            'title': constraint.get('title', f'Constraint {idx}'),
                            'description': constraint.get('description', ''),
                            'impact': constraint.get('impact', 'medium'),
                            'created_at': analysis.created_at.isoformat() if analysis.created_at else None
                        })

        return {
            "success": True,
            "data": active_constraints,
            "count": len(active_constraints)
        }

    except Exception as e:
        logger.error(f"Error fetching active constraints: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch active constraints"
        )


@router.post("/constraints/{constraint_id}/resolve")
async def resolve_constraint(
    constraint_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark a constraint as resolved"""
    try:
        # Extract analysis_id from constraint_id (format: {analysis_id}_primary or {analysis_id}_constraint_{idx})
        analysis_id = int(constraint_id.split('_')[0])

        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()

        if not analysis:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Analysis not found"
            )

        user_progress = _ensure_dict(analysis.user_progress, "user_progress")
        if 'resolved_constraints' not in user_progress:
            user_progress['resolved_constraints'] = []
        if not isinstance(user_progress['resolved_constraints'], list):
            user_progress['resolved_constraints'] = []

        if constraint_id not in user_progress['resolved_constraints']:
            user_progress['resolved_constraints'].append(constraint_id)

            # Award chops for resolving constraint
            current_user.total_chops = (current_user.total_chops or 0) + 30

        analysis.user_progress = user_progress
        db.commit()

        return {
            "success": True,
            "message": "Constraint marked as resolved",
            "data": {
                'constraint_id': constraint_id,
                'resolved': True,
                'chops_earned': 30,
                'total_chops': current_user.total_chops
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error resolving constraint: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to resolve constraint"
        )


class MissionActivateRequest(BaseModel):
    mission_name: Optional[str] = None
    start_date: Optional[str] = None
    day_plans: Optional[list] = None  # [{"day": 1, "durationDays": 1}, ...]
    opening_note: Optional[str] = None

@router.post("/{analysis_id}/activate")
async def activate_mission(
    analysis_id: int,
    body: MissionActivateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Store mission activation configuration"""
    try:
        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()

        if not analysis:
            raise HTTPException(status_code=404, detail="Analysis not found")

        user_progress = _ensure_dict(analysis.user_progress, "user_progress")

        user_progress['mission_config'] = {
            'mission_name': body.mission_name,
            'start_date': body.start_date,
            'day_plans': body.day_plans or [],
            'opening_note': body.opening_note,
            'activated_at': datetime.now(timezone.utc).isoformat()
        }

        if 'completed_actions' not in user_progress:
            user_progress['completed_actions'] = []
        if 'reflections' not in user_progress:
            user_progress['reflections'] = {}
        if 'completion_dates' not in user_progress:
            user_progress['completion_dates'] = {}

        analysis.user_progress = user_progress
        db.commit()

        return {
            "success": True,
            "message": "Mission activated",
            "data": {"analysis_id": analysis_id, "mission_config": user_progress['mission_config']}
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error activating mission: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to activate mission")


class RoadmapCommentRequest(BaseModel):
    task_id: str
    text: str
    parent_id: Optional[str] = None

@router.post("/{analysis_id}/roadmap-comment")
async def add_roadmap_comment(
    analysis_id: int,
    body: RoadmapCommentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Add a comment to an execution roadmap task"""
    try:
        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()

        if not analysis:
            raise HTTPException(status_code=404, detail="Analysis not found")

        user_progress = _ensure_dict(analysis.user_progress, "user_progress")
        if 'roadmap_comments' not in user_progress:
            user_progress['roadmap_comments'] = {}

        task_comments = user_progress['roadmap_comments'].get(body.task_id, [])

        comment_id = f"comment-{datetime.now(timezone.utc).timestamp()}"
        new_comment = {
            'id': comment_id,
            'text': body.text,
            'author': {'name': current_user.name or 'You', 'initials': (current_user.name or 'YO')[:2].upper()},
            'createdAt': datetime.now(timezone.utc).isoformat(),
            'parent_id': body.parent_id,
        }

        if body.parent_id:
            for comment in task_comments:
                if comment['id'] == body.parent_id:
                    if 'replies' not in comment:
                        comment['replies'] = []
                    comment['replies'].append(new_comment)
                    break
        else:
            task_comments.append(new_comment)

        user_progress['roadmap_comments'][body.task_id] = task_comments
        analysis.user_progress = user_progress
        db.commit()

        return {"success": True, "data": new_comment}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error adding roadmap comment: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to add comment")


@router.delete("/{analysis_id}/roadmap-comment/{comment_id}")
async def delete_roadmap_comment(
    analysis_id: int,
    comment_id: str,
    task_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a roadmap comment"""
    try:
        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()
        if not analysis:
            raise HTTPException(status_code=404, detail="Analysis not found")

        user_progress = _ensure_dict(analysis.user_progress, "user_progress")
        if 'roadmap_comments' in user_progress:
            task_comments = user_progress['roadmap_comments'].get(task_id, [])
            user_progress['roadmap_comments'][task_id] = [c for c in task_comments if c['id'] != comment_id]
            analysis.user_progress = user_progress
            db.commit()

        return {"success": True, "message": "Comment deleted"}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete comment")


@router.get("/{analysis_id}")
async def get_mission_details(
    analysis_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get detailed mission information for a specific analysis"""
    try:
        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()

        if not analysis:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Mission not found"
            )

        action_plans = _parse_action_plans(analysis)
        logger.info(f"[Missions] get_mission_details: analysis {analysis_id} has {len(action_plans)} action plans")

        if not action_plans:
            return {
                "success": True,
                "data": {
                    'id': analysis.id,
                    'title': 'No actions available',
                    'steps': []
                }
            }

        user_progress = _ensure_dict(analysis.user_progress, "user_progress")
        logger.info(f"[Missions] user_progress keys: {list(user_progress.keys())}")

        completed_steps = _ensure_list(user_progress.get('completed_actions', []), "completed_actions")
        completion_dates = _ensure_dict(user_progress.get('completion_dates', {}), "completion_dates")
        reflections = _ensure_dict(user_progress.get('reflections', {}), "reflections")
        mission_config = _ensure_dict(user_progress.get('mission_config'), "mission_config") or None

        primary_bottleneck = _ensure_dict(analysis.primary_bottleneck, "primary_bottleneck")

        steps = []
        for idx, action in enumerate(action_plans, 1):
            step_id = f"{analysis.id}_action_{idx}"
            is_completed = step_id in completed_steps

            what_to_do = _ensure_list(
                action.get('what_to_do') or action.get('what_to_do_steps') or action.get('steps'),
                f"action[{idx}].what_to_do"
            )
            why_matters = _ensure_list(
                action.get('why_this_matters') or action.get('why_it_matters') or action.get('why_it_matters_bullets'),
                f"action[{idx}].why_matters"
            )

            steps.append({
                'id': step_id,
                'number': idx,
                'day': idx,
                'label': action.get('title') or action.get('action_title') or f'Action {idx}',
                'description': what_to_do[0] if what_to_do else action.get('description', ''),
                'full_steps': what_to_do,
                'why_it_matters': why_matters,
                'effort': action.get('effort', 'MEDIUM'),
                'done': is_completed,
                'active': not is_completed and (idx == len(completed_steps) + 1),
                'points': 20,
                'toolkit': action.get('ai_tool') or action.get('toolkit'),
                'completed_at': completion_dates.get(step_id),
                'reflection': reflections.get(step_id)
            })

        total = len(action_plans)
        done_count = len(completed_steps)
        logger.info(f"[Missions] analysis {analysis_id}: {done_count}/{total} steps done, mission_config={'present' if mission_config else 'absent'}")

        return {
            "success": True,
            "data": {
                'id': analysis.id,
                'analysis_id': analysis.id,
                'business_goal': analysis.business_goal,
                'title': analysis.strategic_priority or analysis.business_goal,
                'description': primary_bottleneck.get('description', ''),
                'strategic_priority': analysis.strategic_priority,
                'progress': round(done_count / total * 100, 1),
                'total_steps': total,
                'completed_steps': done_count,
                'points_reward': total * 20,
                'status': 'completed' if done_count == total else ('active' if done_count > 0 else 'not_started'),
                'steps': steps,
                'mission_config': mission_config,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Missions] Error in get_mission_details for analysis {analysis_id}: {type(e).__name__}: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch mission details"
        )


@router.post("/{analysis_id}/steps/{step_number}/complete")
async def complete_mission_step(
    analysis_id: int,
    step_number: int,
    body: MissionStepComplete,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Mark a mission step as complete and optionally add reflection"""
    try:
        analysis = db.query(BusinessAnalysis).filter(
            BusinessAnalysis.id == analysis_id,
            BusinessAnalysis.user_id == current_user.id
        ).first()

        if not analysis:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Mission not found"
            )

        if not _parse_action_plans(analysis) or step_number > len(_parse_action_plans(analysis)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid step number"
            )

        # Parse user_progress safely — JSON columns can come back as strings
        user_progress = _ensure_dict(analysis.user_progress, "user_progress")
        if 'completed_actions' not in user_progress:
            user_progress['completed_actions'] = []
        if 'completion_dates' not in user_progress:
            user_progress['completion_dates'] = {}
        if 'reflections' not in user_progress:
            user_progress['reflections'] = {}
        # Ensure completed_actions is a list (might be stored as something else)
        if not isinstance(user_progress['completed_actions'], list):
            user_progress['completed_actions'] = []

        step_id = f"{analysis_id}_action_{step_number}"
        logger.info(f"[Missions] Completing step {step_id}, current completed: {user_progress['completed_actions']}")

        # Mark as completed
        if step_id not in user_progress['completed_actions']:
            user_progress['completed_actions'].append(step_id)

            if 'completion_dates' not in user_progress:
                user_progress['completion_dates'] = {}
            user_progress['completion_dates'][step_id] = datetime.now(timezone.utc).isoformat()

            # Award chops (20 points per step)
            current_user.total_chops = (current_user.total_chops or 0) + 20

        # Save reflection if provided
        if body.reflection:
            if 'reflections' not in user_progress:
                user_progress['reflections'] = {}
            user_progress['reflections'][step_id] = body.reflection

        # Update analysis with new progress
        analysis.user_progress = user_progress

        # Check if mission completed
        is_mission_completed = len(user_progress['completed_actions']) == len(_parse_action_plans(analysis))

        if is_mission_completed:
            # Award bonus chops for completing entire mission
            bonus_chops = len(_parse_action_plans(analysis)) * 5
            current_user.total_chops = (current_user.total_chops or 0) + bonus_chops

        db.commit()

        return {
            "success": True,
            "message": "Step completed successfully",
            "data": {
                'step_id': step_id,
                'completed': True,
                'chops_earned': 20,
                'mission_completed': is_mission_completed,
                'total_chops': current_user.total_chops
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error completing mission step: {str(e)}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to complete mission step"
        )
