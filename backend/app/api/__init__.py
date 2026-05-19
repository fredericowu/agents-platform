from fastapi import APIRouter

from . import agents, consolidation, evals, github, health, lessons, mcp, models, playground, retro_scores, runs, settings, skills, targets, tools, workflows

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(models.router)
api_router.include_router(agents.router)
api_router.include_router(workflows.router)
api_router.include_router(runs.router)
api_router.include_router(targets.router)
# retro_scores and consolidation routers must come before lessons.search_router
# so fixed paths like /api/lessons/pending and /api/lessons/consolidate/*
# are matched before parameterized lesson routes like /{lesson_id}/...
api_router.include_router(retro_scores.router)
api_router.include_router(consolidation.router)
api_router.include_router(lessons.search_router)
api_router.include_router(lessons.target_lessons_router)
api_router.include_router(mcp.router)
api_router.include_router(skills.router)
api_router.include_router(tools.router)
api_router.include_router(evals.router)
api_router.include_router(playground.router)
api_router.include_router(settings.router)
api_router.include_router(github.router)
