# api/routes/business_analyzer.py
"""
Business Analyzer API Routes
Provides AI-powered business analysis with bottleneck identification,
action plans, toolkits, and execution roadmaps.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any

from database.pg_connections import get_db
from database.pg_models import BusinessAnalysis
from api.routes.auth.login import get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/business", tags=["Business Analyzer"])


# =========================================================================
# REQUEST/RESPONSE MODELS
# =========================================================================
class AnalyzeRequest(BaseModel):
    """Request model for business analysis."""
    business_goal: str = Field(
        ...,
        min_length=10,
        max_length=2000,
        description="The business challenge or goal to analyze"
    )


class BottleneckResponse(BaseModel):
    """Bottleneck data structure."""
    id: int
    title: str
    description: str
    priority: str  # HIGH, MEDIUM, LOW
    impact: str


class StrategyResponse(BaseModel):
    """Strategy/action plan data structure."""
    id: int
    bottleneckId: int
    title: str
    description: str
    features: List[str] = []


class AIToolResponse(BaseModel):
    """AI tool recommendation data structure."""
    id: int
    bottleneckId: int
    title: str
    description: str
    price: str
    rating: str
    features: List[str] = []
    pros: List[str] = []
    cons: List[str] = []
    website: str


class AnalysisResponse(BaseModel):
    """Full analysis response matching frontend expectations."""
    analysis_id: int
    business_goal: str
    objective: str
    bottlenecks: List[Dict[str, Any]]
    business_strategies: List[Dict[str, Any]]
    ai_tools: List[Dict[str, Any]]
    roadmap: List[Dict[str, Any]]
    ai_confidence_score: int
    created_at: str
    key_evidence: Optional[List[Dict[str, Any]]] = []
    assumptions: Optional[List[str]] = []
    reasoning_trace: Optional[List[str]] = []


# =========================================================================
# HELPER FUNCTIONS
# =========================================================================
def get_user_id(current_user) -> int:
    """Extract user ID from current_user (handles dict or object)."""
    if isinstance(current_user, dict):
        # Extract id robustly from diverse token payload formats
        user_id = current_user.get("id") or current_user.get("user_id") or current_user.get("sub")
        if not user_id and "user" in current_user:
            user_data = current_user["user"]
            if isinstance(user_data, dict):
                return user_data.get("id") or user_data.get("user_id")
            elif hasattr(user_data, 'id'):
                return user_data.id
        return user_id
    return current_user.id


def parse_json_field(field_value, default=None):
    """Safely parse JSON field from database."""
    if field_value is None:
        return default if default is not None else []

    if isinstance(field_value, (list, dict)):
        return field_value

    if isinstance(field_value, str):
        try:
            return json.loads(field_value)
        except json.JSONDecodeError:
            return default if default is not None else []

    return default if default is not None else []


def format_analysis_for_frontend(analysis: BusinessAnalysis) -> Dict[str, Any]:
    """Transform database analysis to frontend format (NEW SCHEMA ONLY)."""

    # Parse new schema fields
    primary_bottleneck = parse_json_field(analysis.primary_bottleneck, {})
    secondary_constraints = parse_json_field(analysis.secondary_constraints, [])
    action_plans = parse_json_field(analysis.action_plans, [])
    recommended_tool_stacks = parse_json_field(analysis.recommended_tool_stacks, [])
    execution_roadmap = parse_json_field(analysis.execution_roadmap, [])
    single_tool_recommendation = parse_json_field(analysis.single_tool_recommendation, None)

    return {
        "analysis_id": analysis.id,
        "business_goal": analysis.business_goal or "",
        # Primary bottleneck
        "primary_bottleneck": primary_bottleneck,
        # Secondary constraints
        "secondary_constraints": secondary_constraints,
        # Strategic direction
        "what_to_stop": analysis.what_to_stop,
        "strategic_priority": analysis.strategic_priority,
        # Action plans (with toolkits)
        "action_plans": action_plans,
        # Multi-tool automation stacks
        "recommended_tool_stacks": recommended_tool_stacks,
        "total_phases": analysis.total_phases,
        # Execution roadmap
        "estimated_days": analysis.estimated_days,
        "execution_roadmap": execution_roadmap,
        # Additional context
        "exclusions_note": analysis.exclusions_note,
        "motivational_quote": analysis.motivational_quote,
        # Recommendation mode (single tool vs automation stack)
        "recommendation_mode": analysis.recommendation_mode or "automation_stack",
        "single_tool_recommendation": single_tool_recommendation,
        # Metadata
        "created_at": analysis.created_at.isoformat() if analysis.created_at else "",
        "ai_model_used": analysis.ai_model_used,
        "ai_confidence_score": analysis.confidence_score or 90
    }


# =========================================================================
# BACKGROUND TASKS
# =========================================================================

async def _enrich_stacks_background(
    analysis_id: int,
    raw_stacks: list,
    user_query: str,
    bottleneck_title: str,
) -> None:
    """
    BackgroundTask: LLM-enrich automation stacks after response is sent,
    then persist enriched stacks back to the analysis row.
    """
    from database.pg_connections import SessionLocal
    from decision_engine.agentic_analyzer import create_analyzer

    db = SessionLocal()
    try:
        analyzer = create_analyzer(db)
        enriched = await analyzer._enrich_stacks_with_llm(
            stacks=raw_stacks,
            user_query=user_query,
            primary_bottleneck=bottleneck_title,
        )
        db.execute(
            text("UPDATE business_analyses SET recommended_tool_stacks = :stacks WHERE id = :id"),
            {"stacks": json.dumps(enriched), "id": analysis_id},
        )
        db.commit()
        logger.info(f"Background stack enrichment saved for analysis {analysis_id}")
    except Exception as exc:
        logger.error(
            f"Background stack enrichment failed for analysis {analysis_id}: {exc}",
            exc_info=True,
        )
    finally:
        db.close()


# =========================================================================
# API ENDPOINTS
# =========================================================================
@router.post("/analyze", response_model=dict)
async def analyze_business_goal(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Analyze business goal using the Agentic Analyzer.
    """
    from decision_engine.agentic_analyzer import create_analyzer
    from decision_engine.multimodal.handler import MultimodalHandler

    try:
        user_id = get_user_id(current_user)
        logger.info(f"🚀 Starting agentic analysis for user {user_id}")

        content_type = request.headers.get("content-type", "")

        business_goal = ""
        files = []

        if "application/json" in content_type:
            body = await request.json()
            business_goal = body.get("business_goal")
        elif "multipart/form-data" in content_type:
            form_data = await request.form()
            business_goal = form_data.get("business_goal")
            files = form_data.getlist("files")
        else:
            raise HTTPException(status_code=400, detail="Invalid Content-Type")

        # ── Deduplication guard ────────────────────────────────────────────────
        # If the user submits the exact same business goal, return the existing
        # analysis from the database instead of running a new 30s LLM job.
        from sqlalchemy import desc as _desc
        
        _goal_str = business_goal or ""
        if _goal_str.strip():
            recent = (
                db.query(BusinessAnalysis)
                .filter(
                    BusinessAnalysis.user_id == user_id,
                    BusinessAnalysis.business_goal == _goal_str,
                )
                .order_by(_desc(BusinessAnalysis.created_at))
                .first()
            )
            if recent:
                logger.info(
                    f"⚡ Returning existing analysis {recent.id} for user {user_id} "
                    f"(duplicate prompt detected)"
                )
                return {
                    "success": True,
                    "message": "Analysis completed successfully",
                    "data": format_analysis_for_frontend(recent),
                }

        if not business_goal and not files:
            raise HTTPException(status_code=400, detail="Business goal or a document/image upload is required")

        business_goal = business_goal or ""

        # Process multimodal files if present
        if files:
            image_bytes_list = []
            document_bytes_list = []

            for file in files:
                file_bytes = await file.read()
                filename = getattr(file, "filename", "").lower()

                if filename.endswith(('.png', '.jpg', '.jpeg', '.gif')):
                    image_bytes_list.append((file_bytes, filename))
                elif filename.endswith(('.pdf', '.docx', '.doc', '.xlsx', '.csv', '.txt')):
                    document_bytes_list.append((file_bytes, filename))

            if image_bytes_list or document_bytes_list:
                logger.info(f"Processing {len(image_bytes_list)} images and {len(document_bytes_list)} documents for user {user_id}")
                mm_handler = MultimodalHandler(use_vision_for_images=True)
                mm_result = mm_handler.process_multimodal_query(
                    user_query=business_goal,
                    image_bytes_list=image_bytes_list,
                    document_bytes_list=document_bytes_list,
                    enhance_with_llm=False
                )
                combined_context = mm_result.get("combined_context", "")
                if combined_context:
                    business_goal = f"{business_goal}\n\n[Additional Context from Uploaded Files]:\n{combined_context}".strip()

        if not business_goal:
            raise HTTPException(status_code=400, detail="Could not extract any content from the uploaded files to analyze.")

        # Create analyzer and run analysis
        analyzer = create_analyzer(db)
        result = await analyzer.analyze(
            user_query=business_goal,
            user_id=user_id
        )

        logger.info(f"✅ Analysis completed: ID {result['data']['analysis_id']}")

        # Schedule background enrichment of automation stacks
        _raw_stacks = result["data"].get("recommended_tool_stacks", [])
        _bottleneck_title = result["data"].get("primary_bottleneck", {}).get("title", "")
        if _raw_stacks:
            background_tasks.add_task(
                _enrich_stacks_background,
                analysis_id=result["data"]["analysis_id"],
                raw_stacks=_raw_stacks,
                user_query=business_goal,
                bottleneck_title=_bottleneck_title,
            )

        return {
            "success": True,
            "message": "Analysis completed successfully",
            "data": result["data"]
        }

    except Exception as e:
        err_str = str(e)
        if err_str.startswith("UNSAFE_QUERY::"):
            parts = err_str.split("::", 2)
            reason = parts[1] if len(parts) > 1 else "This query cannot be processed."
            try:
                import ast as _ast
                suggestions = _ast.literal_eval(parts[2]) if len(parts) > 2 else []
            except Exception:
                suggestions = []
            logger.warning(f"Unsafe query blocked for user {user_id}: {reason}")
            raise HTTPException(
                status_code=422,
                detail={
                    "blocked": True,
                    "reason": reason,
                    "suggestions": suggestions,
                    "message": (
                        "Our engine can't process this request as described. "
                        "We're built to help with legitimate business challenges."
                    ),
                },
            )
        logger.error(f"❌ Analysis failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Analysis failed: {str(e)}"
        )


@router.post("/analyze/stream")
async def analyze_business_goal_stream(
    request: Request,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    SSE streaming version of /analyze.
    Sends progress events so the UI stays live during the LLM call,
    then delivers the completed result as the final event.
    """
    from decision_engine.agentic_analyzer import create_analyzer
    from decision_engine.multimodal.handler import MultimodalHandler

    user_id = get_user_id(current_user)
    content_type = request.headers.get("content-type", "")
    business_goal = ""
    files = []

    if "application/json" in content_type:
        body = await request.json()
        business_goal = body.get("business_goal", "")
    elif "multipart/form-data" in content_type:
        form_data = await request.form()
        business_goal = form_data.get("business_goal", "")
        files = form_data.getlist("files")

    if not business_goal and not files:
        raise HTTPException(status_code=400, detail="Business goal or file upload required")

    # Resolve multimodal files before streaming starts
    if files:
        mm_handler = MultimodalHandler(use_vision_for_images=True)
        image_bytes_list, document_bytes_list = [], []
        for f in files:
            fb = await f.read()
            fn = getattr(f, "filename", "").lower()
            if fn.endswith(('.png', '.jpg', '.jpeg', '.gif')):
                image_bytes_list.append((fb, fn))
            elif fn.endswith(('.pdf', '.docx', '.doc', '.xlsx', '.csv', '.txt')):
                document_bytes_list.append((fb, fn))
        if image_bytes_list or document_bytes_list:
            mm_result = mm_handler.process_multimodal_query(
                user_query=business_goal,
                image_bytes_list=image_bytes_list,
                document_bytes_list=document_bytes_list,
                enhance_with_llm=False,
            )
            ctx = mm_result.get("combined_context", "")
            if ctx:
                business_goal = f"{business_goal}\n\n[File Context]:\n{ctx}".strip()

    if not business_goal:
        raise HTTPException(status_code=400, detail="Could not extract content from uploads")

    async def event_stream():
        def _send(event: str, payload: dict) -> str:
            return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

        # Idempotency: return a cached recent result immediately if prompt matches
        from sqlalchemy import desc as _desc
        recent = None
        if business_goal and business_goal.strip():
            recent = (
                db.query(BusinessAnalysis)
                .filter(
                    BusinessAnalysis.user_id == user_id,
                    BusinessAnalysis.business_goal == business_goal,
                )
                .order_by(_desc(BusinessAnalysis.created_at))
                .first()
            )
        if recent:
            yield _send("progress", {"step": "complete", "pct": 100, "msg": "Retrieved recent analysis"})
            yield _send("result", {"success": True, "data": format_analysis_for_frontend(recent)})
            return

        try:
            yield _send("progress", {"step": "reading", "pct": 5, "msg": "Reading your business challenge…"})
            await asyncio.sleep(0)

            yield _send("progress", {"step": "analyzing", "pct": 20, "msg": "Identifying bottlenecks…"})
            await asyncio.sleep(0)

            # Run the actual LLM analysis in a thread so we can keep yielding
            loop = asyncio.get_event_loop()
            analyzer = create_analyzer(db)

            # Send intermediate heartbeat while LLM is running
            async def heartbeat():
                steps = [
                    (40, "Mapping strategic constraints…"),
                    (55, "Building action plans…"),
                    (70, "Selecting AI tools…"),
                    (85, "Compiling execution roadmap…"),
                ]
                for pct, msg in steps:
                    await asyncio.sleep(8)
                    yield _send("progress", {"step": "thinking", "pct": pct, "msg": msg})

            analysis_task = loop.run_in_executor(
                None,
                lambda: asyncio.run(analyzer.analyze(user_query=business_goal, user_id=user_id))
            )

            hb_steps = [
                (40, "Mapping strategic constraints…"),
                (55, "Building action plans…"),
                (70, "Selecting AI tools…"),
                (85, "Compiling execution roadmap…"),
                (90, "Reviewing recommendations…"),
                (95, "Finalizing report…"),
                (98, "Almost done…")
            ]
            
            step_idx = 0
            while not analysis_task.done():
                done, _ = await asyncio.wait([analysis_task], timeout=4.0)
                if done:
                    break
                pct, msg = hb_steps[min(step_idx, len(hb_steps) - 1)]
                yield _send("progress", {"step": "thinking", "pct": pct, "msg": msg})
                yield ": keepalive\n\n"
                step_idx += 1

            result = await analysis_task
            yield _send("progress", {"step": "complete", "pct": 100, "msg": "Analysis complete!"})
            yield _send("result", {"success": True, "data": result["data"]})
            # Small pause so Railway's HTTP/2 proxy finishes flushing the final
            # chunk before the generator closes the stream.
            await asyncio.sleep(0.3)
            yield ": done\n\n"

        except Exception as e:
            err_str = str(e)
            if err_str.startswith("UNSAFE_QUERY::"):
                # Format: UNSAFE_QUERY::<reason>::<suggestions_json>
                parts = err_str.split("::", 2)
                reason = parts[1] if len(parts) > 1 else "This query cannot be processed."
                try:
                    import ast
                    suggestions = ast.literal_eval(parts[2]) if len(parts) > 2 else []
                except Exception:
                    suggestions = []
                logger.warning(f"Unsafe query blocked for user {user_id}: {reason}")
                yield _send("unsafe", {
                    "blocked": True,
                    "reason": reason,
                    "suggestions": suggestions,
                    "message": (
                        "Our engine can't process this request as described. "
                        "We're built to help with legitimate business challenges."
                    ),
                })
            else:
                logger.error(f"❌ Streaming analysis failed for user {user_id}: {e}", exc_info=True)
                yield _send("error", {"message": err_str})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/analyses")
async def get_user_analyses(
    limit: int = 10,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get user's previous business analyses.

    Query Parameters:
    - limit: Maximum number of analyses to return (default: 10)

    Returns list of analyses in frontend format.
    """
    try:
        user_id = get_user_id(current_user)
        logger.info(f"📋 Fetching analyses for user {user_id}, limit={limit}")

        analyses = (
            db.query(BusinessAnalysis)
            .filter(BusinessAnalysis.user_id == user_id)
            .order_by(BusinessAnalysis.created_at.desc())
            .limit(limit)
            .all()
        )

        logger.info(f"Found {len(analyses)} analyses for user {user_id}")

        # Transform to frontend format
        ui_analyses = []
        for analysis in analyses:
            try:
                ui_data = format_analysis_for_frontend(analysis)
                ui_analyses.append(ui_data)
            except Exception as e:
                logger.warning(f"Failed to format analysis {analysis.id}: {e}")
                continue

        return {
            "success": True,
            "count": len(ui_analyses),
            "data": ui_analyses
        }

    except Exception as e:
        logger.error(f"❌ Failed to fetch analyses: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch analyses: {str(e)}"
        )


@router.get("/analyses/{analysis_id}")
async def get_analysis_detail(
    analysis_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get detailed view of a specific analysis.

    Path Parameters:
    - analysis_id: ID of the analysis to retrieve

    Returns complete analysis in frontend format.
    """
    try:
        user_id = get_user_id(current_user)

        analysis = (
            db.query(BusinessAnalysis)
            .filter(
                BusinessAnalysis.id == analysis_id,
                BusinessAnalysis.user_id == user_id
            )
            .first()
        )

        if not analysis:
            raise HTTPException(status_code=404, detail="Analysis not found")

        ui_data = format_analysis_for_frontend(analysis)

        return {
            "success": True,
            "data": ui_data
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Failed to fetch analysis {analysis_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to fetch analysis: {str(e)}"
        )


@router.post("/analyze/stream")
async def analyze_business_goal_stream(
    request: Request,
    background_tasks: BackgroundTasks,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    SSE streaming analysis endpoint.
    Emits progress events during analysis, then the final result.

    Event types: progress {pct, msg} | result {data} | error {message}
    """
    from decision_engine.agentic_analyzer import create_analyzer
    from decision_engine.multimodal.handler import MultimodalHandler

    try:
        user_id = get_user_id(current_user)
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))

    # Parse request body (identical logic to /analyze)
    content_type = request.headers.get("content-type", "")
    business_goal = ""
    files = []

    try:
        if "application/json" in content_type:
            body = await request.json()
            business_goal = body.get("business_goal", "")
        elif "multipart/form-data" in content_type:
            form_data = await request.form()
            business_goal = form_data.get("business_goal", "")
            files = form_data.getlist("files")
        else:
            raise HTTPException(status_code=400, detail="Invalid Content-Type")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to parse request: {e}")

    if not business_goal and not files:
        raise HTTPException(status_code=400, detail="Business goal or file upload required")

    # Process multimodal files if present
    if files:
        image_bytes_list = []
        document_bytes_list = []
        for file in files:
            file_bytes = await file.read()
            filename = getattr(file, "filename", "").lower()
            if filename.endswith(('.png', '.jpg', '.jpeg', '.gif')):
                image_bytes_list.append((file_bytes, filename))
            elif filename.endswith(('.pdf', '.docx', '.doc', '.xlsx', '.csv', '.txt')):
                document_bytes_list.append((file_bytes, filename))
        if image_bytes_list or document_bytes_list:
            mm_handler = MultimodalHandler(use_vision_for_images=True)
            mm_result = mm_handler.process_multimodal_query(
                user_query=business_goal,
                image_bytes_list=image_bytes_list,
                document_bytes_list=document_bytes_list,
                enhance_with_llm=False,
            )
            combined = mm_result.get("combined_context", "")
            if combined:
                business_goal = f"{business_goal}\n\n[Additional Context from Uploaded Files]:\n{combined}".strip()

    if not business_goal:
        raise HTTPException(status_code=400, detail="Could not extract content from uploaded files")

    # Deduplication guard: if the user already analyzed this exact same goal
    from sqlalchemy import desc as _desc
    recent = None
    if business_goal and business_goal.strip():
        recent = (
            db.query(BusinessAnalysis)
            .filter(
                BusinessAnalysis.user_id == user_id,
                BusinessAnalysis.business_goal == business_goal,
            )
            .order_by(_desc(BusinessAnalysis.created_at))
            .first()
        )

    if recent:
        async def _cached_stream():
            data = format_analysis_for_frontend(recent)
            yield f"event: progress\ndata: {json.dumps({'pct': 100, 'msg': 'Returning recent analysis'})}\n\n"
            yield f"event: result\ndata: {json.dumps({'data': data})}\n\n"

        return StreamingResponse(
            _cached_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    queue: asyncio.Queue = asyncio.Queue()

    async def _progress_callback(pct: int, msg: str):
        await queue.put({"type": "progress", "pct": pct, "msg": msg})

    async def _run_analysis():
        try:
            analyzer = create_analyzer(db)
            result = await analyzer.analyze(
                user_query=business_goal,
                user_id=user_id,
                progress_callback=_progress_callback,
            )
            # Schedule background enrichment
            _raw_stacks = result["data"].get("recommended_tool_stacks", [])
            _bottleneck_title = result["data"].get("primary_bottleneck", {}).get("title", "")
            if _raw_stacks:
                background_tasks.add_task(
                    _enrich_stacks_background,
                    analysis_id=result["data"]["analysis_id"],
                    raw_stacks=_raw_stacks,
                    user_query=business_goal,
                    bottleneck_title=_bottleneck_title,
                )
            await queue.put({"type": "result", "data": result["data"]})
        except Exception as exc:
            logger.error(f"SSE analysis failed: {exc}", exc_info=True)
            await queue.put({"type": "error", "message": str(exc)})
        finally:
            await queue.put(None)

    async def _generate():
        task = asyncio.create_task(_run_analysis())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=4.0)
                except asyncio.TimeoutError:
                    # Send a keep-alive comment to keep Railway/Cloudflare connections open
                    yield ": keepalive\n\n"
                    continue
                    
                if item is None:
                    break
                event_type = item.pop("type")
                yield f"event: {event_type}\ndata: {json.dumps(item)}\n\n"
        finally:
            task.cancel()

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/analyses/{analysis_id}")
async def delete_analysis(
    analysis_id: int,
    current_user=Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Delete a specific analysis.

    Path Parameters:
    - analysis_id: ID of the analysis to delete
    """
    try:
        user_id = get_user_id(current_user)

        analysis = (
            db.query(BusinessAnalysis)
            .filter(
                BusinessAnalysis.id == analysis_id,
                BusinessAnalysis.user_id == user_id
            )
            .first()
        )

        if not analysis:
            raise HTTPException(status_code=404, detail="Analysis not found")

        db.delete(analysis)
        db.commit()

        logger.info(f"🗑️ Deleted analysis {analysis_id} for user {user_id}")

        return {
            "success": True,
            "message": "Analysis deleted successfully"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"❌ Failed to delete analysis {analysis_id}: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete analysis: {str(e)}"
        )
