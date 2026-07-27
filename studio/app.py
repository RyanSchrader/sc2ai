from __future__ import annotations

import json
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from .assistant import AssistantResponseError, AssistantUnavailableError, OllamaAssistant
from .catalog import RaceName, public_catalog
from .models import (
    ApplyProposalRequest,
    AssistantProposalRequest,
    BenchmarkSuiteCreate,
    BenchmarkSuiteUpdate,
    BotCreate,
    BotUpdate,
    ForkRequest,
    MatchCreate,
    RegressionCreate,
    RestoreRevisionRequest,
    StrategyDocument,
    StrategyProposal,
    blank_strategy,
    validation_result,
)
from .repository import (
    PROJECT_ROOT,
    ConflictError,
    NotFoundError,
    StudioRepository,
)
from .runs import DIFFICULTIES, ENEMY_RACES, RegressionManager, RunManager, discover_maps


def create_app(repository: StudioRepository | None = None) -> FastAPI:
    repo = repository or StudioRepository()
    assistant = OllamaAssistant()
    run_manager = RunManager(repo)
    regression_manager = RegressionManager(repo, run_manager)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        repo.initialize()
        repo.interrupt_active_matches()
        yield
        await regression_manager.shutdown()
        await run_manager.stop_all()

    app = FastAPI(
        title="SC2 Bot Studio",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.state.repository = repo
    app.state.assistant = assistant
    app.state.run_manager = run_manager
    app.state.regression_manager = regression_manager
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_request: Request, exc: NotFoundError):
        return _json_error(404, str(exc))

    @app.exception_handler(ConflictError)
    async def conflict_handler(_request: Request, exc: ConflictError):
        return _json_error(409, str(exc))

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "database": str(repo.database_path)}

    @app.get("/api/catalog")
    async def catalog():
        return public_catalog()

    @app.get("/api/runtime/maps")
    async def maps():
        return {"maps": discover_maps()}

    @app.get("/api/assistant/health")
    async def assistant_health():
        return await assistant.health()

    @app.get("/api/bots")
    async def list_bots(
        include_deleted: bool = Query(False, alias="includeDeleted"),
        search: str | None = None,
        race: str | None = None,
    ):
        return repo.list_bots(
            include_deleted=include_deleted,
            search=search,
            race=race,
        )

    @app.post("/api/bots", status_code=201)
    async def create_bot(body: BotCreate):
        return repo.create_bot(body)

    @app.get("/api/bots/{bot_id}")
    async def get_bot(bot_id: str):
        return repo.get_bot(bot_id)

    @app.patch("/api/bots/{bot_id}")
    async def update_bot(bot_id: str, body: BotUpdate):
        return repo.update_bot(bot_id, body)

    @app.delete("/api/bots/{bot_id}")
    async def trash_bot(bot_id: str):
        return repo.trash_bot(bot_id)

    @app.post("/api/bots/{bot_id}/restore")
    async def restore_bot(bot_id: str):
        return repo.restore_bot(bot_id)

    @app.post("/api/bots/{bot_id}/fork", status_code=201)
    async def fork_bot(bot_id: str, body: ForkRequest):
        return repo.fork_bot(bot_id, body.name)

    @app.get("/api/bots/{bot_id}/revisions")
    async def revisions(bot_id: str):
        return repo.list_revisions(bot_id)

    @app.post("/api/bots/{bot_id}/revisions/{revision}/restore")
    async def restore_revision(
        bot_id: str,
        revision: int,
        body: RestoreRevisionRequest,
    ):
        return repo.restore_revision(bot_id, revision, body.change_summary)

    @app.get("/api/bots/{bot_id}/stats")
    async def bot_stats(
        bot_id: str,
        include_regression: bool = Query(True, alias="includeRegression"),
    ):
        return repo.bot_stats(bot_id, include_regression=include_regression)

    @app.get("/api/bots/{bot_id}/matches")
    async def bot_matches(
        bot_id: str,
        opponent_type: str | None = Query(None, alias="opponentType"),
        enemy_race: str | None = Query(None, alias="enemyRace"),
        difficulty: str | None = None,
        map_name: str | None = Query(None, alias="mapName"),
        result: str | None = None,
        include_regression: bool = Query(True, alias="includeRegression"),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        return repo.list_bot_matches(
            bot_id,
            opponent_type=opponent_type,
            enemy_race=enemy_race,
            difficulty=difficulty,
            map_name=map_name,
            result=result,
            include_regression=include_regression,
            limit=limit,
            offset=offset,
        )

    @app.post("/api/strategies/validate")
    async def validate_strategy(body: StrategyDocument):
        return validation_result(body)

    @app.post("/api/assistant/proposals", status_code=201)
    async def create_proposal(body: AssistantProposalRequest):
        current_strategy = body.strategy
        requested_name = body.requested_name
        if body.base_bot_id:
            bot = repo.get_bot(str(body.base_bot_id), include_deleted=False)
            current_strategy = current_strategy or StrategyDocument.model_validate(bot["strategy"])
            requested_name = requested_name or bot["name"]
        if current_strategy is None:
            current_strategy = blank_strategy(body.requested_race or RaceName.PROTOSS)
        try:
            proposal = await assistant.propose(
                prompt=body.prompt,
                current_strategy=current_strategy,
                requested_name=requested_name,
            )
        except AssistantUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except AssistantResponseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return repo.create_proposal(proposal, body.base_bot_id)

    @app.get("/api/assistant/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str):
        return repo.get_proposal(proposal_id)

    @app.post("/api/assistant/proposals/{proposal_id}/apply")
    async def apply_proposal(proposal_id: str, body: ApplyProposalRequest):
        stored = repo.get_proposal(proposal_id)
        if stored["status"] != "pending":
            raise ConflictError("This proposal has already been resolved.")
        proposal = StrategyProposal.model_validate(stored["proposal"])
        if stored["baseBotId"]:
            result = repo.update_bot(
                stored["baseBotId"],
                BotUpdate(
                    strategy=proposal.strategy,
                    description=proposal.description or None,
                    change_summary=f"Assistant: {proposal.summary}",
                    expected_revision=body.expected_revision,
                ),
            )
        else:
            result = repo.create_bot(
                BotCreate(
                    name=proposal.suggested_name,
                    slug=proposal.suggested_slug,
                    description=proposal.description,
                    race=proposal.strategy.race,
                    tags=["assistant-created"],
                    strategy=proposal.strategy,
                ),
                summary=f"Assistant-created: {proposal.summary}",
            )
        repo.set_proposal_status(proposal_id, "applied")
        return result

    @app.post("/api/assistant/proposals/{proposal_id}/reject")
    async def reject_proposal(proposal_id: str):
        return repo.set_proposal_status(proposal_id, "rejected")

    @app.post("/api/runs", status_code=201)
    async def start_run(body: MatchCreate):
        try:
            return await run_manager.start(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        try:
            return run_manager.public(run_id)
        except (KeyError, NotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str):
        try:
            return await run_manager.stop(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, after: int = 0):
        try:
            run_manager.public(run_id)
        except (KeyError, NotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        async def stream():
            async for event in run_manager.events(run_id, after):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/matches/{match_id}")
    async def get_match(match_id: str):
        return repo.get_match(match_id)

    @app.get("/api/benchmarks")
    async def list_benchmarks(
        include_archived: bool = Query(False, alias="includeArchived"),
    ):
        return repo.list_benchmark_suites(include_archived=include_archived)

    @app.post("/api/benchmarks", status_code=201)
    async def create_benchmark(body: BenchmarkSuiteCreate):
        _validate_benchmark(repo, body)
        return repo.create_benchmark_suite(body)

    @app.post("/api/benchmarks/validate")
    async def validate_benchmark(body: BenchmarkSuiteCreate):
        return _validate_benchmark(repo, body)

    @app.get("/api/benchmarks/{suite_id}")
    async def get_benchmark(suite_id: str):
        return repo.get_benchmark_suite(suite_id)

    @app.put("/api/benchmarks/{suite_id}")
    async def update_benchmark(suite_id: str, body: BenchmarkSuiteUpdate):
        _validate_benchmark(repo, body)
        return repo.update_benchmark_suite(suite_id, body)

    @app.post("/api/benchmarks/{suite_id}/duplicate", status_code=201)
    async def duplicate_benchmark(suite_id: str):
        return repo.duplicate_benchmark_suite(suite_id)

    @app.delete("/api/benchmarks/{suite_id}")
    async def archive_benchmark(suite_id: str):
        return repo.archive_benchmark_suite(suite_id)

    @app.get("/api/bots/{bot_id}/regressions")
    async def list_regressions(bot_id: str):
        return repo.list_regression_batches(bot_id)

    @app.post("/api/regressions", status_code=201)
    async def start_regression(body: RegressionCreate):
        try:
            return await regression_manager.start(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/regressions/{batch_id}")
    async def get_regression(batch_id: str):
        return repo.get_regression_batch(batch_id)

    @app.post("/api/regressions/{batch_id}/cancel")
    async def cancel_regression(batch_id: str):
        return await regression_manager.cancel(batch_id)

    @app.post("/api/regressions/{batch_id}/resume")
    async def resume_regression(batch_id: str):
        return await regression_manager.resume(batch_id)

    @app.get("/api/regressions/{batch_id}/events")
    async def regression_events(batch_id: str):
        repo.get_regression_batch(batch_id)

        async def stream():
            async for event in regression_manager.events(batch_id):
                yield f"data: {json.dumps(event)}\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    frontend_dist = PROJECT_ROOT / "frontend" / "dist"
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
    return app


def _json_error(status_code: int, detail: str):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_code, content={"detail": detail.strip("'")})


def _validate_benchmark(
    repository: StudioRepository, suite: BenchmarkSuiteCreate
) -> dict[str, object]:
    installed = set(discover_maps())
    errors: list[str] = []
    for index, scenario in enumerate(suite.scenarios, start=1):
        if scenario.map_name not in installed:
            errors.append(f"Scenario {index}: map is not installed: {scenario.map_name}")
        if scenario.opponent_type == "computer":
            if scenario.enemy_race not in ENEMY_RACES:
                errors.append(f"Scenario {index}: unsupported enemy race.")
            if scenario.difficulty not in DIFFICULTIES:
                errors.append(f"Scenario {index}: unsupported difficulty.")
        if scenario.opponent_type == "bot" and scenario.opponent_bot_id:
            try:
                repository.get_bot_revision(
                    str(scenario.opponent_bot_id), scenario.opponent_revision
                )
            except NotFoundError as exc:
                errors.append(f"Scenario {index}: {str(exc).strip(chr(39))}")
    if errors:
        raise HTTPException(status_code=422, detail=errors)
    return {"valid": True, "errors": []}


app = create_app()


if __name__ == "__main__":
    uvicorn.run("studio.app:app", host="127.0.0.1", port=8000, reload=False)
