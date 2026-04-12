from src.bot.routers.start import router as start_router
from src.bot.routers.profiling import router as profiling_router
from src.bot.routers.program import router as program_router
from src.bot.routers.detail import router as detail_router
from src.bot.routers.support import router as support_router
from src.bot.routers.support import group_router as support_group_router
from src.bot.routers.expert import router as expert_router
from src.bot.routers.fallback import router as fallback_router

__all__ = [
    "start_router",
    "profiling_router",
    "program_router",
    "detail_router",
    "support_router",
    "support_group_router",
    "expert_router",
    "fallback_router",
]
