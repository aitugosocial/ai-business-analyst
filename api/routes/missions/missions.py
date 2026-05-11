"""
Missions API routes
Handles mission management, user progress tracking, and step completions
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import desc

from database.pg_connections import get_db
from database.pg_models import Mission, MissionStep, UserMission, UserMissionStep, User
from api.routes.auth.login import get_current_user

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/missions", tags=["missions"])


# ===== Pydantic Models =====

class MissionStepResponse(BaseModel):
    id: int
    day: int
    label: str
    description: str
    points: int
    order_index: int
    done: bool = False
    active: bool = False


class MissionResponse(BaseModel):
    id: int
    title: str
    description: str
    category: Optional[str]
    difficulty: str
    progress: int
    totalSteps: int
    completedSteps: int
    pointsReward: int
    estimatedDays: int
    status: str
    icon: Optional[str]
    colorTheme: Optional[str]
    steps: List[MissionStepResponse]


class StartMissionRequest(BaseModel):
    pass  # No body needed


class CompleteStepRequest(BaseModel):
    reflection: Optional[str] = None


# ===== Helper Functions =====

def calculate_progress(completed_steps: int, total_steps: int) -> int:
    """Calculate progress percentage"""
    if total_steps == 0:
        return 0
    return int((completed_steps / total_steps) * 100)


def award_points_to_user(db: Session, user: User, points: int):
    """Award points/chops to user"""
    user.total_chops = (user.total_chops or 0) + points
    db.commit()
    logger.info(f"Awarded {points} chops to user {user.id}. Total: {user.total_chops}")


# ===== API Endpoints =====

@router.get("", response_model=List[MissionResponse])
async def get_missions(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get all available missions with user progress.
    Active missions show current progress, locked missions show 0% progress.
    """
    try:
        # Get all active missions ordered by order_index
        missions = db.query(Mission).filter(Mission.is_active == True).order_by(Mission.order_index).all()

        # Get user's mission progress
        user_missions_dict = {}
        user_missions = db.query(UserMission).filter(UserMission.user_id == current_user.id).all()

        for um in user_missions:
            # Get completed steps for this mission
            completed_step_ids = db.query(UserMissionStep.step_id).filter(
                UserMissionStep.user_mission_id == um.id,
                UserMissionStep.completed == True
            ).all()
            completed_ids = {step_id[0] for step_id in completed_step_ids}

            user_missions_dict[um.mission_id] = {
                'status': um.status,
                'progress': um.progress_percentage,
                'completed_steps': um.completed_steps,
                'completed_step_ids': completed_ids,
                'user_mission_id': um.id
            }

        result = []
        for mission in missions:
            # Get mission steps
            steps = db.query(MissionStep).filter(
                MissionStep.mission_id == mission.id
            ).order_by(MissionStep.order_index).all()

            user_mission_data = user_missions_dict.get(mission.id, {})
            mission_status = user_mission_data.get('status', 'locked')
            progress = user_mission_data.get('progress', 0)
            completed_steps = user_mission_data.get('completed_steps', 0)
            completed_step_ids = user_mission_data.get('completed_step_ids', set())

            # Determine active step
            active_step_index = completed_steps if completed_steps < len(steps) else -1

            # Format steps
            formatted_steps = []
            for idx, step in enumerate(steps):
                formatted_steps.append(MissionStepResponse(
                    id=step.id,
                    day=step.day,
                    label=step.label,
                    description=step.description,
                    points=step.points,
                    order_index=step.order_index,
                    done=step.id in completed_step_ids,
                    active=(idx == active_step_index and mission_status == 'active')
                ))

            result.append(MissionResponse(
                id=mission.id,
                title=mission.title,
                description=mission.description,
                category=mission.category,
                difficulty=mission.difficulty,
                progress=progress,
                totalSteps=mission.total_steps,
                completedSteps=completed_steps,
                pointsReward=mission.points_reward,
                estimatedDays=mission.estimated_days,
                status=mission_status,
                icon=mission.icon,
                colorTheme=mission.color_theme,
                steps=formatted_steps
            ))

        return result

    except Exception as e:
        logger.error(f"Error fetching missions: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch missions: {str(e)}")


@router.get("/{mission_id}", response_model=MissionResponse)
async def get_mission(
    mission_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get single mission with user progress"""
    try:
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")

        # Get user's progress for this mission
        user_mission = db.query(UserMission).filter(
            UserMission.user_id == current_user.id,
            UserMission.mission_id == mission_id
        ).first()

        steps = db.query(MissionStep).filter(
            MissionStep.mission_id == mission_id
        ).order_by(MissionStep.order_index).all()

        completed_step_ids = set()
        mission_status = 'locked'
        progress = 0
        completed_steps = 0

        if user_mission:
            mission_status = user_mission.status
            progress = user_mission.progress_percentage
            completed_steps = user_mission.completed_steps

            # Get completed step IDs
            completed =db.query(UserMissionStep.step_id).filter(
                UserMissionStep.user_mission_id == user_mission.id,
                UserMissionStep.completed == True
            ).all()
            completed_step_ids = {step_id[0] for step_id in completed}

        # Determine active step
        active_step_index = completed_steps if completed_steps < len(steps) else -1

        formatted_steps = []
        for idx, step in enumerate(steps):
            formatted_steps.append(MissionStepResponse(
                id=step.id,
                day=step.day,
                label=step.label,
                description=step.description,
                points=step.points,
                order_index=step.order_index,
                done=step.id in completed_step_ids,
                active=(idx == active_step_index and mission_status == 'active')
            ))

        return MissionResponse(
            id=mission.id,
            title=mission.title,
            description=mission.description,
            category=mission.category,
            difficulty=mission.difficulty,
            progress=progress,
            totalSteps=mission.total_steps,
            completedSteps=completed_steps,
            pointsReward=mission.points_reward,
            estimatedDays=mission.estimated_days,
            status=mission_status,
            icon=mission.icon,
            colorTheme=mission.color_theme,
            steps=formatted_steps
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching mission {mission_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch mission: {str(e)}")


@router.post("/{mission_id}/start")
async def start_mission(
    mission_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Start a new mission"""
    try:
        # Check if mission exists
        mission = db.query(Mission).filter(Mission.id == mission_id).first()
        if not mission:
            raise HTTPException(status_code=404, detail="Mission not found")

        # Check if already started
        existing = db.query(UserMission).filter(
            UserMission.user_id == current_user.id,
            UserMission.mission_id == mission_id
        ).first()

        if existing:
            if existing.status == 'active':
                return {"success": True, "message": "Mission already active"}
            elif existing.status == 'completed':
                return {"success": False, "message": "Mission already completed"}

        # Create new user mission
        user_mission = UserMission(
            user_id=current_user.id,
            mission_id=mission_id,
            status='active',
            progress_percentage=0,
            completed_steps=0
        )
        db.add(user_mission)
        db.flush()

        # Create step tracking entries
        steps = db.query(MissionStep).filter(MissionStep.mission_id == mission_id).all()
        for step in steps:
            user_step = UserMissionStep(
                user_mission_id=user_mission.id,
                step_id=step.id,
                completed=False
            )
            db.add(user_step)

        db.commit()

        logger.info(f"User {current_user.id} started mission {mission_id}")
        return {
            "success": True,
            "message": "Mission started successfully",
            "data": {"user_mission_id": user_mission.id}
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error starting mission: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to start mission: {str(e)}")


@router.post("/{mission_id}/steps/{step_id}/complete")
async def complete_step(
    mission_id: int,
    step_id: int,
    request: CompleteStepRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark a mission step as completed"""
    try:
        # Get user mission
        user_mission = db.query(UserMission).filter(
            UserMission.user_id == current_user.id,
            UserMission.mission_id == mission_id
        ).first()

        if not user_mission:
            raise HTTPException(status_code=404, detail="Mission not started")

        if user_mission.status != 'active':
            raise HTTPException(status_code=400, detail="Mission is not active")

        # Get the step
        step = db.query(MissionStep).filter(MissionStep.id == step_id).first()
        if not step:
            raise HTTPException(status_code=404, detail="Step not found")

        # Get user step tracking
        user_step = db.query(UserMissionStep).filter(
            UserMissionStep.user_mission_id == user_mission.id,
            UserMissionStep.step_id == step_id
        ).first()

        if not user_step:
            raise HTTPException(status_code=404, detail="Step tracking not found")

        if user_step.completed:
            return {"success": True, "message": "Step already completed"}

        # Mark as completed
        user_step.completed = True
        user_step.completed_at = datetime.now(timezone.utc)
        if request.reflection:
            user_step.reflection = request.reflection

        # Update mission progress
        user_mission.completed_steps += 1
        total_steps = db.query(MissionStep).filter(MissionStep.mission_id == mission_id).count()
        user_mission.progress_percentage = calculate_progress(user_mission.completed_steps, total_steps)

        # Award points for completing step
        award_points_to_user(db, current_user, step.points)

        # Check if mission is completed
        if user_mission.completed_steps >= total_steps:
            user_mission.status = 'completed'
            user_mission.completed_at = datetime.now(timezone.utc)

            # Award mission completion bonus
            mission = db.query(Mission).filter(Mission.id == mission_id).first()
            if mission and mission.points_reward > 0:
                award_points_to_user(db, current_user, mission.points_reward)

            logger.info(f"User {current_user.id} completed mission {mission_id}")

        db.commit()

        return {
            "success": True,
            "message": "Step completed successfully",
            "data": {
                "points_awarded": step.points,
                "mission_completed": user_mission.status == 'completed',
                "progress": user_mission.progress_percentage
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Error completing step: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to complete step: {str(e)}")


@router.get("/active/current")
async def get_active_mission(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get user's currently active mission"""
    try:
        user_mission = db.query(UserMission).filter(
            UserMission.user_id == current_user.id,
            UserMission.status == 'active'
        ).order_by(desc(UserMission.started_at)).first()

        if not user_mission:
            return {"success": True, "data": None}

        mission = db.query(Mission).filter(Mission.id == user_mission.mission_id).first()
        steps = db.query(MissionStep).filter(MissionStep.mission_id == mission.id).order_by(MissionStep.order_index).all()

        # Get completed step IDs
        completed = db.query(UserMissionStep.step_id).filter(
            UserMissionStep.user_mission_id == user_mission.id,
            UserMissionStep.completed == True
        ).all()
        completed_step_ids = {step_id[0] for step_id in completed}

        active_step_index = user_mission.completed_steps if user_mission.completed_steps < len(steps) else -1

        formatted_steps = []
        for idx, step in enumerate(steps):
            formatted_steps.append(MissionStepResponse(
                id=step.id,
                day=step.day,
                label=step.label,
                description=step.description,
                points=step.points,
                order_index=step.order_index,
                done=step.id in completed_step_ids,
                active=(idx == active_step_index)
            ))

        result = MissionResponse(
            id=mission.id,
            title=mission.title,
            description=mission.description,
            category=mission.category,
            difficulty=mission.difficulty,
            progress=user_mission.progress_percentage,
            totalSteps=mission.total_steps,
            completedSteps=user_mission.completed_steps,
            pointsReward=mission.points_reward,
            estimatedDays=mission.estimated_days,
            status=user_mission.status,
            icon=mission.icon,
            colorTheme=mission.color_theme,
            steps=formatted_steps
        )

        return {"success": True, "data": result}

    except Exception as e:
        logger.error(f"Error fetching active mission: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch active mission: {str(e)}")
